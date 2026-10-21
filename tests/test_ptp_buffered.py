"""PTP 訊息打包 / 解析、偏移估計，以及 buffered 封包框架（含解密驗證）。"""

from __future__ import annotations

import struct
import unittest

import numpy as np
from chacha20poly1305_reuseable import ChaCha20Poly1305Reusable

from airsender import alac, buffered, ptp


class PtpMessageTests(unittest.TestCase):
    def setUp(self):
        self.cid = bytes.fromhex("a4cf99fffeb77a77")

    def test_header_roundtrip_and_lengths(self):
        cases = {
            "announce": (ptp.build_announce(self.cid, 7), ptp.MSG_ANNOUNCE, 76),
            "signaling": (ptp.build_signaling(self.cid, 8), ptp.MSG_SIGNALING, 120),
            "sync": (ptp.build_sync(self.cid, 9), ptp.MSG_SYNC, 44),
            "follow_up": (ptp.build_follow_up(self.cid, 9, 10**18), ptp.MSG_FOLLOW_UP, 96),
            "delay_req": (ptp.build_delay_req(self.cid, 10, 5), ptp.MSG_DELAY_REQ, 44),
        }
        for name, (msg, kind, length) in cases.items():
            with self.subTest(name):
                h = ptp.parse_header(msg)
                self.assertEqual(h["type"], kind)
                self.assertEqual(h["version"], 2)
                self.assertEqual(h["length"], len(msg))
                self.assertEqual(len(msg), length)
                self.assertEqual(h["clock_id"], self.cid)
                self.assertEqual(msg[0] >> 4, 1, "transportSpecific = 1")

    def test_announce_fields(self):
        msg = ptp.build_announce(self.cid, 1)
        self.assertEqual(msg[47], ptp.OUR_PRIORITY1)
        self.assertEqual(msg[48], ptp.OUR_CLOCK_CLASS)
        self.assertEqual(msg[53:61], self.cid, "grandmasterIdentity = 自己")
        self.assertEqual(struct.unpack(">h", msg[44:46])[0], ptp.UTC_OFFSET)

    def test_timestamp_roundtrip(self):
        for ns in (0, 999_999_999, 1_792_588_102_883_449_500, (1 << 47) * 10**9 + 1):
            self.assertEqual(ptp.unpack_timestamp(ptp.pack_timestamp(ns)), ns)

    def test_follow_up_carries_timestamp_and_correction(self):
        t1 = 1_700_000_000_123_456_789
        msg = ptp.build_follow_up(self.cid, 3, t1)
        self.assertEqual(ptp.unpack_timestamp(msg[34:44]), t1)
        self.assertEqual(ptp.parse_header(msg)["correction_ns"], 0.0)
        # Apple clock-identity TLV 在最後：org 00:0d:93、subtype 4、clock id
        self.assertEqual(msg[-16:-10], b"\x00\x0d\x93\x00\x00\x04")
        self.assertEqual(msg[-10:-2], self.cid)

    def test_delay_resp_echoes_request_identity(self):
        req = ptp.build_delay_req(bytes(8), 42, 1)
        hdr = ptp.parse_header(req)
        resp = ptp.build_delay_resp(self.cid, hdr, 123, req[20:30])
        self.assertEqual(ptp.parse_header(resp)["seq"], 42)
        self.assertEqual(resp[44:54], req[20:30])
        self.assertEqual(ptp.unpack_timestamp(resp[34:44]), 123)


class OffsetEstimatorTests(unittest.TestCase):
    def test_offset_uses_minimum_delay_samples(self):
        est = ptp.OffsetEstimator()
        offset = 5_000_000_000  # 本機比主時鐘快 5 s
        delay = 300_000  # 單向 0.3 ms
        now = 10**12
        for i in range(10):
            jitter = (i % 3) * 2_000_000  # 偶爾多 2–4 ms 的抖動
            est.add_master_to_local(now + i * 125_000_000, offset + delay + jitter)
            est.add_local_to_master(now + i * 125_000_000, -offset + delay + jitter)
        self.assertTrue(est.ready)
        self.assertEqual(est.offset_ns, offset)
        self.assertEqual(est.path_delay_ns, delay)
        self.assertEqual(est.jitter_ns, 4_000_000)

    def test_window_trims_old_samples(self):
        est = ptp.OffsetEstimator(window_ns=1_000_000_000)
        est.add_master_to_local(0, 100)
        est.add_master_to_local(2_000_000_000, 200)
        self.assertEqual(est.offset_ns, 200)

    def test_without_delay_resp_uses_m_only(self):
        est = ptp.OffsetEstimator()
        est.add_master_to_local(0, 777)
        est.add_master_to_local(1, 999)
        self.assertEqual(est.offset_ns, 777)
        self.assertIsNone(est.path_delay_ns)


class BufferedPacketTests(unittest.TestCase):
    def test_network_time_fields(self):
        secs, frac = buffered.network_time_fields(1_792_588_102_500_000_000)
        self.assertEqual(secs, 1_792_588_102)
        self.assertEqual(frac, -(1 << 63), "0.5 s = 2^63，以有號 64-bit 表示為負")
        secs, frac = buffered.network_time_fields(10_250_000_000)
        self.assertEqual((secs, frac), (10, 1 << 62))

    def test_u64_to_plist(self):
        self.assertEqual(buffered._u64_to_plist(0xA4CF99B8177C0008), -6570864315363229688)  # pylint: disable=protected-access
        self.assertEqual(buffered._u64_to_plist(5), 5)  # pylint: disable=protected-access

    def test_packet_framing_and_decrypt(self):
        stream = buffered.BufferedAirPlayStream.__new__(buffered.BufferedAirPlayStream)
        stream._shk = bytes(range(32))  # pylint: disable=protected-access
        stream._cipher = ChaCha20Poly1305Reusable(stream._shk)  # pylint: disable=protected-access
        stream._nonce_counter = 3  # pylint: disable=protected-access
        stream._seq = 0x7FFFFF  # pylint: disable=protected-access
        stream._rtp_time = 0xDEADBEEF  # pylint: disable=protected-access
        stream._ssrc = 0x15000000  # pylint: disable=protected-access
        payload = alac.pack_verbatim_frame(np.zeros((352, 2), dtype=np.int32), 24)
        frame = stream._packet(payload)  # pylint: disable=protected-access

        total = int.from_bytes(frame[:2], "big")
        self.assertEqual(total, len(frame))
        packet = frame[2:]
        self.assertEqual(packet[0], 0x80)
        self.assertEqual(int.from_bytes(packet[0:4], "big") & 0x7FFFFF, 0x7FFFFF)
        self.assertEqual(int.from_bytes(packet[4:8], "big"), 0xDEADBEEF)
        self.assertEqual(int.from_bytes(packet[8:12], "big"), 0x15000000)
        nonce8 = packet[-8:]
        self.assertEqual(int.from_bytes(nonce8, "little"), 3)
        # 以接收端的規則解密：key = shk，nonce = 0⁴ ‖ 尾端 8 bytes，AAD = rtpTime ‖ SSRC
        cipher = ChaCha20Poly1305Reusable(stream._shk)  # pylint: disable=protected-access
        plain = cipher.decrypt(b"\x00" * 4 + nonce8, packet[12:-8], packet[4:12])
        self.assertEqual(plain, payload)


if __name__ == "__main__":
    unittest.main()
