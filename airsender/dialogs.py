"""簡單的 Windows 對話框（tkinter），供系統匣程式詢問 PIN / 密碼。

tkinter 只能在建立它的執行緒使用；這裡每次呼叫都在目前執行緒建立並銷毀一個隱藏的根視窗，
所以可以從系統匣的工作執行緒安全呼叫。
"""

from __future__ import annotations

import logging
from typing import Optional

from . import APP_NAME

_LOGGER = logging.getLogger(__name__)


def ask_string(prompt: str, title: str = APP_NAME, *, secret: bool = False) -> Optional[str]:
    """跳出輸入框；取消時回傳 None。"""
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
        return simpledialog.askstring(
            title, prompt, parent=root, show="*" if secret else None
        )
    finally:
        root.destroy()


def show_info(message: str, title: str = APP_NAME) -> None:
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
