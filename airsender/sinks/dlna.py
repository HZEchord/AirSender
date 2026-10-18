"""DLNA / UPnP AV MediaRenderer（Yamaha MusicCast、Denon HEOS、Sonos S1、各式網路音響與電視）。

控制點以 SetAVTransportURI + Play 要接收端自己來拉我們的 HTTP WAV 串流。
各家韌體對無限長 WAV 的相容性不一，部分裝置需要 DIDL-Lite metadata，這裡一律附上。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Optional
from urllib.parse import urlparse
from xml.sax.saxutils import escape

from ..streamer_types import Device

_LOGGER = logging.getLogger(__name__)

MEDIA_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"


def didl_lite(url: str, title: str) -> str:
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
        '<item id="airsender-live" parentID="0" restricted="1">'
        f"<dc:title>{escape(title)}</dc:title>"
        "<dc:creator>AirSender</dc:creator>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        '<res protocolInfo="http-get:*:audio/wav:DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000" '
        f'sampleFrequency="44100" bitsPerSample="16" nrAudioChannels="2">{escape(url)}</res>'
        "</item></DIDL-Lite>"
    )


async def discover(timeout: float = 4.0) -> list[Device]:
    from async_upnp_client.aiohttp import AiohttpRequester  # pylint: disable=import-outside-toplevel
    from async_upnp_client.client_factory import UpnpFactory  # pylint: disable=import-outside-toplevel
    from async_upnp_client.search import async_search  # pylint: disable=import-outside-toplevel

    locations: dict[str, str] = {}

    async def on_found(headers) -> None:
        location = headers.get("location")
        usn = headers.get("usn", "")
        if location and location not in locations:
            locations[location] = usn

    try:
        await async_search(on_found, timeout=int(timeout), search_target=MEDIA_RENDERER)
    except Exception as exc:  # pylint: disable=broad-except
        _LOGGER.debug("SSDP 搜尋失敗：%s", exc)
        return []

    factory = UpnpFactory(AiohttpRequester(timeout=5), non_strict=True)
    devices: list[Device] = []
    for location, usn in locations.items():
        try:
            upnp_device = await factory.async_create_device(location)
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.debug("讀取 %s 失敗：%s", location, exc)
            continue
        udn = upnp_device.udn or usn.split("::")[0]
        devices.append(
            Device(
                kind="dlna",
                name=upnp_device.friendly_name or urlparse(location).hostname or location,
                identifier=udn,
                address=urlparse(location).hostname or "",
                model=f"{upnp_device.manufacturer} {upnp_device.model_name}".strip(),
                url=location,
            )
        )
    return devices


async def run(
    device: Device,
    url: str,
    volume: Optional[int],
    on_streaming: Callable[[], None],
    set_volume_slot: Callable[[Optional[Callable[[int], None]]], None],
    loop: asyncio.AbstractEventLoop,
) -> None:
    from async_upnp_client.aiohttp import AiohttpRequester  # pylint: disable=import-outside-toplevel
    from async_upnp_client.client_factory import UpnpFactory  # pylint: disable=import-outside-toplevel
    from async_upnp_client.profiles.dlna import DmrDevice, TransportState  # pylint: disable=import-outside-toplevel

    factory = UpnpFactory(AiohttpRequester(timeout=10), non_strict=True)
    upnp_device = await factory.async_create_device(device.url)
    dmr = DmrDevice(upnp_device, event_handler=None)
    title = "Windows 系統音訊"
    try:
        await dmr.async_set_transport_uri(url, title, didl_lite(url, title))
        await dmr.async_play()
        if volume is not None and dmr.has_volume_level:
            await dmr.async_set_volume_level(volume / 100.0)
        if dmr.has_volume_level:
            set_volume_slot(
                lambda pct: asyncio.run_coroutine_threadsafe(
                    dmr.async_set_volume_level(pct / 100.0), loop
                )
            )
        _LOGGER.info("DLNA：%s 開始播放 %s", device.name, url)
        on_streaming()
        started = loop.time()
        while True:
            await asyncio.sleep(3.0)
            try:
                await dmr.async_update(do_ping=False)
            except Exception as exc:  # pylint: disable=broad-except
                raise RuntimeError(f"DLNA 接收端失去回應：{exc}") from exc
            state = dmr.transport_state
            if state in (TransportState.STOPPED, TransportState.NO_MEDIA_PRESENT) and loop.time() - started > 8:
                raise RuntimeError(f"DLNA 接收端停止播放（{state.name if state else '?'}）")
    finally:
        set_volume_slot(None)
        try:
            await dmr.async_stop()
        except Exception:  # pylint: disable=broad-except
            pass
