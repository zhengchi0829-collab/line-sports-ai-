"""Clock and time-zone regression tests for user-specified operations schedule."""
import unittest
from datetime import datetime, timezone
from app.schedule import (TZ, latest_analysis_slot, platform_refresh_window,
                          platform_day)


class ScheduleTests(unittest.TestCase):
    def t(self, day, hh, mm=0):
        return datetime(2026, 11, day, hh, mm, tzinfo=TZ)

    def test_analysis_slots_three_times(self):
        self.assertEqual(latest_analysis_slot(self.t(16, 8, 59)),
                         self.t(15, 20).isoformat())
        self.assertEqual(latest_analysis_slot(self.t(16, 9)),
                         self.t(16, 9).isoformat())
        self.assertEqual(latest_analysis_slot(self.t(16, 14, 59)),
                         self.t(16, 9).isoformat())
        self.assertEqual(latest_analysis_slot(self.t(16, 15)),
                         self.t(16, 15).isoformat())
        self.assertEqual(latest_analysis_slot(self.t(16, 20)),
                         self.t(16, 20).isoformat())
        self.assertEqual(latest_analysis_slot(self.t(17, 0)),
                         self.t(16, 20).isoformat())

    def test_platform_only_0030_with_delayed_wakeup(self):
        self.assertFalse(platform_refresh_window(self.t(16, 0, 29)))
        self.assertTrue(platform_refresh_window(self.t(16, 0, 30)))
        self.assertTrue(platform_refresh_window(self.t(16, 2, 59)))
        self.assertFalse(platform_refresh_window(self.t(16, 3)))
        self.assertFalse(platform_refresh_window(self.t(16, 20)))

    def test_utc_times_interpreted_as_taipei(self):
        utc = datetime(2026, 11, 16, 16, 30, tzinfo=timezone.utc)
        self.assertTrue(platform_refresh_window(utc))
        self.assertEqual(platform_day(utc), "2026-11-17")
        self.assertEqual(latest_analysis_slot(utc),
                         datetime(2026, 11, 16, 20, tzinfo=TZ).isoformat())


if __name__ == "__main__":
    unittest.main()


class WorkflowCronTests(unittest.TestCase):
    def test_schedule_matches_all_four_requested_frequencies(self):
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        monitor = (path / "line-bot-monitor.yml").read_text(encoding="utf-8")
        odds = (path / "tw-odds-10min.yml").read_text(encoding="utf-8")
        legacy = (path / "taiwan-odds-update.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "7,17,27,37,47,57 * * * *"', monitor)
        self.assertIn('cron: "30 16 * * *"', monitor)
        self.assertIn('cron: "0 1,7,12 * * *"', monitor)
        self.assertIn('cron: "0 * * * *"', odds)
        self.assertNotIn('cron:', legacy)
