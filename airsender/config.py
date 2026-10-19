"""使用者設定（存於 %APPDATA%\\AirSender\\config.json）。"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

from . import APP_NAME

_LOGGER = logging.getLogger(__name__)

# AirPlay 延遲預設組：名稱 -> (HomePod 最小緩衝 latencyMin, 接收端緩衝, 本機抖動緩衝)，單位 ms。
# 總延遲 ≈ 三者相加 + 約 15 ms。HomePod mini 實測：最低組約 140 ms、低約 255 ms、標準約 500 ms。
LATENCY_PRESETS: dict[str, tuple[int, int, int]] = {
    "最低（約 140 ms）": (25, 100, 20),
    "低（約 255 ms）": (50, 150, 40),
    "標準（約 500 ms）": (100, 350, 40),
    "高（約 1 s）": (250, 700, 40),
    "最穩（約 2 s）": (250, 1700, 80),
}
DEFAULT_LATENCY_PRESET = "低（約 255 ms）"

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
    device_kind: str = "airplay"  # airplay / cast / dlna / snapcast
    device_url: str = ""  # DLNA 描述檔 URL 或 Snapcast port
    # 各接收端的配對憑證與密碼：{identifier: {"raop": ..., "airplay": ..., "password": ...}}
    credentials: dict[str, dict[str, str]] = field(default_factory=dict)

    # 非 AirPlay 輸出
    http_port: int = 8765  # HTTP WAV 串流伺服器（Google Cast / DLNA 由此拉流）
    snapcast_servers: list[str] = field(default_factory=list)  # "host" 或 "host:port"

    # 延遲相關
    receiver_latency_ms: int = 150  # 接收端緩衝（RTP 時間戳提前量）
    airplay_latency_min_ms: int = 50  # AirPlay 2 SETUP 的 latencyMin（HomePod 最小緩衝；pyatv 原本寫死 250）
    jitter_buffer_ms: int = 40  # 本機抖動緩衝（擷取 -> 傳送之間）

    # 音質相關
    resample_quality: str = "VHQ"  # soxr 品質（固定比率模式）
    dither: bool = True  # 轉成 16-bit 時加入 TPDF dither
    # 時鐘漂移補償："smooth" = 可變速率重取樣微調 ±0.1%（無跳音）；
    # "trim" = 固定比率 VHQ，緩衝超過高水位時一次裁掉（偶發小跳音）
    drift_compensation: str = "smooth"

    # 房間校正 / 低音增強（校正檔存於 room_correction.json）
    room_correction_enabled: bool = False
    bass_boost_db: float = 0.0  # 低架增益 0–9 dB（100 Hz 以下）
    correction_strength: int = 100  # 套用量測修正的百分比
    correction_max_boost_db: float = 6.0
    correction_f_low: int = 40
    correction_f_high: int = 10000
    mic_calibration_file: str = ""  # 可選：REW 格式麥克風校正檔

    # 自動更新
    update_check_days: int = 0  # 每幾天查一次 GitHub Releases；0 = 不檢查（預設關閉，需要的人自行開啟）
    last_update_check: str = ""  # ISO 時間
    update_skip_version: str = ""  # 使用者選擇略過的版本

    # 其他
    volume: Optional[int] = None  # None = 不更動接收端音量
    autostart_stream: bool = False  # 程式啟動後自動連到上次的裝置
    capture_device_index: Optional[int] = None  # None = 跟隨 Windows 預設輸出裝置
    log_level: str = "INFO"

    @classmethod
    def load(cls) -> Config:
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


def latency_preset_name(cfg: Config) -> str:
    """目前設定對應的預設組名稱；不吻合任何組時回傳「自訂」。"""
    current = (cfg.airplay_latency_min_ms, cfg.receiver_latency_ms, cfg.jitter_buffer_ms)
    for name, values in LATENCY_PRESETS.items():
        if values == current:
            return name
    return "自訂"
