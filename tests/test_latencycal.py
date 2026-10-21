"""延遲量測的訊號處理：合成錄音（掃頻音延遲 + 雜音）要能找回正確的延遲。"""

from __future__ import annotations

import unittest

import numpy as np

from airsender import latencycal


class LatencyCalTests(unittest.TestCase):
    def test_matched_filter_finds_delayed_chirp_in_noise(self):
        rate = 48000
        chirp = latencycal.make_chirp(rate, amp=0.3)
        rng = np.random.default_rng(3)
        rec = (0.05 * rng.standard_normal(rate * 3)).astype(np.float32)  # 3 秒白雜音（−26 dBFS）
        delay_ms = 147.3
        start = int(delay_ms / 1000 * rate)
        rec[start : start + len(chirp)] += 0.1 * chirp  # 接收端抵達的掃頻音，−30 dBFS
        env = latencycal.matched_filter(latencycal.bandpass(rec, rate), chirp)
        peaks = latencycal.xcorr_peaks(env, rate, 0.0, 2.9)
        self.assertTrue(peaks, "應該找到峰值")
        t, strength, snr = peaks[0]
        self.assertAlmostEqual(t * 1000, delay_ms, delta=1.0)
        self.assertEqual(strength, 1.0)
        self.assertGreater(snr, 10)

    def test_peak_values_are_plain_floats(self):
        import json

        rate = 48000
        chirp = latencycal.make_chirp(rate)
        rec = np.zeros(rate, dtype=np.float32)
        rec[4800 : 4800 + len(chirp)] += chirp
        env = latencycal.matched_filter(rec, chirp)
        t, strength, snr = latencycal.xcorr_peaks(env, rate, 0.0, 0.9)[0]
        json.dumps({"t": float(t), "s": strength, "snr": snr})  # 設定檔序列化需要 Python 原生型別
        self.assertAlmostEqual(t * 1000, 100.0, delta=1.0)

    def test_consistent_cluster_rejects_outliers(self):
        values = [146.0, 148.5, 147.2, 1037.0, 2696.0, 149.9]
        self.assertEqual(sorted(latencycal.consistent_cluster(values)), [146.0, 147.2, 148.5, 149.9])

    def test_result_summary(self):
        r = latencycal.LatencyResult(median_ms=147.0, values_ms=[146.0, 148.0], rejected=1, repeats=5)
        self.assertTrue(r.consistent)
        self.assertIn("147 ms", r.summary())
        self.assertIn("排除 1", r.summary())
        self.assertFalse(latencycal.LatencyResult(median_ms=float("nan"), values_ms=[], rejected=0, repeats=5).consistent)


if __name__ == "__main__":
    unittest.main()
