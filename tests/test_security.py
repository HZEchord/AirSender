"""安全性修正的回歸測試：更新驗證、PTP 偽造訊息、HTTP 串流存取控制、名稱清理。"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from airsender import ptp, updater
from airsender.httpstream import HttpStreamServer, PcmBroadcaster
from airsender.streamer_types import Device


def _release(**kw):
    base = dict(
        version="9.9.9",
        tag="v9.9.9",
        page_url="https://github.com/HZEchord/AirSender/releases/tag/v9.9.9",
        notes="",
        asset_name="AirSender-Setup-9.9.9.exe",
        asset_url="https://github.com/HZEchord/AirSender/releases/download/v9.9.9/AirSender-Setup-9.9.9.exe",
        asset_api_url="https://api.github.com/repos/HZEchord/AirSender/releases/assets/1",
        asset_size=100,
        digest="sha256:" + "ab" * 32,
    )
    base.update(kw)
    return updater.Release(**base)


class UpdaterTests(unittest.TestCase):
    def test_missing_or_bad_digest_is_rejected(self):
        for digest in (None, "", "md5:abc", "sha256:xyz", "sha256:" + "a" * 10):
            with self.subTest(digest=digest):
                with self.assertRaises(RuntimeError):
                    updater.download(_release(digest=digest))

    def test_non_github_url_is_rejected(self):
        with self.assertRaises(RuntimeError):
            updater.download(_release(asset_url="https://evil.example/AirSender-Setup-9.9.9.exe"))
        with self.assertRaises(RuntimeError):
            updater.download(_release(asset_url="http://github.com/x/AirSender-Setup-9.9.9.exe"))

    def test_asset_name_with_path_characters_is_rejected(self):
        self.assertIsNone(updater.ASSET_PATTERN.match("AirSender-Setup-..\\..\\x.exe"))
        self.assertIsNone(updater.ASSET_PATTERN.match("AirSender-Setup-a/b.exe"))
        self.assertIsNotNone(updater.ASSET_PATTERN.match("AirSender-Setup-0.14.0.exe"))
        with self.assertRaises(RuntimeError):
            updater.download(_release(asset_name="AirSender-Setup-..\\x.exe"))

    def test_launch_refuses_modified_installer(self):
        import tempfile

        d = Path(tempfile.mkdtemp())
        exe = d / "AirSender-Setup-9.9.9.exe"
        exe.write_bytes(b"not the file that was verified")
        self.assertFalse(updater.launch_installer(exe, "ab" * 32))
        self.assertFalse(exe.exists(), "被改過的檔案要刪掉")


class PtpSpoofTests(unittest.TestCase):
    def setUp(self):
        self.clock = ptp.PtpClock("10.0.0.9", local_ip="10.0.0.2")
        self.master = bytes.fromhex("a4cf99b8177c0008")
        self.other = bytes.fromhex("deadbeefdeadbeef")

    def _feed(self, data: bytes, port=ptp.PORT_EVENT, ip="10.0.0.9"):
        self.clock._on_datagram(port, data, (ip, port), self.clock.clock.now_ns())  # pylint: disable=protected-access

    def test_timing_ignored_before_announce_and_from_other_clock(self):
        self._feed(ptp.build_sync(self.master, 1))
        self._feed(ptp.build_follow_up(self.master, 1, 10**18), ptp.PORT_GENERAL)
        self.assertEqual(self.clock.estimator.samples_m, 0, "沒有 Announce 前不採用計時訊息")
        self._feed(ptp.build_announce(self.master, 1), ptp.PORT_GENERAL)
        self.assertEqual(self.clock.master_id, self.master)
        self._feed(ptp.build_sync(self.other, 2))
        self._feed(ptp.build_follow_up(self.other, 2, 10**18), ptp.PORT_GENERAL)
        self.assertEqual(self.clock.estimator.samples_m, 0, "別的 clockIdentity 的訊息要丟掉")
        self._feed(ptp.build_sync(self.master, 3))
        self._feed(
            ptp.build_follow_up(self.master, 3, self.clock.clock.now_ns() - 1_000_000), ptp.PORT_GENERAL
        )
        self.assertEqual(self.clock.estimator.samples_m, 1)

    def test_wrong_source_ip_and_short_packets_ignored(self):
        self._feed(ptp.build_announce(self.master, 1), ptp.PORT_GENERAL, ip="10.0.0.66")
        self.assertIsNone(self.clock.master_id)
        self._feed(ptp.build_announce(self.master, 1), ptp.PORT_GENERAL)
        self._feed(ptp.build_sync(self.master, 5))
        self._feed(ptp.build_follow_up(self.master, 5, 10**18)[:40], ptp.PORT_GENERAL)  # 截斷
        self.assertEqual(self.clock.estimator.samples_m, 0)
        self._feed(b"\x18\x02" + b"\x00" * 10)  # 太短
        self._feed(ptp.build_sync(self.master, 6).replace(b"\x02", b"\x01", 1))  # 版本 1

    def test_estimator_rejects_outliers(self):
        est = ptp.OffsetEstimator()
        for i in range(6):
            est.add_master_to_local(i * 125_000_000, 5_000_000 + i)
        est.add_master_to_local(10**9, 5_000_000 - 10_000_000_000)  # 偽造：提前 10 秒
        self.assertEqual(est.rejected, 1)
        self.assertEqual(est.offset_ns, 5_000_000)

    def test_reply_rate_limited(self):
        sent = []
        self.clock._send = lambda port, data: sent.append(port)  # type: ignore[method-assign]  # pylint: disable=protected-access
        now = self.clock.clock.now_ns()
        for _ in range(20):
            self.clock._on_datagram(
                ptp.PORT_GENERAL, ptp.build_announce(self.master, 1), ("10.0.0.9", 320), now
            )  # pylint: disable=protected-access
        self.assertEqual(len(sent), 1, "250 ms 內只回一次")


class HttpAccessTests(unittest.TestCase):
    def test_token_in_url_and_client_restriction(self):
        loop = asyncio.new_event_loop()
        server = HttpStreamServer(loop, PcmBroadcaster(loop, None), 8765)
        self.assertIn(f"/{server.token}/stream.wav", server.url_for("10.0.0.5"))
        self.assertGreaterEqual(len(server.token), 16)

        class Req:
            def __init__(self, remote):
                self.remote = remote

        server.allowed_ips = {"10.0.0.5"}
        self.assertTrue(server._client_allowed(Req("10.0.0.5")))  # pylint: disable=protected-access
        self.assertFalse(server._client_allowed(Req("10.0.0.6")))  # pylint: disable=protected-access
        self.assertFalse(server._client_allowed(Req(None)))  # pylint: disable=protected-access
        server.allow_any_client = True
        self.assertTrue(server._client_allowed(Req("10.0.0.6")))  # pylint: disable=protected-access
        loop.close()


class NameCleaningTests(unittest.TestCase):
    def test_control_characters_and_length(self):
        d = Device(
            name="Living\r\nRoom\x1b[31m" + "x" * 200, identifier="i", address="1.2.3.4\n", model="HomePod\t"
        )
        self.assertNotIn("\n", d.name)
        self.assertNotIn("\x1b", d.name)
        self.assertEqual(len(d.name), 64)
        self.assertEqual(d.address, "1.2.3.4")
        self.assertEqual(d.model, "HomePod")


if __name__ == "__main__":
    unittest.main()
