"""buffered 推送迴圈的假接收端測試：不用網路與 HomePod，驗證封包時間軸、補靜音與控制器行為。"""

from __future__ import annotations

import asyncio
import time
import unittest

import numpy as np
from chacha20poly1305_reuseable import ChaCha20Poly1305Reusable

from airsender import buffered
from airsender.live_source import DriftController

RATE = 48000
FRAME = buffered.FRAMES_PER_PACKET


class FakePtp:
    """固定偏移的 PTP：主時鐘 = 本機時鐘 − 1 秒。"""

    def __init__(self):
        self.master_id = bytes.fromhex("a4cf99b8177c0008")
        self.locked = True
        self.base = time.time_ns() - time.perf_counter_ns()
        self.offset = 1_000_000_000

        class _Clock:
            def __init__(self_, outer):
                self_.outer = outer

            def now_ns(self_):
                return time.perf_counter_ns() + self_.outer.base

        self.clock = _Clock(self)

        class _Est:
            jitter_ns = 200_000

        self.estimator = _Est()

    def local_from_perf_ns(self, perf_ns):
        return perf_ns + self.base

    def to_master_ns(self, local_ns):
        return local_ns - self.offset

    def master_now_ns(self):
        return self.to_master_ns(self.clock.now_ns())

    def summary(self):
        return "fake"


class FakeSource:
    """以真實時間節奏產生資料：每 10 ms 一塊，可暫停模擬 Windows 無聲。"""

    frame_size = 8  # 24-bit → int32 容器，立體聲

    def __init__(self):
        self.drift = DriftController(1.0)
        self.paused = False
        self._pending = bytearray()
        self._stamps: list[tuple[int, int]] = []  # (累計位元組, perf_ns)
        self._total = 0
        self._popped = 0
        self.started = False

    def feed_realtime(self) -> None:
        """依實際經過的時間補足應該產生的樣本數（Windows 的 asyncio.sleep 粒度約 15 ms，不能假設每次 10 ms）。"""
        now = time.perf_counter()
        if not hasattr(self, "_t0"):
            self._t0 = now
            self._fed = 0
        due = int((now - self._t0) * RATE)
        if self.paused:
            self._t0 += 0  # 暫停時不產生資料，但時間照走（之後不補）
            self._fed = due
            return
        if due > self._fed:
            self.feed(due - self._fed)
            self._fed = due

    def feed(self, frames: int) -> None:
        if self.paused:
            return
        data = (np.arange(frames * 2, dtype=np.int32) % 1000).tobytes()
        self._pending += data
        self._total += len(data)
        self._stamps.append((self._total, time.perf_counter_ns()))

    def trim_to_ms(self, _ms):
        self._pending.clear()
        self._popped = self._total
        self._stamps.clear()

    def mark_started(self):
        self.started = True

    def pop_frames_timed(self, nframes):
        need = nframes * self.frame_size
        if len(self._pending) < need:
            return None
        data = bytes(self._pending[:need])
        del self._pending[:need]
        self._popped += need
        while len(self._stamps) > 1 and self._stamps[0][0] < self._popped:
            self._stamps.pop(0)
        ts = self._stamps[0][1] if self._stamps else time.perf_counter_ns()
        return data, ts


class FakeWriter:
    def __init__(self):
        self.chunks: list[bytes] = []

    def write(self, data):
        self.chunks.append(bytes(data))

    async def drain(self):
        return None

    def get_extra_info(self, _name):
        return None


class FakeReader:
    @staticmethod
    def at_eof():
        return False


def _make_stream(lead_ms: int):
    st = buffered.BufferedAirPlayStream.__new__(buffered.BufferedAirPlayStream)
    st.sample_rate = RATE
    st.bit_depth = 24
    st.lead_ms = lead_ms
    st.prefill_ms = 0
    st.on_streaming = None
    st.stats = buffered.BufferedStats()
    st.ptp = FakePtp()
    st._stop = asyncio.Event()  # pylint: disable=protected-access
    st._shk = bytes(range(32))  # pylint: disable=protected-access
    st._cipher = ChaCha20Poly1305Reusable(st._shk)  # pylint: disable=protected-access
    st._nonce_counter = 0  # pylint: disable=protected-access
    st._seq = 100  # pylint: disable=protected-access
    st._rtp_time = 5000  # pylint: disable=protected-access
    st._ssrc = 0x15000000  # pylint: disable=protected-access
    st._anchor_rtp = None  # pylint: disable=protected-access
    st._anchor_master_ns = None  # pylint: disable=protected-access
    st._volume_db = None  # pylint: disable=protected-access
    st._rtsp = None  # pylint: disable=protected-access
    st._writer = FakeWriter()  # pylint: disable=protected-access
    anchors = []

    async def fake_anchor(rtp_time, master_ns, rate=1):
        anchors.append((rtp_time, master_ns, rate))
        st._anchor_rtp = rtp_time & 0xFFFFFFFF  # pylint: disable=protected-access
        st._anchor_master_ns = master_ns  # pylint: disable=protected-access
        st.stats.anchor_count += 1

    st._set_rate_anchor = fake_anchor  # type: ignore[method-assign]  # pylint: disable=protected-access
    return st, anchors


