"""日誌：主控台 + 每日輪替的檔案（%LOCALAPPDATA%\\AirSender\\logs）。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import log_dir


def setup_logging(level: str = "INFO", console: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        log_dir() / "airsender.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(file_handler)

    if console and sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(fmt)
        stream_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
        root.addHandler(stream_handler)

    # pyatv / zeroconf 在 DEBUG 時非常吵，除非使用者明確要求
    if level.upper() != "DEBUG":
        for noisy in ("pyatv", "zeroconf", "asyncio", "urllib3", "pychromecast", "async_upnp_client", "aiohttp", "charset_normalizer"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
