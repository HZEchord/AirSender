"""Google Cast（Chromecast、Nest 音響、內建 Cast 的電視 / 機上盒）。

接收端以 Default Media Receiver 播放我們的 HTTP WAV 串流（streamType=LIVE）。
pychromecast 是同步 / 自帶執行緒的函式庫，這裡全部透過 run_in_executor 呼叫。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Optional

from ..streamer_types import Device

_LOGGER = logging.getLogger(__name__)


async def discover(loop: asyncio.AbstractEventLoop, timeout: float = 5.0) -> list[Device]:
    import pychromecast  # pylint: disable=import-outside-toplevel

    def _scan():
        casts, browser = pychromecast.get_chromecasts(timeout=timeout)
        try:
            return [
                Device(
                    kind="cast",
                    name=c.cast_info.friendly_name,
                    identifier=str(c.cast_info.uuid),
                    address=str(c.cast_info.host),
                    model=f"{c.cast_info.manufacturer} {c.cast_info.model_name}".strip(),
                )
                for c in casts
                if c.cast_info.cast_type in ("cast", "audio", "group")
            ]
        finally:
            browser.stop_discovery()

    return await loop.run_in_executor(None, _scan)


async def run(
    loop: asyncio.AbstractEventLoop,
    device: Device,
    url: str,
    volume: Optional[int],
    on_streaming: Callable[[], None],
    set_volume_slot: Callable[[Optional[Callable[[int], None]]], None],
) -> None:
    import pychromecast  # pylint: disable=import-outside-toplevel

    def _connect():
        # 直接以掃描時記下的位址連線（port 8009），不重新探索：更快，且跨子網路也可用
        cast = pychromecast.get_chromecast_from_host(
            (device.address, 8009, uuid.UUID(device.identifier), device.model, device.name),
            tries=2,
            timeout=10,
        )
        cast.wait(timeout=15)
        return cast

    cast = await loop.run_in_executor(None, _connect)
    mc = cast.media_controller
    try:
        if volume is not None:
            await loop.run_in_executor(None, cast.set_volume, volume / 100.0)
        set_volume_slot(lambda pct: cast.set_volume(pct / 100.0))

        def _play():
            mc.play_media(
                url,
                "audio/wav",
                title="Windows 系統音訊",
                stream_type="LIVE",
                metadata={"metadataType": 3, "title": "Windows 系統音訊", "artist": "AirSender"},
            )
            mc.block_until_active(timeout=20)

        await loop.run_in_executor(None, _play)
        _LOGGER.info("Cast：%s 開始播放 %s", device.name, url)
        on_streaming()

        idle_since: Optional[float] = None
        while True:
            await asyncio.sleep(2.0)
            status = mc.status
            state = status.player_state if status else None
            if state in ("PLAYING", "BUFFERING"):
                idle_since = None
                continue
            if state == "IDLE":
                reason = status.idle_reason if status else None
                if reason in ("ERROR", "FINISHED", "CANCELLED", "INTERRUPTED"):
                    raise RuntimeError(f"Cast 接收端停止播放（{reason}）")
                idle_since = idle_since or loop.time()
                if loop.time() - idle_since > 10:
                    raise RuntimeError("Cast 接收端閒置超過 10 秒")
    finally:
        set_volume_slot(None)

        def _teardown():
            try:
                mc.stop()
            except Exception:  # pylint: disable=broad-except
                pass
            try:
                cast.quit_app()
            except Exception:  # pylint: disable=broad-except
                pass
            try:
                cast.disconnect(timeout=5)
            except Exception:  # pylint: disable=broad-except
                pass

        await loop.run_in_executor(None, _teardown)
