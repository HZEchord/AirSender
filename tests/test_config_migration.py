"""設定檔遷移與型別防呆。"""

from __future__ import annotations

import unittest

from airsender import config as cm
from airsender.config import Config


class MigrationTests(unittest.TestCase):
    def test_unversioned_file_runs_all_migrations(self):
        data = {
            "correction_f_high": 10000,
            "game_transport": "realtime",
            "game_preset": "較低（約 220 ms）",
            "mode": "game",
            "receiver_latency_ms": 150,
        }
        cfg = Config.from_dict(data)
        self.assertEqual(cfg.correction_f_high, 500)
        self.assertEqual(
            (cfg.game_transport, cfg.game_preset, cfg.receiver_latency_ms),
            ("buffered", "較低（約 130 ms）", 75),
        )
        self.assertEqual(cfg.config_version, cm.CONFIG_VERSION)

    def test_current_version_skips_migrations(self):
        data = {
            "config_version": cm.CONFIG_VERSION,
            "correction_f_high": 10000,
            "game_transport": "realtime",
            "game_preset": "較低（約 220 ms）",
        }
        cfg = Config.from_dict(data)
        self.assertEqual(cfg.correction_f_high, 10000, "已是新版本的檔案不再套用舊遷移")
        self.assertEqual(cfg.game_transport, "realtime")

    def test_user_choice_preserved(self):
        cfg = Config.from_dict({"game_transport": "realtime", "game_preset": "最低（約 140 ms）"})
        self.assertEqual((cfg.game_transport, cfg.game_preset), ("realtime", "最低（約 140 ms）"))

    def test_unknown_and_wrong_typed_fields_are_dropped(self):
        cfg = Config.from_dict(
            {
                "bogus": 1,
                "receiver_latency_ms": "abc",
                "mute_local_output": "yes",
                "bass_boost_db": 3,
                "volume": None,
            }
        )
        self.assertEqual(cfg.receiver_latency_ms, Config().receiver_latency_ms)
        self.assertFalse(cfg.mute_local_output)
        self.assertEqual(cfg.bass_boost_db, 3.0)
        self.assertIsNone(cfg.volume)
        self.assertFalse(hasattr(cfg, "bogus"))

    def test_non_dict_file_falls_back(self):
        import json
        import tempfile
        from pathlib import Path

        d = tempfile.mkdtemp()
        p = Path(d) / "config.json"
        p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        old = cm.config_path
        cm.config_path = lambda: p
        try:
            self.assertEqual(Config.load().receiver_latency_ms, Config().receiver_latency_ms)
        finally:
            cm.config_path = old


if __name__ == "__main__":
    unittest.main()
