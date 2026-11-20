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
from datetime import date, datetime, timedelta, timezone
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
    def offline_analysis(sport, matchup, history=None, players=None, **kwargs):
        return {'type': 'flex', 'altText': matchup, 'contents': {'type': 'bubble',
            'header': {'type': 'box', 'layout': 'vertical', 'contents': [
                {'type': 'text', 'text': sport},
                {'type': 'text', 'text': matchup}]},
            'body': {'type': 'box', 'layout': 'vertical', 'contents': [
                {'type': 'text', 'text': history or 'no verified history'},
                {'type': 'text', 'text': players or 'no verified roster'},
                {'type': 'box', 'layout': 'vertical', 'contents': []}]},
            'footer': {'type': 'box', 'layout': 'vertical', 'contents': [
                {'type': 'button', 'action': {'type': 'message', 'label': 'Other',
                                               'text': '足球'}}]}}}

    winner_team_details = AsyncMock(return_value=(None, None))




class FastButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_future_analysis_reuses_verified_scheduled_fixture(self):
        requests = []
        def handler(req):
            requests.append(str(req.url))
            raise AssertionError("Verified future fixture must not refetch ESPN")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            ctl = Controller(UI, client)
            when = datetime.now(TZ) + timedelta(days=1)
            game = Game("verified-future", "soccer", "英超", when,
                        "Home", "Away", source="ESPN", state="scheduled")
            snap = Snapshot([game], datetime.now(TZ), [], 1)
            ctl.fixtures.last["soccer_epl"] = snap
            ctl.fixtures.cache["soccer_epl"] = (time.monotonic(), snap)
            result = await ctl.fixtures.on_date("soccer_epl", when.date())
            self.assertEqual([g.ident for g in result.games], ["verified-future"])
            self.assertEqual(requests, [])
            await ctl.close()


    async def test_historical_date_cache_reuses_real_snapshot_across_taps(self):
        """Historic exact-date queries should not repeatedly wait for providers."""
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: self.fail("Cached historic dates must not query the network"))) as client:
            ctl = Controller(UI, client)
            historic = date(2026, 6, 1)
            slot = "historical-cached"
            from app.schedule import latest_analysis_slot
            # Set the exact key used by the date-specific cache.
            key = ("soccer_epl", historic, latest_analysis_slot(datetime.now(TZ)))
            verified = Snapshot([], datetime.now(TZ), [], 2)
            ctl.fixtures.date_cache[key] = (time.monotonic() - 3600, verified)
            result = await ctl.fixtures.on_date("soccer_epl", historic)
            self.assertIs(result, verified)
            await ctl.close()

    async def test_failed_historical_date_cache_is_not_kept_for_hours(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(503))) as client:
            ctl = Controller(UI, client)
            from app.schedule import latest_analysis_slot
            old_date = date(2026, 6, 1)
            key = ("soccer_epl", old_date, latest_analysis_slot(datetime.now(TZ)))
            stale = Snapshot([], datetime.now(TZ), ["upstream_unavailable"], 0)
            ctl.fixtures.date_cache[key] = (time.monotonic() - 3600, stale)
            updated = await ctl.fixtures.on_date("soccer_epl", old_date)
            self.assertIsNot(updated, stale)
            self.assertIn("http_503", json.dumps(updated.errors))
            await ctl.close()

    async def test_verified_analysis_stats_cache_last_until_next_slot(self):
        """A repeat click should not rerun ESPN when last-five data was verified."""
        from app.schedule import latest_analysis_slot
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: self.fail("Verified cached stats must not be fetched"))) as client:
            ctl = Controller(UI, client)
            sport, league, first, second = "足球", "英超", "A", "B"
            cached_value = ("5場4勝", "球員A", "A", {"form_games": 5})
            key = (sport, league, first, second, None, None, None,
                   latest_analysis_slot(datetime.now(TZ)))
            ctl.details[key] = (time.monotonic() - 2400, cached_value)
            self.assertEqual(
                await ctl.team_details(sport, first, second, league),
                cached_value)
            await ctl.close()

    async def test_incomplete_analysis_stats_refresh_before_next_slot(self):
        """Unverified data must not be retained for hours just to gain speed."""
        from app.schedule import latest_analysis_slot
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(503))) as client:
            ctl = Controller(UI, client)
            key = ("足球", "英超", "A", "B", None, None, None,
                   latest_analysis_slot(datetime.now(TZ)))
            ctl.details[key] = (time.monotonic() - 2400,
                                ("資料不足", "無主力名單", None, None))
            # ESPN 503 quickly returns an unverified fallback; it must not
            # incorrectly claim the previous incomplete record as verified.
            result = await ctl.team_details("足球", "A", "B", "英超")
            self.assertNotEqual(result[0], "資料不足")
            self.assertIsNone(result[2])
            await ctl.close()

    async def test_today_uses_scheduled_cache_without_external_requests(self):
        requests = []
        def handler(req):
            requests.append(str(req.url))
            raise AssertionError("Today's cached menu must not query ESPN")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            ctl = Controller(UI, client)
            current = datetime.now(TZ)
            tomorrow_game = Game(
                "upcoming-test", "soccer", "英超",
                current + timedelta(hours=1),
                "Home", "Away", source="verified-test")
            ctl.fixtures.last["soccer_epl"] = Snapshot(
                [tomorrow_game], current, [], 1)
            start = time.monotonic()
            result = await ctl.reply("今日賽事英超")
            self.assertLess(time.monotonic() - start, 0.5)
            self.assertEqual(requests, [])
            self.assertIn("Home", json.dumps(result, ensure_ascii=False))
            await ctl.close()

    async def test_cold_today_returns_loading_without_slow_network(self):
        calls = []
        async def slow_handler(req):
            calls.append(str(req.url))
            await asyncio.sleep(.35)
            return httpx.Response(200, json={"events": []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow_handler)) as client:
            ctl = Controller(UI, client)
            started = time.monotonic()
            result = await ctl.reply("今日賽事英超")
            self.assertLess(time.monotonic() - started, .22)
            self.assertIn("背景同步", json.dumps(result, ensure_ascii=False))
            self.assertIn("soccer_epl", ctl.fixtures.jobs)
            await asyncio.sleep(.01)
            self.assertTrue(calls)
            await ctl.close()

    async def test_sports_league_navigation_does_not_wait_for_esports_sources(self):
        async def slow_handler(req):
            await asyncio.sleep(.35)
            return httpx.Response(200, json={"events": []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow_handler)) as client:
            ctl = Controller(UI, client)
            started = time.monotonic()
            response = await ctl.reply("NBA")
            self.assertLess(time.monotonic() - started, .22)
            self.assertIn("背景同步", json.dumps(response, ensure_ascii=False))
            await ctl.close()

