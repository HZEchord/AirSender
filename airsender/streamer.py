"""串流控制：掃描裝置、連線、串流、自動重連；依接收端種類分派到不同輸出路徑。

- airplay：pyatv RAOP / AirPlay 2，低延遲（LiveAudioSource 推送）
- cast / dlna：接收端拉我們的 HTTP WAV 串流（HttpStreamServer），無損但延遲數秒
- snapcast：TCP 推 raw PCM 給 snapserver

所有非同步操作都在專屬的 asyncio 事件迴圈執行緒上進行；對外提供執行緒安全的方法。
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Optional

import pyatv
from pyatv.const import Protocol
from pyatv.interface import MediaMetadata

from . import APP_NAME, firewall, pairing, patches, roomcal
from .capture import CaptureStatus
from .config import Config
from .httpstream import HttpStreamServer, PcmBroadcaster
from .live_source import LiveAudioSource
from .sinks import HTTP_PULL_KINDS
from .sinks import cast as cast_sink
from .sinks import dlna as dlna_sink
from .sinks import snapcast as snapcast_sink
from .streamer_types import Device, State

_LOGGER = logging.getLogger(__name__)

__all__ = ["Streamer", "AirPlayStreamer", "Device", "State"]

StateCallback = Callable[[State, str], None]


def _enable_high_resolution_timer() -> None:
    """Windows 預設計時器解析度約 15.6 ms，會讓 pyatv 每個封包的 asyncio.sleep 都睡過頭。

    改成 1 ms 讓送封包的節奏精準，減少抖動與補送。
    """
    try:
        import ctypes  # pylint: disable=import-outside-toplevel

        ctypes.windll.winmm.timeBeginPeriod(1)  # type: ignore[attr-defined]
        _LOGGER.debug("已將系統計時器解析度設為 1 ms")
    except Exception as exc:  # pylint: disable=broad-except
        _LOGGER.debug("無法設定計時器解析度：%s", exc)


class Streamer:
    def __init__(
        self, config: Config, on_state: Optional[StateCallback] = None, *, persist: bool = True
    ) -> None:
        """persist=False 時不把選擇的裝置 / 音量寫回設定檔（量測與測試工具用）。"""
        self.config = config
        self._on_state = on_state
        self._persist = persist
        # 曾測試 SelectorEventLoop（UDP 直接 sendto）與 Windows 預設 Proactor 的差異，
        # 在 125 封包/秒下 CPU 差異落在量測雜訊內（約 4–5% 單核心），故維持預設迴圈。
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="stream-loop", daemon=True
        )
        self._task: Optional[asyncio.Task] = None
        self._atv = None
        self._source: Optional[LiveAudioSource] = None
        self._sink_set_volume: Optional[Callable[[int], None]] = None
        self.broadcaster = PcmBroadcaster(self._loop, config)
        self.http = HttpStreamServer(self._loop, self.broadcaster, config.http_port)
        self.device: Optional[Device] = None
        self.state = State.IDLE
        self.message = ""
        patches.apply()

    # ------------------------------------------------------------ 生命週期
    def start_loop(self) -> None:
        if not self._thread.is_alive():
            _enable_high_resolution_timer()
            self._thread.start()

    def shutdown(self) -> None:
        with suppress(Exception):
            self.stop()
        with suppress(Exception):
            asyncio.run_coroutine_threadsafe(self.http.stop(), self._loop).result(5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3.0)

    # ------------------------------------------------------------ 對外 API
    @property
    def is_active(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def source(self) -> Optional[LiveAudioSource]:
        """AirPlay 路徑的即時音源（其他路徑為 None）。"""
        return self._source

    @property
    def capture_status(self) -> Optional[CaptureStatus]:
        if self._source is not None and self._source.capture is not None:
            return self._source.capture.status
        if self.broadcaster.capture is not None:
            return self.broadcaster.capture.status
        return None

    def scan(self, timeout: float = 5.0) -> list[Device]:
        fut = asyncio.run_coroutine_threadsafe(self._scan(timeout), self._loop)
        return fut.result(timeout + 20)

    def rebuild_eq(self) -> None:
        """依目前設定重建房間校正 / 低音增強 EQ（串流中即時生效，不必重連）。"""
        roomcal.ENGINE.rebuild(self.config)

    def start(self, device: Device) -> None:
        self.rebuild_eq()
        self.device = device
        self.config.device_identifier = device.identifier
        self.config.device_name = device.name
        self.config.device_address = device.address
        self.config.device_kind = device.kind
        self.config.device_url = device.url
        self._save()
        asyncio.run_coroutine_threadsafe(self._start(device), self._loop).result(15)

    def stop(self) -> None:
        asyncio.run_coroutine_threadsafe(self._stop(), self._loop).result(15)

    def restart(self) -> None:
        """套用新設定（延遲、音質等）：若正在串流則重新連線。"""
        if self.device and self.is_active:
            self.start(self.device)

    def saved_device(self) -> Optional[Device]:
        """上次使用的接收端（供自動連線；AirPlay 仍需掃描確認在線）。"""
        if not self.config.device_identifier:
            return None
        return Device(
            name=self.config.device_name or "",
            identifier=self.config.device_identifier,
            address=self.config.device_address or "",
            model="",
            kind=self.config.device_kind or "airplay",
            url=self.config.device_url or "",
        )

    def set_volume(self, percent: int) -> None:
        self.config.volume = max(0, min(100, percent))
        self._save()
        asyncio.run_coroutine_threadsafe(self._set_volume(self.config.volume), self._loop)

    def volume_step(self, delta: int) -> Optional[int]:
        """音量 ±delta；以接收端目前音量為基準（若已連線且可讀）。"""
        current = self.config.volume
        if self._atv is not None:
            with suppress(Exception):
                current = int(round(self._atv.audio.volume))
        if current is None:
            current = 50
        new = max(0, min(100, current + delta))
        self.set_volume(new)
        return new

    def pair(
        self,
        device: Device,
        ask_pin: Callable[[str], Optional[str]],
        notify: Callable[[str], None],
    ) -> dict[str, bool]:
        """與 AirPlay 接收端配對（RAOP 與 AirPlay）。回傳各協定是否成功。會先停止串流，完成後恢復。"""
        if device.kind != "airplay":
            raise RuntimeError("只有 AirPlay 接收端需要配對")
        fut = asyncio.run_coroutine_threadsafe(
            self._pair(device, ask_pin, notify), self._loop
        )
        return fut.result(600)

    def set_password(self, device: Device, password: Optional[str]) -> None:
        saved = self.config.credentials.setdefault(device.identifier, {})
        if password:
            saved["password"] = password
        else:
            saved.pop("password", None)
        self.config.save()
        self.restart()

    def forget_credentials(self, device: Device) -> None:
        self.config.credentials.pop(device.identifier, None)
        self.config.save()

    def has_credentials(self, device: Device) -> bool:
        return bool(self.config.credentials.get(device.identifier))

    def add_snapcast_server(self, spec: str) -> Device:
        device = snapcast_sink.parse_server(spec)
        if spec not in self.config.snapcast_servers:
            self.config.snapcast_servers.append(spec)
            self.config.save()
        return device

    def remove_snapcast_server(self, spec: str) -> None:
        with suppress(ValueError):
            self.config.snapcast_servers.remove(spec)
        self.config.save()

    # ------------------------------------------------------------ 內部（事件迴圈執行緒）
    def _save(self) -> None:
        if self._persist:
            self.config.save()

    def _set_state(self, state: State, message: str = "") -> None:
        self.state = state
        self.message = message
        _LOGGER.info("狀態：%s %s", state.value, message)
        if self._on_state:
            try:
                self._on_state(state, message)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("狀態回呼失敗")

    async def _scan(self, timeout: float) -> list[Device]:
        async def guarded(name: str, coro: Awaitable[list[Device]]) -> list[Device]:
            try:
                return await asyncio.wait_for(coro, timeout + 10)
            except Exception as exc:  # pylint: disable=broad-except
                _LOGGER.warning("%s 掃描失敗：%s", name, exc)
                return []

        results = await asyncio.gather(
            guarded("AirPlay", self._scan_airplay(timeout)),
            guarded("Google Cast", cast_sink.discover(self._loop, timeout)),
            guarded("DLNA", dlna_sink.discover(min(timeout, 4.0))),
        )
        devices = [d for group in results for d in group]
        devices.extend(snapcast_sink.manual_devices(self.config.snapcast_servers))
        devices.sort(key=lambda d: (d.kind, d.name))
        return devices

    async def _scan_airplay(self, timeout: float) -> list[Device]:
        confs = await pyatv.scan(self._loop, timeout=timeout)
        devices = []
        for conf in confs:
            if conf.get_service(Protocol.RAOP) is None:
                continue
            model = conf.device_info.raw_model or conf.device_info.model.name
            devices.append(
                Device(conf.name, conf.identifier or "", str(conf.address), str(model), kind="airplay")
            )
        return devices

    async def _find_airplay(self, device: Device):
        confs = []
        if device.address:
            with suppress(Exception):
                confs = await pyatv.scan(self._loop, hosts=[device.address], timeout=3)
        if not confs and device.identifier:
            confs = await pyatv.scan(self._loop, identifier=device.identifier, timeout=6)
        for conf in confs:
            if conf.get_service(Protocol.RAOP) is not None:
                return conf
        return None

    async def _start(self, device: Device) -> None:
        await self._stop()
        self._task = self._loop.create_task(self._run(device))

    async def _stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        self._set_state(State.IDLE)

    async def _pair(
        self,
        device: Device,
        ask_pin: Callable[[str], Optional[str]],
        notify: Callable[[str], None],
    ) -> dict[str, bool]:
        was_active = self.is_active
        await self._stop()
        conf = await self._find_airplay(device)
        if conf is None:
            raise RuntimeError(f"在網路上找不到 {device.name}")

        async def ask(prompt: str) -> Optional[str]:
            return await self._loop.run_in_executor(None, ask_pin, prompt)

        saved = dict(self.config.credentials.get(device.identifier, {}))
        results: dict[str, bool] = {}
        for protocol in (Protocol.RAOP, Protocol.AirPlay):
            if conf.get_service(protocol) is None:
                continue
            key = pairing.PROTOCOL_KEYS[protocol]
            try:
                creds = await pairing.pair_protocol(self._loop, conf, protocol, ask, notify)
            except Exception as exc:  # pylint: disable=broad-except
                _LOGGER.warning("%s 配對失敗：%s", protocol.name, exc)
                notify(f"{protocol.name} 配對失敗：{exc}")
                results[key] = False
                continue
            if creds:
                saved[key] = creds
            results[key] = bool(creds)
        self.config.credentials[device.identifier] = saved
        self.config.save()
        if was_active:
            self._task = self._loop.create_task(self._run(device))
        return results

    async def _set_volume(self, percent: int) -> None:
        try:
            if self._atv is not None:
                await self._atv.audio.set_volume(float(percent))
            elif self._sink_set_volume is not None:
                await self._loop.run_in_executor(None, self._sink_set_volume, percent)
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.warning("設定音量失敗：%s", exc)

    def _metadata(self) -> MediaMetadata:
        return MediaMetadata(
            title="Windows 系統音訊",
            artist=socket.gethostname(),
            album=APP_NAME,
        )

    def _set_sink_volume_slot(self, fn: Optional[Callable[[int], None]]) -> None:
        self._sink_set_volume = fn

    # ------------------------------------------------------------ 串流主迴圈
    async def _run(self, device: Device) -> None:
        attempt = 0
        while True:
            try:
                self._set_state(
                    State.CONNECTING if attempt == 0 else State.RECONNECTING,
                    f"連線到 {device.name}",
                )
                if device.kind == "airplay":
                    await self._run_airplay(device)
                elif device.kind in HTTP_PULL_KINDS:
                    await self._run_http_pull(device)
                elif device.kind == "snapcast":
                    await snapcast_sink.run(
                        device,
                        self.broadcaster,
                        lambda: self._set_state(State.STREAMING, device.name),
                    )
                else:
                    raise RuntimeError(f"不支援的接收端種類：{device.kind}")
                attempt = 0
                # 正常回傳代表接收端結束了串流（例如被其他來源接管）
                self._set_state(State.RECONNECTING, "串流被接收端結束，重新連線")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pylint: disable=broad-except
                attempt += 1
                _LOGGER.warning("串流失敗：%s", exc, exc_info=_LOGGER.isEnabledFor(logging.DEBUG))
                wait = min(2 * attempt, 15)
                message = await self._explain_error(device, str(exc))
                self._set_state(State.RECONNECTING, f"{message}（{wait} 秒後重試）")
                await asyncio.sleep(wait)
                continue
            await asyncio.sleep(1.0)

    async def _explain_error(self, device: Device, message: str) -> str:
        lowered = message.lower()
        if device.kind == "airplay" and (
            "authentication" in lowered or "password" in lowered or "pair" in lowered
        ):
            return message + "；此接收端需要配對或密碼，請在選單「配對與密碼」設定"
        if "no response" in lowered or "timeout" in lowered or "timed out" in lowered:
            present = await self._loop.run_in_executor(None, firewall.rules_present)
            if not present:
                return message + "；可能被 Windows 防火牆攔截，請在選單選「允許通過 Windows 防火牆」"
        return message

    async def _run_airplay(self, device: Device) -> None:
        conf = await self._find_airplay(device)
        if conf is None:
            raise RuntimeError(f"在網路上找不到 {device.name}")
        pairing.apply_credentials(conf, self.config.credentials.get(device.identifier, {}))

        patches.set_latency_ms(self.config.receiver_latency_ms)
        patches.set_latency_min_ms(self.config.airplay_latency_min_ms)
        atv = await pyatv.connect(conf, self._loop, protocol=Protocol.RAOP)
        self._atv = atv
        try:
            self._source = LiveAudioSource(
                jitter_ms=self.config.jitter_buffer_ms,
                capture_device_index=self.config.capture_device_index,
                quality=self.config.resample_quality,
                dither=self.config.dither,
                drift_compensation=self.config.drift_compensation,
                metadata=self._metadata(),
                on_first_read=lambda: self._set_state(State.STREAMING, device.name),
            )
            if self.config.volume is not None:
                with suppress(Exception):
                    await atv.audio.set_volume(float(self.config.volume))
            await atv.stream.stream_file(self._source, metadata=self._metadata())
        finally:
            if self._source is not None:
                self._source.stop()
                self._source = None
            atv.close()
            self._atv = None

    async def _run_http_pull(self, device: Device) -> None:
        await self.http.start()
        url = self.http.url_for(device.address)
        on_streaming = lambda: self._set_state(  # noqa: E731
            State.STREAMING, f"{device.name}（HTTP 拉流，延遲由接收端緩衝決定）"
        )
        if device.kind == "cast":
            await cast_sink.run(
                self._loop, device, url, self.config.volume, on_streaming, self._set_sink_volume_slot
            )
        else:
            await dlna_sink.run(
                device, url, self.config.volume, on_streaming, self._set_sink_volume_slot, self._loop
            )


# 向後相容的別名
AirPlayStreamer = Streamer
