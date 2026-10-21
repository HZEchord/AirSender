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
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from airsender.config import Config  # noqa: E402
from airsender.latencycal import measure  # noqa: E402
from airsender.logging_setup import setup_logging  # noqa: E402
from airsender.streamer import AirPlayStreamer, State  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default="", help="接收端名稱（部分比對），預設第一台 HomePod")
    ap.add_argument("--latency", type=int, default=250, help="接收端緩衝 ms")
    ap.add_argument("--jitter", type=int, default=40, help="本機抖動緩衝 ms")
    ap.add_argument(
        "--latency-min", type=int, default=250, help="AirPlay 2 SETUP latencyMin ms（pyatv 預設 250）"
    )
    ap.add_argument("--volume", type=int, default=15, help="測試時 HomePod 音量 %%（預設 15，請勿太大）")
    ap.add_argument("--amp", type=float, default=0.3, help="測試音振幅（0–1，預設 0.3）")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--mic", type=int, default=None, help="麥克風裝置索引（預設系統預設輸入）")
    ap.add_argument("--settle", type=float, default=4.0, help="串流開始後等待秒數")
    ap.add_argument(
        "--mode",
        choices=["game", "music"],
        default="game",
        help="game = realtime；music = buffered（ALAC 24-bit + PTP）",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging("DEBUG" if args.verbose else "WARNING", console=True)

    cfg = Config()
    cfg.receiver_latency_ms = args.latency
    cfg.jitter_buffer_ms = args.jitter
    cfg.airplay_latency_min_ms = args.latency_min
    cfg.volume = args.volume
    cfg.mode = args.mode

    streamer = AirPlayStreamer(cfg, lambda s, m: print(f"  [{s.value}] {m}", flush=True), persist=False)
    streamer.start_loop()
    try:
        print("掃描接收端…")
        devices = streamer.scan(5)
        if not devices:
            print("找不到接收端")
            return 1
        key = args.device.lower()
        target = next((d for d in devices if key and (key in d.name.lower() or key == d.address)), None)
        if target is None:
            target = next(
                (d for d in devices if "HomePod" in d.model or "AudioAccessory" in d.model), devices[0]
            )
        print(
            f"目標：{target.label}，接收端緩衝 {args.latency} ms，latencyMin {args.latency_min} ms，HomePod 音量 {args.volume}%"
        )

        streamer.start(target)
        deadline = time.monotonic() + 20
        while streamer.state != State.STREAMING and time.monotonic() < deadline:
            time.sleep(0.2)
        if streamer.state != State.STREAMING:
            print("串流未能開始")
            return 1
        print(f"串流中，等待 {args.settle:.0f} 秒穩定…")
        time.sleep(args.settle)

        result = measure(
            repeats=args.repeats,
            amp=args.amp,
            mic_index=args.mic,
            progress=lambda m, _f: print(f"  {m}", flush=True),
        )

        src = streamer.source
        if src:
            print(
                f"串流統計：本機緩衝 {src.buffered_ms:.0f} ms，漂移補償 {src.drift_ppm:+.0f} ppm，underrun {src.underruns}，trim {src.trims}"
            )
        print(f"麥克風：{result.mic_name}；輸出：{result.output_name}")
        print("\n每次量測（相對於送出時間的峰值；括號內為相對強度 / 訊噪比）：")
        for i, found in enumerate(result.per_emit):
            desc = [f"{d_ms:7.1f} ms ({strength:.2f} / {snr:.0f}x)" for d_ms, strength, snr in found]
            print(
                f"  #{i + 1}: " + ", ".join(desc)
                if desc
                else f"  #{i + 1}: 沒有偵測到峰值（雜音太大或音量太低）"
            )
        print()
        if result.consistent:
            print(f"HomePod 端對端延遲：中位數 {result.summary()}")
            print(
                f"  組成估計：接收端緩衝 / 提前量 {args.latency} ms + 擷取 / 混音 + 網路 + HomePod 內部處理與聲音傳播"
            )
        elif result.rejected:
            print("偵測到的峰值彼此不一致，可能被環境雜音干擾。請增加 --repeats 或降低環境噪音後重試。")
        else:
            print("沒有偵測到來自 HomePod 的峰值。請提高 --volume 或 --amp，或把麥克風靠近 HomePod。")
        return 0
    finally:
        streamer.shutdown()


if __name__ == "__main__":
    sys.exit(main())
