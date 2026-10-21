"""AirPlay 2 buffered 模式用的 PTP 時鐘（IEEE 1588 / gPTP 風格、unicast、UDP 319 / 320）。

角色：**從屬（slave）**。HomePod 在 buffered 串流中自己當主時鐘（Mac → HomePod 的抓包亦然），
傳送端只要：
  1. 回應 HomePod 的 Announce / Signaling（以較差的 priority1 = 250 參與 BMCA，讓 HomePod 勝出）；
  2. 收 Sync（t2）與 Follow_Up（t1 = preciseOriginTimestamp + correctionField）；
  3. 每次 Follow_Up 後送 Delay_Req（t3）、收 Delay_Resp（t4）；
  4. 以「最小延遲樣本」估計本機時鐘與主時鐘的差，之後 SETRATEANCHORTIME 用主時鐘時間表示。

所有訊息都是 unicast，且來源埠必須等於目的埠（319 → 319、320 → 320）。Windows 綁 319 / 320 不需要管理員權限。
時間戳為 48-bit 秒 + 32-bit 奈秒；本機時鐘以 perf_counter 為基礎、單調不倒退。

協定細節整理自公開抓包分析與開源接收端（nqptp / shairport-sync）的行為，見 ROADMAP.md。
"""

from __future__ import annotations

import asyncio
import collections
import logging
import struct
import time
import uuid
from typing import Optional

_LOGGER = logging.getLogger(__name__)

PORT_EVENT = 319
PORT_GENERAL = 320

MSG_SYNC = 0x0
MSG_DELAY_REQ = 0x1
MSG_FOLLOW_UP = 0x8
MSG_DELAY_RESP = 0x9
MSG_ANNOUNCE = 0xB
MSG_SIGNALING = 0xC

FLAG_TWO_STEP = 0x0200
FLAG_UNICAST = 0x0400
FLAG_PTP_TIMESCALE = 0x0008

GPTP_OUI = b"\x00\x80\xc2"
APPLE_OUI = b"\x00\x0d\x93"

HEADER_LEN = 34
_HEADER = struct.Struct(">BBHBBHqI8sHHBb")

# 我方 BMCA 參數：priority1 250（Mac 傳送端用的值，輸給 HomePod 的 248）
OUR_PRIORITY1 = 250
OUR_PRIORITY2 = 128
OUR_CLOCK_CLASS = 248
OUR_CLOCK_ACCURACY = 0xFE
OUR_VARIANCE = 0xFFFF
UTC_OFFSET = 37
TIME_SOURCE_INTERNAL = 0xA0

WINDOW_NS = 8_000_000_000  # 偏移估計的樣本視窗（8 秒；本機與 HomePod 時鐘差數十 ppm 內誤差 < 1 ms）
LOCK_MIN_SAMPLES = 2


def local_clock_identity() -> bytes:
    """EUI-48 → EUI-64（中間插 FF FE），與 Apple 裝置相同的做法。"""
    mac = uuid.getnode().to_bytes(6, "big")
    return mac[:3] + b"\xff\xfe" + mac[3:]


class LocalClock:
    """單調本機時鐘，以 Unix 奈秒為刻度（起點對齊 time.time_ns，之後只靠 perf_counter 前進）。"""

    def __init__(self) -> None:
        self.base_ns = time.time_ns() - time.perf_counter_ns()

    def now_ns(self) -> int:
        return time.perf_counter_ns() + self.base_ns


def pack_timestamp(ns: int) -> bytes:
    secs, nanos = divmod(int(ns), 1_000_000_000)
    return secs.to_bytes(6, "big") + nanos.to_bytes(4, "big")


def unpack_timestamp(data: bytes) -> int:
    secs = int.from_bytes(data[:6], "big")
    nanos = int.from_bytes(data[6:10], "big")
    return secs * 1_000_000_000 + nanos


def build_header(
    msg_type: int,
    length: int,
    clock_id: bytes,
    seq: int,
    *,
    flags: int,
    control: int,
    log_interval: int,
    port: int = 1,
    correction_ns: float = 0.0,
) -> bytes:
    return _HEADER.pack(
        0x10 | msg_type,  # transportSpecific = 1（Apple / 802.1AS）
        2,  # versionPTP
        length,
        0,  # domainNumber
        0,
        flags,
        int(correction_ns * 65536),
        0,
        clock_id,
        port,
        seq & 0xFFFF,
        control,
        log_interval,
    )


