"""Streamer 的監看邏輯：無聲待命、系統喚醒重連、接收端被占用時靜默待命（不需要真實接收端）。"""

from __future__ import annotations

import asyncio
import time
import unittest

from airsender import streamer as sm
from airsender.capture import CaptureStatus
from airsender.config import Config
from airsender.streamer_types import Device, State

DEVICE = Device(name="測試", identifier="x", address="127.0.0.1", model="HomePod", kind="airplay")


class _Base(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.idle_standby_minutes = 1
        self.states: list[State] = []
        self.s = sm.Streamer(self.cfg, lambda st, _m: self.states.append(st), persist=False)
        self.s.start_loop()

    def tearDown(self):
        self.s.shutdown()

    def run_in_loop(self, coro, timeout=10.0):
        return asyncio.run_coroutine_threadsafe(coro, self.s._loop).result(timeout)  # pylint: disable=protected-access


class IdleTimeoutTests(_Base):
    def test_idle_timeout_only_when_streaming_and_silent(self):
        s = self.s
        now = time.monotonic()
        s.state = State.IDLE
        s._streaming_since = now - 600  # pylint: disable=protected-access
        self.assertFalse(s._idle_timeout_reached(now))  # pylint: disable=protected-access
        s.state = State.STREAMING
        self.assertTrue(s._idle_timeout_reached(now))  # pylint: disable=protected-access
        # 最近有聲音 → 不待命
        cap = CaptureStatus(last_sound_monotonic=now - 5)
        s.capture_status_override = cap
        type(s).capture_status = property(lambda self_: cap)
        try:
            self.assertFalse(s._idle_timeout_reached(now))  # pylint: disable=protected-access
        finally:
            del type(s).capture_status
        self.cfg.idle_standby_minutes = 0
        self.assertFalse(s._idle_timeout_reached(now))  # pylint: disable=protected-access


class SuperviseTests(_Base):
    def test_idle_raises_and_cancels_stream(self):
        s = self.s
        cancelled = []

        async def fake_run_once(_device):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        s._run_once = fake_run_once  # type: ignore[method-assign]
        s._idle_timeout_reached = lambda _now: True  # type: ignore[method-assign]
        with self.assertRaises(sm._IdleStandby):  # pylint: disable=protected-access
            self.run_in_loop(s._run_supervised(DEVICE))  # pylint: disable=protected-access
        self.assertEqual(cancelled, [True])

    def test_sleep_gap_raises_system_resumed(self):
        s = self.s

        async def fake_run_once(_device):
            await asyncio.sleep(30)

        s._run_once = fake_run_once  # type: ignore[method-assign]
        old = sm.SLEEP_GAP_SECONDS
        sm.SLEEP_GAP_SECONDS = -1.0  # 任何停頓都算睡眠
        try:
            with self.assertRaises(sm._SystemResumed):  # pylint: disable=protected-access
                self.run_in_loop(s._run_supervised(DEVICE))  # pylint: disable=protected-access
        finally:
            sm.SLEEP_GAP_SECONDS = old

    def test_normal_end_returns(self):
        s = self.s

        async def fake_run_once(_device):
            return None

        s._run_once = fake_run_once  # type: ignore[method-assign]
        self.assertIsNone(self.run_in_loop(s._run_supervised(DEVICE)))  # pylint: disable=protected-access


class BusyYieldTests(_Base):
    def test_waits_while_receiver_busy_without_reconnecting(self):
        s = self.s
        busy = [True, True, False]
        connects = []

        async def fake_run_once(_device):
            connects.append(time.monotonic())
            if len(connects) >= 2:
                await asyncio.sleep(30)  # 第二次連線後停住，測試結束時取消

        async def fake_busy(_device):
            return busy.pop(0) if busy else False

        s._run_once = fake_run_once  # type: ignore[method-assign]
        s._receiver_busy = fake_busy  # type: ignore[method-assign]
        old = sm.BUSY_POLL_SECONDS
        sm.BUSY_POLL_SECONDS = 0.05
        try:
            fut = asyncio.run_coroutine_threadsafe(s._run(DEVICE), s._loop)  # pylint: disable=protected-access
            time.sleep(1.5)
            fut.cancel()
        finally:
            sm.BUSY_POLL_SECONDS = old
        self.assertIn(State.WAITING, self.states)
        self.assertEqual(len(connects), 2, "被占用期間不得重連")
        self.assertGreater(self.states.index(State.WAITING), 0)


if __name__ == "__main__":
    unittest.main()
