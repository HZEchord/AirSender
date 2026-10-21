"""執行緒安全的位元組環形緩衝；滿了會丟掉最舊的資料，確保延遲不會無限累積。"""

from __future__ import annotations

import collections
import threading
from typing import Optional


class ByteRingBuffer:
    def __init__(self, capacity: int) -> None:
        self._buf = bytearray()
        self._cap = max(1, capacity)
        self._lock = threading.Lock()
        self.dropped_bytes = 0
        # 時間標記：每次 push 記 (累計推入位元組數, 時間戳)，pop_timed 可回推被取出資料的擷取時刻
        self._pushed = 0
        self._popped = 0
        self._marks: collections.deque[tuple[int, int]] = collections.deque()

    @property
    def capacity(self) -> int:
        return self._cap

    def push(self, data: bytes, timestamp_ns: Optional[int] = None) -> None:
        with self._lock:
            self._buf += data
            self._pushed += len(data)
            if timestamp_ns is not None:
                self._marks.append((self._pushed, timestamp_ns))
            overflow = len(self._buf) - self._cap
            if overflow > 0:
                del self._buf[:overflow]
                self.dropped_bytes += overflow
                self._popped += overflow
                self._drop_marks()

    def _drop_marks(self) -> None:
        while self._marks and self._marks[0][0] < self._popped:
            self._marks.popleft()

    def pop_timed(self, nbytes: int) -> tuple[bytes, Optional[int], int]:
        """取出 nbytes，並回傳 (資料, 含最後一個位元組的那次 push 的時間戳, 該次 push 在這之後還剩的位元組數)。

        呼叫端以「剩餘位元組 / 位元率」把時間戳往前推，就得到最後一個樣本的擷取時刻。
        """
        with self._lock:
            out = bytes(self._buf[:nbytes])
            del self._buf[:nbytes]
            self._popped += len(out)
            self._drop_marks()
            if not self._marks or not out:
                return out, None, 0
            cum_end, ts = self._marks[0]
            return out, ts, max(0, cum_end - self._popped)

    def pop(self, nbytes: int) -> bytes:
        with self._lock:
            out = bytes(self._buf[:nbytes])
            del self._buf[:nbytes]
            return out

    def clear(self) -> None:
        with self._lock:
            self._popped += len(self._buf)
            self._buf.clear()
            self._marks.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)
