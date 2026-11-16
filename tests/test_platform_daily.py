"""The Odds API quota safety and scheduled-only platform update."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx

from app.odds import OddsGateway
from app.schedule import TZ, platform_credit_budget, platform_market_plan


class PlatformDailyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 17, 0, 31, tzinfo=TZ)

    def test_shared_monthly_quota_budget(self):
        self.assertEqual(platform_credit_budget(107, self.now), 7)
        self.assertEqual(platform_credit_budget(3, self.now), 0)
        self.assertLessEqual(platform_credit_budget(500, self.now), 40)

    def test_budget_limits_markets_even_for_active_leagues(self):
        supported = [("NBA", "basketball_nba"), ("英超", "soccer_epl"),
                     ("MLB", "baseball_mlb"), ("西甲", "soccer_spain_la_liga"),
                     ("德甲", "soccer_germany_bundesliga")]
        plan = platform_market_plan(supported, 3, self.now)
        self.assertEqual(len(plan), 3)
        self.assertEqual(sum(len(m.split(",")) for _, _, m in plan), 3)
        self.assertIn("NBA", [label for label, _, _ in plan])

    def test_markets_expand_when_credits_available(self):
        plan = platform_market_plan([("NBA", "basketball_nba")], 3, self.now)
        self.assertEqual(plan[0][2], "h2h,spreads,totals")

    async def test_daily_batch_once_even_when_user_taps_more_leagues(self):
        requests = []
        def handler(req):
            requests.append(req)
            if req.url.path.endswith("/sports/"):
                return httpx.Response(200, json=[
                    {"key": "basketball_nba", "active": True},
                    {"key": "soccer_epl", "active": True},
                    {"key": "baseball_mlb", "active": True},
                ], headers={"x-requests-remaining": "107"})
            if req.url.path.endswith("/odds"):
                return httpx.Response(200, json=[{
                    "home_team": "Home", "away_team": "Away",
                    "commence_time": "2026-11-18T12:00:00Z",
                    "bookmakers": [{
                        "title": "Public Test Book", "last_update": "2026-11-17T00:30:00Z",
                        "markets": [{"key": "h2h", "outcomes": [
                            {"name": "Home", "price": 1.6},
                            {"name": "Away", "price": 2.1},
                        ]}],
                    }],
                }], headers={"x-requests-remaining": "100"})
            raise AssertionError("unexpected API route")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = OddsGateway(client)
            with tempfile.TemporaryDirectory() as folder:
                gateway.daily_file = Path(folder) / "platform.json"
                gateway.daily = {}
                with patch.dict("os.environ", {"ODDS_API_KEY": "test-key"}):
                    with patch("app.odds.datetime") as clock:
                        clock.now.return_value = self.now
                        await gateway.refresh_platform_daily()
                        initial_calls = len(requests)
                        self.assertGreater(initial_calls, 1)
                        for league in ("NBA", "英超", "MLB"):
                            await gateway.feed("platform", league, self.now.date().isoformat())
                        self.assertEqual(len(requests), initial_calls)
                        await gateway.refresh_platform_daily()
                        self.assertEqual(len(requests), initial_calls)
                        self.assertEqual(gateway.daily["state"], "ready")
                        self.assertLessEqual(gateway.daily["credits_planned"], 7)
                        self.assertEqual(gateway.daily["day"], "2026-11-17")

    async def test_missing_key_stops_without_any_billable_call(self):
        seen = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: (seen.append(1), httpx.Response(200))[1])) as client:
            gateway = OddsGateway(client)
            with tempfile.TemporaryDirectory() as folder:
                gateway.daily_file = Path(folder) / "platform.json"
                gateway.daily = {}
                with patch.dict("os.environ", {"ODDS_API_KEY": "", "THE_ODDS_API_KEY": ""}):
                    with patch("app.odds.datetime") as clock:
                        clock.now.return_value = self.now
                        await gateway.refresh_platform_daily()
                self.assertEqual(seen, [])
                self.assertEqual(gateway.daily["state"], "missing_key")


if __name__ == "__main__":
    unittest.main()
