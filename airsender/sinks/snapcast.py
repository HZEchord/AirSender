"""Snapcast：以 TCP 把 raw PCM 推給 snapserver 的 tcp:// 來源。

snapserver.conf 範例（接收端那台機器）：
    [stream]
    source = tcp://0.0.0.0:4953?name=AirSender&mode=server&sampleformat=44100:16:2
延遲由 snapserver 的 buffer（預設 1000 ms，可降到 300–500 ms）決定；多房間同步由 Snapcast 負責。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from ..httpstream import PcmBroadcaster
from ..streamer_types import Device

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 4953


def parse_server(spec: str) -> Device:
    """'host' 或 'host:port' -> Device。"""
    host, _, port = spec.strip().partition(":")
    port_num = int(port) if port else DEFAULT_PORT
    return Device(
        kind="snapcast",
        name=f"Snapcast {host}:{port_num}",
        identifier=f"snapcast:{host}:{port_num}",
        address=host,
        model="snapserver tcp source",
        url=str(port_num),
    )


def manual_devices(specs: list[str]) -> list[Device]:
    devices = []
    for spec in specs:
        try:
            devices.append(parse_server(spec))
        except ValueError:
            _LOGGER.warning("無效的 Snapcast 伺服器設定：%s", spec)
    return devices


async def run(device: Device, broadcaster: PcmBroadcaster, on_streaming: Callable[[], None]) -> None:
    port = int(device.url or DEFAULT_PORT)
    reader, writer = await asyncio.wait_for(asyncio.open_connection(device.address, port), timeout=10)
    sub = broadcaster.subscribe()
    try:
        _LOGGER.info("Snapcast：已連線 %s:%d，開始推送 44.1 kHz / 16-bit / 2 ch", device.address, port)
        on_streaming()
        while True:
            data = await sub.get()
            writer.write(data)
            await writer.drain()
            if reader.at_eof():
                raise RuntimeError("Snapcast 伺服器關閉連線")
    finally:
        broadcaster.unsubscribe(sub)
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=2)
        except Exception:  # pylint: disable=broad-except
            pass
