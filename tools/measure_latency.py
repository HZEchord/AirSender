"""端對端延遲量測：電腦喇叭輸出 → AirSender → HomePod → 電腦麥克風。

做法：串流開始並穩定後，以低音量送出數個 30 ms 的掃頻音（1–6 kHz chirp），
同時用麥克風錄音，對錄音做交叉相關找出掃頻音抵達的時間，即為總延遲
（包含 Windows 混音、擷取、網路、HomePod 接收端緩衝與喇叭、聲音在空氣中傳播、麥克風）。

用法：
  python tools/measure_latency.py --device HomePod --latency 250 --volume 15

注意：麥克風也會錄到電腦本身喇叭的聲音（若有接喇叭），會出現一個較早的峰值；
HomePod 的峰值會在 100 ms 之後。結果會列出所有明顯峰值。
"""

from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pyaudiowpatch as pa
import soxr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from airsender.config import Config  # noqa: E402
from airsender.logging_setup import setup_logging  # noqa: E402
from airsender.streamer import AirPlayStreamer, State  # noqa: E402

CHIRP_F0 = 1000.0
CHIRP_F1 = 6000.0


def make_chirp(rate: int, duration: float = 0.05, f0: float = CHIRP_F0, f1: float = CHIRP_F1, amp: float = 0.3):
    """線性掃頻音。50 ms、1–6 kHz：匹配濾波後的處理增益約 24 dB，對環境雜音有抵抗力。"""
    n = int(rate * duration)
    t = np.arange(n) / rate
    k = (f1 - f0) / duration
    phase = 2 * np.pi * (f0 * t + 0.5 * k * t * t)
    env = np.hanning(n)
    return (amp * env * np.sin(phase)).astype(np.float32)


def bandpass(x: np.ndarray, rate: int, lo: float = CHIRP_F0 * 0.9, hi: float = CHIRP_F1 * 1.1) -> np.ndarray:
    """FFT 帶通：濾掉人聲、空調、交通等低頻與高頻雜音，只留掃頻音所在頻帶。"""
    X = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1.0 / rate)
    X[(freqs < lo) | (freqs > hi)] = 0
    return np.fft.irfft(X, len(x)).astype(np.float32)


def consistent_cluster(values: list[float], tolerance_ms: float = 15.0) -> list[float]:
    """找出最大一群彼此相差在 tolerance 內的量測值（排除被雜音誤判的離群值）。"""
    best: list[float] = []
    for center in values:
        group = [v for v in values if abs(v - center) <= tolerance_ms]
        if len(group) > len(best):
            best = group
    return best


class MicRecorder:
    def __init__(self, p: pa.PyAudio, device_index: int | None):
        info = p.get_device_info_by_index(device_index) if device_index is not None else p.get_default_input_device_info()
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
        self.t0: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
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


