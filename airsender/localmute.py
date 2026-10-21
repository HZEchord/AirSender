"""串流時靜音電腦的輸出端點（Windows Core Audio，透過 pycaw）。

原理：WASAPI loopback 擷取的是混音引擎送往裝置的資料流。裝置以硬體套用音量 / 靜音
（多數 USB DAC、HDMI、藍牙）時，端點靜音不影響 loopback，所以可以「電腦不出聲、HomePod 照常」。
少數以軟體套用音量的裝置（常見於內建 Realtek 類比輸出）靜音後 loopback 也會變靜音，
這種情況下開啟此選項會讓 HomePod 跟著無聲；`hardware_mute_supported()` 可先查詢。

所有呼叫都在工作執行緒進行（會初始化 COM），程式結束或停止串流時一定還原。
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

_LOGGER = logging.getLogger(__name__)

_lock = threading.Lock()
_state: dict[str, object] = {"muted_by_us": False, "device_name": None}


def _endpoint(device_name: Optional[str]):
    """取得輸出端點的 IAudioEndpointVolume；device_name 為 None 時用預設輸出。"""
    from pycaw.pycaw import AudioUtilities  # pylint: disable=import-outside-toplevel

    if device_name:
        for dev in AudioUtilities.GetAllDevices():
            if dev.FriendlyName and device_name.lower() in str(dev.FriendlyName).lower():
                return dev, _volume_of(dev)
    dev = AudioUtilities.GetSpeakers()
    return dev, _volume_of(dev)


def _volume_of(dev):
    vol = getattr(dev, "EndpointVolume", None)
    if vol is not None:
        return vol
    from ctypes import POINTER, cast  # pylint: disable=import-outside-toplevel

    from comtypes import CLSCTX_ALL  # pylint: disable=import-outside-toplevel
    from pycaw.pycaw import IAudioEndpointVolume  # pylint: disable=import-outside-toplevel

    iface = dev._dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)  # pylint: disable=protected-access
    return cast(iface, POINTER(IAudioEndpointVolume))


def _with_com(fn):
    import comtypes  # pylint: disable=import-outside-toplevel

    comtypes.CoInitialize()
    try:
        return fn()
    finally:
        comtypes.CoUninitialize()


def hardware_mute_supported(device_name: Optional[str] = None) -> Optional[bool]:
    """端點是否以硬體套用靜音（ENDPOINT_HARDWARE_SUPPORT_MUTE = 0x2）。不明時回傳 None。"""

    def work():
        _, vol = _endpoint(device_name)
        return bool(vol.QueryHardwareSupport() & 0x2)

    try:
        return _with_com(work)
    except Exception as exc:  # pylint: disable=broad-except
        _LOGGER.debug("查詢端點硬體支援失敗：%s", exc)
        return None


def set_muted(muted: bool, device_name: Optional[str] = None) -> bool:
    """靜音 / 還原輸出端點。只還原我們自己設下的靜音（使用者本來就靜音的不動）。回傳是否成功。"""

    def work():
        with _lock:
            dev, vol = _endpoint(device_name)
            if muted:
                if _state["muted_by_us"]:
                    return True
                if vol.GetMute():
                    _LOGGER.info("輸出端點 %s 本來就是靜音，不更動", dev.FriendlyName)
                    return True
                vol.SetMute(1, None)
                _state["muted_by_us"] = True
                _state["device_name"] = dev.FriendlyName
                _LOGGER.info("串流中靜音電腦輸出：%s", dev.FriendlyName)
                return True
            if not _state["muted_by_us"]:
                return True
            vol.SetMute(0, None)
            _state["muted_by_us"] = False
            _LOGGER.info("還原電腦輸出：%s", dev.FriendlyName)
            return True

    try:
        return bool(_with_com(work))
    except Exception as exc:  # pylint: disable=broad-except
        _LOGGER.warning("設定輸出端點靜音失敗：%s", exc)
        return False


def restore() -> None:
    """程式結束前呼叫：若還是我們設的靜音，解除。"""
    if _state["muted_by_us"]:
        set_muted(False, _state.get("device_name"))  # type: ignore[arg-type]


def muted_by_us() -> bool:
    return bool(_state["muted_by_us"])
