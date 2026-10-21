"""ByteRingBuffer 的時間標記：pop_timed 要能回推取出資料最後一個位元組的擷取時刻。"""

from __future__ import annotations

import unittest

from airsender.ringbuffer import ByteRingBuffer


class RingTimedTests(unittest.TestCase):
    def test_pop_timed_tracks_push_marks(self):
        ring = ByteRingBuffer(1000)
        ring.push(b"a" * 100, 1_000)  # 位元組 0..99 在 t=1000 抵達
        ring.push(b"b" * 100, 2_000)  # 100..199 在 t=2000
        data, ts, remaining = ring.pop_timed(50)
        self.assertEqual((data, ts, remaining), (b"a" * 50, 1_000, 50))
        data, ts, remaining = ring.pop_timed(100)  # 50..149：最後一個位元組屬於第二次 push，之後還剩 50
        self.assertEqual((len(data), ts, remaining), (100, 2_000, 50))
        data, ts, remaining = ring.pop_timed(50)
        self.assertEqual((ts, remaining), (2_000, 0))
        self.assertEqual(ring.pop_timed(10), (b"", None, 0))

    def test_overflow_keeps_accounting(self):
        ring = ByteRingBuffer(150)
        ring.push(b"a" * 100, 1)
        ring.push(b"b" * 100, 2)  # 溢位：前 50 個 a 被丟掉
        self.assertEqual(len(ring), 150)
        data, ts, remaining = ring.pop_timed(50)
        self.assertEqual((data, ts, remaining), (b"a" * 50, 1, 0))
        data, ts, remaining = ring.pop_timed(100)
        self.assertEqual((data, ts, remaining), (b"b" * 100, 2, 0))

    def test_push_without_timestamp(self):
        ring = ByteRingBuffer(100)
        ring.push(b"x" * 10)
        self.assertEqual(ring.pop_timed(5), (b"x" * 5, None, 0))
        self.assertEqual(ring.pop(5), b"x" * 5)


if __name__ == "__main__":
    unittest.main()
