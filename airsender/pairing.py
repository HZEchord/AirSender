"""與接收端配對（取得憑證）。

HomePod 不需要配對。Apple TV（開啟「需要裝置驗證」）、設了密碼的第三方接收端等才需要。
pyatv 的流程：begin() → 若裝置顯示 PIN 則由使用者輸入，否則我們產生 PIN 由使用者在裝置上輸入
→ finish() → 取得 credentials 字串，之後連線時填回 service.credentials。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Optional

import pyatv
from pyatv.const import Protocol
from pyatv.interface import AppleTV, BaseConfig

_LOGGER = logging.getLogger(__name__)

PinProvider = Callable[[str], Awaitable[Optional[str]]]
Notifier = Callable[[str], None]

PROTOCOL_KEYS: dict[Protocol, str] = {Protocol.RAOP: "raop", Protocol.AirPlay: "airplay"}


async def pair_protocol(
    loop: asyncio.AbstractEventLoop,
    conf: BaseConfig,
    protocol: Protocol,
    ask_pin: PinProvider,
    notify: Notifier,
) -> Optional[str]:
    """對單一協定配對，成功回傳 credentials 字串。"""
    if conf.get_service(protocol) is None:
        return None
    pairing = await pyatv.pair(conf, protocol, loop)
    try:
        await pairing.begin()
        if pairing.device_provides_pin:
            pin = await ask_pin(f"請輸入 {conf.name} 畫面上顯示的 PIN（{protocol.name}）")
            if not pin:
                return None
            pairing.pin(int(pin.strip()))
        else:
            pin = 1000 + secrets.randbelow(9000)
            pairing.pin(pin)
            notify(f"請在 {conf.name} 上輸入 PIN：{pin}")
        await pairing.finish()
        if pairing.has_paired:
            _LOGGER.info("%s 的 %s 配對成功", conf.name, protocol.name)
            return pairing.service.credentials
        _LOGGER.warning("%s 的 %s 配對未完成", conf.name, protocol.name)
        return None
    finally:
        await pairing.close()


def apply_credentials(conf: BaseConfig, saved: dict[str, str]) -> None:
    """把儲存的憑證 / 密碼填回設定物件（連線前呼叫）。"""
    for protocol, key in PROTOCOL_KEYS.items():
        creds = saved.get(key)
        service = conf.get_service(protocol)
        if service is not None and creds:
            service.credentials = creds
    password = saved.get("password")
    if password:
        for protocol in PROTOCOL_KEYS:
            service = conf.get_service(protocol)
            if service is not None:
                service.password = password


def needs_pairing(atv: Optional[AppleTV], conf: BaseConfig) -> bool:
    """粗略判斷：RAOP 服務標示需要密碼或裝置驗證。"""
    service = conf.get_service(Protocol.RAOP)
    if service is None:
        return False
    if service.requires_password:
        return True
    # Apple TV 的 RAOP 在 "需要裝置驗證" 時會以 et=4 + pw 標示，pyatv 連線時會丟出 AuthenticationError
    return False
