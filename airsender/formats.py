"""AirPlay 串流格式選擇。

實測（HomePod mini，audioOS 980.77.2，realtime 串流）：
- 44.1 kHz 與 48 kHz 的 16-bit PCM 都能正確播放（麥克風量測音高正確）
- 88.2 / 96 kHz 以半速播放 → 上限 48 kHz
- 24-bit 各種打包都被當 16-bit 解讀 → 位元深度固定 16-bit
所以可選的只有取樣率：來源是 48k 系列（48 / 96 / 192 kHz）時用 48 kHz 可免去 48 → 44.1 的重取樣。
"""

from __future__ import annotations

import logging
from typing import Optional

_LOGGER = logging.getLogger(__name__)

AUDIO_FORMAT_FLAG = {44100: 0x800, 48000: 0x8000}  # AirPlay 2 SETUP audioFormat：PCM/<rate>/16/2
SUPPORTED_RATES = (44100, 48000)


def device_native_rate(device_index: Optional[int]) -> Optional[int]:
    """擷取裝置（loopback）的原生取樣率；失敗回傳 None。"""
    try:
        import pyaudiowpatch as pa  # pylint: disable=import-outside-toplevel

        p = pa.PyAudio()
        try:
            if device_index is not None:
                info = p.get_device_info_by_index(device_index)
            else:
                info = p.get_default_wasapi_loopback()
            return int(info["defaultSampleRate"])
        finally:
            p.terminate()
    except Exception as exc:  # pylint: disable=broad-except
        _LOGGER.debug("無法查詢擷取裝置取樣率：%s", exc)
        return None


def choose_airplay_rate(setting: str, source_rate: Optional[int]) -> int:
    """依設定（auto / 44100 / 48000）與來源取樣率決定 AirPlay 串流取樣率。"""
    if setting in ("44100", "48000"):
        return int(setting)
    if source_rate and source_rate % 48000 == 0:
        return 48000
    return 44100
