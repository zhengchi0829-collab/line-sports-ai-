"""Chrome fallback tests without accessing public network or paid APIs."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from scripts import tw_browser_capture
from scripts.update_tw_official_odds import FetchFailure, TZ, refresh


class BrowserCaptureTests(unittest.TestCase):
    def test_finds_preinstalled_chrome(self):
        requested = []
        def fake_which(binary):
            requested.append(binary)
            return "/usr/bin/google-chrome" if binary == "google-chrome" else None
        self.assertEqual(tw_browser_capture.find_chrome(fake_which),
                         "/usr/bin/google-chrome")
        self.assertEqual(requested, ["google-chrome"])

    def test_unavailable_browser_is_explicit(self):
        with patch.object(tw_browser_capture, "find_chrome", return_value=None):
            with self.assertRaisesRegex(FetchFailure, "CHROME_NOT_INSTALLED"):
                tw_browser_capture.browser_fetch()

    def test_public_rendered_table_is_accepted_only_if_verifiable(self):
        html = """<table><tr><th>日期</th><th>時間</th><th>運動</th>
        <th>客隊</th><th>主隊</th><th>客勝賠率</th><th>主勝賠率</th></tr>
        <tr><td>2026/11/16</td><td>20:00</td><td>足球</td>
        <td>客隊甲</td><td>主隊乙</td><td>1.85</td><td>2.15</td></tr></table>"""
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "odds.json"
            status = Path(folder) / "status.json"
            result = refresh(out, status,
                             now=datetime(2026, 11, 16, 16, 0, tzinfo=TZ),
                             fetch=lambda: html)
            self.assertEqual(result, 0)
            self.assertIn("2.15", out.read_text(encoding="utf-8"))

    def test_blocked_chrome_keeps_existing_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "odds.json"
            status = Path(folder) / "status.json"
            original = '{"day":"2026-11-15"}'
            out.write_text(original, encoding="utf-8")
            result = refresh(out, status,
                             now=datetime(2026, 11, 16, 16, 0, tzinfo=TZ),
                             fetch=lambda: (_ for _ in ()).throw(
                                 FetchFailure("CHROME_HTTP_403")))
            self.assertEqual(result, 1)
            self.assertEqual(out.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