def parse_header(data: bytes) -> Optional[dict]:
    if len(data) < HEADER_LEN:
        return None
    (b0, version, length, domain, _r, flags, correction, _r2, clock_id, port, seq, control, log_interval) = (
        _HEADER.unpack(data[:HEADER_LEN])
    )
    return {
        "type": b0 & 0x0F,
        "version": version & 0x0F,
        "length": length,
        "domain": domain,
        "flags": flags,
        "correction_ns": correction / 65536.0,
        "clock_id": clock_id,
        "port": port,
        "seq": seq,
        "control": control,
        "log_interval": log_interval,
    }


# ------------------------------------------------------------------ 訊息建構
def build_announce(clock_id: bytes, seq: int, priority1: int = OUR_PRIORITY1) -> bytes:
    body = struct.pack(
        ">10shBBBBHB8sHB",
        b"\x00" * 10,  # originTimestamp
        UTC_OFFSET,
        0,
        priority1,
        OUR_CLOCK_CLASS,
        OUR_CLOCK_ACCURACY,
        OUR_VARIANCE,
        OUR_PRIORITY2,
        clock_id,  # grandmasterIdentity = 自己
        0,  # stepsRemoved
        TIME_SOURCE_INTERNAL,
    )
    path_trace = struct.pack(">HH8s", 0x0008, 8, clock_id)
    length = HEADER_LEN + len(body) + len(path_trace)
    header = build_header(
        MSG_ANNOUNCE,
        length,
        clock_id,
        seq,
        flags=FLAG_UNICAST | FLAG_PTP_TIMESCALE,
        control=5,
        log_interval=0,
    )
    return header + body + path_trace


def _org_tlv(oui: bytes, subtype: int, payload: bytes) -> bytes:
    return struct.pack(">HH", 0x0003, 6 + len(payload)) + oui + subtype.to_bytes(3, "big") + payload


def build_signaling(clock_id: bytes, seq: int, *, stop: bool = False) -> bytes:
    """Mac 傳送端風格的 Signaling：802.1AS Message Interval Request + 兩個 Apple TLV（值取自抓包）。"""
    if stop:
        interval_req = _org_tlv(GPTP_OUI, 2, bytes([0x7E, 0x7E, 0x7E, 0x00]))
    else:
        interval_req = _org_tlv(
            GPTP_OUI, 2, bytes([0xFD, 0xFD, 0xFE, 0x02])
        )  # linkDelay -3, timeSync -3, announce -2
    apple1 = _org_tlv(APPLE_OUI, 1, bytes.fromhex("00030000000027100000271000000000"))
    apple5 = _org_tlv(APPLE_OUI, 5, bytes.fromhex("000f0000000027100000271000000000") + b"\x00" * 10)
    body = b"\xff" * 10 + interval_req + apple1 + apple5  # targetPortIdentity = all ones
    length = HEADER_LEN + len(body)
    header = build_header(
        MSG_SIGNALING, length, clock_id, seq, flags=FLAG_UNICAST, control=5, log_interval=0x7F
    )
    return header + body


def build_delay_req(clock_id: bytes, seq: int, t3_ns: int) -> bytes:
    header = build_header(
        MSG_DELAY_REQ,
        HEADER_LEN + 10,
        clock_id,
        seq,
        flags=FLAG_UNICAST | FLAG_PTP_TIMESCALE,
        control=1,
        log_interval=0x7F,
    )
    return header + pack_timestamp(t3_ns)


def build_sync(clock_id: bytes, seq: int) -> bytes:
    header = build_header(
        MSG_SYNC,
        HEADER_LEN + 10,
        clock_id,
        seq,
        flags=FLAG_TWO_STEP | FLAG_UNICAST | FLAG_PTP_TIMESCALE,
        control=0,
        log_interval=-3,
    )
    return header + b"\x00" * 10


