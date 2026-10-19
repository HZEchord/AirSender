"""房間校正：量測流程、校正檔、即時 EQ 引擎。

流程（參考 REW / Sonos Trueplay 的作法，簡化成桌面場景）：
1. 串流中，從電腦播放對數掃頻 → AirSender → HomePod → 電腦麥克風錄音（麥克風放在聆聽位置）
2. 反摺積得脈衝響應，頻率相依視窗取量值響應，多次掃頻與多個位置以功率平均
3. 1/6 八度平滑後存成校正檔（%APPDATA%\\AirSender\\room_correction.json）
4. 串流時由 CorrectionEngine 依「量測響應 + 低音增強 + 強度 + 範圍限制」產生最小相位 FIR 即時套用
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from . import dsp
from .config import Config, config_dir

_LOGGER = logging.getLogger(__name__)

PROFILE_FS = 44100
GRID = np.geomspace(20.0, 20000.0, 240)  # 儲存用對數頻率網格（1/24 八度）


@dataclass
class CorrectionProfile:
    created: str
    positions: int
    sweeps: int
    device_name: str
    mic_name: str
    note: str = ""
    freqs: list[float] = field(default_factory=lambda: [float(f) for f in GRID])
    measured_db: list[float] = field(default_factory=list)  # 平滑後、以 200 Hz–2 kHz 平均為 0 dB
    snr_db: float = 0.0
    latency_ms: float = 0.0

    @staticmethod
    def path() -> Path:
        return config_dir() / "room_correction.json"

    def save(self) -> Path:
        p = self.path()
        p.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")
        return p

    @classmethod
    def load(cls) -> Optional[CorrectionProfile]:
        p = cls.path()
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except (OSError, ValueError, TypeError) as exc:
            _LOGGER.warning("無法讀取校正檔：%s", exc)
            return None

    def measured(self) -> tuple[np.ndarray, np.ndarray]:
        return np.asarray(self.freqs), np.asarray(self.measured_db)


def load_mic_calibration(path: str) -> Optional[Callable[[np.ndarray], np.ndarray]]:
    """讀取 REW 風格的麥克風校正檔（每行「頻率 dB」），回傳把響應減去麥克風偏差的函式。"""
    if not path:
        return None
    try:
        rows = []
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.replace(",", " ").split()
            if len(parts) >= 2:
                try:
                    rows.append((float(parts[0]), float(parts[1])))
                except ValueError:
                    continue
        if len(rows) < 2:
            return None
        rows.sort()
        f = np.array([r[0] for r in rows])
        d = np.array([r[1] for r in rows])
        return lambda freqs: np.interp(freqs, f, d)
    except OSError as exc:
        _LOGGER.warning("讀取麥克風校正檔失敗：%s", exc)
        return None


# ---------------------------------------------------------------- 即時引擎


class CorrectionEngine:
    """擷取鏈路呼叫 process()；設定改變時 rebuild()，不必重新連線。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[dsp.FirProcessor] = None
        self._fs = PROFILE_FS
        self.bypass = False  # 校正量測期間設為 True
        self.pre_gain_db = 0.0
        self.active = False
        self.profile: Optional[CorrectionProfile] = None

    def rebuild(self, cfg: Config, fs: int = PROFILE_FS) -> None:
        self.profile = CorrectionProfile.load() if cfg.room_correction_enabled else None
        measured = None
        if self.profile is not None and self.profile.measured_db:
            freqs, measured = self.profile.measured()
        else:
            freqs = GRID
        use = (measured is not None) or (cfg.bass_boost_db != 0)
        if not use:
            with self._lock:
                self._proc = None
                self.active = False
                self.pre_gain_db = 0.0
            return
        limits = dsp.CorrectionLimits(
            f_low=float(cfg.correction_f_low),
            f_high=float(cfg.correction_f_high),
            max_boost_db=float(cfg.correction_max_boost_db),
        )
        corr = dsp.correction_curve(
            np.asarray(freqs),
            measured,
            strength=cfg.correction_strength / 100.0,
            bass_boost_db=float(cfg.bass_boost_db),
            limits=limits,
        )
        taps, pre = dsp.minimum_phase_fir(np.asarray(freqs), corr, fs, n_taps=4096)
        with self._lock:
            self._proc = dsp.FirProcessor(taps, 2)
            self._fs = fs
            self.pre_gain_db = pre
            self.active = True
        _LOGGER.info("房間校正 EQ 已更新（預先衰減 %.1f dB，量測 %s，低音 %+.1f dB，強度 %d%%）",
                     pre, "有" if measured is not None else "無", cfg.bass_boost_db, cfg.correction_strength)

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.bypass:
            return x
        proc = self._proc
        if proc is None:
            return x
        with self._lock:
            return proc.process(x)

    def curves(self, cfg: Config) -> dict[str, np.ndarray]:
        """給面板畫圖：量測、修正、修正後預估（dB，對 GRID）。"""
        freqs = GRID
        measured = None
        if self.profile is not None and self.profile.measured_db:
            _, measured = self.profile.measured()
        corr = dsp.correction_curve(
            freqs, measured, strength=cfg.correction_strength / 100.0, bass_boost_db=float(cfg.bass_boost_db),
            limits=dsp.CorrectionLimits(float(cfg.correction_f_low), float(cfg.correction_f_high), float(cfg.correction_max_boost_db)),
        )
        out = {"freqs": freqs, "correction": corr}
        if measured is not None:
            out["measured"] = measured
            out["result"] = measured + corr
        return out


