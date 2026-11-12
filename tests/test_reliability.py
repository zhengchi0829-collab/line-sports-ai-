import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
from app.reliability import (Controller, Fixtures, Game, Snapshot, SPORTS, TZ,
    parse_feed, select_games, fixture_cards, notice)

NOW = datetime(2026, 11, 12, 20, 0, tzinfo=TZ)
SOCCER, BASKETBALL, BASEBALL = SPORTS.values()


def nba_game(ident='1', when='2026-11-13T01:00:00Z', status=1):
    return {'gameId': ident, 'gameTimeUTC': when, 'gameStatus': status,
            'homeTeam': {'teamId': 1, 'teamCity': 'Los Angeles', 'teamName': 'Lakers'},
            'awayTeam': {'teamId': 2, 'teamCity': 'Golden State', 'teamName': 'Warriors'}}


def espn_event():
    return {'id': 'e1', 'date': '2026-11-13T01:00:00Z', 'competitions': [
        {'status': {'type': {'state': 'pre'}}, 'competitors': [
            {'homeAway': 'home', 'team': {'displayName': 'Home'}},
            {'homeAway': 'away', 'team': {'displayName': 'Away'}}]}]}


class UI:
    @staticmethod
    def team_zh(name):
        return name

    @staticmethod
    def menu_card():
        return {'type': 'flex', 'altText': 'menu', 'contents': {'type': 'bubble',
            'body': {'type': 'box', 'layout': 'vertical', 'contents': [{'type': 'text', 'text': 'menu'}]}}}

    @staticmethod
    def offline_analysis(sport, matchup, history=None, players=None):
        return {'type': 'flex', 'altText': matchup, 'contents': {'type': 'bubble',
            'header': {'type': 'box', 'layout': 'vertical', 'contents': []},
            'body': {'type': 'box', 'layout': 'vertical', 'contents': [
                {'type': 'text', 'text': sport}, {'type': 'text', 'text': history or 'no verified history'},
                {'type': 'text', 'text': players or 'no verified roster'}]}}}

    winner_team_details = AsyncMock(return_value=(None, None))


