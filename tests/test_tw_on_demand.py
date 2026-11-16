import json
import unittest
from datetime import datetime
import httpx
from app.odds import OddsGateway, TZ
from app.reliability import odds_available_menu, odds_league_menu

NOW = datetime(2026, 11, 16, 16, 30, tzinfo=TZ)
PAGE = """<table><tr><th>日期</th><th>時間</th><th>運動</th><th>客隊</th>
<th>主隊</th><th>客勝賠率</th><th>主勝賠率</th></tr>
<tr><td>2026/11/16</td><td>20:00</td><td>足球</td><td>客方</td>
<td>主方</td><td>1.75</td><td>3.10</td></tr></table>"""


class TaiwanOnDemandTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_html_populates_bot_cache(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, text=PAGE,
                                       headers={"content-type": "text/html"}))) as client:
            gateway = OddsGateway(client)
            self.assertEqual((await gateway.check_tw_public_now(NOW))["status"], "ok")
            games = gateway.official_daily_rows("足球", NOW.date().isoformat())
            self.assertEqual(len(games), 1)
            self.assertEqual(games[0]["home_price"], 3.1)

    async def test_403_does_not_repeat_and_is_visible(self):
        seen = []
        def reject(req):
            seen.append(1)
            return httpx.Response(403, text="Forbidden")
        async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
            gateway = OddsGateway(client)
            self.assertEqual((await gateway.check_tw_public_now(NOW))["status"], "http_403")
            self.assertEqual((await gateway.check_tw_public_now(NOW))["status"], "http_403")
            self.assertEqual(len(seen), 1)
            display = json.dumps(odds_available_menu({}, gateway.tw_probe),
                                 ensure_ascii=False)
            self.assertIn("HTTP 403", display)
            self.assertIn("台彩立即更新", display)

    async def test_empty_menu_shows_actual_403_reason(self):
        card = json.dumps(odds_league_menu(
            "台灣運彩賠率", available={},
            probe={"status": "http_403"}), ensure_ascii=False)
        self.assertIn("HTTP 403", card)
        self.assertIn("台彩立即更新", card)
        self.assertNotIn("尚無快照", card)

    async def test_unstructured_page_never_generates_prices(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, text="<div>今天 2.5 1.9</div>",
                headers={"content-type": "text/html"}))) as client:
            gateway = OddsGateway(client)
            self.assertEqual((await gateway.check_tw_public_now(NOW))["status"],
                             "no_verified_prices")
            # A bad on-demand response must not manufacture new odds.
            # Existing independently verified daily snapshots may still exist.
            self.assertEqual(gateway.tw_live, {})
