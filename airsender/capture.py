"""WASAPI loopback 擷取。

把 Windows 目前正在播放的聲音（loopback）以裝置原生格式取出（通常是 float32 / 44.1k~192k），
以 libsoxr 高品質重取樣到接收端需要的取樣率（AirPlay 為 44100 Hz），
加入 TPDF dither 後轉成 16-bit little-endian PCM，交給 sink。
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pyaudiowpatch as pa
import soxr

_LOGGER = logging.getLogger(__name__)

# 可變速率模式允許的最大比率調整（±0.2%）；實際由 DriftController 限制在更小範圍
MAX_RATE_ADJUST = 0.002


@dataclass
class CaptureStatus:
    device_name: str = ""
    source_rate: int = 0
    source_channels: int = 0
    source_format: str = ""
    target_rate: int = 0
    running: bool = False
    errors: int = 0
    last_error: str = ""
    overflows: int = 0
    frames_captured: int = field(default=0)


def _co_initialize() -> bool:
    try:
        import ctypes  # pylint: disable=import-outside-toplevel

        hr = ctypes.windll.ole32.CoInitializeEx(None, 0x0)  # COINIT_MULTITHREADED  # type: ignore[attr-defined]
        return hr in (0, 1)  # S_OK / S_FALSE（已初始化）
    except Exception:  # pylint: disable=broad-except
        return False


def _co_uninitialize() -> None:
    try:
        import ctypes  # pylint: disable=import-outside-toplevel

        ctypes.windll.ole32.CoUninitialize()  # type: ignore[attr-defined]
    except Exception:  # pylint: disable=broad-except
        pass


def list_loopback_devices() -> list[tuple[int, str, int]]:
    """列出所有可擷取的輸出裝置 (index, 名稱, 取樣率)。index 為 loopback 裝置索引。"""
    p = pa.PyAudio()
    try:
        out = []
        for info in p.get_loopback_device_info_generator():
            name = str(info["name"]).replace(" [Loopback]", "")
            out.append((int(info["index"]), name, int(info["defaultSampleRate"])))
        return out
    finally:
        p.terminate()


def default_output_name() -> str:
    p = pa.PyAudio()
    try:
        info = p.get_default_wasapi_loopback()
        return str(info["name"]).replace(" [Loopback]", "")
    except Exception:  # pylint: disable=broad-except
        return ""
    finally:
        p.terminate()


class WasapiLoopbackCapture:
    """在背景執行緒持續擷取系統音訊並送到 sink(bytes)。"""

    def __init__(
        self,
        sink: Callable[[bytes], None],
        *,
        target_rate: int = 44100,
        target_channels: int = 2,
        device_index: Optional[int] = None,
        quality: str = "VHQ",
        dither: bool = True,
        block_ms: int = 10,
        variable_rate: bool = False,
        rate_adjust: Optional[Callable[[], float]] = None,
    ) -> None:
        """variable_rate=True 時改用 soxr 可變速率模式，每個區塊以 rate_adjust() 回傳的
        比例（例如 +0.0005 = 輸入視為快 0.05%）微調，用來吸收擷取端與傳送端的時鐘漂移。"""
        if target_channels != 2:
            raise ValueError("目前只支援雙聲道輸出")
        self.sink = sink
        self.target_rate = target_rate
        self.device_index = device_index
        self.quality = quality
        self.dither = dither
        self.block_ms = block_ms
        self.variable_rate = variable_rate
        self.rate_adjust = rate_adjust
        self.status = CaptureStatus(target_rate=target_rate)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._rng = np.random.default_rng()

    # ------------------------------------------------------------------ 控制
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="wasapi-loopback", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

    # ------------------------------------------------------------------ 內部
    def _pick_device(self, p: pa.PyAudio) -> dict:
        if self.device_index is not None:
            info = p.get_device_info_by_index(self.device_index)
            if not info.get("isLoopbackDevice"):
                info = p.get_wasapi_loopback_analogue_by_dict(info)
            return info
        return p.get_default_wasapi_loopback()

    def _default_changed(self, current_name: str) -> bool:
        probe = pa.PyAudio()
        try:
            info = probe.get_default_wasapi_loopback()
            return str(info["name"]) != current_name
        except Exception:  # pylint: disable=broad-except
            return False
        finally:
            probe.terminate()

    def _watch_default_device(self, current_name: str, changed: threading.Event) -> None:
        """獨立執行緒：每 10 秒檢查一次預設輸出裝置是否變更（PyAudio 初始化要數十毫秒，
        不能在擷取執行緒上做，否則會造成 underrun）。"""
        while not self._stop.wait(10.0) and not changed.is_set():
            if self._default_changed(current_name):
                changed.set()

    def _run(self) -> None:
        # WASAPI 需要呼叫執行緒已初始化 COM。PortAudio 只在第一次 Pa_Initialize 的執行緒初始化，
        # 若程式其他執行緒（例如量測工具的主執行緒）先建立了 PyAudio，這裡開串流會得到
        # 「[Errno -9999] Unanticipated host error」。自己初始化一次最保險（重複呼叫無害）。
        com_initialized = _co_initialize()
        try:
            self._capture_loop()
        finally:
            if com_initialized:
                _co_uninitialize()

    def _capture_loop(self) -> None:
        while not self._stop.is_set():
            p = pa.PyAudio()
            stream = None
            try:
                info = self._pick_device(p)
                rate = int(info["defaultSampleRate"])
                channels = int(info["maxInputChannels"])
                index = int(info["index"])
                block = max(64, int(rate * self.block_ms / 1000))

                fmt = pa.paFloat32
                try:
                    p.is_format_supported(
                        rate,
                        input_device=index,
                        input_channels=channels,
                        input_format=fmt,
                    )
                except ValueError:
                    fmt = pa.paInt16

                # callback 模式：PortAudio 自己的執行緒把每個區塊丟進佇列，這裡的工作執行緒
                # 以 timeout 等待。好處：stop() 永遠能在 0.25 s 內返回並確實關閉 WASAPI
                # 串流（阻塞式 read() 在沒有程式播音時會卡住，導致裝置無法釋放）。
                chunks: queue.SimpleQueue[bytes] = queue.SimpleQueue()
                status = self.status

                def on_audio(in_data, _frame_count, _time_info, flags, _chunks=chunks, _status=status):
                    if flags & pa.paInputOverflow:
                        _status.overflows += 1
                    _chunks.put(in_data)
                    return (None, pa.paContinue)

                stream = p.open(
                    format=fmt,
                    channels=channels,
                    rate=rate,
                    input=True,
                    input_device_index=index,
                    frames_per_buffer=block,
                    stream_callback=on_audio,
                )

                # 兩段式重取樣：
                # 1) 固定比率（VHQ）把裝置取樣率降到目標取樣率；取樣率相同時略過
                # 2) 可變速率段在目標取樣率上做 ±0.1% 的微調（漂移補償）
                # 可變速率引擎（vr32）在 176.4 kHz 輸入端直接跑比固定比率多耗約 3% 單核心，
                # 先降到 44.1 kHz 再微調，成本降到原本的 1/4 且固定段保有 VHQ 品質。
                resampler = None
                if rate != self.target_rate:
                    resampler = soxr.ResampleStream(
                        rate, self.target_rate, 2, dtype="float32", quality=self.quality
                    )
                drift_stage = None
                if self.variable_rate:
                    drift_stage = soxr.ResampleStream(
                        self.target_rate * (1 + MAX_RATE_ADJUST),
                        self.target_rate,
                        2,
                        dtype="float32",
                        quality=self.quality,
                        vr=True,
                    )
                    drift_stage.set_io_ratio(self.target_rate, self.target_rate, 0)

                self.status.device_name = str(info["name"]).replace(" [Loopback]", "")
                self.status.source_rate = rate
                self.status.source_channels = channels
                self.status.source_format = "float32" if fmt == pa.paFloat32 else "int16"
                self.status.running = True
                _LOGGER.info(
                    "開始擷取：%s（%d Hz / %d ch / %s）-> %d Hz 16-bit%s",
                    self.status.device_name,
                    rate,
                    channels,
                    self.status.source_format,
                    self.target_rate,
                    (f"，soxr {self.quality}" if resampler else "，不需重取樣")
                    + ("，可變速率漂移補償" if drift_stage else ""),
                )

                device_changed = threading.Event()
                if self.device_index is None:
                    threading.Thread(
                        target=self._watch_default_device,
                        args=(str(info["name"]), device_changed),
                        name="default-device-watch",
                        daemon=True,
                    ).start()

                while not self._stop.is_set():
                    try:
                        raw = chunks.get(timeout=0.25)
                    except queue.Empty:  # noqa: PERF203
                        # 沒有程式在播音時 loopback 不會送資料，這是正常的；
                        # 但若串流本身停了（裝置被拔除），就重新開啟。
                        if not stream.is_active():
                            raise RuntimeError("擷取串流已停止（裝置被移除或變更？）") from None
                        if device_changed.is_set():
                            break
                        continue
                    self._process(raw, fmt, channels, resampler, drift_stage)
                    if device_changed.is_set():
                        _LOGGER.info("Windows 預設輸出裝置已變更，重新開啟擷取")
                        break
            except Exception as exc:  # pylint: disable=broad-except
                self.status.errors += 1
                self.status.last_error = str(exc)
                _LOGGER.warning("音訊擷取失敗：%s（1 秒後重試）", exc)
                self._stop.wait(1.0)
            finally:
                self.status.running = False
                if stream is not None:
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception:  # pylint: disable=broad-except
                        pass
                p.terminate()

    def _process(self, raw: bytes, fmt: int, channels: int, resampler, drift_stage) -> None:
        if fmt == pa.paFloat32:
            x = np.frombuffer(raw, dtype=np.float32)
        else:
            x = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            x *= 1.0 / 32768.0
        if x.size == 0:
            return
        x = x.reshape(-1, channels)
        if channels == 1:
            x = np.repeat(x, 2, axis=1)
        elif channels > 2:
            x = x[:, :2]  # 只取前左/前右
        x = np.ascontiguousarray(x, dtype=np.float32)

        if resampler is not None:
            x = resampler.resample_chunk(x)
            if x.size == 0:
                return

        if drift_stage is not None:
            adj = 0.0
            if self.rate_adjust is not None:
                adj = max(-MAX_RATE_ADJUST, min(MAX_RATE_ADJUST, self.rate_adjust()))
            drift_stage.set_io_ratio(
                self.target_rate * (1.0 + adj), self.target_rate, x.shape[0]
            )
            x = drift_stage.resample_chunk(x)
            if x.size == 0:
                return

        # 以下全為就地運算，避免每個區塊多次配置暫存陣列
        x = x if x.flags.writeable and x.base is None else x.copy()
        x *= 32767.0
        if self.dither:
            # TPDF dither：兩個均勻分布相減（三角分布），幅度 ±1 LSB
            noise = self._rng.random(x.shape, dtype=np.float32)
            noise -= self._rng.random(x.shape, dtype=np.float32)
            x += noise
        np.rint(x, out=x)
        np.clip(x, -32768.0, 32767.0, out=x)
        pcm = x.astype("<i2")
        self.status.frames_captured += pcm.shape[0]
        self.sink(pcm.tobytes())
