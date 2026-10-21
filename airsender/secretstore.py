"""以 Windows DPAPI（使用者範圍）保護設定檔裡的憑證。

格式："dpapi:" + base64(CryptProtectData 的密文)。只有同一個 Windows 使用者帳號能解回，
備份 / 同步 AppData 到別台機器時無法讀取（屆時重新配對即可）。非 Windows 或 DPAPI 失敗時維持明文。
"""

from __future__ import annotations

import base64
import ctypes
import logging
import sys
from ctypes import wintypes

_LOGGER = logging.getLogger(__name__)
PREFIX = "dpapi:"
_ENTROPY = b"AirSender-credentials-v1"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DataBlob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _call(func_name: str, data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    func = getattr(crypt32, func_name)
    inp = _blob(data)
    entropy = _blob(_ENTROPY)
    out = _DataBlob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    if not func(ctypes.byref(inp), None, ctypes.byref(entropy), None, None, flags, ctypes.byref(out)):
        raise OSError(ctypes.get_last_error() or ctypes.GetLastError())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def available() -> bool:
    return sys.platform == "win32"


def protect(text: str) -> str:
    """明文 → "dpapi:…"；已是加密格式或 DPAPI 不可用時原樣回傳。"""
    if not text or text.startswith(PREFIX) or not available():
        return text
    try:
        return PREFIX + base64.b64encode(_call("CryptProtectData", text.encode("utf-8"))).decode("ascii")
    except Exception as exc:  # pylint: disable=broad-except
        _LOGGER.warning("DPAPI 加密失敗，憑證以明文儲存：%s", exc)
        return text


def unprotect(text: str) -> str:
    """ "dpapi:…" → 明文；明文原樣回傳；解不開時回傳空字串（需要重新配對）。"""
    if not text or not text.startswith(PREFIX):
        return text
    try:
        raw = base64.b64decode(text[len(PREFIX) :])
        return _call("CryptUnprotectData", raw).decode("utf-8")
    except Exception as exc:  # pylint: disable=broad-except
        _LOGGER.warning("DPAPI 解密失敗（設定檔來自別的使用者或機器？）：%s", exc)
        return ""


def protect_credentials(creds: dict) -> dict:
    return {
        dev: {k: protect(v) if isinstance(v, str) else v for k, v in entry.items()}
        for dev, entry in creds.items()
        if isinstance(entry, dict)
    }


def unprotect_credentials(creds: dict) -> dict:
    return {
        dev: {k: unprotect(v) if isinstance(v, str) else v for k, v in entry.items()}
        for dev, entry in creds.items()
        if isinstance(entry, dict)
    }
