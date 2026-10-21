"""房間校正用的 DSP：掃頻、反摺積、頻率相依視窗、平滑、最小相位 FIR、即時 FFT 摺積。

全部只用 numpy，不引入 scipy。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# ---------------------------------------------------------------- 掃頻訊號


def exponential_sweep(fs: int, seconds: float = 6.0, f1: float = 20.0, f2: float = 20000.0, amp: float = 0.3):
    """Farina 對數掃頻與其反濾波器。回傳 (sweep, inverse)，皆為 float32。"""
    n = int(fs * seconds)
    t = np.arange(n) / fs
    ratio = np.log(f2 / f1)
    sweep = np.sin(2 * np.pi * f1 * seconds / ratio * (np.exp(t * ratio / seconds) - 1.0))
    # 頭尾各 50 ms 淡入淡出，避免喀噠聲
    fade = int(0.05 * fs)
    env = np.ones(n)
    env[:fade] = np.linspace(0, 1, fade)
    env[-fade:] = np.linspace(1, 0, fade)
    sweep = (amp * env * sweep).astype(np.float32)
    # 反濾波器：時間反轉並以 -6 dB/oct 補償能量（讓結果脈衝響應頻譜平坦）
    inv = sweep[::-1] * np.exp(-t * ratio / seconds)
    inv = (inv / np.sum(sweep * inv[::-1])).astype(np.float32)  # 正規化使 sweep * inv 的峰值為 1
    return sweep, inv


def impulse_response(
    recording: np.ndarray, inverse: np.ndarray, fs: int, search_from: float = 0.0, search_to: float = 3.0
):
    """錄音與反濾波器摺積得到脈衝響應；回傳 (ir, peak_index)。

    search_from/search_to：在摺積結果中尋找直達聲峰值的時間範圍（秒，相對於掃頻開始）。"""
    n = len(recording) + len(inverse) - 1
    nfft = 1 << int(np.ceil(np.log2(n)))
    ir = np.fft.irfft(np.fft.rfft(recording, nfft) * np.fft.rfft(inverse, nfft), nfft)[:n]
    # 線性摺積的「零延遲」對應索引 len(inverse) - 1
    origin = len(inverse) - 1
    i0 = origin + int(search_from * fs)
    i1 = min(len(ir), origin + int(search_to * fs))
    peak = i0 + int(np.argmax(np.abs(ir[i0:i1])))
    return ir, peak


# ---------------------------------------------------------------- 頻率響應


def magnitude_response(
    ir: np.ndarray, peak: int, fs: int, nfft: int = 65536
) -> tuple[np.ndarray, np.ndarray]:
    """頻率相依視窗（FDW）的量值響應（dB）。

    低頻（< 200 Hz）用 500 ms 視窗把房間駐波算進去（這是低音感的來源）；
    高頻（> 2 kHz）用 8 ms 視窗只看直達聲，避免把反射與梳狀濾波「修」進 EQ；中間以對數頻率交叉淡入。
    """
    pre = int(0.005 * fs)
    start = max(0, peak - pre)

    def windowed(length_s: float) -> np.ndarray:
        length = int(length_s * fs)
        seg = np.zeros(nfft)
        chunk = ir[start : start + length]
        w = np.ones(len(chunk))
        half = len(chunk) // 2
        w[half:] = 0.5 * (1 + np.cos(np.linspace(0, np.pi, len(chunk) - half)))  # 後半 Hann 淡出
        seg[: len(chunk)] = chunk * w
        return 20 * np.log10(np.abs(np.fft.rfft(seg)) + 1e-12)

    freqs = np.fft.rfftfreq(nfft, 1.0 / fs)
    long_db = windowed(0.5)
    short_db = windowed(0.008)
    f_lo, f_hi = 200.0, 2000.0
    x = np.clip((np.log(np.maximum(freqs, 1.0)) - np.log(f_lo)) / (np.log(f_hi) - np.log(f_lo)), 0.0, 1.0)
    mix = 0.5 - 0.5 * np.cos(np.pi * x)
    return freqs, (1 - mix) * long_db + mix * short_db


def smooth_octave(
    freqs: np.ndarray, db: np.ndarray, fraction: float = 3.0, f_min: float = 20.0
) -> np.ndarray:
    """1/fraction 八度平滑（在功率域平均）。"""
    out = np.array(db, dtype=float)
    power = 10 ** (db / 10.0)
    half = 0.5 / fraction
    valid = freqs >= f_min
    idx = np.where(valid)[0]
    logf = np.log2(np.maximum(freqs, 1e-6))
    # 用累積和在對數頻率上做移動平均（以索引近似：先重取樣到對數網格）
    grid = np.linspace(logf[idx[0]], logf[idx[-1]], 2048)
    p_grid = np.interp(grid, logf[idx], power[idx])
    cum = np.concatenate([[0.0], np.cumsum(p_grid)])
    step = grid[1] - grid[0]
    k = max(1, int(round(half / step)))
    i = np.arange(len(grid))
    lo = np.clip(i - k, 0, len(grid))
    hi = np.clip(i + k + 1, 0, len(grid))
    sm_grid = (cum[hi] - cum[lo]) / (hi - lo)
    out[idx] = 10 * np.log10(np.interp(logf[idx], grid, sm_grid) + 1e-20)
    return out


# ---------------------------------------------------------------- 修正曲線與 FIR


@dataclass
class CorrectionLimits:
    f_low: float = 40.0  # 低於此頻率不修正（喇叭推不出來、麥克風也不準）
    f_high: float = 10000.0  # 高於此頻率不修正（位置敏感）
    max_boost_db: float = 6.0
    max_cut_db: float = 12.0


def bass_shelf_db(freqs: np.ndarray, gain_db: float, corner_hz: float = 100.0) -> np.ndarray:
    """低架：corner 以下 +gain，往上在約一個八度內平滑歸零。"""
    if gain_db == 0:
        return np.zeros_like(freqs)
    x = np.clip((np.log2(np.maximum(freqs, 1.0)) - np.log2(corner_hz)) / 1.0, 0.0, 1.0)
    return gain_db * (0.5 + 0.5 * np.cos(np.pi * x))


def correction_curve(
    freqs: np.ndarray,
    measured_db: np.ndarray | None,
    *,
    strength: float = 1.0,
    bass_boost_db: float = 0.0,
    limits: CorrectionLimits | None = None,
) -> np.ndarray:
    """由平滑後的量測響應算出修正曲線（dB）。measured_db 為 None 時只有低音增強。"""
    limits = limits or CorrectionLimits()
    corr = np.zeros_like(freqs)
    if measured_db is not None:
        ref_band = (freqs >= 200) & (freqs <= 2000)
        reference = float(np.mean(measured_db[ref_band]))
        corr = -(measured_db - reference) * strength
        corr = np.clip(corr, -limits.max_cut_db, limits.max_boost_db)
        # 範圍外以半個八度淡出
        lo = np.clip((np.log2(np.maximum(freqs, 1.0)) - np.log2(limits.f_low)) / 0.5, 0, 1)
        hi = np.clip((np.log2(limits.f_high) - np.log2(np.maximum(freqs, 1.0))) / 0.5, 0, 1)
        corr = corr * (0.5 - 0.5 * np.cos(np.pi * lo)) * (0.5 - 0.5 * np.cos(np.pi * hi))
    corr = corr + bass_shelf_db(freqs, bass_boost_db)
    return corr


def minimum_phase_fir(
    freqs: np.ndarray, gain_db: np.ndarray, fs: int, n_taps: int = 4096
) -> tuple[np.ndarray, float]:
    """由目標量值（dB）產生最小相位 FIR（倒頻譜法）。

    回傳 (taps, pre_gain_db)：taps 已乘上預先衰減，使濾波器最大增益為 0 dB（不會因 EQ 增益而削波）。"""
    nfft = 1 << int(np.ceil(np.log2(n_taps * 8)))
    target_f = np.fft.rfftfreq(nfft, 1.0 / fs)
    mag_db = np.interp(target_f, freqs, gain_db)
    pre_gain_db = -float(np.max(mag_db))
    mag = 10 ** ((mag_db + pre_gain_db) / 20.0)
    # 倒頻譜法：log|H| -> 實倒頻譜 -> 摺疊成最小相位
    log_mag = np.log(np.maximum(mag, 1e-6))
    cep = np.fft.irfft(log_mag, nfft)
    fold = np.zeros(nfft)
    fold[0] = cep[0]
    fold[1 : nfft // 2] = 2 * cep[1 : nfft // 2]
    fold[nfft // 2] = cep[nfft // 2]
    h_min = np.fft.irfft(np.exp(np.fft.rfft(fold, nfft)), nfft)
    taps = h_min[:n_taps]
    # 尾端淡出避免截斷漣波
    fade = n_taps // 8
    taps[-fade:] *= 0.5 * (1 + np.cos(np.linspace(0, np.pi, fade)))
    return taps.astype(np.float32), pre_gain_db


def fir_response_db(taps: np.ndarray, fs: int, freqs: np.ndarray) -> np.ndarray:
    nfft = 1 << int(np.ceil(np.log2(len(taps) * 8)))
    h = 20 * np.log10(np.abs(np.fft.rfft(taps, nfft)) + 1e-12)
    return np.interp(freqs, np.fft.rfftfreq(nfft, 1.0 / fs), h)


# ---------------------------------------------------------------- 即時處理


class FirProcessor:
    """重疊相加 FFT 摺積，支援長度不固定的區塊；不增加區塊延遲（只有 FIR 本身的最小相位群延遲）。"""

    def __init__(self, taps: np.ndarray, channels: int = 2, max_block: int = 2048) -> None:
        self.taps = np.asarray(taps, dtype=np.float32)
        self.channels = channels
        self.n_taps = len(self.taps)
        self.nfft = 1 << int(np.ceil(np.log2(max_block + self.n_taps - 1)))
        self._h = np.fft.rfft(self.taps, self.nfft)
        self._tail = np.zeros((self.n_taps - 1, channels), dtype=np.float64)

    def process(self, x: np.ndarray) -> np.ndarray:
        n = x.shape[0]
        if n == 0:
            return x
        if n + self.n_taps - 1 > self.nfft:
            # 區塊比預期大：分段處理
            return np.concatenate(
                [
                    self.process(x[i : i + self.nfft - self.n_taps + 1])
                    for i in range(0, n, self.nfft - self.n_taps + 1)
                ]
            )
        y = np.fft.irfft(np.fft.rfft(x, self.nfft, axis=0) * self._h[:, None], self.nfft, axis=0)[
            : n + self.n_taps - 1
        ]
        y[: self.n_taps - 1] += self._tail
        out = y[:n]
        # 更新尾巴：這一塊超出 n 的部分
        new_tail = np.zeros_like(self._tail)
        tail_part = y[n:]
        new_tail[: len(tail_part)] = tail_part
        self._tail = new_tail
        return out.astype(np.float32)
