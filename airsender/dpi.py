"""高 DPI 支援。

沒有宣告 DPI 感知時，Windows 會以 96 DPI 繪製整個程式再用點陣放大，
系統匣選單與對話框的文字都會模糊。必須在建立任何視窗之前呼叫。
"""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)

_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
_PROCESS_PER_MONITOR_DPI_AWARE = 2


def enable_dpi_awareness() -> str:
    """回傳實際啟用的模式名稱（記錄用）。"""
    try:
        import ctypes  # pylint: disable=import-outside-toplevel

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        try:
            if user32.SetProcessDpiAwarenessContext(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
                return "per-monitor v2"
        except (AttributeError, OSError):
            pass
        try:
            shcore = ctypes.windll.shcore  # type: ignore[attr-defined]
            if shcore.SetProcessDpiAwareness(_PROCESS_PER_MONITOR_DPI_AWARE) == 0:
                return "per-monitor"
        except (AttributeError, OSError):
            pass
        if user32.SetProcessDPIAware():
            return "system"
    except Exception as exc:  # pylint: disable=broad-except
        _LOGGER.debug("無法設定 DPI 感知：%s", exc)
    return "none"


def system_dpi() -> int:
    try:
        import ctypes  # pylint: disable=import-outside-toplevel

        return int(ctypes.windll.user32.GetDpiForSystem())  # type: ignore[attr-defined]
    except Exception:  # pylint: disable=broad-except
        return 96
