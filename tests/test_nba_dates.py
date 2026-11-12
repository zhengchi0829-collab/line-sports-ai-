import unittest
from datetime import datetime
import httpx
from app.reliability import Fixtures, TZ
from test_reliability import espn_event

NOW = datetime(2026, 11, 12, 20, 0, tzinfo=TZ)


class NBADateTests(unittest.IsolatedAsyncioTestCase):
    async def test_nba_dates_are_single_dates(self):
        async with httpx.AsyncClient() as client:
            specs = Fixtures(client).specs('basketball', NOW)
            dates = [params['dates'] for _, _, params, kind in specs if kind == 'espn']
            self.assertEqual(dates, ['20261112', '20261113'])

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
            self.assertEqual(snap.successes, 2)
            self.assertEqual(len(snap.games), 1)
            await store.close()

