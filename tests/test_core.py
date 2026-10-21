"""不需要音效裝置或網路的單元測試。"""

import struct
import unittest

from airsender.httpstream import wav_header
from airsender.live_source import DriftController
from airsender.ringbuffer import ByteRingBuffer
from airsender.sinks.dlna import didl_lite
from airsender.sinks.snapcast import parse_server


class RingBufferTests(unittest.TestCase):
    def test_push_pop_in_order(self):
        ring = ByteRingBuffer(100)
        ring.push(b"abc")
        ring.push(b"def")
        self.assertEqual(len(ring), 6)
        self.assertEqual(ring.pop(4), b"abcd")
        self.assertEqual(ring.pop(10), b"ef")
        self.assertEqual(len(ring), 0)

    def test_drops_oldest_when_full(self):
        ring = ByteRingBuffer(4)
        ring.push(b"1234")
        ring.push(b"56")
        self.assertEqual(ring.pop(4), b"3456")
        self.assertEqual(ring.dropped_bytes, 2)


class DriftControllerTests(unittest.TestCase):
    def test_converges_toward_constant_drift(self):
        """模擬擷取端比傳送端快 500 ppm：控制器輸出應收斂到約 +500 ppm，緩衝回到目標附近。"""
        target = 40.0
        ctl = DriftController(target)
        buffered = target
        drift = 500e-6
        t = 0.0
        dt = 0.008
        for _ in range(int(600 / dt)):
            adj = ctl.update(buffered, t)
            buffered += (drift - adj) * 1000 * dt  # ms
            t += dt
        self.assertAlmostEqual(ctl.adjustment, drift, delta=60e-6)
        self.assertAlmostEqual(buffered, target, delta=5.0)

    def test_output_is_clamped(self):
        ctl = DriftController(40.0, max_adj=1e-3)
        ctl.update(40.0, 0.0)
        adj = ctl.update(400.0, 10.0)
        self.assertLessEqual(abs(adj), 1e-3)

    def test_reprime_keeps_integral(self):
        ctl = DriftController(40.0)
        ctl._integral = 3e-4  # pylint: disable=protected-access
        ctl.reprime()
        self.assertEqual(ctl.adjustment, 3e-4)
        self.assertIsNone(ctl._filtered)  # pylint: disable=protected-access


class WavHeaderTests(unittest.TestCase):
    def test_header_layout(self):
        hdr = wav_header()
        self.assertEqual(len(hdr), 44)
        self.assertEqual(hdr[:4], b"RIFF")
        self.assertEqual(hdr[8:16], b"WAVEfmt ")
        tag, channels, rate, byte_rate, align, bits = struct.unpack("<HHIIHH", hdr[20:36])
        self.assertEqual((tag, channels, rate, byte_rate, align, bits), (1, 2, 44100, 176400, 4, 16))
        self.assertEqual(hdr[36:40], b"data")
        self.assertEqual(struct.unpack("<I", hdr[40:44])[0], 0xFFFFFFFF)


class SinkHelperTests(unittest.TestCase):
    def test_parse_snapcast_server(self):
        dev = parse_server("192.168.1.10")
        self.assertEqual((dev.kind, dev.address, dev.url), ("snapcast", "192.168.1.10", "4953"))
        dev = parse_server("pi.local:5000")
        self.assertEqual(
            (dev.address, dev.url, dev.identifier), ("pi.local", "5000", "snapcast:pi.local:5000")
        )
        with self.assertRaises(ValueError):
            parse_server("host:abc")

    def test_didl_escapes(self):
        xml = didl_lite("http://10.0.0.1:8765/stream.wav?a=1&b=2", "A & B")
        self.assertIn("A &amp; B", xml)
        self.assertIn("a=1&amp;b=2", xml)
        self.assertIn('protocolInfo="http-get:*:audio/wav', xml)


class FormatTests(unittest.TestCase):
    def test_choose_airplay_rate(self):
        from airsender.formats import choose_airplay_rate

        self.assertEqual(choose_airplay_rate("auto", 176400), 44100)
        self.assertEqual(choose_airplay_rate("auto", 48000), 48000)
        self.assertEqual(choose_airplay_rate("auto", 192000), 48000)
        self.assertEqual(choose_airplay_rate("auto", None), 44100)
        self.assertEqual(choose_airplay_rate("44100", 48000), 44100)
        self.assertEqual(choose_airplay_rate("48000", 176400), 48000)


if __name__ == "__main__":
    unittest.main()
