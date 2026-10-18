"""WASAPI loopback 擷取。

把 Windows 目前正在播放的聲音（loopback）以裝置原生格式取出（通常是 float32 / 44.1k~192k），
以 libsoxr 高品質重取樣到接收端需要的取樣率（AirPlay 為 44100 Hz），
加入 TPDF dither 後轉成 16-bit little-endian PCM，交給 sink。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

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
    frames_captured: int = field(default=0)


def list_loopback_devices() -> List[Tuple[int, str, int]]:
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
        """獨立執行緒：每 5 秒檢查一次預設輸出裝置是否變更（PyAudio 初始化要數十毫秒，
        不能在擷取執行緒上做，否則會造成 underrun）。"""
        while not self._stop.wait(5.0) and not changed.is_set():
            if self._default_changed(current_name):
                changed.set()

    def _run(self) -> None:
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

                stream = p.open(
                    format=fmt,
                    channels=channels,
                    rate=rate,
                    input=True,
                    input_device_index=index,
                    frames_per_buffer=block,
                )

                resampler = None
                if self.variable_rate:
                    # 建構時的比率必須是之後會用到的最大值
                    resampler = soxr.ResampleStream(
                        rate * (1 + MAX_RATE_ADJUST),
                        self.target_rate,
                        2,
                        dtype="float32",
                        quality=self.quality,
                        vr=True,
                    )
                    resampler.set_io_ratio(rate, self.target_rate, 0)
                elif rate != self.target_rate:
                    resampler = soxr.ResampleStream(
                        rate, self.target_rate, 2, dtype="float32", quality=self.quality
                    )

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
                    "，soxr 可變速率（漂移補償）"
                    if self.variable_rate
                    else (f"，soxr {self.quality}" if resampler else "，不需重取樣"),
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
                    raw = stream.read(block, exception_on_overflow=False)
                    self._process(raw, fmt, channels, resampler, rate)
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

    def _process(
        self, raw: bytes, fmt: int, channels: int, resampler, rate: int
    ) -> None:
        if fmt == pa.paFloat32:
            x = np.frombuffer(raw, dtype=np.float32)
        else:
            x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if x.size == 0:
            return
        x = x.reshape(-1, channels)
        if channels == 1:
            x = np.repeat(x, 2, axis=1)
        elif channels > 2:
            x = x[:, :2]  # 只取前左/前右
        x = np.ascontiguousarray(x, dtype=np.float32)

        if resampler is not None:
            if self.variable_rate and self.rate_adjust is not None:
                adj = max(-MAX_RATE_ADJUST, min(MAX_RATE_ADJUST, self.rate_adjust()))
                resampler.set_io_ratio(rate * (1.0 + adj), self.target_rate, x.shape[0])
            x = resampler.resample_chunk(x)
            if x.size == 0:
                return

        if self.dither:
            # TPDF dither：兩個均勻分布相減，幅度 ±1 LSB
            noise = self._rng.random(x.shape, dtype=np.float32) - self._rng.random(
                x.shape, dtype=np.float32
            )
            x = x + noise * (1.0 / 32768.0)

        pcm = np.clip(np.rint(x * 32767.0), -32768, 32767).astype("<i2")
        self.status.frames_captured += pcm.shape[0]
        self.sink(pcm.tobytes())