def matched_filter(rec: np.ndarray, template: np.ndarray) -> np.ndarray:
    n = int(2 ** np.ceil(np.log2(len(rec) + len(template))))
    R = np.fft.rfft(rec, n)
    T = np.fft.rfft(template, n)
    return np.abs(np.fft.irfft(R * np.conj(T), n)[: len(rec)])


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default="", help="接收端名稱（部分比對），預設第一台 HomePod")
    ap.add_argument("--latency", type=int, default=250, help="接收端緩衝 ms")
    ap.add_argument("--jitter", type=int, default=40, help="本機抖動緩衝 ms")
    ap.add_argument("--volume", type=int, default=15, help="測試時 HomePod 音量 %%（預設 15，請勿太大）")
    ap.add_argument("--amp", type=float, default=0.3, help="測試音振幅（0–1，預設 0.3）")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--mic", type=int, default=None, help="麥克風裝置索引（預設系統預設輸入）")
    ap.add_argument("--settle", type=float, default=4.0, help="串流開始後等待秒數")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging("DEBUG" if args.verbose else "WARNING", console=True)

    cfg = Config()
    cfg.receiver_latency_ms = args.latency
    cfg.jitter_buffer_ms = args.jitter
    cfg.volume = args.volume

    streamer = AirPlayStreamer(cfg, lambda s, m: print(f"  [{s.value}] {m}", flush=True))
    streamer.start_loop()
    p = pa.PyAudio()
    try:
        print("掃描接收端…")
        devices = streamer.scan(5)
        if not devices:
            print("找不到接收端")
            return 1
        key = args.device.lower()
        target = next((d for d in devices if key and (key in d.name.lower() or key == d.address)), None)
        if target is None:
            target = next((d for d in devices if "HomePod" in d.model or "AudioAccessory" in d.model), devices[0])
        print(f"目標：{target.label}，接收端緩衝 {args.latency} ms，HomePod 音量 {args.volume}%")

        streamer.start(target)
        deadline = time.monotonic() + 20
        while streamer.state != State.STREAMING and time.monotonic() < deadline:
            time.sleep(0.2)
        if streamer.state != State.STREAMING:
            print("串流未能開始")
            return 1
        print(f"串流中，等待 {args.settle:.0f} 秒穩定…")
        time.sleep(args.settle)

        out_info = p.get_default_wasapi_device(d_out=True)
        out_rate = int(out_info["defaultSampleRate"])
        out = p.open(format=pa.paFloat32, channels=2, rate=out_rate, output=True, output_device_index=int(out_info["index"]))
        out_latency = float(out.get_output_latency())
        chirp = make_chirp(out_rate, amp=args.amp)
        stereo = np.column_stack([chirp, chirp]).astype(np.float32).tobytes()
        # 先送 200 ms 靜音讓輸出串流進入穩定狀態
        out.write(np.zeros((int(out_rate * 0.2), 2), dtype=np.float32).tobytes())

        mic = MicRecorder(p, args.mic)
        mic.start()
        time.sleep(0.5)
        print(f"麥克風：{mic.name} @ {mic.rate} Hz；輸出：{out_info['name']} @ {out_rate} Hz")
        template = soxr.resample(chirp, out_rate, mic.rate).astype(np.float32)

        emit_times = []
        for i in range(args.repeats):
            t_emit = time.perf_counter() + out_latency
            out.write(stereo)
            emit_times.append(t_emit)
            print(f"  送出測試音 {i + 1}/{args.repeats}", flush=True)
            time.sleep(3.0)
        time.sleep(1.0)
        rec = mic.stop()
        out.stop_stream()
        out.close()

        src = streamer.source
        if src:
            print(f"串流統計：本機緩衝 {src.buffered_ms:.0f} ms，漂移補償 {src.drift_ppm:+.0f} ppm，underrun {src.underruns}，trim {src.trims}")

        if mic.t0 is None or rec.size == 0:
            print("麥克風沒有錄到資料")
            return 1

        env = matched_filter(bandpass(rec, mic.rate), template)

        candidates = []
        print("\n每次量測（相對於送出時間的峰值；括號內為相對強度 / 訊噪比）：")
        for i, t_emit in enumerate(emit_times):
            rel = t_emit - mic.t0
            peaks = xcorr_peaks(env, mic.rate, rel - 0.05, rel + 2.9)
            desc = []
            for t, strength, snr in peaks:
                d_ms = (t - rel) * 1000
                desc.append(f"{d_ms:7.1f} ms ({strength:.2f} / {snr:.0f}x)")
                if d_ms > 100:
                    candidates.append(d_ms)
            print(f"  #{i + 1}: " + ", ".join(desc) if desc else f"  #{i + 1}: 沒有偵測到峰值（雜音太大或音量太低）")

        print()
        results = consistent_cluster(candidates)
        rejected = len(candidates) - len(results)
        if len(results) >= 2 or (results and args.repeats == 1):
            med = statistics.median(results)
            print(
                f"HomePod 端對端延遲：中位數 {med:.0f} ms"
                f"（{min(results):.0f}–{max(results):.0f} ms，{len(results)}/{args.repeats} 次一致"
                + (f"，排除 {rejected} 個不一致峰值" if rejected else "")
                + "）"
            )
            print(f"  組成估計：接收端緩衝 {args.latency} ms + 本機抖動緩衝 {args.jitter} ms + 擷取/混音約 20–30 ms + 網路 + HomePod 內部處理與聲音傳播")
        elif candidates:
            print(f"偵測到的峰值彼此不一致（{candidates}），可能被環境雜音干擾。請增加 --repeats 或降低環境噪音後重試。")
        else:
            print("沒有偵測到來自 HomePod 的峰值。請提高 --volume 或 --amp，或把麥克風靠近 HomePod。")
        return 0
    finally:
        p.terminate()
        streamer.shutdown()


if __name__ == "__main__":
    sys.exit(main())
