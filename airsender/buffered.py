"""AirPlay 2 **buffered** 串流（stream type 103）傳送端：ALAC 24-bit over TCP + PTP 時間錨。

與 realtime（type 96，pyatv 內建）的差別：
  - 音訊走 TCP，每個封包 352 frames 的 ALAC（這裡用未壓縮框架，仍是無損）；
  - 時間基準是 PTP 主時鐘（HomePod 自己），以 SETRATEANCHORTIME 告訴接收端「rtpTime X 在主時鐘時間 T 播放」；
  - 接收端支援 24-bit（/info bufferStream 宣告 ALAC 44.1/24、48/24）；
  - 傳送端可以提前推送，Wi-Fi 抖動由接收端的緩衝吸收，不再需要本機抖動緩衝。

流程：GET /info → pair-verify → SETUP（timingProtocol PTP）→ 事件通道 → RECORD → SETPEERS →
SETUP streams[type 103] → TCP dataPort → 等 PTP 鎖定 → primer 封包 → SETRATEANCHORTIME → 持續推送。

封包格式（接收端以 shairport-sync / 抓包為準）：
  u16 BE 總長（含這 2 bytes）‖ u32 BE (0x80000000 | 23-bit seq) ‖ u32 BE rtpTime ‖ u32 BE SSRC（格式碼）‖
  ChaCha20-Poly1305(key = shk, nonce = 0⁴ ‖ 8-byte 計數器 LE, AAD = rtpTime ‖ SSRC) 密文 ‖ 16-byte tag ‖ 8-byte 計數器
"""

from __future__ import annotations

import asyncio
import logging
import plistlib
import random
import socket
import struct
import time
from collections.abc import Callable
from typing import Optional
from uuid import uuid4

import numpy as np
from chacha20poly1305_reuseable import ChaCha20Poly1305Reusable
from pyatv.auth.hap_channel import setup_channel
from pyatv.auth.hap_pairing import HapCredentials
from pyatv.protocols.airplay.auth import verify_connection
from pyatv.protocols.airplay.channels import EventChannel
from pyatv.support.http import decode_bplist_from_body, http_connect
from pyatv.support.rtsp import RtspSession

from . import alac
from .live_source import DriftController, LiveAudioSource
from .ptp import PtpClock

_LOGGER = logging.getLogger(__name__)

EVENTS_SALT = "Events-Salt"
EVENTS_WRITE_INFO = "Events-Write-Encryption-Key"
EVENTS_READ_INFO = "Events-Read-Encryption-Key"

STREAM_TYPE_BUFFERED = 103
FRAMES_PER_PACKET = alac.FRAME_LENGTH
FEEDBACK_INTERVAL = 2.0

# audioFormat 旗標（/info supportedFormats 的位元）與封包 SSRC 格式碼
AUDIO_FORMAT = {(44100, 16): 0x40000, (44100, 24): 0x80000, (48000, 16): 0x100000, (48000, 24): 0x200000}
SSRC_FORMAT = {(48000, 24): 0x15000000}  # 實際抓包確認的值；其他組合未知


def _u64_to_plist(value: int) -> int:
    """plist 整數以 64-bit 有號傳；超過 2^63 的值轉成負數（接收端以 bit-cast 讀回）。"""
    value &= 0xFFFFFFFFFFFFFFFF
    return value - (1 << 64) if value >= (1 << 63) else value


def network_time_fields(master_ns: int) -> tuple[int, int]:
    secs, nanos = divmod(int(master_ns), 1_000_000_000)
    frac = (nanos << 64) // 1_000_000_000
    return secs, _u64_to_plist(frac)


class BufferedStats:
    def __init__(self) -> None:
        self.packets = 0
        self.bytes = 0
        self.silence_packets = 0
        self.lead_ms = 0.0
        self.anchor_count = 0
        self.ptp_summary = ""
        self.ptp_jitter_ms = 0.0


