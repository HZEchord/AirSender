"""Streamer 與各輸出端共用的型別（獨立模組以避免循環匯入）。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class State(Enum):
    IDLE = "閒置"
    CONNECTING = "連線中"
    STREAMING = "串流中"
    RECONNECTING = "重新連線中"


@dataclass(frozen=True)
class Device:
    """一個可輸出的接收端。kind：airplay / cast / dlna / snapcast。"""

    name: str
    identifier: str
    address: str
    model: str
    kind: str = "airplay"
    url: str = ""  # DLNA：描述檔 URL；Snapcast：port

    @property
    def label(self) -> str:
        return f"{self.name}（{self.model}，{self.address}）"
