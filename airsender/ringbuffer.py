"""執行緒安全的位元組環形緩衝；滿了會丟掉最舊的資料，確保延遲不會無限累積。"""

from __future__ import annotations

import threading


class ByteRingBuffer:
    def __init__(self, capacity: int) -> None:
        self._buf = bytearray()
        self._cap = max(1, capacity)
        self._lock = threading.Lock()
        self.dropped_bytes = 0

    @property
    def capacity(self) -> int:
        return self._cap

    def push(self, data: bytes) -> None:
        with self._lock:
            self._buf += data
            overflow = len(self._buf) - self._cap
            if overflow > 0:
                del self._buf[:overflow]
                self.dropped_bytes += overflow

    def pop(self, nbytes: int) -> bytes:
        with self._lock:
            out = bytes(self._buf[:nbytes])
            del self._buf[:nbytes]
            return out

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)
