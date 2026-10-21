"""ALAC 未壓縮框架：以獨立的位元讀取器照 ALACDecoder 的流程解回來，驗證打包正確。"""

from __future__ import annotations

import unittest

import numpy as np

from airsender import alac


class _BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def read(self, nbits: int) -> int:
        value = 0
        for _ in range(nbits):
            byte = self.data[self.pos >> 3]
            bit = (byte >> (7 - (self.pos & 7))) & 1
            value = (value << 1) | bit
            self.pos += 1
        return value


def _decode(frame: bytes, bit_depth: int, frame_length: int = alac.FRAME_LENGTH) -> np.ndarray:
    r = _BitReader(frame)
    element = r.read(3)
    assert element == 1, "必須是 CPE（ID_CPE = 1）"
    assert r.read(4) == 0, "instance tag"
    assert r.read(12) == 0, "unused bits"
    header = r.read(4)
    partial = header >> 3
    bytes_shifted = (header >> 1) & 3
    escape = header & 1
    assert bytes_shifted == 0 and escape == 1
    n = r.read(32) if partial else frame_length
    out = np.zeros((n, 2), dtype=np.int64)
    sign = 1 << (bit_depth - 1)
    for i in range(n):
        for ch in range(2):
            v = r.read(bit_depth)
            out[i, ch] = v - (1 << bit_depth) if v & sign else v
    assert r.read(3) == 7, "ID_END"
    # 剩餘必須是零補位、且不超過 7 bits
    assert len(frame) * 8 - r.pos < 8
    assert r.read(len(frame) * 8 - r.pos) == 0
    return out


class AlacVerbatimTests(unittest.TestCase):
    def test_roundtrip_24bit_full_frame(self):
        rng = np.random.default_rng(1)
        samples = rng.integers(-(1 << 23), (1 << 23) - 1, size=(352, 2), dtype=np.int32)
        samples[0] = (-(1 << 23), (1 << 23) - 1)
        samples[1] = (0, -1)
        frame = alac.pack_verbatim_frame(samples, 24)
        self.assertEqual(len(frame), alac.frame_bytes(24))
        np.testing.assert_array_equal(_decode(frame, 24), samples)

    def test_roundtrip_16bit_full_frame(self):
        rng = np.random.default_rng(2)
        samples = rng.integers(-32768, 32767, size=(352, 2), dtype=np.int16)
        frame = alac.pack_verbatim_frame(samples, 16)
        self.assertEqual(len(frame), alac.frame_bytes(16))
        np.testing.assert_array_equal(_decode(frame, 16), samples)

    def test_partial_frame(self):
        samples = np.array([[1, -1], [123456, -123456], [8388607, -8388608]], dtype=np.int32)
        frame = alac.pack_verbatim_frame(samples, 24)
        np.testing.assert_array_equal(_decode(frame, 24), samples)

    def test_frame_size(self):
        # 23 + 352*2*24 + 3 = 16922 bits -> 2116 bytes
        self.assertEqual(alac.frame_bytes(24), 2116)
        self.assertEqual(alac.frame_bytes(16), (23 + 352 * 32 + 3 + 7) // 8)

    def test_cookie_and_fmtp(self):
        cookie = alac.magic_cookie(48000, 24)
        self.assertEqual(len(cookie), 24)
        self.assertEqual(cookie[:4], (352).to_bytes(4, "big"))
        self.assertEqual(cookie[-4:], (48000).to_bytes(4, "big"))
        self.assertEqual(alac.fmtp(44100, 16), "352 0 16 40 10 14 2 255 0 0 44100")


if __name__ == "__main__":
    unittest.main()
