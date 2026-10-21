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
    "較低（約 220 ms）": (25, 150, 30),
    "低（約 255 ms）": (50, 150, 40),
    "標準（約 500 ms）": (100, 350, 40),
    "高（約 1 s）": (250, 700, 40),
    "最穩（約 2 s）": (250, 1700, 80),
}
DEFAULT_LATENCY_PRESET = "低（約 255 ms）"

# buffered 傳輸的預設組：名稱 → 提前量 ms（總延遲 ≈ 提前量 + 約 50 ms；HomePod mini 實測 60 → 111、75 → 127、100 → 149、350 → 400 ms；50 以下斷音）
BUFFERED_PRESETS: dict[str, int] = {
    "最低（約 110 ms）": 60,
    "較低（約 130 ms）": 75,
    "低（約 150 ms）": 100,
    "標準（約 250 ms）": 200,
    "高（約 550 ms）": 500,
    "最穩（約 1 s）": 1000,
}
DEFAULT_BUFFERED_PRESET = "標準（約 250 ms）"
BUFFERED_OVERHEAD_MS = 50  # 預填 20 + 擷取 ~10 + 接收端 arrivalToRender 4 + 傳播

# 傳輸方式：realtime = AirPlay 2 realtime 串流（PCM 16-bit、UDP、本機抖動緩衝）；
# buffered = AirPlay 2 buffered 串流（ALAC 24-bit / 48 kHz、TCP、PTP 時間錨、接收端緩衝）
TRANSPORTS: dict[str, str] = {"realtime": "realtime 16-bit", "buffered": "buffered 24-bit"}