class ParserTests(unittest.TestCase):
    def test_full_nba_schedule_not_today_only(self):
        game = nba_game(when='2026-12-25T22:00:00Z')
        game['gameDateTimeUTC'] = game.pop('gameTimeUTC')
        rows = parse_feed('nba_schedule', {'leagueSchedule': {'gameDates': [{'games': [game]}]}})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].start.hour, 6)
        self.assertEqual(rows[0].start.day, 26)

    def test_bad_game_does_not_discard_valid_game(self):
        rows = parse_feed('nba_today', {'scoreboard': {'games': [{'broken': True}, nba_game()]}})
        self.assertEqual(len(rows), 1)

    def test_completed_nba_excluded(self):
        self.assertEqual(parse_feed('nba_today', {'scoreboard': {'games': [nba_game(status=3)]}}), [])

    def test_time_tbd_not_fake_exact_start(self):
        game = nba_game()
        game['isTimeTBD'] = True
        snap = Snapshot(parse_feed('nba_today', {'scoreboard': {'games': [game]}}), NOW)
        card = fixture_cards('basketball', snap, str, now=NOW)
        encoded = json.dumps(card, ensure_ascii=False)
        self.assertIn('\u6642\u9593\u5f85\u5b9a', encoded)

    def test_openliga_local_time_is_berlin_not_utc(self):
        g = {'matchID': 4, 'matchDateTime': '2026-11-15T15:30:00', 'matchIsFinished': False,
             'team1': {'teamName': 'Home'}, 'team2': {'teamName': 'Away'}}
        self.assertEqual(parse_feed('openliga', [g])[0].start.hour, 22)

    def test_mlb_next_season_selected(self):
        g = {'gamePk': 8, 'gameDate': '2027-03-20T17:00:00Z', 'status': {'abstractGameState': 'Preview'},
             'teams': {'home': {'team': {'id': 1, 'name': 'H'}}, 'away': {'team': {'id': 2, 'name': 'A'}}}}
        rows = parse_feed('mlb', {'dates': [{'games': [g]}]})
        self.assertEqual(select_games(rows, NOW)[0].start.month, 3)

    def test_mlb_postponement_excluded(self):
        g = {'status': {'abstractGameState': 'Preview', 'detailedState': 'Postponed'}}
        self.assertEqual(parse_feed('mlb', {'dates': [{'games': [g]}]}), [])

    def test_missing_payload_distinct_from_empty(self):
        with self.assertRaises(ValueError):
            parse_feed('nba_today', {})
        self.assertEqual(parse_feed('nba_today', {'scoreboard': {'games': []}}), [])

    def test_today_uses_next_available_not_only_day_1_or_7(self):
        games = [Game('1', 'soccer', 'L', NOW + timedelta(days=3), 'H', 'A')]
        self.assertEqual(select_games(games, NOW, True), games)

    def test_past_fixture_not_promoted_to_future(self):
        g = Game('1', 'soccer', 'L', NOW - timedelta(days=1), 'H', 'A')
        self.assertFalse(select_games([g], NOW))

    def test_card_home_away_and_button_preserved(self):
        g = Game('1', 'basketball', 'NBA', NOW + timedelta(days=1), 'Home', 'Away')
        card = fixture_cards('basketball', Snapshot([g], NOW), str, now=NOW)
        self.assertEqual(card['contents']['contents'][0]['footer']['contents'][0]['action']['text'],
                         '\u5206\u6790' + BASKETBALL + ' Home vs Away')
        self.assertIn('quickReply', card)

    def test_error_notice_never_claims_no_matches(self):
        card = json.dumps(notice('basketball', Snapshot(errors=['403'])), ensure_ascii=False)
        self.assertIn('\u4e26\u4e0d\u4ee3\u8868\u6c92\u6709\u6bd4\u8cfd', card)
        self.assertIn('\u6a21\u64ec\u5c0d\u6230\u5206\u6790', card)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_nba_403_does_not_skip_fallback(self):
        calls = []
        def handler(req):
            calls.append(str(req.url))
            if 'espn' in req.url.host:
                return httpx.Response(200, json={'events': [espn_event()]})
            return httpx.Response(403)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            store = Fixtures(client)
            snap = await store.get('basketball')
            self.assertEqual(len(snap.games), 1)
            self.assertEqual(snap.games[0].source, 'ESPN')
            self.assertEqual(len(calls), 3)
            await store.close()

    async def test_calls_coalesced_and_cached(self):
        calls = []
        async def handler(req):
            calls.append(req.url)
            await asyncio.sleep(.01)
            return httpx.Response(200, json=[])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            store = Fixtures(client)
            await asyncio.gather(*(store.get('soccer') for _ in range(6)))
            await store.get('soccer')
            self.assertEqual(len(calls), 1)
            await store.close()

    async def test_rate_limit_is_respected(self):
        calls = []
        def handler(req):
            calls.append(req.url)
            return httpx.Response(429, headers={'Retry-After': '900'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            store = Fixtures(client)
            args = ('one', 'https://example.com/data', {}, 'openliga')
            await store.source(*args)
            _, err = await store.source(*args)
            self.assertEqual(len(calls), 1)
            self.assertEqual(err, 'one:cooldown')

    async def test_three_sports_failure_isolated(self):
        async with httpx.AsyncClient() as client:
            ctl = Controller(UI, client)
            async def fake_get(sport):
                if sport == 'basketball':
                    raise RuntimeError('source failed')
                return Snapshot()
            ctl.fixtures.get = fake_get
            messages = await ctl.reply('\u4eca\u65e5\u8cfd\u4e8b')
            self.assertEqual(len(messages), 3)
            self.assertTrue(all(m['type'] == 'flex' for m in messages))

    async def test_analysis_parsing_works(self):
        async with httpx.AsyncClient() as client:
            ctl = Controller(UI, client)
            ctl.team_details = AsyncMock(return_value=('History', 'Roster'))
            cards = await ctl.reply('\u5206\u6790' + SOCCER + ' A vs B')
            self.assertEqual(cards[0]['altText'], 'A vs B')
            self.assertIn('History', json.dumps(cards))
            await ctl.close()

    async def test_simulation_works_without_apis(self):
        async with httpx.AsyncClient() as client:
            ctl = Controller(UI, client)
            ctl.team_details = AsyncMock(side_effect=AssertionError('Must not need API'))
            cards = await ctl.reply('\u6a21\u64ec\u5206\u6790' + BASKETBALL + ' A vs B')
            self.assertIn('\u975e\u771f\u5be6\u8cfd\u7a0b', json.dumps(cards, ensure_ascii=False))
            ctl.team_details.assert_not_called()
            await ctl.close()

    async def test_stale_cache_labeled_and_age_not_reset(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403))) as client:
            store = Fixtures(client)
            stamp = time.monotonic() - 500
            old = Snapshot([Game('a', 'soccer', 'L', NOW + timedelta(days=1), 'H', 'A')], NOW)
            store.cache['soccer'] = (stamp, old)
            snap = await store.get('soccer')
            self.assertTrue(snap.stale)
            self.assertEqual(store.cache['soccer'][0], stamp)
            self.assertTrue((await store.get('soccer')).stale)
            await store.close()

    async def test_mlb_range_covers_offseason(self):
        async with httpx.AsyncClient() as client:
            params = Fixtures(client).specs('baseball', NOW)[0][2]
            days = (datetime.fromisoformat(params['endDate']) - datetime.fromisoformat(params['startDate'])).days
            self.assertGreater(days, 180)


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Local entrypoint tests isolate preserved UI and all outbound HTTP.
        self.ui_patch = patch.dict(sys.modules, {'app.legacy': UI})
        self.ui_patch.start()
        from app import main
        self.main = main
        self.env = patch.dict(os.environ, {'LINE_CHANNEL_SECRET': 'test-secret', 'LINE_CHANNEL_ACCESS_TOKEN': 'test-token'})
        self.env.start()
        main.app.state.pending = set()
        main.app.state.events = {}
        main.app.state.controller = types.SimpleNamespace(reply=AsyncMock(return_value=[{'type': 'text', 'text': 'ok'}]))
        self.sent = []
        def send(req):
            self.sent.append(json.loads(req.content))
            return httpx.Response(200, json={})
        main.app.state.line_client = httpx.AsyncClient(transport=httpx.MockTransport(send))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')

    async def asyncTearDown(self):
        tasks = list(self.main.app.state.pending)
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.aclose()
        await self.main.app.state.line_client.aclose()
        self.env.stop()
        self.ui_patch.stop()

    async def post(self, body, sign=True):
        raw = json.dumps(body).encode()
        signature = base64.b64encode(hmac.new(b'test-secret', raw, hashlib.sha256).digest()).decode() if sign else 'wrong'
        return await self.client.post('/webhooks/line', content=raw, headers={'x-line-signature': signature})

    def event(self):
        return {'type': 'message', 'webhookEventId': 'event-1', 'replyToken': 'reply-test',
                'message': {'type': 'text', 'text': BASKETBALL}}

    async def test_invalid_signature_rejected(self):
        r = await self.post({'events': [self.event()]}, False)
        self.assertEqual(r.status_code, 401)
        self.assertFalse(self.sent)

    async def test_empty_verification(self):
        r = await self.post({'events': []})
        self.assertEqual(r.status_code, 200)

    async def test_webhook_ack_does_not_wait_for_data(self):
        gate = asyncio.Event()
        async def blocked(raw):
            await gate.wait()
            return [{'type': 'text', 'text': 'done'}]
        self.main.app.state.controller.reply = blocked
        r = await asyncio.wait_for(self.post({'events': [self.event()]}), .25)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.sent), 0)
        gate.set()
        await asyncio.gather(*self.main.app.state.pending)
        self.assertEqual(len(self.sent), 1)

    async def test_redelivery_deduplicated(self):
        body = {'events': [self.event()]}
        await self.post(body)
        await self.post(body)
        await asyncio.gather(*self.main.app.state.pending)
        self.assertEqual(len(self.sent), 1)

    async def test_unknown_event_not_crash(self):
        r = await self.post({'events': [{'type': 'unfollow'}]})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.sent)

    async def test_head_health_ok(self):
        r = await self.client.head('/health')
        self.assertEqual(r.status_code, 200)


if __name__ == '__main__':
    unittest.main()
