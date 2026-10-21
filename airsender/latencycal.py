"""端對端延遲量測（電腦喇叭輸出 → AirSender → 接收端 → 電腦麥克風）。

做法：串流中以低音量送出數個 50 ms 的線性掃頻音（1–6 kHz），同時用麥克風錄音，
對錄音做匹配濾波找出掃頻音抵達的時間，即為總延遲（含 Windows 混音、擷取、網路、
接收端緩衝與喇叭、空氣傳播、麥克風）。控制面板「校正延遲」與 tools/measure_latency.py 共用這裡的程式。

注意：麥克風也會錄到電腦本身喇叭的聲音（若有接喇叭），會出現一個較早的峰值；
接收端的峰值在 100 ms 之後，所以只採計 > 100 ms 的峰值。
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

_LOGGER = logging.getLogger(__name__)

CHIRP_F0 = 1000.0
CHIRP_F1 = 6000.0
MIN_RECEIVER_MS = 100.0  # 比這早的峰值視為電腦喇叭直達聲

ProgressFn = Callable[[str, float], None]


def make_chirp(
    rate: int, duration: float = 0.05, f0: float = CHIRP_F0, f1: float = CHIRP_F1, amp: float = 0.3
) -> np.ndarray:
    """線性掃頻音。50 ms、1–6 kHz：匹配濾波後的處理增益約 24 dB，對環境雜音有抵抗力。"""
    n = int(rate * duration)
    t = np.arange(n) / rate
    k = (f1 - f0) / duration
    phase = 2 * np.pi * (f0 * t + 0.5 * k * t * t)
    env = np.hanning(n)
    return (amp * env * np.sin(phase)).astype(np.float32)


def bandpass(x: np.ndarray, rate: int, lo: float = CHIRP_F0 * 0.9, hi: float = CHIRP_F1 * 1.1) -> np.ndarray:
    """FFT 帶通：濾掉人聲、空調、交通等低頻與高頻雜音，只留掃頻音所在頻帶。"""
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1.0 / rate)
    spec[(freqs < lo) | (freqs > hi)] = 0
    return np.fft.irfft(spec, len(x)).astype(np.float32)


def consistent_cluster(values: list[float], tolerance_ms: float = 15.0) -> list[float]:
    """找出最大一群彼此相差在 tolerance 內的量測值（排除被雜音誤判的離群值）。"""
    best: list[float] = []
    for center in values:
        group = [v for v in values if abs(v - center) <= tolerance_ms]
        if len(group) > len(best):
            best = group
    return best


def matched_filter(rec: np.ndarray, template: np.ndarray) -> np.ndarray:
    n = int(2 ** np.ceil(np.log2(len(rec) + len(template))))
    spec_r = np.fft.rfft(rec, n)
    spec_t = np.fft.rfft(template, n)
    return np.abs(np.fft.irfft(spec_r * np.conj(spec_t), n)[: len(rec)])


def xcorr_peaks(env: np.ndarray, rate: int, t_start: float, t_end: float, min_snr: float = 10.0):
    """回傳 (時間秒, 相對強度, 訊噪比) 的峰值清單，只看 [t_start, t_end] 區間。

    訊噪比 = 峰值 / 區間內匹配濾波輸出的中位數（即雜音底）。低於 min_snr 的視為雜音。
    """
    i0, i1 = max(0, int(t_start * rate)), min(len(env), int(t_end * rate))
    seg = env[i0:i1]
    if seg.size == 0:
        return []
    noise = float(np.median(seg)) + 1e-12
    peak = float(seg.max())
    if peak / noise < min_snr:
        return []
    thr = max(0.35 * peak, min_snr * noise)
    peaks = []
    guard = int(0.05 * rate)
    idx = np.where(seg >= thr)[0]
    while idx.size:
        i = idx[0]
        window = seg[i : i + guard]
        j = i + int(np.argmax(window))
        peaks.append(((i0 + j) / rate, float(seg[j] / peak), float(seg[j] / noise)))
        idx = idx[idx > j + guard]
    return peaks


class MicRecorder:
    """背景執行緒錄麥克風，記下第一個樣本的 perf_counter 時刻。"""

    def __init__(self, p, device_index: Optional[int]):
        import pyaudiowpatch as pa  # pylint: disable=import-outside-toplevel

        info = (
            p.get_device_info_by_index(device_index)
            if device_index is not None
            else p.get_default_input_device_info()
        )
        self.rate = int(info["defaultSampleRate"])
        self.channels = int(info["maxInputChannels"])
        self.name = str(info["name"])
        self.block = int(self.rate * 0.02)
        self.stream = p.open(
            format=pa.paFloat32,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=int(info["index"]),
            frames_per_buffer=self.block,
        )
        self.input_latency = float(self.stream.get_input_latency())
        self.chunks: list[np.ndarray] = []
        self.t0: Optional[float] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        first = True
        while not self._stop.is_set():
            data = self.stream.read(self.block, exception_on_overflow=False)
            if first:
                # 第一個區塊的第一個樣本大約在「現在 - 區塊長度 - 輸入延遲」
                self.t0 = time.perf_counter() - self.block / self.rate - self.input_latency
                first = False
            x = np.frombuffer(data, dtype=np.float32).reshape(-1, self.channels).mean(axis=1)
            self.chunks.append(x)

    def stop(self) -> np.ndarray:
        self._stop.set()
        self._thread.join(2)
        self.stream.stop_stream()
        self.stream.close()
        return np.concatenate(self.chunks) if self.chunks else np.zeros(0, dtype=np.float32)


@dataclass
class LatencyResult:
    median_ms: float
    values_ms: list[float]
    rejected: int
    repeats: int
    mic_name: str = ""
    output_name: str = ""
    per_emit: list[list[tuple[float, float, float]]] = field(
        default_factory=list
    )  # 每次送出偵測到的 (ms, 強度, 訊噪比)

    @property
    def consistent(self) -> bool:
        return len(self.values_ms) >= 2 or (len(self.values_ms) == 1 and self.repeats == 1)

    def summary(self) -> str:
        if not self.consistent:
            return "沒有一致的峰值"
        lo, hi = min(self.values_ms), max(self.values_ms)
        text = f"{self.median_ms:.0f} ms（{lo:.0f}–{hi:.0f}，{len(self.values_ms)}/{self.repeats} 次一致"
        if self.rejected:
            text += f"，排除 {self.rejected} 個"
        return text + "）"


def measure(
    *,
    repeats: int = 5,
    amp: float = 0.3,
    interval_s: float = 3.0,
    mic_index: Optional[int] = None,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[threading.Event] = None,
) -> LatencyResult:
    """在串流進行中量測。呼叫端需確保串流已穩定（建議開始後等 3–4 秒）。"""
    import pyaudiowpatch as pa  # pylint: disable=import-outside-toplevel
    import soxr  # pylint: disable=import-outside-toplevel

    p = pa.PyAudio()
    try:
        out_info = p.get_default_wasapi_device(d_out=True)
        out_rate = int(out_info["defaultSampleRate"])
        out = p.open(
            format=pa.paFloat32,
            channels=2,
            rate=out_rate,
            output=True,
            output_device_index=int(out_info["index"]),
        )
        out_latency = float(out.get_output_latency())
        chirp = make_chirp(out_rate, amp=amp)
        stereo = np.column_stack([chirp, chirp]).astype(np.float32).tobytes()
        # 先送 200 ms 靜音讓輸出串流進入穩定狀態
        out.write(np.zeros((int(out_rate * 0.2), 2), dtype=np.float32).tobytes())

        mic = MicRecorder(p, mic_index)
        mic.start()
        time.sleep(0.5)
        template = soxr.resample(chirp, out_rate, mic.rate).astype(np.float32)

        emit_times: list[float] = []
        try:
            for i in range(repeats):
                if cancel is not None and cancel.is_set():
                    raise RuntimeError("已取消")
                if progress:
                    progress(f"測試音 {i + 1}/{repeats}", i / repeats)
                t_emit = time.perf_counter() + out_latency
                out.write(stereo)
                emit_times.append(t_emit)
                time.sleep(interval_s)
            time.sleep(1.0)
        finally:
            rec = mic.stop()
            out.stop_stream()
            out.close()

        if mic.t0 is None or rec.size == 0:
            raise RuntimeError("麥克風沒有錄到資料")
        if progress:
            progress("分析中", 0.95)
        env = matched_filter(bandpass(rec, mic.rate), template)
        candidates: list[float] = []
        per_emit: list[list[tuple[float, float, float]]] = []
        for t_emit in emit_times:
            rel = t_emit - mic.t0
            peaks = xcorr_peaks(env, mic.rate, rel - 0.05, rel + interval_s - 0.1)
            found = []
            for t, strength, snr in peaks:
                d_ms = float((t - rel) * 1000)  # 轉成 Python float，存進設定檔時才能序列化
                found.append((d_ms, float(strength), float(snr)))
                if d_ms > MIN_RECEIVER_MS:
                    candidates.append(d_ms)
            per_emit.append(found)
        results = consistent_cluster(candidates)
        median = float(statistics.median(results)) if results else float("nan")
        result = LatencyResult(
            median_ms=median,
            values_ms=results,
            rejected=len(candidates) - len(results),
            repeats=repeats,
            mic_name=mic.name,
            output_name=str(out_info["name"]),
            per_emit=per_emit,
        )
        _LOGGER.info("延遲量測：%s（麥克風 %s）", result.summary(), mic.name)
        return result
    finally:
        p.terminate()
