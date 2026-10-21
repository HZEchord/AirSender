"""ALAC（Apple Lossless）未壓縮框架打包。

AirPlay 2 的 buffered 串流以 ALAC 承載音訊。ALAC 位元流允許「escape」框架：樣本原樣寫入、
不做預測與 Rice 編碼，解碼端照常接受。這讓我們不必實作壓縮器就能無損送出 24-bit；
代價是頻寬等同 PCM（48 kHz / 24-bit 立體聲約 2.3 Mbit/s，區網上不是問題）。

框架格式（依 Apple 開源的 ALACDecoder.cpp，Apache-2.0；這裡是獨立實作）：

    3 bits   element type = 1（CPE，立體聲）
    4 bits   element instance tag = 0
    12 bits  unused = 0
    1 bit    partial frame：numSamples 不等於 frameLength 時為 1
    2 bits   bytes shifted = 0
    1 bit    escape = 1（未壓縮）
    [32 bits numSamples，只在 partial 時]
    numSamples × (左 bitDepth bits, 右 bitDepth bits)，二補數、big-endian 位元序
    3 bits   ID_END = 7
    補零到位元組邊界
"""

from __future__ import annotations

import numpy as np

FRAME_LENGTH = 352  # AirPlay 的 ALAC frameLength（spf）
_ID_CPE = 1
_ID_END = 7


def pack_verbatim_frame(samples: np.ndarray, bit_depth: int, frame_length: int = FRAME_LENGTH) -> bytes:
    """把 (n, 2) 的整數樣本打包成一個未壓縮 ALAC 框架。

    samples：int16 / int32 陣列，值域須在 bit_depth 位元的二補數範圍內（24-bit 即 ±2^23）。
    """
    if bit_depth not in (16, 24):
        raise ValueError(f"不支援的位元深度 {bit_depth}")
    if samples.ndim != 2 or samples.shape[1] != 2:
        raise ValueError("需要 (n, 2) 的立體聲樣本")
    n = int(samples.shape[0])
    if n == 0 or n > frame_length:
        raise ValueError(f"樣本數 {n} 必須在 1..{frame_length}")

    partial = n != frame_length
    # 標頭：3 + 4 + 12 + 4 = 23 bits（partial 時再加 32 bits）
    header = ((_ID_CPE << 4) | 0) << 16  # type, tag, 12 bits unused
    header |= (int(partial) << 3) | (0 << 1) | 1
    header_bits = 23
    if partial:
        header = (header << 32) | n
        header_bits += 32

    mask = (1 << bit_depth) - 1
    flat = samples.astype(np.int64).reshape(-1) & mask  # 交錯 L R L R…，轉成無號 bit_depth 位元
    if bit_depth == 16:
        body = flat.astype(">u2").tobytes()
    else:
        body = flat.astype(">u4").view(np.uint8).reshape(-1, 4)[:, 1:].tobytes()
    body_bits = n * 2 * bit_depth

    total_bits = header_bits + body_bits + 3
    pad = (-total_bits) % 8
    value = (header << body_bits) | int.from_bytes(body, "big")
    value = ((value << 3) | _ID_END) << pad
    return value.to_bytes((total_bits + pad) // 8, "big")


def frame_bytes(bit_depth: int, frame_length: int = FRAME_LENGTH) -> int:
    """一個完整未壓縮框架的位元組數（用於估算頻寬）。"""
    total_bits = 23 + frame_length * 2 * bit_depth + 3
    return (total_bits + 7) // 8


def magic_cookie(sample_rate: int, bit_depth: int, frame_length: int = FRAME_LENGTH) -> bytes:
    """ALACSpecificConfig（24 bytes），某些接收端以 fmtp 字串或 cookie 建立解碼器時使用。"""
    import struct  # pylint: disable=import-outside-toplevel

    return struct.pack(
        ">IBBBBBBHII",
        frame_length,
        0,  # compatibleVersion
        bit_depth,
        40,  # pb
        10,  # mb
        14,  # kb
        2,  # numChannels
        255,  # maxRun
        0,  # maxFrameBytes（0 = 未知）
        0,  # avgBitRate（0 = 未知）
    ) + struct.pack(">I", sample_rate)


def fmtp(sample_rate: int, bit_depth: int, frame_length: int = FRAME_LENGTH) -> str:
    """RAOP 風格的 fmtp 參數字串：frameLength compatibleVersion bitDepth pb mb kb channels maxRun maxFrameBytes avgBitRate sampleRate。"""
    return f"{frame_length} 0 {bit_depth} 40 10 14 2 255 0 0 {sample_rate}"