class ParserTests(unittest.TestCase):
    def test_espn_basketball_leagues_are_labeled_correctly(self):
        for kind, expected in (('basketball_ncaa', 'NCAA'),
                               ('basketball_nbl', 'NBL'),
                               ('espn', 'NBA')):
            games = parse_feed(kind, {'events': [espn_event()]})
            self.assertEqual(len(games), 1)
            self.assertEqual(games[0].league, expected)

    def test_ucl_event_keeps_authoritative_club_ids(self):
        event = espn_event()
        event['competitions'][0]['competitors'][0]['team']['id'] = 'H'
        event['competitions'][0]['competitors'][1]['team']['id'] = 'A'
        rows = parse_feed('soccer_uefa.champions', {'events': [event]})
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].home_team_id, rows[0].away_team_id), ('H', 'A'))

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

    def test_today_never_includes_future_dates(self):
        games = [Game('1', 'soccer', 'L', NOW + timedelta(days=3), 'H', 'A')]
        self.assertEqual(select_games(games, NOW, today=True), [])
        self.assertEqual(select_games(games, NOW, future=True), games)

    def test_ucl_listing_can_show_nine_same_day_games(self):
        games = [Game(str(i), 'soccer', '歐冠', NOW + timedelta(days=3),
                      'H' + str(i), 'A' + str(i)) for i in range(9)]
        self.assertEqual(len(select_games(games, NOW, future=True)), 9)

    def test_past_fixture_not_promoted_to_future(self):
        g = Game('1', 'soccer', 'L', NOW - timedelta(days=1), 'H', 'A')
        self.assertFalse(select_games([g], NOW))

    def test_card_home_away_and_button_preserved(self):
        g = Game('1', 'basketball', 'NBA', NOW + timedelta(days=1), 'Home', 'Away')
        card = fixture_cards('basketball', Snapshot([g], NOW), str, now=NOW)
        self.assertEqual(card['contents']['contents'][0]['footer']['contents'][0]['action']['text'],
                         '分析NBA 2026-11-13 Home vs Away')
        self.assertIn('quickReply', card)

    def test_error_notice_never_claims_no_matches(self):
        card = json.dumps(notice('basketball', Snapshot(errors=['403'])), ensure_ascii=False)
        self.assertIn('賽程資料來源暫時沒有回應', card)
        self.assertIn('重新查詢', card)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_tenant_game_catalog_is_not_exposed_in_sports_bot(self):
        """Former sports-trial commands are read-only local guidance, not API calls."""
        requests = []
        def handler(req):
            requests.append(str(req.url))
            raise AssertionError("A game-catalog API must never be called")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            ctl = Controller(UI, client)
            for command in ("體育集成檢查", "體育遊戲清單",
                            "集成平台檢查", "集成遊戲清單"):
                result = await ctl.reply(command)
                shown = json.dumps(result, ensure_ascii=False)
                self.assertIn("賠率", shown)
                self.assertIn("沒有接入其遊戲清單", shown)
                for blocked in ("試玩", "ATG", "百家樂", "SUPER體育",
                                "AP體育", "FB體育", "TQ體育"):
                    self.assertNotIn(blocked, shown)
            self.assertEqual(requests, [])
            await ctl.close()

    async def test_odds_and_match_analysis_routes_remain_available(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(500))) as client:
            ctl = Controller(UI, client)
            odds = json.dumps(await ctl.reply("檢視賠率"), ensure_ascii=False)
            self.assertIn("台灣運彩賠率", odds)
            self.assertIn("娛樂平台賠率", odds)
            analysis = json.dumps(await ctl.reply("賽事分析"), ensure_ascii=False)
            self.assertIn("今日", analysis)
            await ctl.close()

    async def test_ucl_five_games_backfilled_from_previous_season(self):
        kickoff = datetime(2026, 11, 25, 4, 0, tzinfo=TZ)
        fixture = Game('ucl-future', 'soccer', '歐冠', kickoff,
                       'Home', 'Away', source='ESPN',
                       home_team_id='H', away_team_id='A')
        def result(ident, when, home_goals, away_goals):
            return {'id': ident, 'date': when, 'competitions': [{
                'status': {'type': {'completed': True}},
                'competitors': [
                    {'team': {'id': 'H'}, 'score': {'value': home_goals}},
                    {'team': {'id': 'A'}, 'score': {'value': away_goals}}]}]}
        recent = [
            result('r1', '2026-09-09T20:00:00Z', 2, 0),
            result('r2', '2026-10-13T20:00:00Z', 0, 1),
            result('r3', '2026-10-21T20:00:00Z', 1, 1),
            result('r4', '2026-11-04T20:00:00Z', 3, 0)]
        older = result('p1', '2025-03-12T20:00:00Z', 1, 0)
        calls = []
        def handler(req):
            calls.append(str(req.url))
            if req.url.path.endswith('/schedule'):
                rows = recent + [older, recent[0]] if req.url.params.get('season') == '2025' else recent
                return httpx.Response(200, json={'events': rows})
            if req.url.path.endswith('/roster'):
                return httpx.Response(200, json={'athletes': []})
            if req.url.path.endswith('/leaders'):
                return httpx.Response(200, json={'leaders': []})
            raise AssertionError('UCL must use IDs from the selected ESPN fixture')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            ctl = Controller(UI, client)
            history, players, winner, metrics = await ctl.team_details(
                '足球', 'Home', 'Away', '歐冠', fixture=fixture)
            self.assertEqual(winner, 'Home')
            self.assertEqual(metrics['form_games'], 5)
            self.assertTrue(metrics['ucl_cross_season'])
            self.assertIn('跨賽季', history)
            self.assertEqual(len([c for c in calls if 'season=2025' in c]), 2)
            await ctl.close()

    async def test_ucl_duplicate_seasons_never_invent_a_fifth_game(self):
        kickoff = datetime(2026, 11, 25, 4, 0, tzinfo=TZ)
        fixture = Game('ucl-future', 'soccer', '歐冠', kickoff,
                       'Home', 'Away', source='ESPN',
                       home_team_id='H', away_team_id='A')
        same = {'id': 'only-one', 'date': '2026-11-04T20:00:00Z',
                'competitions': [{'status': {'type': {'completed': True}},
                    'competitors': [
                        {'team': {'id': 'H'}, 'score': {'value': 1}},
                        {'team': {'id': 'A'}, 'score': {'value': 1}}]}]}
        def handler(req):
            if req.url.path.endswith('/schedule'):
                return httpx.Response(200, json={'events': [same]})
            raise AssertionError('UCL must use fixture team IDs')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            ctl = Controller(UI, client)
            history, _, winner, metrics = await ctl.team_details(
                '足球', 'Home', 'Away', '歐冠', fixture=fixture)
            self.assertEqual((winner, metrics), (None, None))
            self.assertIn('1/5場', history)
            await ctl.close()

    async def test_nba_month_failure_does_not_skip_other_months(self):
        calls = []
        def handler(req):
            calls.append(str(req.url))
            if req.url.params.get('dates') == '202612':
                return httpx.Response(403)
            return httpx.Response(200, json={'events': [espn_event()]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            store = Fixtures(client)
            snap = await store.get('basketball')
            self.assertEqual(len(snap.games), 1)
            self.assertEqual(snap.games[0].source, 'ESPN')
            self.assertEqual(snap.successes, 2)
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
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]['type'], 'flex')
            self.assertIn('今日', json.dumps(messages, ensure_ascii=False))

    async def test_analysis_parsing_works(self):
        async with httpx.AsyncClient() as client:
            ctl = Controller(UI, client)
            ctl.team_details = AsyncMock(return_value=('History', 'Roster', None, None))
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
        async def blocked(raw, user_id=None):
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