ENGINE = CorrectionEngine()


# ---------------------------------------------------------------- 量測流程

ProgressFn = Callable[[str, float], None]


class Calibrator:
    """在串流中播放掃頻並錄音。呼叫端需確保 Streamer 正在串流到 AirPlay 裝置。"""

    def __init__(self, cfg: Config, *, sweeps: int = 3, sweep_seconds: float = 6.0, amp: float = 0.3) -> None:
        self.cfg = cfg
        self.sweeps = sweeps
        self.sweep_seconds = sweep_seconds
        self.amp = amp
        self.cancel = threading.Event()

    def expected_latency_s(self) -> float:
        return (self.cfg.airplay_latency_min_ms + self.cfg.receiver_latency_ms + self.cfg.jitter_buffer_ms + 15) / 1000.0

    def measure_position(self, progress: ProgressFn, mic_index: Optional[int] = None) -> tuple[np.ndarray, float, float]:
        """量一個麥克風位置（self.sweeps 次掃頻平均）。回傳 (GRID 上的 dB, snr_db, latency_ms)。"""
        import pyaudiowpatch as pa  # pylint: disable=import-outside-toplevel

        p = pa.PyAudio()
        try:
            out_info = p.get_default_wasapi_device(d_out=True)
            out_rate = int(out_info["defaultSampleRate"])
            mic_info = p.get_device_info_by_index(mic_index) if mic_index is not None else p.get_default_input_device_info()
            mic_rate = int(mic_info["defaultSampleRate"])
            mic_ch = int(mic_info["maxInputChannels"])
            sweep_out, _ = dsp.exponential_sweep(out_rate, self.sweep_seconds, amp=self.amp)
            sweep_mic, inv_mic = dsp.exponential_sweep(mic_rate, self.sweep_seconds, amp=self.amp)
            out = p.open(format=pa.paFloat32, channels=2, rate=out_rate, output=True, output_device_index=int(out_info["index"]))
            mic = p.open(format=pa.paFloat32, channels=mic_ch, rate=mic_rate, input=True,
                         input_device_index=int(mic_info["index"]), frames_per_buffer=int(mic_rate * 0.02))
            powers = []
            snrs = []
            lats = []
            try:
                for k in range(self.sweeps):
                    if self.cancel.is_set():
                        raise RuntimeError("已取消")
                    progress(f"掃頻 {k + 1}/{self.sweeps}", (k) / self.sweeps)
                    chunks: list[np.ndarray] = []
                    stop = threading.Event()

                    def rec_loop(stop=stop, chunks=chunks):
                        while not stop.is_set():
                            d = mic.read(int(mic_rate * 0.02), exception_on_overflow=False)
                            chunks.append(np.frombuffer(d, dtype=np.float32).reshape(-1, mic_ch).mean(axis=1))

                    th = threading.Thread(target=rec_loop, daemon=True)
                    th.start()
                    time.sleep(0.3)
                    stereo = np.column_stack([sweep_out, sweep_out]).astype(np.float32)
                    out.write(np.zeros((int(out_rate * 0.2), 2), dtype=np.float32).tobytes())
                    out.write(stereo.tobytes())
                    # 等聲音經過延遲與殘響抵達
                    time.sleep(self.expected_latency_s() + 1.2)
                    stop.set()
                    th.join(2)
                    rec = np.concatenate(chunks).astype(np.float64)
                    ir, peak = dsp.impulse_response(rec, inv_mic, mic_rate, search_from=0.0, search_to=self.sweep_seconds + 2.0)
                    origin = len(inv_mic) - 1
                    lat_ms = (peak - origin) / mic_rate * 1000.0
                    # 訊噪比：峰值附近 vs 峰值前 200 ms 的雜訊
                    noise_seg = ir[max(0, peak - int(0.3 * mic_rate)) : max(1, peak - int(0.05 * mic_rate))]
                    snr = 20 * np.log10(abs(ir[peak]) / (np.sqrt(np.mean(noise_seg**2)) + 1e-12))
                    freqs, mag = dsp.magnitude_response(ir, peak, mic_rate)
                    grid_db = np.interp(GRID, freqs, mag)
                    powers.append(10 ** (grid_db / 10.0))
                    snrs.append(snr)
                    lats.append(lat_ms)
                    _LOGGER.info("掃頻 %d：延遲 %.0f ms，SNR %.1f dB", k + 1, lat_ms, snr)
            finally:
                out.stop_stream()
                out.close()
                mic.stop_stream()
                mic.close()
            avg_db = 10 * np.log10(np.mean(powers, axis=0) + 1e-20)
            return avg_db, float(np.mean(snrs)), float(np.median(lats))
        finally:
            p.terminate()

    @staticmethod
    def finalize(position_dbs: list[np.ndarray], *, mic_cal: Optional[Callable[[np.ndarray], np.ndarray]] = None) -> np.ndarray:
        """多位置功率平均 → 麥克風校正 → 1/6 八度平滑 → 以 200 Hz–2 kHz 平均為 0 dB。"""
        avg = 10 * np.log10(np.mean([10 ** (d / 10.0) for d in position_dbs], axis=0) + 1e-20)
        if mic_cal is not None:
            avg = avg - mic_cal(GRID)
        sm = dsp.smooth_octave(GRID, avg, fraction=6, f_min=20.0)
        ref = (GRID >= 200) & (GRID <= 2000)
        return sm - float(np.mean(sm[ref]))