def _parse_packets(chunks: list[bytes]):
    out = []
    for c in chunks:
        total = int.from_bytes(c[:2], "big")
        assert total == len(c)
        pkt = c[2:]
        seq = int.from_bytes(pkt[0:4], "big") & 0x7FFFFF
        rtp = int.from_bytes(pkt[4:8], "big")
        out.append((seq, rtp, len(pkt) - 12 - 16 - 8))
    return out


class BufferedPumpTests(unittest.TestCase):
    def _run(self, lead_ms: int, scenario) -> tuple:
        async def main():
            st, anchors = _make_stream(lead_ms)
            src = FakeSource()
            st.source = src
            pump = asyncio.ensure_future(st._pump(FakeReader()))  # pylint: disable=protected-access
            try:
                await scenario(src, st)
            finally:
                st.stop()
                await asyncio.wait_for(pump, 2.0)
            return st, src, anchors

        return asyncio.run(main())

    def test_continuous_audio_keeps_lead_and_timeline(self):
        async def scenario(src, st):
            for _ in range(60):  # 約 0.6–0.9 秒
                src.feed_realtime()
                await asyncio.sleep(0.01)
            self.assertGreater(st.stats.packets, 60)

        st, src, anchors = self._run(100, scenario)
        self.assertEqual(len(anchors), 1)
        self.assertTrue(src.started)
        pkts = _parse_packets(st._writer.chunks)  # pylint: disable=protected-access
        self.assertEqual(pkts[0][2], 0, "第一個是空負載的 primer")
        audio = pkts[1:]
        for a, b in zip(audio, audio[1:], strict=False):
            self.assertEqual((a[0] + 1) & 0x7FFFFF, b[0], "序號連續")
            self.assertEqual((a[1] + FRAME) & 0xFFFFFFFF, b[1], "時間戳每封包 +352")
        self.assertEqual(audio[0][1], anchors[0][0], "錨點 = 第一個音訊封包的 rtpTime")
        self.assertEqual(st.stats.silence_packets, 0)
        # HomePod 看到的提前量 ≈ 提前量（允許排程抖動）
        self.assertAlmostEqual(st.stats.lead_ms, 100, delta=40)

    def test_silence_is_filled_when_source_stops_and_controller_frozen(self):
        async def scenario(src, st):
            for _ in range(20):
                src.feed_realtime()
                await asyncio.sleep(0.01)
            adj_before = src.drift.adjustment
            src.paused = True
            await asyncio.sleep(0.4)  # 來源停了 400 ms
            self.assertGreater(st.stats.silence_packets, 30, "應補上接近 400 ms 的靜音")
            self.assertEqual(src.drift.adjustment, adj_before, "補靜音期間控制器不更新")
            lead = st.next_play_lead_ms()
            self.assertIsNotNone(lead)
            self.assertGreaterEqual(lead, 100 - 2 * FRAME * 1000 / RATE - 5)
            src.paused = False
            for _ in range(20):
                src.feed_realtime()
                await asyncio.sleep(0.01)

        st, _src, _anchors = self._run(100, scenario)
        pkts = _parse_packets(st._writer.chunks)  # pylint: disable=protected-access
        for a, b in zip(pkts[1:], pkts[2:], strict=False):
            self.assertEqual((a[1] + FRAME) & 0xFFFFFFFF, b[1], "補靜音不會打斷時間軸")

    def test_rtp_wraparound_is_masked(self):
        async def scenario(src, st):
            st._rtp_time = 0xFFFFFFFF - FRAME  # pylint: disable=protected-access
            for _ in range(10):
                src.feed_realtime()
                await asyncio.sleep(0.01)

        st, _src, _anchors = self._run(100, scenario)
        pkts = _parse_packets(st._writer.chunks)  # pylint: disable=protected-access
        rtps = [p[1] for p in pkts[1:]]
        self.assertTrue(any(r < FRAME * 4 for r in rtps), "時間戳應繞回 0 附近")
        self.assertIsNotNone(st.next_play_lead_ms())


if __name__ == "__main__":
    unittest.main()
