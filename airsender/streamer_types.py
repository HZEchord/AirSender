"""Streamer 與各輸出端共用的型別（獨立模組以避免循環匯入）。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class State(Enum):
    IDLE = "閒置"
    CONNECTING = "連線中"
    STREAMING = "串流中"
    RECONNECTING = "重新連線中"
    WAITING = "待命（接收端被占用）"
    STANDBY = "待命（無聲）"


@dataclass(frozen=True)
class Device:
    """一個可輸出的接收端。kind：airplay / cast / dlna / snapcast。"""

    name: str
    identifier: str
    address: str
    model: str
    kind: str = "airplay"
    url: str = ""  # DLNA：描述檔 URL；Snapcast：port

    def __post_init__(self) -> None:
        # 名稱 / 型號來自區網（mDNS / SSDP），只留可列印字元並限制長度，避免記錄注入與版面被撐爆
        object.__setattr__(self, "name", clean_text(self.name, 64))
        object.__setattr__(self, "model", clean_text(self.model, 64))
        object.__setattr__(self, "address", clean_text(self.address, 64))

    @property
    def label(self) -> str:
        return f"{self.name}（{self.model}，{self.address}）"


def clean_text(value: object, limit: int) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isprintable() and ch not in "\r\n\t")
    return text[:limit]