def build_follow_up(clock_id: bytes, seq: int, t1_ns: int) -> bytes:
    info_tlv = _org_tlv(GPTP_OUI, 1, b"\x00" * 22)  # Follow_Up information TLV（全零）
    apple4 = _org_tlv(APPLE_OUI, 4, clock_id + b"\x00\x00")  # clock source identity
    body = pack_timestamp(t1_ns) + info_tlv + apple4
    header = build_header(
        MSG_FOLLOW_UP,
        HEADER_LEN + len(body),
        clock_id,
        seq,
        flags=FLAG_UNICAST | FLAG_PTP_TIMESCALE,
        control=2,
        log_interval=-3,
    )
    return header + body


def build_delay_resp(clock_id: bytes, req: dict, t4_ns: int, requesting_port_identity: bytes) -> bytes:
    body = pack_timestamp(t4_ns) + requesting_port_identity
    header = build_header(
        MSG_DELAY_RESP,
        HEADER_LEN + len(body),
        clock_id,
        req["seq"],
        flags=FLAG_UNICAST | FLAG_PTP_TIMESCALE,
        control=3,
        log_interval=0x7F,
    )
    return header + body


# ------------------------------------------------------------------ 偏移估計
class OffsetEstimator:
    """以視窗內「最小單向延遲」的樣本估計 offset = 本機 − 主時鐘（奈秒）。

    m = t2 − t1（主→本機，含路徑延遲）、s = t4 − t3（本機→主，含路徑延遲）：
      offset = (min m − min s) / 2，path = (min m + min s) / 2。
    沒有 Delay_Resp 時只用 m（誤差 = 單向延遲，區網上約 1 ms，只影響固定延遲）。
    """

    def __init__(self, window_ns: int = WINDOW_NS) -> None:
        self.window_ns = window_ns
        self._m: collections.deque[tuple[int, int]] = collections.deque()
        self._s: collections.deque[tuple[int, int]] = collections.deque()
        self.samples_m = 0
        self.samples_s = 0

    def _trim(self, dq: collections.deque[tuple[int, int]], now: int) -> None:
        while dq and now - dq[0][0] > self.window_ns:
            dq.popleft()

    OUTLIER_NS = 1_000_000_000  # 與目前中位數差超過 1 秒的樣本視為異常（偽造或壞封包），不採用
    rejected = 0

    def _plausible(self, dq: collections.deque[tuple[int, int]], value: int) -> bool:
        if len(dq) < 4:
            return True
        values = sorted(v for _, v in dq)
        median = values[len(values) // 2]
        return abs(value - median) <= self.OUTLIER_NS

    def add_master_to_local(self, local_ns: int, m_ns: int) -> None:
        if not self._plausible(self._m, m_ns):
            self.rejected += 1
            return
        self._m.append((local_ns, m_ns))
        self.samples_m += 1
        self._trim(self._m, local_ns)

    def add_local_to_master(self, local_ns: int, s_ns: int) -> None:
        if not self._plausible(self._s, s_ns):
            self.rejected += 1
            return
        self._s.append((local_ns, s_ns))
        self.samples_s += 1
        self._trim(self._s, local_ns)

    @property
    def ready(self) -> bool:
        return len(self._m) >= LOCK_MIN_SAMPLES

    @property
    def offset_ns(self) -> Optional[int]:
        if not self._m:
            return None
        min_m = min(v for _, v in self._m)
        if self._s:
            min_s = min(v for _, v in self._s)
            return (min_m - min_s) // 2
        return min_m

    @property
    def path_delay_ns(self) -> Optional[int]:
        if not self._m or not self._s:
            return None
        return (min(v for _, v in self._m) + min(v for _, v in self._s)) // 2

    @property
    def jitter_ns(self) -> Optional[int]:
        if len(self._m) < 2:
            return None
        values = [v for _, v in self._m]
        return max(values) - min(values)


# ------------------------------------------------------------------ UDP 端點
class _Endpoint(asyncio.DatagramProtocol):
    def __init__(self, owner: PtpClock, port: int) -> None:
        self.owner = owner
        self.port = port
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.owner._on_datagram(self.port, data, addr, self.owner.clock.now_ns())  # pylint: disable=protected-access

    def error_received(self, exc) -> None:
        _LOGGER.debug("PTP 埠 %d 錯誤：%s", self.port, exc)


class PtpClock:
    """對一台接收端（peer_ip）的 PTP 從屬時鐘。start() 後背景運作，master_now_ns() 取主時鐘時間。"""

    def __init__(self, peer_ip: str, *, local_ip: Optional[str] = None) -> None:
        self.peer_ip = peer_ip
        self.local_ip = local_ip
        self.clock = LocalClock()
        self.clock_id = local_clock_identity()
        self.estimator = OffsetEstimator()
        self._event: Optional[_Endpoint] = None
        self._general: Optional[_Endpoint] = None
        self._task: Optional[asyncio.Task] = None
        self._seq = {"announce": 0, "signaling": 0, "delay_req": 0, "sync": 0}
        self._pending_sync: dict[int, int] = {}  # seq -> t2
        self._pending_delay: dict[int, int] = {}  # seq -> t3
        self.master_id: Optional[bytes] = None  # grandmasterIdentity（SETRATEANCHORTIME 的 timelineID）
        self.master_priority1: Optional[int] = None
        self.first_follow_up_ns: Optional[int] = None
        self.last_follow_up_ns: Optional[int] = None
        self._last_reply_ns = 0
        self.stats = {
            "announce_rx": 0,
            "sync_rx": 0,
            "follow_up_rx": 0,
            "delay_resp_rx": 0,
            "signaling_rx": 0,
            "other_rx": 0,
            "delay_req_rx": 0,
        }
        self.responding_as_master = False

    # ------------------------------------------------------------ 生命週期
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        for port in (PORT_EVENT, PORT_GENERAL):
            _transport, proto = await loop.create_datagram_endpoint(
                lambda port=port: _Endpoint(self, port), local_addr=(self.local_ip or "0.0.0.0", port)
            )
            if port == PORT_EVENT:
                self._event = proto
            else:
                self._general = proto
        self._task = asyncio.create_task(self._announce_loop())
        _LOGGER.info("PTP 從屬時鐘啟動：peer %s，本機 clockIdentity %s", self.peer_ip, self.clock_id.hex())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        for ep in (self._event, self._general):
            if ep and ep.transport:
                ep.transport.close()
        self._event = self._general = None

    # ------------------------------------------------------------ 狀態
    @property
    def locked(self) -> bool:
        return self.estimator.ready and self.master_id is not None

    @property
    def offset_ns(self) -> Optional[int]:
        return self.estimator.offset_ns

    def to_master_ns(self, local_ns: int) -> int:
        offset = self.estimator.offset_ns
        if offset is None:
            raise RuntimeError("PTP 尚未鎖定")
        return local_ns - offset

    def master_now_ns(self) -> int:
        return self.to_master_ns(self.clock.now_ns())

    def local_from_perf_ns(self, perf_ns: int) -> int:
        """把 time.perf_counter_ns() 的時刻換成本機時鐘刻度（與 clock.now_ns 同基準）。"""
        return perf_ns + self.clock.base_ns

    def to_local_ns(self, master_ns: int) -> int:
        offset = self.estimator.offset_ns
        if offset is None:
            raise RuntimeError("PTP 尚未鎖定")
        return master_ns + offset

    async def wait_locked(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.locked:
                return True
            await asyncio.sleep(0.05)
        return self.locked

    def summary(self) -> str:
        est = self.estimator
        off = est.offset_ns
        path = est.path_delay_ns
        jit = est.jitter_ns
        return (
            f"master={self.master_id.hex() if self.master_id else '-'} "
            f"offset={off / 1e6 if off is not None else float('nan'):.3f} ms "
            f"path={path / 1e6 if path is not None else float('nan'):.3f} ms "
            f"jitter={jit / 1e6 if jit is not None else float('nan'):.3f} ms "
            f"m={est.samples_m} s={est.samples_s} rx={self.stats}"
        )

    # ------------------------------------------------------------ 傳送
    def _send(self, port: int, data: bytes) -> None:
        ep = self._event if port == PORT_EVENT else self._general
        if ep and ep.transport:
            ep.transport.sendto(data, (self.peer_ip, port))

    def _next(self, key: str) -> int:
        self._seq[key] = (self._seq[key] + 1) & 0xFFFF
        return self._seq[key]

    def send_announce(self) -> None:
        self._send(PORT_GENERAL, build_announce(self.clock_id, self._next("announce")))

    def send_signaling(self, stop: bool = False) -> None:
        self._send(PORT_GENERAL, build_signaling(self.clock_id, self._next("signaling"), stop=stop))

    def _send_delay_req(self) -> None:
        seq = self._next("delay_req")
        t3 = self.clock.now_ns()
        self._pending_delay[seq] = t3
        if len(self._pending_delay) > 16:
            oldest = min(self._pending_delay)
            self._pending_delay.pop(oldest, None)
        self._send(PORT_EVENT, build_delay_req(self.clock_id, seq, t3))

    async def _announce_loop(self) -> None:
        """每秒送 Announce + Signaling（HomePod 回應前也送，當作邀請）。"""
        try:
            while True:
                self.send_announce()
                self.send_signaling()
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------ 接收
    def _on_datagram(self, port: int, data: bytes, addr, rx_ns: int) -> None:
        if addr[0] != self.peer_ip:
            return
        hdr = parse_header(data)
        if hdr is None or hdr["version"] != 2 or hdr["domain"] != 0:
            return
        kind = hdr["type"]
        # 只接受已知主時鐘（從 Announce 學到的 grandmaster）送來的計時訊息；來源 IP 可偽造，clockIdentity 多一道門檻
        timing = kind in (MSG_SYNC, MSG_FOLLOW_UP, MSG_DELAY_RESP)
        if timing and (self.master_id is None or hdr["clock_id"] != self.master_id):
            self.stats["other_rx"] += 1
            return
        if kind in (MSG_FOLLOW_UP, MSG_DELAY_RESP, MSG_SYNC, MSG_DELAY_REQ) and len(data) < HEADER_LEN + 10:
            return
        if kind == MSG_SYNC and port == PORT_EVENT:
            self.stats["sync_rx"] += 1
            self._pending_sync[hdr["seq"]] = rx_ns
            if len(self._pending_sync) > 16:
                self._pending_sync.pop(min(self._pending_sync), None)
        elif kind == MSG_FOLLOW_UP:
            self.stats["follow_up_rx"] += 1
            t2 = self._pending_sync.pop(hdr["seq"], None)
            if t2 is None:
                return
            t1 = unpack_timestamp(data[HEADER_LEN : HEADER_LEN + 10]) + int(hdr["correction_ns"])
            self.estimator.add_master_to_local(rx_ns, t2 - t1)
            if self.master_id is None:
                self.master_id = hdr["clock_id"]
            if self.first_follow_up_ns is None:
                self.first_follow_up_ns = rx_ns
                _LOGGER.info("收到第一個 Follow_Up：主時鐘 %s", hdr["clock_id"].hex())
            self.last_follow_up_ns = rx_ns
            self._send_delay_req()
        elif kind == MSG_DELAY_RESP:
            self.stats["delay_resp_rx"] += 1
            t3 = self._pending_delay.pop(hdr["seq"], None)
            if t3 is None:
                return
            t4 = unpack_timestamp(data[HEADER_LEN : HEADER_LEN + 10]) + int(hdr["correction_ns"])
            self.estimator.add_local_to_master(rx_ns, t4 - t3)
        elif kind == MSG_ANNOUNCE:
            self.stats["announce_rx"] += 1
            if len(data) >= 64:
                priority1 = data[47]
                gm = data[53:61]
                if self.master_id != gm:
                    _LOGGER.info("接收端 Announce：grandmaster %s priority1 %d", gm.hex(), priority1)
                self.master_id = gm
                self.master_priority1 = priority1
            # 回應：我們的 Announce（priority1 250，讓它勝出）；每 250 ms 最多回一次，避免被拿來當反射器
            if rx_ns - self._last_reply_ns > 250_000_000:
                self._last_reply_ns = rx_ns
                self.send_announce()
        elif kind == MSG_SIGNALING:
            self.stats["signaling_rx"] += 1
            if rx_ns - self._last_reply_ns > 250_000_000:
                self._last_reply_ns = rx_ns
                self.send_signaling()
        elif kind == MSG_DELAY_REQ:
            # 對方把我們當主時鐘：仍回 Delay_Resp（以本機時鐘）避免它放棄
            self.stats["delay_req_rx"] += 1
            self.responding_as_master = True
            self._send(PORT_GENERAL, build_delay_resp(self.clock_id, hdr, rx_ns, data[20:30]))
        else:
            self.stats["other_rx"] += 1
            _LOGGER.debug("PTP 其他訊息 type=%#x len=%d", kind, len(data))
