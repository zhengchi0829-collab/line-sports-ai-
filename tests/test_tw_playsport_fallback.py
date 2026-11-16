"""Strict public third-party Taiwan odds parsing with source provenance."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from scripts.tw_playsport_fallback import TZ, parse_football, refresh, fetch_public_football

HTML = """<table class="predictgame-table predictgame-table-soccer">
<tr><th>賽事資訊</th></tr>
<tr gameid="2026111641047">
<td class="td-gameinfo"><p class="football__nameWorld">歐洲國家聯賽</p>
<h3>1047</h3><h4>PM 11:00</h4></td>
<td class="td-teaminfo"><div><h3>摩爾多瓦</h3></div></td>
<td class="td-bank-bet03"><strong class="team-side">客</strong>
<span class="data-wrap"><span>3.8</span></span></td>
<td class="td-bank-bet02">大 2.5, 2.00</td></tr>
<tr><td class="td-teaminfo"><h3>法羅群島</h3></td>
<td class="td-bank-bet03">主 1.60</td>
<td class="td-bank-bet02">小 2.5, 1.48</td></tr>
<tr><td class="td-teaminfo"></td>
<td class="td-bank-bet03">和 2.85</td><td></td></tr>
<tr class="gaprow"><td colspan="4"></td></tr>
</table>"""
NOW = datetime(2026, 11, 16, 16, 59, tzinfo=TZ)


class PlaySportTests(unittest.TestCase):
    def test_realistic_public_odds(self):
        result = parse_football(HTML, NOW.date(), cutoff=NOW)
        self.assertEqual(list(result), ["足球"])
        game = result["足球"][0]
        self.assertEqual((game["away"], game["home"]), ("摩爾多瓦", "法羅群島"))
        self.assertEqual((game["away_price"], game["home_price"], game["draw_price"]),
                         (3.8, 1.6, 2.85))
        self.assertEqual(game["total"], 2.5)
        self.assertEqual((game["over_price"], game["under_price"]), (2, 1.48))

    def test_completed_game_is_not_reported(self):
        self.assertEqual(parse_football(
            HTML, NOW.date(), cutoff=datetime(2026, 11, 16, 23, 30, tzinfo=TZ)), {})

    def test_robots_disallow_is_respected(self):
        def fetch(url):
            if url.endswith("/robots.txt"):
                return "User-agent: *\\nDisallow: /gamesData/"
            return HTML
        with self.assertRaisesRegex(ValueError, "ROBOTS_DISALLOW"):
            fetch_public_football(NOW.date(), fetch=fetch)

    def test_source_is_labeled_as_third_party(self):
        def fetch(url):
            if url.endswith("/robots.txt"):
                return "User-agent: *\\nAllow: /"
            return HTML
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder) / "odds.json"
            q=Path(folder) / "status.json"
            self.assertEqual(refresh(p,q,NOW,fetch=fetch),0)
            row=json.loads(p.read_text())
            self.assertEqual(row["capture_method"],"third_party_playsport")
            self.assertIn("NOT verified",row["source_note"])
            self.assertEqual(len(row["leagues"]["足球"]),1)

    def test_unknown_html_never_invents_quotes(self):
        self.assertEqual(parse_football("<div>主1.5 客2.5</div>",NOW.date()),{})


if __name__ == "__main__":
    unittest.main()
