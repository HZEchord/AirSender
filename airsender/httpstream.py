"""HTTP 即時 WAV 串流：給「由接收端自己來拉 URL」的裝置使用（Google Cast、DLNA、Sonos、任何播放器）。

PcmBroadcaster 擁有一個 WASAPI 擷取（固定比率 VHQ，44.1 kHz / 16-bit / 立體聲），
把 PCM 扇出給每個訂閱者；每個訂閱者有獨立的有上限佇列，接收端拉得太慢就丟最舊的資料，
確保延遲不會無限累積。

這條路徑是無損的，但延遲由接收端的預緩衝決定（Chromecast 約 2–4 秒、DLNA 1–5 秒），
無法像 AirPlay 路徑那樣壓到 0.5 秒。
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from typing import Optional

from aiohttp import web

from . import APP_NAME, __version__
from .capture import WasapiLoopbackCapture
from .config import Config

_LOGGER = logging.getLogger(__name__)

SAMPLE_RATE = 44100
CHANNELS = 2
SAMPLE_BYTES = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_BYTES


def wav_header(sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS, sample_bytes: int = SAMPLE_BYTES) -> bytes:
    """長度未知的 WAV 標頭（RIFF / data 大小填 0xFFFFFFFF，是即時串流的慣例，各家接收端都接受）。"""
    block_align = channels * sample_bytes
    return b"".join(
        [
            b"RIFF",
            struct.pack("<I", 0xFFFFFFFF),
            b"WAVE",
            b"fmt ",
            struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, sample_rate * block_align, block_align, sample_bytes * 8),
            b"data",
            struct.pack("<I", 0xFFFFFFFF),
        ]
    )


def local_ip_for(target_ip: str) -> str:
    """找出通往目標位址時本機會使用的 IP（多網卡環境下讓接收端拉得到我們）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target_ip, 9))
        return sock.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        sock.close()


class Subscriber:
    """單一接收端的佇列；超過上限時丟最舊資料。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, max_seconds: float = 2.0) -> None:
        self.loop = loop
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.max_bytes = int(max_seconds * BYTES_PER_SECOND)
        self.buffered = 0
        self.dropped = 0

    def push_threadsafe(self, data: bytes) -> None:
        self.loop.call_soon_threadsafe(self._push, data)

    def _push(self, data: bytes) -> None:
        self.queue.put_nowait(data)
        self.buffered += len(data)
        while self.buffered > self.max_bytes and not self.queue.empty():
            old = self.queue.get_nowait()
            self.buffered -= len(old)
            self.dropped += len(old)

    async def get(self) -> bytes:
        data = await self.queue.get()
        self.buffered -= len(data)
        return data


class PcmBroadcaster:
    def __init__(self, loop: asyncio.AbstractEventLoop, config: Config) -> None:
        self.loop = loop
        self.config = config
        self._subs: set[Subscriber] = set()
        self.capture: Optional[WasapiLoopbackCapture] = None

    def subscribe(self) -> Subscriber:
        sub = Subscriber(self.loop)
        self._subs.add(sub)
        if self.capture is None:
            self.capture = WasapiLoopbackCapture(
                self._fanout,
                target_rate=SAMPLE_RATE,
                target_channels=CHANNELS,
                device_index=self.config.capture_device_index,
                quality=self.config.resample_quality,
                dither=self.config.dither,
                variable_rate=False,
            )
            self.capture.start()
            _LOGGER.info("HTTP 串流：開始擷取")
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subs.discard(sub)
        if not self._subs and self.capture is not None:
            self.capture.stop()
            self.capture = None
            _LOGGER.info("HTTP 串流：沒有接收端，停止擷取")

    def _fanout(self, data: bytes) -> None:
        for sub in tuple(self._subs):
            sub.push_threadsafe(data)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)


class HttpStreamServer:
    """aiohttp 伺服器：/stream.wav（WAV）、/stream.pcm（raw s16le）、/（狀態）。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, broadcaster: PcmBroadcaster, port: int) -> None:
        self.loop = loop
        self.broadcaster = broadcaster
        self.port = port
        self._runner: Optional[web.AppRunner] = None
        self.clients: dict[str, int] = {}

    async def start(self) -> None:
        if self._runner is not None:
            return
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/stream.wav", self._stream_wav)
        app.router.add_get("/stream.pcm", self._stream_pcm)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port, reuse_address=True)
        await site.start()
        _LOGGER.info("HTTP 串流伺服器：port %d", self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @property
    def running(self) -> bool:
        return self._runner is not None

    def url_for(self, target_ip: str, fmt: str = "wav") -> str:
        return f"http://{local_ip_for(target_ip)}:{self.port}/stream.{fmt}"

    async def _index(self, request: web.Request) -> web.Response:
        return web.Response(
            text=(
                f"{APP_NAME} {__version__}\n"
                f"WAV: /stream.wav  (audio/wav, {SAMPLE_RATE} Hz / 16-bit / {CHANNELS} ch)\n"
                f"PCM: /stream.pcm  (audio/L16 little-endian)\n"
                f"clients: {len(self.clients)}\n"
            )
        )

    async def _stream_wav(self, request: web.Request) -> web.StreamResponse:
        return await self._stream(request, "audio/wav", wav_header())

    async def _stream_pcm(self, request: web.Request) -> web.StreamResponse:
        return await self._stream(request, f"audio/L16;rate={SAMPLE_RATE};channels={CHANNELS}", b"")

    async def _stream(self, request: web.Request, content_type: str, header: bytes) -> web.StreamResponse:
        peer = request.remote or "?"
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": content_type,
                "Cache-Control": "no-cache, no-store",
                "Connection": "close",
                # 部分 DLNA 接收端會檢查這些標頭
                "transferMode.dlna.org": "Streaming",
                "contentFeatures.dlna.org": "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000",
                "Accept-Ranges": "none",
                "icy-name": f"{APP_NAME} - {socket.gethostname()}",
            },
        )
        await response.prepare(request)
        sub = self.broadcaster.subscribe()
        self.clients[peer] = self.clients.get(peer, 0) + 1
        _LOGGER.info("HTTP 串流：%s 開始接收 %s", peer, content_type)
        try:
            if header:
                await response.write(header)
            # 先送一小段靜音讓接收端的解碼器立刻啟動，不必等第一個真實區塊
            await response.write(bytes(int(0.05 * BYTES_PER_SECOND)))
            while True:
                data = await sub.get()
                await response.write(data)
        except (ConnectionResetError, asyncio.CancelledError, ConnectionError):
            pass
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.debug("HTTP 串流 %s 結束：%s", peer, exc)
        finally:
            self.broadcaster.unsubscribe(sub)
            self.clients[peer] -= 1
            if self.clients[peer] <= 0:
                del self.clients[peer]
            _LOGGER.info("HTTP 串流：%s 結束（丟棄 %d bytes）", peer, sub.dropped)
        return response
