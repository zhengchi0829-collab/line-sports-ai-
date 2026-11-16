"""Ten-minute external odds cache changes LINE data without changing source code."""
import json
import unittest
from datetime import datetime, timedelta

import httpx

from app.odds import OddsGateway, TZ

NOW = datetime(2026, 11, 16, 17, 40, tzinfo=TZ)


def fixture():
    return {
        "day": NOW.date().isoformat(),
        "captured_at": NOW.isoformat(),
        "capture_method": "third_party_playsport",
        "source_url": "https://www.playsport.cc/gamesData/result?allianceid=4&gametime=20261116",
        "leagues": {"足球": [{
            "home": "法羅群島", "away": "摩爾多瓦",
            "start": "2026-11-16T23:00:00+08:00",
            "home_price": 1.6, "away_price": 3.95, "draw_price": 2.9,
        }]},
    }


class RemoteOddsTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_branch_becomes_live_quote(self):
        calls = []
        def serve(request):
            calls.append(str(request.url))
            return httpx.Response(200, json=fixture())
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            gateway = OddsGateway(client)
            state = await gateway.refresh_remote_reference(NOW)
            self.assertEqual(state["status"], "ok")
            self.assertEqual(state["count"], 1)
            displayed = gateway.official_daily_rows("足球", NOW.date().isoformat())
            self.assertEqual(len(displayed), 1)
            self.assertEqual(displayed[0]["away_price"], 3.95)
            self.assertIn("第三方", displayed[0]["source"])
            self.assertEqual(len(calls), 1)
            await gateway.refresh_remote_reference(NOW)
            self.assertEqual(len(calls), 1)

    async def test_invalid_source_is_not_accepted(self):
        broken = fixture()
        broken["source_url"] = "https://some-bookmaker.example/odds"
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json=broken))) as client:
            gateway = OddsGateway(client)
            state = await gateway.refresh_remote_reference(NOW)
            self.assertEqual(state["status"], "invalid_source")
            self.assertEqual(gateway.tw_remote, {})

    async def test_old_snapshot_is_rejected(self):
        stale = fixture()
        stale["captured_at"] = (NOW - timedelta(hours=2)).isoformat()
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json=stale))) as client:
            gateway = OddsGateway(client)
            state = await gateway.refresh_remote_reference(NOW)
            self.assertEqual(state["status"], "stale_snapshot")
            self.assertEqual(gateway.tw_remote, {})

    async def test_network_error_preserves_previous_verified_snapshot(self):
        with httpx.Client() as unused:
            pass
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(503))) as client:
            gateway = OddsGateway(client)
            gateway.tw_remote = fixture()
            state = await gateway.refresh_remote_reference(NOW)
            self.assertEqual(state["status"], "http_503")
            self.assertEqual(gateway.tw_remote["leagues"]["足球"][0]["away_price"], 3.95)


if __name__ == "__main__":
    unittest.main()
