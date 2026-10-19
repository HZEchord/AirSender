"""簡單的對話框（PIN / 密碼輸入、訊息）。

系統匣程式執行時會註冊 UiThread，所有對話框在該執行緒顯示；
沒有 UiThread（命令列工具）時，就在目前執行緒建立並銷毀一個暫時的 Tk 根視窗。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from . import APP_NAME

_LOGGER = logging.getLogger(__name__)
_ui: Any = None


def set_ui_thread(ui: Any) -> None:
    global _ui  # pylint: disable=global-statement
    _ui = ui


def ask_string(prompt: str, title: str = APP_NAME, *, secret: bool = False) -> Optional[str]:
    """跳出輸入框；取消時回傳 None。"""
    if _ui is not None:
        return _ui.ask_string(prompt, title, secret=secret)
    try:
        import tkinter as tk  # pylint: disable=import-outside-toplevel
        from tkinter import simpledialog  # pylint: disable=import-outside-toplevel
    except ImportError:
        _LOGGER.error("此環境沒有 tkinter，無法顯示輸入對話框")
        return None

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return simpledialog.askstring(title, prompt, parent=root, show="*" if secret else None)
    finally:
        root.destroy()


def show_info(message: str, title: str = APP_NAME) -> None:
    if _ui is not None:
        _ui.show_info(message, title)
        return
    try:
        import tkinter as tk  # pylint: disable=import-outside-toplevel
        from tkinter import messagebox  # pylint: disable=import-outside-toplevel
    except ImportError:
        _LOGGER.info("%s: %s", title, message)
        return
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        messagebox.showinfo(title, message, parent=root)
    finally:
        root.destroy()


def ask_yes_no(message: str, title: str = APP_NAME) -> bool:
    if _ui is not None:
        return bool(_ui.ask_yes_no(message, title))
    try:
        import tkinter as tk  # pylint: disable=import-outside-toplevel
        from tkinter import messagebox  # pylint: disable=import-outside-toplevel
    except ImportError:
        return False
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return bool(messagebox.askyesno(title, message, parent=root))
    finally:
        root.destroy()
