"""把即時擷取的系統音訊包裝成 pyatv 的 AudioSource。

pyatv 以固定節奏（每 352 frame 一個封包）呼叫 readframes()。這裡維持一個很小的抖動緩衝：
- 第一次讀取前等到緩衝累積 jitter_ms 的資料（上限 1 秒）
- 緩衝不足時補靜音（underrun）
- 緩衝超過高水位時丟掉多餘資料（時鐘漂移補償），讓本機延遲維持在 jitter_ms 左右
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Optional

from pyatv.exceptions import NotSupportedError
from pyatv.interface import MediaMetadata
from pyatv.protocols.raop.audio_source import AudioSource, _to_audio_samples

from .capture import WasapiLoopbackCapture
from .ringbuffer import ByteRingBuffer
from .roomcal import ENGINE

_LOGGER = logging.getLogger(__name__)


class DriftController:
    """PI 控制器：依緩衝量與目標的差距，輸出重取樣比率的微調值。

    系統模型：緩衝變化率 = -(adj - drift) * 1000 ms/s。
    取自然頻率 ω≈0.016 rad/s（時間常數約 1 分鐘）、接近臨界阻尼，得 kp=3e-5、ki=2.5e-7。
    量測值先經過 0.5 秒的一階低通，濾掉封包節奏造成的 ±5 ms 抖動。
    輸出限制在 ±max_adj（預設 0.1%，約 1.7 音分，聽不出音高差異）。
    """

    def __init__(
        self,
        target_ms: float,
        *,
        kp: float = 3e-5,
        ki: float = 2.5e-7,
        max_adj: float = 1e-3,
        filter_tau: float = 0.5,
    ) -> None:
        self.target_ms = target_ms
        self.kp = kp
        self.ki = ki
        self.max_adj = max_adj
        self.filter_tau = filter_tau
        self._integral = 0.0
        self._filtered: Optional[float] = None
        self._last_time: Optional[float] = None
        self.adjustment = 0.0

    @property
    def filtered_ms(self) -> float:
        return self._filtered if self._filtered is not None else self.target_ms

    def update(self, buffered_ms: float, now: float) -> float:
        if self._last_time is None or self._filtered is None:
            self._last_time = now
            self._filtered = buffered_ms
            return self.adjustment
        dt = max(0.0, min(now - self._last_time, 1.0))
        self._last_time = now
        alpha = dt / (self.filter_tau + dt)
        self._filtered += alpha * (buffered_ms - self._filtered)
        error = self._filtered - self.target_ms
        self._integral += self.ki * error * dt
        self._integral = max(-self.max_adj, min(self.max_adj, self._integral))
        self.adjustment = max(
            -self.max_adj, min(self.max_adj, self.kp * error + self._integral)
        )
        return self.adjustment

    def reset(self) -> None:
        self._integral = 0.0
        self._filtered = None
        self._last_time = None
        self.adjustment = 0.0

    def reprime(self) -> None:
        """資料中斷後重新開始：保留已學到的漂移量（積分項），重置濾波器與時間基準。"""
        self._filtered = None
        self._last_time = None
        self.adjustment = self._integral


class LiveAudioSource(AudioSource):
    def __init__(
        self,
        *,
        jitter_ms: int = 40,
        capture_device_index: Optional[int] = None,
        quality: str = "VHQ",
        dither: bool = True,
        drift_compensation: str = "smooth",
        metadata: Optional[MediaMetadata] = None,
        on_first_read: Optional[Callable[[], None]] = None,
        bit_depth: int = 16,
    ) -> None:
        self._jitter_ms = max(5, jitter_ms)
        self.bit_depth = bit_depth
        self._capture_device_index = capture_device_index
        self._quality = quality
        self._dither = dither
        self._smooth = drift_compensation == "smooth"
        self.drift = DriftController(float(self._jitter_ms))
        self._metadata = metadata or MediaMetadata()
        self._on_first_read = on_first_read

        self._sample_rate = 44100
        self._channels = 2
        self._sample_size = 2
        self._frame_size = 4
        self._jitter_bytes = 0
        self._high_water = 0
        self._ring = ByteRingBuffer(1)
        self.capture: Optional[WasapiLoopbackCapture] = None

        self._started = False
        self._primed = False
        self._stopped = False
        self.underruns = 0
        self.trims = 0
        self.reprimes = 0

    # ------------------------------------------------------------ 由 patches 呼叫
    def configure(self, sample_rate: int, channels: int, sample_size: int) -> None:
        """接收端格式確定後（RTSP 完成 SETUP）由 open_source 補丁呼叫。"""
        expected = 2 if self.bit_depth == 16 else 4  # 24-bit 以 int32 容器承載
        if sample_size != expected:
            raise NotSupportedError(f"位元深度不符：來源 {self.bit_depth}-bit，接收端要求 {sample_size * 8}-bit 容器")
        if channels != 2:
            raise NotSupportedError(f"只支援雙聲道接收端（收到 {channels} 聲道）")
        self._sample_rate = sample_rate
        self._channels = channels
        self._sample_size = sample_size
        self._frame_size = channels * sample_size

        bytes_per_ms = sample_rate * self._frame_size / 1000.0
        self._jitter_bytes = self._align(int(self._jitter_ms * bytes_per_ms))
        # 高水位 = 抖動緩衝的 3 倍再加 60 ms，超過就裁掉（時鐘漂移）
        self._high_water = self._align(int(self._jitter_bytes * 3 + 60 * bytes_per_ms))
        # 環形緩衝硬上限 1 秒，絕不讓延遲無限累積
        self._ring = ByteRingBuffer(self._align(int(1000 * bytes_per_ms)))

        if self.capture:
            self.capture.stop()
        self.capture = WasapiLoopbackCapture(
            self._ring.push,
            target_rate=sample_rate,
            target_channels=channels,
            device_index=self._capture_device_index,
            quality=self._quality,
            dither=self._dither,
            variable_rate=self._smooth,
            rate_adjust=lambda: self.drift.adjustment,
            processor=ENGINE.process,
            bit_depth=self.bit_depth,
        )
        self.drift.reset()
        self.capture.start()
        self._started = False

    async def wait_ready(self, timeout: float = 3.0) -> None:
        """等擷取真的開始出資料且累積到抖動緩衝量（在 pyatv 開始計時之前呼叫）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self._ring) < self._jitter_bytes and not self._stopped:
            if loop.time() >= deadline:
                status = self.capture.status if self.capture else None
                detail = f"：{status.last_error}" if status and status.last_error else ""
                _LOGGER.warning("等待音訊擷取逾時%s，仍繼續串流（會先送靜音）", detail)
                return
            await asyncio.sleep(0.01)

    # ------------------------------------------------------------ buffered 模式用（不經 pyatv）
    @property
    def frame_size(self) -> int:
        return self._frame_size

    def trim_to_ms(self, keep_ms: float) -> None:
        """丟掉多餘的舊資料，只留 keep_ms（串流開始前握手期間累積的聲音不需要送）。"""
        keep = self._align(int(keep_ms * self._sample_rate * self._frame_size / 1000.0))
        excess = len(self._ring) - keep
        if excess > 0:
            self._ring.pop(self._align(excess))

    def mark_started(self) -> None:
        self._started = True
        self._primed = True
        if self._on_first_read:
            try:
                self._on_first_read()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("on_first_read 回呼失敗")

    def pop_frames(self, nframes: int) -> Optional[bytes]:
        """有足夠資料才回傳（原生位元組序的整數樣本），否則 None；不補靜音、不等待。"""
        need = nframes * self._frame_size
        if len(self._ring) < need:
            return None
        return self._ring.pop(need)

    def _align(self, nbytes: int) -> int:
        return nbytes - (nbytes % self._frame_size)

    # ------------------------------------------------------------ AudioSource
    async def readframes(self, nframes: int) -> bytes:
        if self._stopped:
            return b""
        need = nframes * self._frame_size

        if not self._started:
            # pyatv 的節奏計時在呼叫這裡之前就已開始，所以不能在這裡等待；
            # 預填已在 wait_ready() 完成。RTSP 握手期間多累積的舊資料直接丟掉，
            # 讓緩衝精準落在目標值（這些是串流開始前的聲音，丟掉不會有可聽的接縫）。
            excess = len(self._ring) - self._jitter_bytes
            if excess > 0:
                self._ring.pop(self._align(excess))
            self._started = True
            self._primed = len(self._ring) >= self._jitter_bytes
            _LOGGER.info(
                "開始送出音訊（抖動緩衝 %d ms，起始 %.0f ms）",
                self._jitter_ms,
                self.buffered_ms,
            )
            if self._on_first_read:
                try:
                    self._on_first_read()
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("on_first_read 回呼失敗")

        available = len(self._ring)

        # Windows 沒有任何程式在播音時，loopback 不會送資料；資料中斷後重新出現時，
        # 先讓緩衝重新累積到目標量再開始送（期間送靜音，原本就是靜音所以聽不出來），
        # 否則之後會一直以接近 0 的緩衝運作而容易斷音。
        if not self._primed:
            if available < self._jitter_bytes:
                return bytes(need)
            self._primed = True
            self.reprimes += 1
            self.drift.reprime()
            _LOGGER.debug("緩衝重新預填完成（第 %d 次）", self.reprimes)

        if self._smooth:
            self.drift.update(
                available * 1000.0 / (self._sample_rate * self._frame_size),
                asyncio.get_running_loop().time(),
            )
        if available > self._high_water:
            drop = self._align(available - self._jitter_bytes)
            self._ring.pop(drop)
            self.trims += 1
            _LOGGER.debug("緩衝過高（%d bytes），丟棄 %d bytes", available, drop)

        data = self._ring.pop(need)
        if len(data) < need:
            self.underruns += 1
            self._primed = False
            if self.underruns in (1, 10, 100) or self.underruns % 1000 == 0:
                _LOGGER.debug("緩衝不足（第 %d 次），補靜音並等待重新預填", self.underruns)
            data = data + bytes(need - len(data))
        return _to_audio_samples(data)

    async def get_metadata(self) -> MediaMetadata:
        return self._metadata

    async def close(self) -> None:
        self.stop()

    def stop(self) -> None:
        self._stopped = True
        if self.capture:
            self.capture.stop()
            self.capture = None

    # ------------------------------------------------------------ 屬性
    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def sample_size(self) -> int:
        return self._sample_size

    @property
    def duration(self) -> int:
        return 0

    @property
    def drift_ppm(self) -> float:
        """目前的重取樣比率微調量（百萬分之一）。"""
        return self.drift.adjustment * 1e6

    @property
    def buffered_ms(self) -> float:
        if self._sample_rate == 0:
            return 0.0
        return len(self._ring) * 1000.0 / (self._sample_rate * self._frame_size)
