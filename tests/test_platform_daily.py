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


    async def test_shared_cache_restores_after_deploy_without_charge(self):
        """New web instance reads same-day prices without any more /odds calls."""
        class MemoryRedis:
            def __init__(self):
                self.data = {}
            async def get(self, key):
                return self.data.get(key)
            async def set(self, key, value, ex=None):
                self.data[key] = value.encode()
                return True
            async def aclose(self):
                return None
        requests = []
        def handler(req):
            requests.append(str(req.url))
            if req.url.path.endswith("/sports/"):
                return httpx.Response(200, json=[
                    {"key": "basketball_nba", "active": True}
                ], headers={"x-requests-remaining": "107"})
            if req.url.path.endswith("/odds"):
                return httpx.Response(200, json=[{
                    "home_team": "Home", "away_team": "Away",
                    "commence_time": "2026-11-18T12:00:00Z",
                    "bookmakers": [{
                        "title": "Book", "last_update": "2026-11-17T00:30:00Z",
                        "markets": [{"key": "h2h", "outcomes": [
                            {"name": "Home", "price": 1.6},
                            {"name": "Away", "price": 2.1},
                        ]}],
                    }],
                }], headers={"x-requests-remaining": "100"})
            raise AssertionError("Unexpected provider API call")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            shared = MemoryRedis()
            with tempfile.TemporaryDirectory() as directory:
                first = OddsGateway(client)
                first.daily_file = Path(directory) / "first.json"
                first.daily = {}
                first.shared_redis = shared
                with patch.dict("os.environ", {"ODDS_API_KEY": "test-key"}), \
                     patch("app.odds.datetime") as clock:
                    clock.now.return_value = self.now
                    await first.refresh_platform_daily()
                    self.assertEqual(first.daily["state"], "ready")
                    self.assertTrue(first.stored_platform_rows("NBA"))
                    initial = len(requests)
                    second = OddsGateway(client)
                    second.daily_file = Path(directory) / "second.json"
                    second.daily = {}
                    second.shared_redis = shared
                    self.assertTrue(await second.restore_shared_daily())
                    self.assertEqual(second.daily["state"], "ready")
                    self.assertEqual(second.stored_platform_rows("NBA"),
                                     first.stored_platform_rows("NBA"))
                    await second.refresh_platform_daily()
                    self.assertEqual(len(requests), initial)
                    await second.close()
                await first.close()

    async def test_no_paid_api_call_when_shared_marker_cannot_be_saved(self):
        class BrokenRedis:
            async def get(self, key):
                return None
            async def set(self, key, value, ex=None):
                raise ConnectionError("shared instance unavailable")
            async def aclose(self):
                return None
        calls = []
        def handler(req):
            calls.append(req.url.path)
            if req.url.path.endswith("/sports/"):
                return httpx.Response(200, json=[
                    {"key": "basketball_nba", "active": True}
                ], headers={"x-requests-remaining": "107"})
            raise AssertionError("Must not buy odds if shared cache is down")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = OddsGateway(client)
            with tempfile.TemporaryDirectory() as directory:
                gateway.daily_file = Path(directory) / "daily.json"
                gateway.daily = {}
                gateway.shared_redis = BrokenRedis()
                with patch.dict("os.environ", {"ODDS_API_KEY": "test-key"}), \
                     patch("app.odds.datetime") as clock:
                    clock.now.return_value = self.now
                    await gateway.refresh_platform_daily()
                self.assertEqual(gateway.daily["state"], "storage_unavailable")
                self.assertEqual(calls, ["/v4/sports/"])
                await gateway.close()

    async def test_pick_single_bookmaker_with_complete_markets(self):
        requests = []
        def handler(req):
            requests.append(str(req.url))
            if req.url.path.endswith("/sports/"):
                return httpx.Response(200, json=[
                    {"key": "basketball_nba", "active": True}
                ], headers={"x-requests-remaining": "107"})
            if req.url.path.endswith("/odds"):
                common = {
                    "home_team": "Home", "away_team": "Away",
                    "commence_time": "2026-11-18T12:00:00Z",
                }
                books = [
                    {"title": "Partial", "last_update": "2026-11-17T00:29:00Z",
                     "markets": [{"key": "h2h", "outcomes": [
                         {"name": "Home", "price": 1.6},
                         {"name": "Away", "price": 2.1}]}]},
                    {"title": "Full", "last_update": "2026-11-17T00:30:00Z",
                     "markets": [
                         {"key": "h2h", "outcomes": [
                             {"name": "Home", "price": 1.7},
                             {"name": "Away", "price": 2.0}]},
                         {"key": "totals", "outcomes": [
                             {"name": "Over", "point": 224.5, "price": 1.91},
                             {"name": "Under", "point": 224.5, "price": 1.91}]}]}
                ]
                return httpx.Response(200, json=[{**common, "bookmakers": books}],
                                      headers={"x-requests-remaining": "100"})
            raise AssertionError("unexpected")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = OddsGateway(client)
            with tempfile.TemporaryDirectory() as directory:
                gateway.daily_file = Path(directory) / "daily.json"
                gateway.daily = {}
                with patch.dict("os.environ", {"ODDS_API_KEY": "test-key"}), \
                     patch("app.odds.datetime") as clock:
                    clock.now.return_value = self.now
                    await gateway.refresh_platform_daily()
                    row = gateway.stored_platform_rows("NBA")[0]
                self.assertEqual(row["book"], "Full")
                self.assertEqual(row["total"], 224.5)
                self.assertEqual(row["home_price"], 1.7)
                await gateway.close()

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