# 使用模式：遊戲 / 影片 = 盡量壓低延遲；音樂 = 大緩衝求穩（將來 buffered 模式完成後改走 24-bit / PTP）
MODES: dict[str, str] = {"game": "遊戲 / 影片", "music": "音樂"}

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
    # 使用模式與各模式對應的延遲預設組
    mode: str = "game"  # game / music
    game_preset: str = "較低（約 130 ms）"
    music_preset: str = "標準（約 250 ms）"
    # 各模式的傳輸方式（見 TRANSPORTS）：遊戲 / 影片預設 realtime，音樂預設 buffered
    game_transport: str = "buffered"
    music_transport: str = "buffered"
    buffered_bit_depth: int = 24  # buffered 串流的位元深度（HomePod 宣告支援 24）
    receiver_latency_ms: int = 75  # 接收端緩衝 / buffered 提前量；預設 = 遊戲模式 buffered「較低」組
    airplay_latency_min_ms: int = 25  # AirPlay 2 SETUP 的 latencyMin（HomePod 最小緩衝；pyatv 原本寫死 250）
    airplay_sample_rate: str = "auto"  # auto / 44100 / 48000；HomePod realtime 實測上限 48 kHz、16-bit 固定
    jitter_buffer_ms: int = 30  # 本機抖動緩衝（擷取 -> 傳送之間）

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
    correction_f_high: int = 500  # 只修房間主導的低頻區（Schroeder 頻率以下）；高頻受麥克風指向與位置影響太大
    mic_calibration_file: str = ""  # 可選：REW 格式麥克風校正檔

    # 自動更新
    update_check_days: int = 0  # 每幾天查一次 GitHub Releases；0 = 不檢查（預設關閉，需要的人自行開啟）
    last_update_check: str = ""  # ISO 時間
    update_skip_version: str = ""  # 使用者選擇略過的版本

    # 其他
    volume: Optional[int] = None  # None = 不更動接收端音量
    # 麥克風實測的端對端延遲，依傳輸方式各存一筆：{"ms", "formula_ms", "lead", "preset", "mode", "when", "device", "values"}
    latency_measured: dict[str, dict] = field(default_factory=dict)
    autostart_stream: bool = False  # 程式啟動後自動連到上次的裝置
    mute_local_output: bool = False  # 串流中把電腦的輸出端點靜音（硬體靜音的裝置 loopback 不受影響）
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
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        # 0.7.0 的預設修正上限是 10 kHz；實機證明高頻不該修，0.7.2 起預設 500 Hz。
        # 舊設定檔若仍是 10000（從未手動調整過的值），視為沿用新預設。
        if cfg.correction_f_high == 10000:
            cfg.correction_f_high = 500
        # 0.11.0 的遊戲 / 影片模式預設是 realtime「較低」；0.11.1 起預設 buffered「較低」（實測更低延遲且 24-bit）。
        # 舊設定檔若仍是未改過的 0.11.0 預設，視為沿用新預設；手動選過的組合不動。
        if cfg.game_transport == "realtime" and cfg.game_preset == "較低（約 220 ms）" and "game_transport" in data:
            cfg.game_transport, cfg.game_preset = "buffered", "較低（約 130 ms）"
            if cfg.mode == "game":
                cfg.receiver_latency_ms = BUFFERED_PRESETS[cfg.game_preset]
        return cfg

    def save(self) -> None:
        path = config_path()
        try:
            path.write_text(
                json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            _LOGGER.warning("無法寫入設定檔 %s：%s", path, exc)


def mode_transport(cfg: Config, mode: str) -> str:
    value = cfg.game_transport if mode == "game" else cfg.music_transport
    return value if value in TRANSPORTS else ("buffered" if mode == "music" else "realtime")


def presets_for(transport: str) -> dict[str, object]:
    return BUFFERED_PRESETS if transport == "buffered" else LATENCY_PRESETS


def default_preset_for(transport: str) -> str:
    return DEFAULT_BUFFERED_PRESET if transport == "buffered" else DEFAULT_LATENCY_PRESET


def mode_preset_name(cfg: Config, mode: str) -> str:
    name = cfg.game_preset if mode == "game" else cfg.music_preset
    transport = mode_transport(cfg, mode)
    return name if name in presets_for(transport) else default_preset_for(transport)


def apply_mode(cfg: Config, mode: str) -> bool:
    """切換模式並套用該模式的延遲預設組；回傳延遲參數是否改變（需重連）。"""
    cfg.mode = mode if mode in MODES else "game"
    name = mode_preset_name(cfg, cfg.mode)
    if mode_transport(cfg, cfg.mode) == "buffered":
        lead = BUFFERED_PRESETS[name]
        changed = lead != cfg.receiver_latency_ms
        cfg.receiver_latency_ms = lead
        return changed
    lmin, lat, jit = LATENCY_PRESETS[name]
    changed = (lmin, lat, jit) != (cfg.airplay_latency_min_ms, cfg.receiver_latency_ms, cfg.jitter_buffer_ms)
    cfg.airplay_latency_min_ms, cfg.receiver_latency_ms, cfg.jitter_buffer_ms = lmin, lat, jit
    return changed


def latency_formula_ms(cfg: Config) -> int:
    """依設定值的公式估計（未經麥克風校正）。"""
    if mode_transport(cfg, cfg.mode) == "buffered":
        return cfg.receiver_latency_ms + BUFFERED_OVERHEAD_MS
    return cfg.airplay_latency_min_ms + cfg.receiver_latency_ms + cfg.jitter_buffer_ms + 15


def estimated_latency_ms(cfg: Config) -> int:
    """估計總延遲：有同一傳輸方式的麥克風實測值時，以實測修正公式（實測 − 當時公式 = 固定開銷差）。"""
    base = latency_formula_ms(cfg)
    m = cfg.latency_measured.get(mode_transport(cfg, cfg.mode)) if cfg.latency_measured else None
    if m and isinstance(m.get("ms"), (int, float)) and isinstance(m.get("formula_ms"), (int, float)):
        return int(round(base + (m["ms"] - m["formula_ms"])))
    return base


def measured_latency_text(cfg: Config) -> str:
    """狀態文字：「實測 147 ms（10-21 23:40）」；設定已與量測時不同則註明。"""
    m = cfg.latency_measured.get(mode_transport(cfg, cfg.mode)) if cfg.latency_measured else None
    if not m or not isinstance(m.get("ms"), (int, float)):
        return ""
    same = m.get("formula_ms") == latency_formula_ms(cfg)
    text = f"實測 {m['ms']:.0f} ms（{m.get('when', '')}"
    if not same:
        text += f"，當時「{str(m.get('preset', '')).split('（')[0]}」"
    return text + "）"


def latency_preset_name(cfg: Config) -> str:
    """目前設定對應的預設組名稱；不吻合任何組時回傳「自訂」。"""
    if mode_transport(cfg, cfg.mode) == "buffered":
        for name, lead in BUFFERED_PRESETS.items():
            if lead == cfg.receiver_latency_ms:
                return name
        return "自訂"
    current = (cfg.airplay_latency_min_ms, cfg.receiver_latency_ms, cfg.jitter_buffer_ms)
    for name, values in LATENCY_PRESETS.items():
        if values == current:
            return name
    return "自訂"
