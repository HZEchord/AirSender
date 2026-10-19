"""房間校正 DSP 的離線測試：合成房間響應可被量到並修正。"""

import unittest

import numpy as np

from airsender import dsp

FS = 44100


def synth_room_db(f: np.ndarray) -> np.ndarray:
    def peak(f0, g, q):
        return g * np.exp(-0.5 * ((np.log2(np.maximum(f, 1)) - np.log2(f0)) / (1 / q)) ** 2)

    return peak(60, 8, 2) + peak(130, -7, 2.5) + peak(2000, 3, 1.5)


class DspTests(unittest.TestCase):
    def test_sweep_deconvolution_and_correction(self):
        rng = np.random.default_rng(0)
        sweep, inv = dsp.exponential_sweep(FS, seconds=4.0)
        n = len(sweep) + FS
        nfft = 1 << int(np.ceil(np.log2(n)))
        f = np.fft.rfftfreq(nfft, 1 / FS)
        room_taps, _ = dsp.minimum_phase_fir(f, synth_room_db(f), FS, n_taps=8192)
        rec = np.fft.irfft(np.fft.rfft(sweep, nfft) * np.fft.rfft(room_taps, nfft), nfft)[:n]
        rec = np.roll(rec, int(0.12 * FS)) + 0.002 * rng.standard_normal(n)

        ir, peak = dsp.impulse_response(rec, inv, FS, search_from=0.05, search_to=1.0)
        delay_ms = (peak - (len(inv) - 1)) / FS * 1000
        self.assertAlmostEqual(delay_ms, 120.0, delta=1.0)

        freqs, mag = dsp.magnitude_response(ir, peak, FS)
        sm = dsp.smooth_octave(freqs, mag, fraction=6)
        ref = (freqs >= 200) & (freqs <= 2000)
        truth = synth_room_db(freqs)
        err = (sm - sm[ref].mean()) - (truth - truth[ref].mean())
        band = (freqs >= 40) & (freqs <= 10000)
        self.assertLess(np.sqrt(np.mean(err[band] ** 2)), 1.0)

        corr = dsp.correction_curve(freqs, sm)
        taps, pre = dsp.minimum_phase_fir(freqs, corr, FS, n_taps=4096)
        self.assertLessEqual(pre, 0.0)
        resid = truth + dsp.fir_response_db(taps, FS, freqs) - pre
        resid -= resid[ref].mean()
        inner = (freqs >= 50) & (freqs <= 8000)
        self.assertLess(np.max(np.abs(resid[inner])), 3.0)
        self.assertGreater(np.max(np.abs((truth - truth[ref].mean())[inner])), 6.0)

    def test_fir_processor_matches_convolution(self):
        rng = np.random.default_rng(1)
        taps = rng.standard_normal(512).astype(np.float32) * 0.05
        x = rng.standard_normal((5000, 2)).astype(np.float32)
        proc = dsp.FirProcessor(taps, 2)
        out, i = [], 0
        while i < len(x):
            n = int(rng.integers(100, 700))
            out.append(proc.process(x[i : i + n]))
            i += n
        y = np.concatenate(out)
        ref = np.column_stack([np.convolve(x[:, c], taps)[: len(x)] for c in range(2)])
        self.assertLess(np.max(np.abs(y - ref)), 1e-4)

    def test_bass_shelf_and_limits(self):
        freqs = np.geomspace(20, 20000, 200)
        shelf = dsp.bass_shelf_db(freqs, 6.0)
        self.assertAlmostEqual(shelf[0], 6.0, places=3)
        self.assertAlmostEqual(shelf[-1], 0.0, places=3)
        measured = np.where(freqs < 100, -20.0, 0.0)  # 低頻深陷 20 dB
        corr = dsp.correction_curve(freqs, measured, limits=dsp.CorrectionLimits(max_boost_db=6.0))
        self.assertLessEqual(np.max(corr), 6.0 + 1e-6)


if __name__ == "__main__":
    unittest.main()
