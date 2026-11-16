import unittest
from datetime import datetime
import httpx
from app.reliability import Fixtures, TZ
from test_reliability import espn_event

NOW = datetime(2026, 11, 12, 20, 0, tzinfo=TZ)


class NBADateTests(unittest.IsolatedAsyncioTestCase):
    async def test_nbl_future_schedule_looks_beyond_tomorrow(self):
        async with httpx.AsyncClient() as client:
            specs = Fixtures(client).specs('basketball_nbl', NOW)
            dates = [params['dates'] for _, _, params, kind in specs]
            self.assertEqual(dates, ['202611', '202612'])
            self.assertTrue(all(kind == 'basketball_nbl' for _, _, _, kind in specs))

    async def test_nba_sources_use_months_and_current_date(self):
        async with httpx.AsyncClient() as client:
            specs = Fixtures(client).specs('basketball', NOW)
            dates = [params['dates'] for _, _, params, kind in specs if kind == 'espn']
            self.assertEqual(dates, ['202611', '202612', '20261112'])
            self.assertTrue(all('cdn.nba.com' not in url for _, url, _, _ in specs))

    async def test_nba_range_400_is_avoided(self):
        def handler(req):
            if 'espn' not in req.url.host:
                return httpx.Response(403)
            if '-' in req.url.params.get('dates', ''):
                return httpx.Response(400)
            return httpx.Response(200, json={'events': [espn_event()]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            store = Fixtures(client)
            snap = await store.get('basketball')
            self.assertEqual(snap.successes, 3)
            self.assertEqual(len(snap.games), 1)
            await store.close()