class BufferedAirPlayStream:
    """一條 buffered 串流。run() 直到 stop() 或連線失敗。"""

    DEFAULT_PREFILL_MS = 20

    def __init__(
        self,
        address: str,
        port: int,
        credentials: HapCredentials,
        source: LiveAudioSource,
        *,
        sample_rate: int = 48000,
        bit_depth: int = 24,
        lead_ms: int = 500,
        latency_min_ms: int = 250,
        latency_max_ms: int = 2000,
        prefill_ms: int = DEFAULT_PREFILL_MS,
        name: str = "AirSender",
        on_streaming: Optional[Callable[[], None]] = None,
    ) -> None:
        self.address = address
        self.port = port
        self.credentials = credentials
        self.source = source
        self.sample_rate = sample_rate
        self.bit_depth = bit_depth
        self.lead_ms = lead_ms
        self.latency_min_ms = latency_min_ms
        self.latency_max_ms = latency_max_ms
        self.prefill_ms = prefill_ms
        self.name = name
        self.on_streaming = on_streaming
        self.stats = BufferedStats()
        self.ptp: Optional[PtpClock] = None

        self._stop = asyncio.Event()
        self._rtsp: Optional[RtspSession] = None
        self._conn = None
        self._event_transport = None
        self._control_transport = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._shk = random.randbytes(32)
        self._cipher = ChaCha20Poly1305Reusable(self._shk)
        self._nonce_counter = 0
        self._seq = random.randrange(1 << 23)
        self._rtp_time = random.randrange(1 << 32)
        self._ssrc = SSRC_FORMAT.get((sample_rate, bit_depth), 0)
        self._anchor_rtp: Optional[int] = None
        self._anchor_master_ns: Optional[int] = None
        self._volume_db: Optional[float] = None

    # ------------------------------------------------------------ 公開
    def stop(self) -> None:
        self._stop.set()

    async def set_volume(self, percent: float) -> None:
        """接收端音量（AirPlay 的 dB 刻度：-30 ~ 0，-144 = 靜音）。"""
        linear = max(0.0, min(100.0, percent)) / 100.0
        db = -144.0 if linear <= 0.001 else -30.0 + 30.0 * linear
        self._volume_db = db
        if self._rtsp is not None:
            await self._rtsp.set_parameter("volume", f"{db:.6f}")

    def _due_ns(self, rtp_time: int) -> int:
        return self._anchor_master_ns + ((rtp_time - self._anchor_rtp) & 0xFFFFFFFF) * 1_000_000_000 // self.sample_rate

    def next_play_lead_ms(self) -> Optional[float]:
        """下一個要送的封包距離播放時刻還有多久（主時鐘）。"""
        if self.ptp is None or self._anchor_rtp is None or not self.ptp.locked:
            return None
        return (self._due_ns(self._rtp_time) - self.ptp.master_now_ns()) / 1e6

    # ------------------------------------------------------------ 主流程
    async def run(self) -> None:
        self._conn = await http_connect(self.address, self.port)
        self._rtsp = RtspSession(self._conn)
        local_ip = self._conn.local_ip
        self.ptp = PtpClock(self.address, local_ip=local_ip)
        feedback_task: Optional[asyncio.Task] = None
        try:
            info = await self._rtsp.info()
            _LOGGER.debug("/info: model=%s sourceVersion=%s", info.get("model"), info.get("sourceVersion"))
            verifier = await verify_connection(self.credentials, self._conn)

            await self.ptp.start()
            event_port, peer_info = await self._setup_session(local_ip)
            _LOGGER.info("SETUP 完成：eventPort %s，接收端 timingPeerInfo %s", event_port, peer_info)
            await self._open_event_channel(verifier, event_port)
            await self._record()
            await self._set_peers(local_ip)
            data_port, control_port = await self._setup_stream()
            _LOGGER.info("串流 SETUP 完成：dataPort %d controlPort %d", data_port, control_port)

            reader, self._writer = await asyncio.open_connection(self.address, data_port)
            sock = self._writer.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            feedback_task = asyncio.create_task(self._feedback_loop())

            if not await self.ptp.wait_locked(10.0):
                raise RuntimeError(f"PTP 未鎖定（{self.ptp.summary()}）")
            _LOGGER.info("PTP 鎖定：%s", self.ptp.summary())

            if self._volume_db is not None:
                await self._rtsp.set_parameter("volume", f"{self._volume_db:.6f}")

            await self._pump(reader)
        finally:
            if feedback_task:
                feedback_task.cancel()
            await self._teardown()

    # ------------------------------------------------------------ RTSP 步驟
    async def _setup_session(self, local_ip: str) -> tuple[int, dict]:
        body = {
            "deviceID": "AA:BB:CC:DD:EE:FF",
            "macAddress": "AA:BB:CC:DD:EE:FF",
            "sessionUUID": str(uuid4()).upper(),
            "groupUUID": str(uuid4()).upper(),
            "groupContainsGroupLeader": False,
            "isMultiSelectAirPlay": True,
            "model": "iPhone14,3",
            "name": self.name,
            "osBuildVersion": "20F66",
            "osName": "iPhone OS",
            "osVersion": "16.5",
            "senderSupportsRelay": False,
            "sourceVersion": "690.7.1",
            "statsCollectionEnabled": False,
            "timingPort": 319,
            "timingProtocol": "PTP",
            "timingPeerInfo": {
                "Addresses": [local_ip],
                "ID": local_ip,
                "SupportsClockPortMatchingOverride": True,
            },
            "timingPeerList": [{"Addresses": [local_ip], "ID": local_ip, "SupportsClockPortMatchingOverride": True}],
        }
        resp = await self._rtsp.setup(body=body)
        if resp.code != 200:
            raise RuntimeError(f"SETUP（PTP）被拒：{resp.code} {resp.message}")
        plist = decode_bplist_from_body(resp)
        _LOGGER.debug("SETUP 回應：%s", plist)
        return int(plist.get("eventPort", 0)), plist.get("timingPeerInfo", {})

    async def _open_event_channel(self, verifier, event_port: int) -> None:
        retries = 5
        while True:
            try:
                self._event_transport, _ = await setup_channel(
                    EventChannel, verifier, self.address, event_port, EVENTS_SALT, EVENTS_READ_INFO, EVENTS_WRITE_INFO
                )
                return
            except ConnectionRefusedError:
                retries -= 1
                if retries == 0:
                    raise
                await asyncio.sleep(1.0)

    async def _record(self) -> None:
        resp = await self._rtsp.exchange(
            "RECORD",
            headers={"Range": "npt=0-", "RTP-Info": "seq=0;rtptime=0", "X-Apple-ProtocolVersion": "1"},
            allow_error=True,
        )
        _LOGGER.debug("RECORD → %s %s", resp.code, resp.headers.get("Audio-Latency"))
        if resp.code != 200:
            raise RuntimeError(f"RECORD 被拒：{resp.code} {resp.message}")

    async def _set_peers(self, local_ip: str) -> None:
        body = plistlib.dumps([local_ip, self.address], fmt=plistlib.FMT_BINARY)  # pylint: disable=no-member
        resp = await self._rtsp.exchange(
            "SETPEERS",
            uri="/peer-list-changed",
            headers={"Content-Type": "application/x-apple-binary-plist"},
            body=body,
            allow_error=True,
        )
        _LOGGER.debug("SETPEERS → %s", resp.code)

    async def _setup_stream(self) -> tuple[int, int]:
        loop = asyncio.get_running_loop()
        self._control_transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=(self._conn.local_ip, 0)
        )
        control_port = self._control_transport.get_extra_info("sockname")[1]
        sr = self.sample_rate
        stream = {
            "type": STREAM_TYPE_BUFFERED,
            "ct": 2,  # ALAC
            "audioFormat": AUDIO_FORMAT[(sr, self.bit_depth)],
            "audioMode": "default",
            "sr": sr,
            "spf": FRAMES_PER_PACKET,
            "shk": self._shk,
            "asc": alac.magic_cookie(sr, self.bit_depth),
            "latencyMin": int(sr * self.latency_min_ms / 1000),
            "latencyMax": int(sr * self.latency_max_ms / 1000),
            "controlPort": control_port,
            "isMedia": True,
            "supportsDynamicStreamID": False,
            "streamConnectionID": self._rtsp.session_id,
        }
        resp = await self._rtsp.setup(body={"streams": [stream]})
        if resp.code != 200:
            raise RuntimeError(f"SETUP（buffered）被拒：{resp.code} {resp.message}")
        plist = decode_bplist_from_body(resp)
        _LOGGER.debug("串流 SETUP 回應：%s", plist)
        s = plist["streams"][0]
        self._audio_buffer_size = int(s.get("audioBufferSize", 0))
        return int(s["dataPort"]), int(s.get("controlPort", 0))

    async def _feedback_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(FEEDBACK_INTERVAL)
                try:
                    await self._rtsp.feedback(allow_error=True)
                except Exception as ex:  # pylint: disable=broad-except
                    _LOGGER.debug("feedback 失敗：%s", ex)
        except asyncio.CancelledError:
            pass

    async def _set_rate_anchor(self, rtp_time: int, master_ns: int, rate: int = 1) -> None:
        secs, frac = network_time_fields(master_ns)
        body = {
            "rate": rate,
            "rtpTime": rtp_time & 0xFFFFFFFF,
            "networkTimeSecs": secs,
            "networkTimeFrac": frac,
            "networkTimeTimelineID": _u64_to_plist(int.from_bytes(self.ptp.master_id, "big")),
            "networkTimeFlags": 0,
        }
        resp = await self._rtsp.exchange("SETRATEANCHORTIME", body=body, allow_error=True)
        if resp.code != 200:
            raise RuntimeError(f"SETRATEANCHORTIME 被拒：{resp.code} {resp.message}")
        self._anchor_rtp = rtp_time & 0xFFFFFFFF
        self._anchor_master_ns = master_ns
        self.stats.anchor_count += 1

    async def _teardown(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # pylint: disable=broad-except
                pass
            self._writer = None
        if self._rtsp is not None:
            try:
                await asyncio.wait_for(self._rtsp.exchange("TEARDOWN", allow_error=True), 3.0)
            except Exception:  # pylint: disable=broad-except
                pass
        for t in (self._event_transport, self._control_transport):
            if t is not None:
                t.close()
        self._event_transport = self._control_transport = None
        if self.ptp is not None:
            await self.ptp.stop()
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._rtsp = None

    # ------------------------------------------------------------ 封包
    def _packet(self, payload: bytes) -> bytes:
        header = struct.pack(">III", 0x80000000 | (self._seq & 0x7FFFFF), self._rtp_time & 0xFFFFFFFF, self._ssrc)
        nonce8 = self._nonce_counter.to_bytes(8, "little")
        self._nonce_counter += 1
        encrypted = self._cipher.encrypt(b"\x00" * 4 + nonce8, payload, header[4:12])  # ct ‖ tag
        packet = header + encrypted + nonce8
        return (len(packet) + 2).to_bytes(2, "big") + packet

    def _send_frame(self, samples: np.ndarray) -> None:
        payload = alac.pack_verbatim_frame(samples, self.bit_depth)
        self._writer.write(self._packet(payload))
        self._seq = (self._seq + 1) & 0x7FFFFF
        self._rtp_time = (self._rtp_time + samples.shape[0]) & 0xFFFFFFFF
        self.stats.packets += 1
        self.stats.bytes += len(payload)

    def _send_primer(self) -> None:
        """串流開頭一個空負載封包，帶第一個框架的 rtpTime（Apple 傳送端的做法）。"""
        saved_seq = self._seq
        self._seq = (self._seq - 1) & 0x7FFFFF
        self._writer.write(self._packet(b""))
        self._seq = saved_seq

    async def _pump(self, reader: asyncio.StreamReader) -> None:
        """推送迴圈。

        時間錨以**第一個封包的擷取時刻**為準：rtpTime₀ 在主時鐘 master(擷取時刻₀) + 提前量 播放。
        之後每個封包的「播放時刻 − 擷取時刻」都應等於提前量，漂移控制器就量這個值，
        不受擷取區塊到達節奏（10 ms 一塊、偶爾成串）影響，每次串流的總延遲也因此一致。
        """
        src = self.source
        dtype = "<i2" if self.bit_depth == 16 else "<i4"
        packet_ns = FRAMES_PER_PACKET * 1_000_000_000 // self.sample_rate
        silence = np.zeros((FRAMES_PER_PACKET, 2), dtype=np.int32)
        lead_ns = self.lead_ms * 1_000_000
        loop = asyncio.get_running_loop()

        # 起始：丟掉握手期間累積的舊資料，等下一個新鮮的封包
        src.trim_to_ms(0)
        controller = DriftController(float(self.lead_ms))
        src.drift = controller
        first = None
        deadline = loop.time() + 3.0
        while first is None and not self._stop.is_set():
            first = src.pop_frames_timed(FRAMES_PER_PACKET)
            if first is None:
                if loop.time() > deadline:
                    first = (bytes(FRAMES_PER_PACKET * src.frame_size), time.perf_counter_ns())  # 沒聲音：以靜音開始
                    break
                await asyncio.sleep(0.002)
        if first is None:
            return
        data, cap_ns = first
        cap_local = self.ptp.local_from_perf_ns(cap_ns if cap_ns is not None else time.perf_counter_ns())

        self._send_primer()
        anchor_master = self.ptp.to_master_ns(cap_local) + lead_ns
        await self._set_rate_anchor(self._rtp_time, anchor_master)
        self._send_frame(np.frombuffer(data, dtype=dtype).reshape(-1, 2))
        _LOGGER.info(
            "錨定：rtpTime %d = 擷取時刻 + %d ms（擷取到送出 %.1f ms，PTP %s）",
            self._anchor_rtp, self.lead_ms, (self.ptp.clock.now_ns() - cap_local) / 1e6, self.ptp.summary(),
        )
        src.mark_started()
        if self.on_streaming:
            try:
                self.on_streaming()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("on_streaming 回呼失敗")

        fill_trigger_ns = lead_ns - 2 * packet_ns  # 下一個封包的播放時刻逼近到提前量少兩個封包 → 來源停了，補靜音
        last_log = time.monotonic()
        while not self._stop.is_set():
            sent_any = False
            while True:
                item = src.pop_frames_timed(FRAMES_PER_PACKET)
                if item is None:
                    break
                data, cap_ns = item
                due = self._due_ns(self._rtp_time)
                self._send_frame(np.frombuffer(data, dtype=dtype).reshape(-1, 2))
                sent_any = True
                if cap_ns is not None:
                    # 控制量：播放時刻 − 擷取時刻（目標 = 提前量）
                    value_ms = (due - self.ptp.to_master_ns(self.ptp.local_from_perf_ns(cap_ns))) / 1e6
                    controller.update(value_ms, loop.time())
                    self.stats.lead_ms = (due - self.ptp.master_now_ns()) / 1e6  # HomePod 看到的提前量（送出時）
            # 來源沒資料（Windows 沒人播音）時補靜音讓時間軸連續，補到提前量為止；
            # 之後真實資料接上時，其播放時刻 − 擷取時刻仍約等於提前量
            lead = self.next_play_lead_ms()
            if lead is not None and lead * 1e6 < fill_trigger_ns:
                while lead * 1e6 < lead_ns:
                    self._send_frame(silence)
                    self.stats.silence_packets += 1
                    lead += packet_ns / 1e6
                sent_any = True
            if lead is not None:
                jit = self.ptp.estimator.jitter_ns
                if jit is not None:
                    self.stats.ptp_jitter_ms = jit / 1e6
            if sent_any:
                await self._writer.drain()
            if reader.at_eof():
                raise RuntimeError("接收端關閉了音訊連線")
            if time.monotonic() - last_log > 5.0:
                last_log = time.monotonic()
                self.stats.ptp_summary = self.ptp.summary()
                _LOGGER.debug(
                    "buffered：lead %.1f ms，封包 %d（靜音 %d），漂移 %+.0f ppm，%s",
                    self.stats.lead_ms,
                    self.stats.packets,
                    self.stats.silence_packets,
                    controller.adjustment * 1e6,
                    self.stats.ptp_summary,
                )
            await asyncio.sleep(0.002)
