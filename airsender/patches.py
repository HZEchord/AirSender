"""對 pyatv 的兩個小補丁。

1. open_source：讓 stream_file() 接受我們的 LiveAudioSource（即時音訊），
   而不是只接受檔案 / 緩衝（那條路會經過 miniaudio 解碼）。
2. StreamContext.reset：pyatv 把接收端延遲寫死為 1.5 秒（22050 + 44100 frames）。
   這裡改成可設定，讓使用者在延遲與穩定度之間取捨。
"""

from __future__ import annotations

import logging

import pyatv.protocols.raop as raop_module
from pyatv.protocols.raop.protocols import StreamContext

from .live_source import LiveAudioSource

_LOGGER = logging.getLogger(__name__)

_state = {"latency_ms": 1500, "applied": False}


def set_latency_ms(latency_ms: int) -> None:
    _state["latency_ms"] = max(100, min(int(latency_ms), 3000))


def latency_frames(sample_rate: int) -> int:
    return int(sample_rate * _state["latency_ms"] / 1000)


def apply() -> None:
    if _state["applied"]:
        return
    _state["applied"] = True

    original_open_source = raop_module.open_source

    async def patched_open_source(file, sample_rate, channels, sample_size):
        if isinstance(file, LiveAudioSource):
            file.configure(sample_rate, channels, sample_size)
            await file.wait_ready()
            return file
        return await original_open_source(file, sample_rate, channels, sample_size)

    raop_module.open_source = patched_open_source  # type: ignore[assignment]

    original_reset = StreamContext.reset

    def patched_reset(self: StreamContext) -> None:
        original_reset(self)
        self.latency = latency_frames(self.sample_rate)
        _LOGGER.debug(
            "接收端延遲設為 %d frames（%d ms）", self.latency, _state["latency_ms"]
        )

    StreamContext.reset = patched_reset  # type: ignore[assignment]
