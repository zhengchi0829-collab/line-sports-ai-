"""Tests for 10-minute monitoring and bounded self-recovery; no API calls."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts import monitor_bot, auto_recover_bot


class MonitorTests(unittest.TestCase):
    def payloads(self):
        now = time.time()
        health = {"status": "ok", "service": "line-sports-ai", "version": "0.7.9"}
        features = {
            "version": "0.7.9", "bot_started_epoch": now - 150,
            "background_refresh": {
                "active": True, "last_run_epoch": now - 30,
                "counts": {"soccer_ucl": 36, "basketball": 278},
                "source_successes": {"soccer_ucl": 2, "soccer_epl": 2,
                                     "basketball": 3},
                "source_errors": {},
            },
            "line_webhook": {"status": "ready"},
            "richmenu": {"action_count": 6},
            "providers": {"soccer_ucl": {}, "basketball": {}},
        }
        return health, features

    def check_payloads(self, health, features):
        def feed(base, path, timeout=80):
            return health if path == "/health" else features
        with patch.object(monitor_bot, "get_json", side_effect=feed):
            return monitor_bot.check("https://example.com")

    def test_healthy_bot(self):
        health, features = self.payloads()
        self.assertIn("OK", self.check_payloads(health, features))

    def test_stale_background_is_restartable(self):
        health, features = self.payloads()
        features["background_refresh"]["last_run_epoch"] = time.time() - 1800
        with self.assertRaises(monitor_bot.MonitorError) as error:
            self.check_payloads(health, features)
        self.assertEqual(error.exception.code, "stale_refresh")
        self.assertTrue(error.exception.restartable)

    def test_failed_core_provider_not_restarted(self):
        health, features = self.payloads()
        features["background_refresh"]["source_successes"]["soccer_ucl"] = 0
        with self.assertRaises(monitor_bot.MonitorError) as error:
            self.check_payloads(health, features)
        self.assertEqual(error.exception.code, "upstream_failed")
        self.assertFalse(error.exception.restartable)

    def test_many_provider_errors_not_restarted(self):
        health, features = self.payloads()
        features["background_refresh"]["source_errors"] = {
            "baseball_milb": ["timeout"] * 5, "basketball_tpbl": ["timeout"] * 3,
        }
        with self.assertRaises(monitor_bot.MonitorError) as error:
            self.check_payloads(health, features)
        self.assertFalse(error.exception.restartable)

    def test_line_webhook_errors_not_restarted(self):
        health, features = self.payloads()
        features["line_webhook"]["status"] = "test_failed"
        with self.assertRaises(monitor_bot.MonitorError) as error:
            self.check_payloads(health, features)
        self.assertEqual(error.exception.code, "webhook_external")
        self.assertFalse(error.exception.restartable)

    def test_unreachable_web_server_is_restartable(self):
        with patch.object(monitor_bot, "get_json", side_effect=TimeoutError()):
            with self.assertRaises(monitor_bot.MonitorError) as error:
                monitor_bot.check("https://example.com")
        self.assertEqual(error.exception.code, "service_unreachable")
        self.assertTrue(error.exception.restartable)

    def test_diag_file_and_report_for_unrecoverable_error(self):
        health, features = self.payloads()
        features["background_refresh"]["source_successes"]["basketball"] = 0
        with tempfile.TemporaryDirectory() as tmp:
            report, diag = Path(tmp) / "report", Path(tmp) / "diag"
            with patch.object(monitor_bot, "get_json",
                              side_effect=[health, features]):
                exit_code = monitor_bot.run("https://example.com", report,
                                            max_attempts=1, diagnostics=diag)
            self.assertEqual(exit_code, 1)
            self.assertFalse(json.loads(diag.read_text())["restartable"])
            self.assertIn("upstream_failed", report.read_text())



    def test_fixture_slot_marked_but_unfinished_is_reported(self):
        from datetime import datetime
        from scripts.monitor_bot import TZ
        health, features = self.payloads()
        when = datetime(2026, 11, 16, 21, 1, tzinfo=TZ)
        features["background_refresh"]["last_fixture_slot"] = "2026-11-16T20:00:00+08:00"
        features["background_refresh"]["last_fixture_refresh_epoch"] = None
        with patch.object(monitor_bot, "datetime") as clock:
            clock.now.return_value = when
            with self.assertRaises(monitor_bot.MonitorError) as failure:
                self.check_payloads(health, features)
        self.assertEqual(failure.exception.code, "analysis_update_missed")
        self.assertFalse(failure.exception.restartable)
        features["background_refresh"]["last_fixture_refresh_epoch"] = when.timestamp()
        with patch.object(monitor_bot, "datetime") as clock:
            clock.now.return_value = when
            self.assertIn("OK", self.check_payloads(health, features))

    def test_line_schema_validation_failure_is_reported(self):
        health, features = self.payloads()
        features["bot_started_epoch"] -= 400
        features["line_message_validation"] = 400
        with self.assertRaises(monitor_bot.MonitorError) as failure:
            self.check_payloads(health, features)
        self.assertEqual(failure.exception.code, "line_validation_invalid")

class RecoveryPolicyTests(unittest.TestCase):
    def test_not_restartable(self):
        allowed, _ = auto_recover_bot.policy(
            {"restartable": False, "ok": False},
            [(100, "good deploy")], 10000)
        self.assertFalse(allowed)

    def test_redeploy_after_grace_period(self):
        now = 20000
        self.assertTrue(auto_recover_bot.policy(
            {"restartable": True, "ok": False},
            [(now - 1200, "normal deploy")], now)[0])

    def test_recent_deploy_suppresses_redeploy(self):
        now = 20000
        self.assertFalse(auto_recover_bot.policy(
            {"restartable": True, "ok": False},
            [(now - 100, "new version")], now)[0])

    def test_redeploy_cooldown_suppresses_repeat(self):
        now = 20000
        self.assertFalse(auto_recover_bot.policy(
            {"restartable": True, "ok": False},
            [(now - 1500, auto_recover_bot.PREFIX + ": unavailable")], now)[0])

    def test_allow_when_recovery_cooldown_ended(self):
        now = 20000
        self.assertTrue(auto_recover_bot.policy(
            {"restartable": True, "ok": False},
            [(now - 8000, auto_recover_bot.PREFIX + ": unavailable")], now)[0])


if __name__ == "__main__":
    unittest.main()
