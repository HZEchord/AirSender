"""命令列房間校正：串流到 AirPlay 接收端，播放掃頻、用麥克風量測，存成校正檔。

用法：
  python tools/room_calibrate.py --device 臥室 --positions 1 --sweeps 3
  python tools/room_calibrate.py --show        # 只顯示目前校正檔與修正曲線
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from airsender import roomcal  # noqa: E402
from airsender.config import Config  # noqa: E402
from airsender.logging_setup import setup_logging  # noqa: E402
from airsender.streamer import State, Streamer  # noqa: E402


def print_curves(cfg: Config) -> None:
    roomcal.ENGINE.rebuild(cfg)
    curves = roomcal.ENGINE.curves(cfg)
    freqs = curves["freqs"]
    print("freq(Hz)   measured  correction  result")
    for f0 in (40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 800, 1000, 2000, 4000, 8000, 12000):
        i = int(np.argmin(np.abs(freqs - f0)))
        m = f"{curves['measured'][i]:+7.1f}" if "measured" in curves else "    n/a"
        r = f"{curves['result'][i]:+7.1f}" if "result" in curves else "    n/a"
        print(f"{f0:8d}  {m}   {curves['correction'][i]:+7.1f}   {r}")
    print(f"pre-gain {roomcal.ENGINE.pre_gain_db:+.1f} dB")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default="", help="AirPlay 接收端名稱（部分比對）")
    ap.add_argument("--positions", type=int, default=1)
    ap.add_argument("--sweeps", type=int, default=3)
    ap.add_argument("--volume", type=int, default=None, help="校正時接收端音量（預設不更動）")
    ap.add_argument("--show", action="store_true", help="只顯示目前校正檔")
    ap.add_argument(
        "--verify", action="store_true", help="EQ 開啟下再量一次，與校正檔比較殘差（驗證校正效果）"
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    setup_logging("DEBUG" if args.verbose else "INFO", console=True)
    cfg = Config.load()
    if args.show:
        print_curves(cfg)
        return 0

    if args.volume is not None:
        cfg.volume = args.volume
    streamer = Streamer(cfg, lambda s, m: print(f"  [{s.value}] {m}", flush=True), persist=False)
    streamer.start_loop()
    try:
        devices = [d for d in streamer.scan(5) if d.kind == "airplay"]
        key = args.device.lower()
        target = next((d for d in devices if key and key in d.name.lower()), devices[0] if devices else None)
        if target is None:
            print("找不到 AirPlay 接收端")
            return 1
        print(f"串流到 {target.name} 並開始校正（{args.positions} 個位置 × {args.sweeps} 次掃頻）")
        streamer.start(target)
        t0 = time.monotonic()
        while streamer.state != State.STREAMING and time.monotonic() - t0 < 25:
            time.sleep(0.3)
        if streamer.state != State.STREAMING:
            print("串流未開始")
            return 1
        time.sleep(1.5)
        cal = roomcal.Calibrator(cfg, sweeps=args.sweeps)
        if args.verify:
            profile = roomcal.CorrectionProfile.load()
            if profile is None:
                print("沒有校正檔可驗證")
                return 1
            roomcal.ENGINE.bypass = False  # EQ 開啟
            d, snr, lat = cal.measure_position(lambda msg, frac: print(f"  {msg}", flush=True))
            after = roomcal.Calibrator.finalize(
                [d], mic_cal=roomcal.load_mic_calibration(cfg.mic_calibration_file)
            )
            before = np.asarray(profile.measured_db)
            from airsender import dsp  # pylint: disable=import-outside-toplevel

            corr = dsp.correction_curve(
                roomcal.GRID,
                before,
                strength=cfg.correction_strength / 100.0,
                bass_boost_db=float(cfg.bass_boost_db),
                limits=dsp.CorrectionLimits(
                    float(cfg.correction_f_low),
                    float(cfg.correction_f_high),
                    float(cfg.correction_max_boost_db),
                ),
            )
            curves = {"result": before + corr}
            if not roomcal.ENGINE.active:
                print("注意：EQ 引擎未啟用（設定檔 room_correction_enabled 為 false？），此次量測等同未校正")
            print(f"\n驗證：SNR {snr:.1f} dB。各頻率（相對 200 Hz–2 kHz 平均）：")
            print("freq(Hz)   校正前   預估   校正後實測")
            band = (roomcal.GRID >= 40) & (roomcal.GRID <= cfg.correction_f_high)
            for f0 in (40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 1000, 2000, 4000):
                i = int(np.argmin(np.abs(roomcal.GRID - f0)))
                print(f"{f0:8d}  {before[i]:+7.1f}  {curves['result'][i]:+7.1f}  {after[i]:+7.1f}")
            dev_before = np.sqrt(np.mean(before[band] ** 2))
            dev_after = np.sqrt(np.mean(after[band] ** 2))
            print(
                f"\n{int(roomcal.GRID[band][0])}–{cfg.correction_f_high} Hz 偏離平坦的 RMS：校正前 {dev_before:.1f} dB → 校正後 {dev_after:.1f} dB"
            )
            return 0
        roomcal.ENGINE.bypass = True
        dbs, snrs, lats = [], [], []
        for pos in range(args.positions):
            if args.positions > 1:
                input(f"請把麥克風放到第 {pos + 1} 個位置後按 Enter…")
            d, snr, lat = cal.measure_position(lambda msg, frac: print(f"  {msg}", flush=True))
            dbs.append(d)
            snrs.append(snr)
            lats.append(lat)
            print(f"  位置 {pos + 1}：SNR {snr:.1f} dB，延遲 {lat:.0f} ms")
        roomcal.ENGINE.bypass = False
        measured = cal.finalize(dbs, mic_cal=roomcal.load_mic_calibration(cfg.mic_calibration_file))
        profile = roomcal.CorrectionProfile(
            created=time.strftime("%Y-%m-%d %H:%M"),
            positions=args.positions,
            sweeps=args.sweeps,
            device_name=target.name,
            mic_name="",
            measured_db=[float(v) for v in measured],
            snr_db=float(np.mean(snrs)),
            latency_ms=float(np.median(lats)),
        )
        path = profile.save()
        print(f"\n校正檔已儲存：{path}")
        cfg.room_correction_enabled = True
        cfg.save()
        print_curves(cfg)
        return 0
    finally:
        streamer.shutdown()


if __name__ == "__main__":
    sys.exit(main())
