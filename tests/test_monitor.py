"""Validate the external monitor without internet or secrets."""
import time
import unittest
from unittest.mock import patch

from scripts import monitor_bot


class MonitorTests(unittest.TestCase):
    def payloads(self):
        now = time.time()
        health = {"service": "line-sports-ai", "status": "ok", "version": "0.7.8"}
        features = {
            "version": "0.7.8",
            "bot_started_epoch": now - 120,
            "background_refresh": {
                "active": True, "last_run_epoch": now - 20,
                "counts": {"soccer_ucl": 12, "basketball": 18},
                "source_successes": {"soccer_ucl": 2, "soccer_epl": 2, "basketball": 3},
                "source_errors": {},
            },
            "line_webhook": {"status": "ready"},
            "richmenu": {"action_count": 6},
            "providers": {"soccer_ucl": {}, "basketball": {}},
        }
        return health, features

    def check_payloads(self, health, features):
        def feed(base, path, timeout=85):
            return health if path == "/health" else features
        with patch.object(monitor_bot, "get_json", side_effect=feed):
            return monitor_bot.check("https://example.com")

    def test_healthy_bot(self):
        health, features = self.payloads()
        self.assertIn("OK", self.check_payloads(health, features))

    def test_stale_background_refresh_is_detected(self):
        health, features = self.payloads()
        features["background_refresh"]["last_run_epoch"] = time.time() - 1800
        with self.assertRaisesRegex(ValueError, "stale"):
            self.check_payloads(health, features)

    def test_all_critical_sources_failed_is_detected(self):
        health, features = self.payloads()
        features["background_refresh"]["source_successes"]["soccer_ucl"] = 0
        with self.assertRaisesRegex(ValueError, "Core fixture"):
            self.check_payloads(health, features)

    def test_repeated_provider_timeouts_are_detected(self):
        health, features = self.payloads()
        features["background_refresh"]["source_errors"] = {
            "basketball_tpbl": ["timeout"] * 5,
            "baseball_milb": ["timeout"] * 3,
        }
        with self.assertRaisesRegex(ValueError, "Many fixture"):
            self.check_payloads(health, features)

    def test_bad_line_webhook_status_is_detected(self):
        health, features = self.payloads()
        features["line_webhook"]["status"] = "test_failed"
        with self.assertRaisesRegex(ValueError, "webhook"):
            self.check_payloads(health, features)


if __name__ == "__main__":
    unittest.main()
