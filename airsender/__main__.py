"""命令列入口。

airsender                     啟動系統匣程式
airsender --list              列出網路上的 AirPlay 接收端
airsender --device 客廳 --headless   不開系統匣，直接串流到名稱含「客廳」的裝置（Ctrl+C 結束）
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from . import APP_NAME, __version__
from .config import Config
from .logging_setup import setup_logging

_LOGGER = logging.getLogger(__name__)


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="airsender", description=__doc__)
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--list", action="store_true", help="列出 AirPlay 接收端後結束")
    parser.add_argument("--device", help="接收端名稱（部分比對）或 IP")
    parser.add_argument(
        "--snapcast", metavar="HOST[:PORT]", help="直接推送到 snapserver 的 tcp 來源（預設 port 4953）"
    )
    parser.add_argument("--headless", action="store_true", help="不顯示系統匣，直接串流")
    parser.add_argument("--latency", type=int, help="接收端緩衝毫秒數（100–2000，預設 150）")
    parser.add_argument("--jitter", type=int, help="本機抖動緩衝毫秒數（預設 40）")
    parser.add_argument(
        "--latency-min",
        type=int,
        dest="latency_min",
        help="HomePod 最小緩衝（AirPlay 2 latencyMin）毫秒數，25–250（預設 50）",
    )
    parser.add_argument("--volume", type=int, help="接收端音量 0-100")
    parser.add_argument("--no-dither", action="store_true", help="關閉 TPDF dither")
    parser.add_argument("--quality", choices=["VHQ", "HQ", "MQ"], help="重取樣品質")
    parser.add_argument(
        "--drift",
        choices=["smooth", "trim"],
        help="時鐘漂移補償：smooth=可變速率微調（預設）、trim=固定比率+裁切",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="顯示除錯訊息")
    return parser.parse_args(argv)


def _apply_overrides(config: Config, args) -> None:
    if args.latency is not None:
        config.receiver_latency_ms = args.latency
    if args.jitter is not None:
        config.jitter_buffer_ms = args.jitter
    if args.latency_min is not None:
        config.airplay_latency_min_ms = args.latency_min
    if args.volume is not None:
        config.volume = args.volume
    if args.no_dither:
        config.dither = False
    if args.quality:
        config.resample_quality = args.quality
    if args.drift:
        config.drift_compensation = args.drift


def main(argv=None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    config = Config.load()
    _apply_overrides(config, args)
    headless = args.list or args.headless
    setup_logging("DEBUG" if args.verbose else config.log_level, console=headless)

    if not headless:
        from .dpi import enable_dpi_awareness

        _LOGGER.info("DPI 感知：%s", enable_dpi_awareness())
        from .tray import run_tray

        run_tray()
        return 0

    from .sinks import KIND_LABELS
    from .sinks.snapcast import parse_server
    from .streamer import State, Streamer

    streamer = Streamer(config, lambda s, m: print(f"[{s.value}] {m}"))
    streamer.start_loop()
    try:
        if args.snapcast and not args.list:
            devices = [parse_server(args.snapcast)]
        else:
            print("掃描接收端（AirPlay / Google Cast / DLNA）…")
            devices = streamer.scan(timeout=5.0)
        if not devices:
            print("找不到任何接收端。")
            return 1
        for d in devices:
            print(f"  - [{KIND_LABELS.get(d.kind, d.kind)}] {d.label}")
        if args.list:
            return 0

        target = None
        if args.snapcast:
            target = devices[0]
        elif args.device:
            key = args.device.lower()
            target = next((d for d in devices if key in d.name.lower() or key == d.address), None)
            if target is None:
                print(f"找不到符合「{args.device}」的裝置。")
                return 1
        elif config.device_identifier:
            target = next((d for d in devices if d.identifier == config.device_identifier), None)
        if target is None:
            target = devices[0]

        print(
            f"串流到 {target.name}（接收端緩衝 {config.receiver_latency_ms} ms，"
            f"抖動緩衝 {config.jitter_buffer_ms} ms），Ctrl+C 結束。"
        )
        streamer.start(target)
        last_report = time.monotonic()
        while True:
            time.sleep(0.5)
            src = streamer.source
            if streamer.state == State.STREAMING and src and time.monotonic() - last_report > 10:
                last_report = time.monotonic()
                print(
                    f"  緩衝 {src.buffered_ms:.0f} ms，漂移補償 {src.drift_ppm:+.0f} ppm，"
                    f"underrun {src.underruns}，trim {src.trims}"
                )
    except KeyboardInterrupt:
        print("\n停止中…")
        return 0
    finally:
        streamer.shutdown()


if __name__ == "__main__":
    sys.exit(main())
