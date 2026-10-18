"""使用者設定（存於 %APPDATA%\\AirSender\\config.json）。"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional

from . import APP_NAME

_LOGGER = logging.getLogger(__name__)

# 選單顯示名稱 -> 接收端緩衝（毫秒）。這是 AirPlay 接收端「收到封包後等多久才播」的時間，
# 是整體延遲的主要來源。設太低時 HomePod 會因封包來不及到而出現斷音或靜音。
LATENCY_PRESETS: dict[str, int] = {
    "極低 (250 ms)": 250,
    "低 (500 ms)": 500,
    "中 (1000 ms)": 1000,
    "標準 (1500 ms)": 1500,
    "高 (2000 ms)": 2000,
}

DRIFT_MODES: dict[str, str] = {
    "平滑：可變速率微調（建議）": "smooth",
    "固定比率 VHQ + 裁切": "trim",
}

RESAMPLE_QUALITIES: dict[str, str] = {
    "最高 (VHQ)": "VHQ",
    "高 (HQ)": "HQ",
    "中 (MQ)": "MQ",
}


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    path = Path(base) / APP_NAME / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return config_dir() / "config.json"


@dataclass
class Config:
    """所有可持久化的設定。"""

    # 上次選擇的接收端
    device_identifier: Optional[str] = None
    device_name: Optional[str] = None
    device_address: Optional[str] = None

    # 延遲相關
    receiver_latency_ms: int = 500  # 接收端緩衝
    jitter_buffer_ms: int = 40  # 本機抖動緩衝（擷取 -> 傳送之間）

    # 音質相關
    resample_quality: str = "VHQ"  # soxr 品質（固定比率模式）
    dither: bool = True  # 轉成 16-bit 時加入 TPDF dither
    # 時鐘漂移補償："smooth" = 可變速率重取樣微調 ±0.1%（無跳音）；
    # "trim" = 固定比率 VHQ，緩衝超過高水位時一次裁掉（偶發小跳音）
    drift_compensation: str = "smooth"

    # 其他
    volume: Optional[int] = None  # None = 不更動接收端音量
    autostart_stream: bool = False  # 程式啟動後自動連到上次的裝置
    capture_device_index: Optional[int] = None  # None = 跟隨 Windows 預設輸出裝置
    log_level: str = "INFO"

    @classmethod
    def load(cls) -> "Config":
        path = config_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _LOGGER.warning("無法讀取設定檔 %s：%s，改用預設值", path, exc)
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        path = config_path()
        try:
            path.write_text(
                json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            _LOGGER.warning("無法寫入設定檔 %s：%s", path, exc)
