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

_state = {"latency_ms": 1500, "latency_min_ms": 250, "latency_max_ms": 2000, "sample_rate": None, "applied": False}
_stats = {"retransmit_requests": 0, "retransmit_packets": 0}


def reset_stats() -> None:
    _stats["retransmit_requests"] = 0
    _stats["retransmit_packets"] = 0


def stats() -> dict[str, int]:
    return dict(_stats)


def set_latency_ms(latency_ms: int) -> None:
    _state["latency_ms"] = max(100, min(int(latency_ms), 3000))


def latency_frames(sample_rate: int) -> int:
    return int(sample_rate * _state["latency_ms"] / 1000)


def set_sample_rate(rate) -> None:
    """AirPlay 串流取樣率（None = 依接收端 mDNS 宣告，通常 44100）。實測 HomePod 可收 48000。"""
    _state["sample_rate"] = int(rate) if rate else None


def set_latency_min_ms(latency_min_ms: int) -> None:
    """AirPlay 2 SETUP 的 latencyMin（pyatv 寫死 11025 frames = 250 ms）。

    接收端可能以此決定最低緩衝量；降低它是壓縮總延遲的實驗旋鈕。"""
    _state["latency_min_ms"] = max(10, min(int(latency_min_ms), 2000))


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

    # 3. RTSP SETUP：把 AirPlay 2 串流描述裡的 latencyMin / latencyMax 換成可設定的值
    from pyatv.support.rtsp import RtspSession  # pylint: disable=import-outside-toplevel

    original_setup = RtspSession.setup

    async def patched_setup(self, headers=None, body=None):
        if isinstance(body, dict) and body.get("streams"):
            stream = body["streams"][0]
            if "latencyMin" in stream:
                if _state["sample_rate"]:
                    from .formats import AUDIO_FORMAT_FLAG  # pylint: disable=import-outside-toplevel

                    stream["sr"] = int(_state["sample_rate"])
                    stream["audioFormat"] = AUDIO_FORMAT_FLAG.get(int(_state["sample_rate"]), 0x800)
                sample_rate = int(stream.get("sr", 44100))
                stream["latencyMin"] = int(sample_rate * _state["latency_min_ms"] / 1000)
                stream["latencyMax"] = max(
                    stream["latencyMin"], int(sample_rate * _state["latency_max_ms"] / 1000)
                )
                _LOGGER.debug("SETUP latencyMin=%d latencyMax=%d frames", stream["latencyMin"], stream["latencyMax"])
        return await original_setup(self, headers=headers, body=body)

    RtspSession.setup = patched_setup  # type: ignore[assignment]

    # 3b. StreamClient 的輸出格式：覆寫取樣率（接收端 mDNS 宣告 44100，但 realtime 實測可收 48000）
    from pyatv.protocols.raop.stream_client import StreamClient  # pylint: disable=import-outside-toplevel

    original_update = StreamClient._update_output_properties  # pylint: disable=protected-access

    def patched_update(self, properties):
        original_update(self, properties)
        if _state["sample_rate"]:
            self.context.sample_rate = int(_state["sample_rate"])

    StreamClient._update_output_properties = patched_update  # type: ignore[assignment]  # pylint: disable=protected-access

    # 4. 統計接收端要求重傳的次數（Wi-Fi 抖動的直接指標）
    from pyatv.protocols.raop.stream_client import ControlClient  # pylint: disable=import-outside-toplevel

    original_retransmit = ControlClient._retransmit_lost_packets  # pylint: disable=protected-access

    def patched_retransmit(self, request, addr):
        _stats["retransmit_requests"] += 1
        _stats["retransmit_packets"] += int(getattr(request, "lost_packets", 1) or 1)
        return original_retransmit(self, request, addr)

    ControlClient._retransmit_lost_packets = patched_retransmit  # type: ignore[assignment]  # pylint: disable=protected-access
