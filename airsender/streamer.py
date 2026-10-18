"""AirPlay 串流控制：掃描裝置、連線、串流、自動重連。

所有 pyatv 操作都在專屬的 asyncio 事件迴圈執行緒上進行；對外提供執行緒安全的方法。
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional

import pyatv
from pyatv.const import Protocol
from pyatv.interface import MediaMetadata

from . import APP_NAME, firewall, patches
from .config import Config
from .live_source import LiveAudioSource

_LOGGER = logging.getLogger(__name__)


class State(Enum):
    IDLE = "閒置"
    CONNECTING = "連線中"
    STREAMING = "串流中"
    RECONNECTING = "重新連線中"


@dataclass(frozen=True)
class Device:
    name: str
    identifier: str
    address: str
    model: str

    @property
    def label(self) -> str:
        return f"{self.name}（{self.model}，{self.address}）"


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


class AirPlayStreamer:
    def __init__(self, config: Config, on_state: Optional[StateCallback] = None) -> None:
        self.config = config
        self._on_state = on_state
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="airplay-loop", daemon=True
        )
        self._task: Optional[asyncio.Task] = None
        self._atv = None
        self._source: Optional[LiveAudioSource] = None
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
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3.0)

    # ------------------------------------------------------------ 對外 API
    @property
    def is_active(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def source(self) -> Optional[LiveAudioSource]:
        return self._source

    def scan(self, timeout: float = 5.0) -> List[Device]:
        fut = asyncio.run_coroutine_threadsafe(self._scan(timeout), self._loop)
        return fut.result(timeout + 10)

    def start(self, device: Device) -> None:
        self.device = device
        self.config.device_identifier = device.identifier
        self.config.device_name = device.name
        self.config.device_address = device.address
        self.config.save()
        asyncio.run_coroutine_threadsafe(self._start(device), self._loop).result(15)

    def stop(self) -> None:
        asyncio.run_coroutine_threadsafe(self._stop(), self._loop).result(15)

    def restart(self) -> None:
        """套用新設定（延遲、音質等）：若正在串流則重新連線。"""
        if self.device and self.is_active:
            self.start(self.device)

    def set_volume(self, percent: int) -> None:
        self.config.volume = max(0, min(100, percent))
        self.config.save()
        if self._atv is not None:
            asyncio.run_coroutine_threadsafe(self._set_volume(percent), self._loop)

    # ------------------------------------------------------------ 內部（事件迴圈執行緒）
    def _set_state(self, state: State, message: str = "") -> None:
        self.state = state
        self.message = message
        _LOGGER.info("狀態：%s %s", state.value, message)
        if self._on_state:
            try:
                self._on_state(state, message)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("狀態回呼失敗")

    async def _scan(self, timeout: float) -> List[Device]:
        confs = await pyatv.scan(self._loop, timeout=timeout)
        devices = []
        for conf in confs:
            if conf.get_service(Protocol.RAOP) is None:
                continue
            model = conf.device_info.raw_model or conf.device_info.model.name
            devices.append(
                Device(conf.name, conf.identifier or "", str(conf.address), str(model))
            )
        devices.sort(key=lambda d: d.name)
        return devices

    async def _find(self, device: Device):
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

    async def _set_volume(self, percent: int) -> None:
        if self._atv is None:
            return
        try:
            await self._atv.audio.set_volume(float(percent))
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.warning("設定音量失敗：%s", exc)

    def _metadata(self) -> MediaMetadata:
        return MediaMetadata(
            title="Windows 系統音訊",
            artist=socket.gethostname(),
            album=APP_NAME,
        )

    async def _run(self, device: Device) -> None:
        attempt = 0
        while True:
            atv = None
            try:
                self._set_state(
                    State.CONNECTING if attempt == 0 else State.RECONNECTING,
                    f"連線到 {device.name}",
                )
                conf = await self._find(device)
                if conf is None:
                    raise RuntimeError(f"在網路上找不到 {device.name}")

                patches.set_latency_ms(self.config.receiver_latency_ms)
                atv = await pyatv.connect(conf, self._loop, protocol=Protocol.RAOP)
                self._atv = atv

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

                attempt = 0
                await atv.stream.stream_file(self._source, metadata=self._metadata())
                # 正常回傳代表接收端結束了串流（例如被其他來源接管）
                self._set_state(State.RECONNECTING, "串流被接收端結束，重新連線")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pylint: disable=broad-except
                attempt += 1
                _LOGGER.warning("串流失敗：%s", exc, exc_info=_LOGGER.isEnabledFor(logging.DEBUG))
                wait = min(2 * attempt, 15)
                message = str(exc)
                if "no response" in message.lower() or "timeout" in message.lower():
                    present = await self._loop.run_in_executor(None, firewall.rules_present)
                    if not present:
                        message += "；可能被 Windows 防火牆攔截，請在選單選「允許通過 Windows 防火牆」"
                self._set_state(State.RECONNECTING, f"{message}（{wait} 秒後重試）")
                await asyncio.sleep(wait)
                continue
            finally:
                if self._source is not None:
                    self._source.stop()
                    self._source = None
                if atv is not None:
                    atv.close()
                self._atv = None
            await asyncio.sleep(1.0)
