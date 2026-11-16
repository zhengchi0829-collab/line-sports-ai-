import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from scripts.update_tw_official_odds import (
    TZ, FetchFailure, direct_fetch, parse_public_html, refresh)

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=TZ)
PAGE = """<table><tr><th>日期</th><th>時間</th><th>運動</th>
<th>客隊</th><th>主隊</th><th>客勝賠率</th><th>和局賠率</th><th>主勝賠率</th></tr>
<tr><td>2026/11/16</td><td>20:00</td><td>足球</td>
<td>客隊一</td><td>主隊一</td><td>1.70</td><td>2.95</td><td>3.10</td></tr>
<tr><td>2026/11/17</td><td>20:00</td><td>足球</td>
<td>客隊二</td><td>主隊二</td><td>1.80</td><td>2.99</td><td>3.30</td></tr></table>"""


class TaiwanOfficialOddsTests(unittest.TestCase):
    def test_explicit_current_date_only(self):
        rows = parse_public_html(PAGE, NOW.date())
        self.assertEqual(list(rows), ["足球"])
        self.assertEqual(len(rows["足球"]), 1)
        self.assertEqual(rows["足球"][0]["home_price"], 3.1)
        self.assertEqual(rows["足球"][0]["away_price"], 1.7)
        self.assertEqual(rows["足球"][0]["start"][:10], "2026-11-16")

    def test_unsupported_layout_is_not_guesswork(self):
        self.assertEqual(parse_public_html(
            "<div>台灣運彩 甲乙 1.8 2.1 足球</div>", NOW.date()), {})

    def test_no_fake_prices_when_site_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "snapshot.json"
            status = Path(tmp) / "status.json"
            old = '{"day":"2026-11-15"}'
            output.write_text(old, encoding="utf-8")
            code = refresh(output, status, NOW,
                           fetch=lambda: (_ for _ in ()).throw(
                               FetchFailure("HTTP_403")))
            self.assertEqual(code, 1)
            self.assertEqual(output.read_text(encoding="utf-8"), old)
            self.assertEqual(json.loads(status.read_text())["error"], "HTTP_403")

    def test_valid_page_writes_today_and_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "snapshot.json"
            status = Path(tmp) / "status.json"
            self.assertEqual(refresh(output, status, NOW, fetch=lambda: PAGE), 0)
            record = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(record["day"], "2026-11-16")
            self.assertEqual(record["capture_method"], "official_public_html")
            self.assertEqual(len(record["leagues"]["足球"]), 1)

    def test_http_403_is_one_attempt(self):
        with patch("scripts.update_tw_official_odds.urlopen",
                   side_effect=HTTPError("https://www.sportslottery.com.tw/",
                                         403, "Forbidden", {}, None)) as request:
            with self.assertRaisesRegex(FetchFailure, "HTTP_403"):
                direct_fetch()
            self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
