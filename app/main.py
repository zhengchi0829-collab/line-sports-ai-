"""LINE entrypoint with immediate signature-verified webhook acknowledgement.

The previous UI and team-data helpers are preserved byte-for-byte in legacy.py.
No external queue or new API key is used. LINE push is used only as a fail-safe when reply delivery fails.
The bounded in-process tasks/cache are not a durable production queue.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from .team_logos import rendered_team_logo, _badge as fallback_team_badge
from .schedule import latest_analysis_slot, platform_refresh_window, platform_day

from . import legacy as ui
from .reliability import (Controller, Game, Snapshot, SPORTS, TZ, VERSION,
                          fixture_cards, notice, quick_menu, date_category_menu,
                          date_league_menu, odds_category_menu, odds_league_menu, odds_source_card,
                          select_games, SPECIFIED, holographic_messages,
                          odds_fixture_picker, odds_single_match_card,
                          odds_today_available, odds_available_menu,
                          verified_form_match)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("sports_bot")
log.setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)


async def startup_probe(app):
    """Probe real data and validate Flex payloads without messaging any user."""
    ctl = app.state.controller
    try:
        results = await asyncio.gather(*(ctl.fixtures.get(s) for s in SPORTS))
        for sport, snap in zip(SPORTS, results):
            log.info("startup_probe version=%s sport=%s count=%s errors=%s",
                     VERSION, sport, len(snap.games), ",".join(snap.errors) or "none")
        samples = [ui.menu_card(), notice("basketball", Snapshot(errors=["test"]))]
        for name in SPORTS.values():
            card = ui.offline_analysis(name, "A vs B")
            assert isinstance(card, dict) and card.get("type") == "flex"
            assert len(card["contents"]["body"]["contents"]) >= 3
            samples.append(card)
        # Verify today's matches cannot leak into future, and vice versa.
        check_now = datetime.now(TZ)
        sample_today = Game("today-test", "soccer", "test", check_now, "Team A", "Team B")
        sample_tomorrow = Game("tomorrow-test", "soccer", "test",
                               check_now + timedelta(days=1), "Team C", "Team D")
        assert [g.ident for g in select_games([sample_today, sample_tomorrow],
                                                check_now, today=True)] == ["today-test"]
        assert [g.ident for g in select_games([sample_today, sample_tomorrow],
                                                check_now, future=True)] == ["tomorrow-test"]
        for sport in SPORTS.values():
            handicap_card = ui.offline_analysis(sport, "A vs B")
            assert any("讓球" in str(row) for row in handicap_card["contents"]["body"]["contents"])
        sample_past = Game("past-test", "baseball", "MLB",
                           check_now - timedelta(days=2), "A", "B", state="final")
        assert [g.ident for g in select_games(
            [sample_today, sample_tomorrow, sample_past], check_now,
            on_date=sample_past.start.date())] == ["past-test"]
        dated = fixture_cards("baseball", Snapshot(games=[sample_today], fetched=check_now),
                              ui.team_zh, on_date=check_now.date(), now=check_now)
        dated_command = dated["contents"]["contents"][0]["footer"]["contents"][0]["action"]["text"]
        assert check_now.date().isoformat() in dated_command
        # Champions League regression cases: an aggregate or penalty winner
        # is NOT necessarily the winner of this particular fixture. Also
        # never use results that occur after the selected kickoff.
        fixture_cutoff = datetime(2026, 11, 25, 4, 0, tzinfo=TZ)
        def ucl_result(when, ours, theirs, *, complete=True):
            return {"date": when, "competitions": [{
                "status": {"type": {"completed": complete}},
                "competitors": [
                    {"team": {"id": "home"}, "score": {"displayValue": str(ours)},
                     "winner": True},
                    {"team": {"id": "away"}, "score": {"value": theirs},
                     "winner": False},
                ]}]}
        drawn = verified_form_match(
            ucl_result("2026-11-04T20:00:00Z", 1, 1),
            "home", fixture_cutoff, soccer=True)
        assert drawn is not None and drawn[1:] == ("D", 1, 1)
        assert verified_form_match(
            ucl_result("2026-11-04T20:00:00Z", 0, 2),
            "home", fixture_cutoff, soccer=True)[1] == "L"
        assert verified_form_match(
            ucl_result("2026-11-04T20:00:00Z", 3, 1),
            "home", fixture_cutoff, soccer=True)[1] == "W"
        assert verified_form_match(
            ucl_result("2026-11-25T02:00:00Z", 3, 1),
            "home", fixture_cutoff, soccer=True) is None
        assert verified_form_match(
            ucl_result("2026-11-04T20:00:00Z", 3, 1, complete=False),
            "home", fixture_cutoff, soccer=True) is None
        log.info("ucl_form_logic_test=passed date_cutoff=yes draw_vs_winner=yes")
        # Sportsbook prices must appear only in the explicitly selected odds menu.
        odds_ui = odds_category_menu()
        odds_actions = str(odds_ui)
        assert "台灣運彩賠率" in odds_actions and "娛樂平台賠率" in odds_actions
        assert "賠率資料狀態" not in odds_actions
        for provider in ("台灣運彩賠率", "娛樂平台賠率"):
            source = provider.removesuffix("賠率")
            sport_menu = odds_league_menu(provider)
            top = str(sport_menu)
            for sport, leagues in (
                    ("足球", ("英超", "西甲", "德甲", "義甲", "法甲", "歐冠")),
                    ("籃球", ("NBA", "NCAA", "NBL", "TPBL")),
                    ("棒球", ("MLB", "NPB", "KBO", "CPBL", "MiLB"))):
                assert "賠率分類" + source + sport in top
                league_menu = odds_league_menu(provider, sport)
                choices = str(league_menu)
                for league in leagues:
                    assert "賠率" + source + league in choices
                assert "返回球類分類" in choices
            assert "賠率" + source + "英超" not in top  # hidden until chosen
        no_odds = await app.state.controller.reply("分析NBA 未驗證主隊 vs 未驗證客隊")
        assert not any("獨贏賠率" in str(card) or "真實盤口" in str(card)
                       or "盤口更新：" in str(card) for card in no_odds)
        # Validate quoted markets based on the *actual* snapshot fields.
        # Screenshots sometimes have 1X2 only; verified published references
        # may additionally contain totals. Never demand absent markets.
        today_tw = app.state.controller.odds.official_daily_rows(
            "足球", check_now.strftime("%Y-%m-%d"))
        if today_tw:
            selected_card = (await app.state.controller.reply(
                "賠率單場台灣運彩足球#0"))[0]
            card_text = str(selected_card)
            assert "獨贏分析" in card_text
            assert "盤口結論" in card_text
            assert "讓分分析：尚無完整實際盤口" in card_text
            first = today_tw[0]
            totals_available = (first.get("total") is not None and
                                first.get("over_price") is not None and
                                first.get("under_price") is not None)
            if totals_available:
                assert "大小分分析：" in card_text
                assert "大小分分析：尚無完整實際盤口" not in card_text
            else:
                assert "大小分分析：尚無完整實際盤口" in card_text
            log.info("tw_odds_market_analysis=passed count=%s totals=%s",
                     len(today_tw), totals_available)
        today_available = odds_today_available(app.state.controller.odds)
        if sum(today_available.values()):
            home_menu = (await app.state.controller.reply("台灣運彩賠率"))[0]
            assert "台彩已更新" in str(home_menu)
            available_menu = (await app.state.controller.reply("台彩已更新"))[0]
            for league, count in today_available.items():
                if count:
                    assert "賠率台灣運彩" + league in str(available_menu)
                    event_menu = (await app.state.controller.reply(
                        "賠率台灣運彩" + league))[0]
                    assert "獨贏｜" in str(event_menu)
            missing_leagues = [league for league, count in today_available.items()
                               if count == 0]
            if missing_leagues:
                missing_card = (await app.state.controller.reply(
                    "賠率台灣運彩" + missing_leagues[0]))[0]
                assert "台彩已更新" in str(missing_card)
            log.info("tw_odds_direct_availability=passed total=%s",
                     sum(today_available.values()))
        png = fallback_team_badge("測試隊", "足球")
        assert png.startswith(bytes.fromhex("89504e470d0a1a0a")) and 200 < len(png) < 50000
        log.info("team_logo_local_png_selfcheck=passed bytes=%d", len(png))
        log.info("odds_menu_exclusive_checks=passed version=%s", VERSION)
        log.info("date_filter_checks=passed version=%s", VERSION)
        log.info("startup_ui_checks=passed version=%s", VERSION)
        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        if token:
            headers = {"Authorization": "Bearer " + token}
            response = await app.state.line_client.post(
                "https://api.line.me/v2/bot/message/validate/reply",
                headers=headers, json={"messages": holographic_messages(samples)})
            app.state.line_validation = response.status_code
            log.info("line_message_validation status=%s version=%s", response.status_code, VERSION)
            new_cards = [date_category_menu("今日賽事"),
                         date_category_menu("未來賽事"),
                         odds_category_menu(),
                         odds_source_card("台灣運彩賠率"),
                         odds_source_card("娛樂平台賠率"),
                         odds_league_menu("台灣運彩賠率",
                                          available=today_available),
                         odds_available_menu(today_available),
                         odds_league_menu("娛樂平台賠率"),
                         *(odds_league_menu(provider, sport,
                              today_available if provider == "台灣運彩賠率" else None)
                           for provider in ("台灣運彩賠率", "娛樂平台賠率")
                           for sport in ("足球", "籃球", "棒球"))]
            for batch_index in range(0, len(new_cards), 5):
                # LINE validation accepts at most five messages per request.
                group = new_cards[batch_index:batch_index + 5]
                checked = await app.state.line_client.post(
                    "https://api.line.me/v2/bot/message/validate/reply",
                    headers=headers, json={"messages": holographic_messages(group)})
                log.info("line_new_menus_validation batch=%s status=%s version=%s",
                         batch_index // 5 + 1, checked.status_code, VERSION)
                if checked.status_code != 200:
                    log.warning("line_new_menus_detail batch=%s %s",
                                batch_index // 5 + 1, checked.text[:800])
            # Verify the actual Taiwan screenshot odds cards, including their
            # stable HTTPS team-image URLs, against LINE's card schema.
            snapshot_odds = app.state.controller.odds.official_daily_rows(
                "足球", datetime.now(TZ).strftime("%Y-%m-%d"))
            if snapshot_odds:
                example_cards = [
                    odds_fixture_picker("tw", "足球", snapshot_odds, {}, page=0),
                    odds_single_match_card("tw", "足球", snapshot_odds[0], {}),
                ]
                if len(snapshot_odds) > 4:
                    example_cards.append(
                        odds_fixture_picker("tw", "足球", snapshot_odds, {}, page=1))
                if len(snapshot_odds) > 4:
                    assert "賠率頁台灣運彩足球#1" in str(example_cards[0])
                    assert "賠率頁台灣運彩足球#0" in str(example_cards[-1])
                    assert "賠率單場台灣運彩足球#4" in str(example_cards[-1])
                checked = await app.state.line_client.post(
                    "https://api.line.me/v2/bot/message/validate/reply",
                    headers=headers, json={"messages": holographic_messages(example_cards)})
                log.info("line_taiwan_odds_logo_cards status=%s fixtures=%s",
                         checked.status_code, len(snapshot_odds))
                if checked.status_code != 200:
                    log.warning("line_taiwan_odds_logo_cards_detail %s", checked.text[:750])
            # Validate primary navigation in one official LINE schema request.
            theme_cards = [
                (await app.state.controller.reply("選單"))[0],
                (await app.state.controller.reply("足球"))[0],
                (await app.state.controller.reply("籃球"))[0],
                (await app.state.controller.reply("棒球"))[0],
            ]
            check_theme = await app.state.line_client.post(
                "https://api.line.me/v2/bot/message/validate/reply",
                headers=headers, json={"messages": holographic_messages(theme_cards)})
            log.info("line_holographic_menus_validation status=%s version=%s",
                     check_theme.status_code, VERSION)
            if check_theme.status_code != 200:
                log.warning("line_holographic_menus_detail %s",
                            check_theme.text[:850])
            date_cards = [date_category_menu(SPECIFIED),
                          date_category_menu(SPECIFIED, check_now.date()),
                          date_league_menu("今日賽事", "棒球", check_now.date())]
            check_date = await app.state.line_client.post(
                "https://api.line.me/v2/bot/message/validate/reply",
                headers=headers, json={"messages": holographic_messages(date_cards)})
            log.info("line_date_menus_validation status=%s version=%s",
                     check_date.status_code, VERSION)
            if check_date.status_code != 200:
                log.warning("line_date_menus_detail %s", check_date.text[:900])
            for sport, snap in zip(SPORTS, results):
                if snap.games:
                    card = fixture_cards(sport, snap, ui.team_zh)
                    checked = await app.state.line_client.post(
                        "https://api.line.me/v2/bot/message/validate/reply",
                        headers=headers, json={"messages": holographic_messages([card])})
                    log.info("line_fixture_validation sport=%s status=%s", sport, checked.status_code)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.error("startup_probe failed=%s", type(exc).__name__)


async def inspect_line_rich_menu(app):
    """Log button action types for the default LINE rich menu, never user data."""
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token:
        return
    headers = {"Authorization": "Bearer " + token}
    try:
        default = await app.state.line_client.get(
            "https://api.line.me/v2/bot/user/all/richmenu", headers=headers)
        if default.status_code != 200:
            log.warning("line_richmenu_default status=%s", default.status_code)
            return
        menu_id = default.json().get("richMenuId")
        if not menu_id:
            log.warning("line_richmenu_default missing_id")
            return
        details = await app.state.line_client.get(
            "https://api.line.me/v2/bot/richmenu/" + menu_id, headers=headers)
        if details.status_code != 200:
            log.warning("line_richmenu_detail status=%s", details.status_code)
            return
        menu = details.json()
        actions = [{
            "type": area.get("action", {}).get("type"),
            "text": str(area.get("action", {}).get("text", ""))[:35],
            "data": str(area.get("action", {}).get("data", ""))[:35],
            "uri": bool(area.get("action", {}).get("uri")),
        } for area in menu.get("areas", [])]
        log.warning("line_richmenu_actions count=%s actions=%s",
                    len(actions), actions)
        app.state.richmenu_check = {"action_count": len(actions), "actions": actions}
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        log.warning("line_richmenu_inspect_error kind=%s", type(exc).__name__)


async def configure_and_verify_rich_menu(app):
    """Publish the six sports/odds buttons before verifying the active menu."""
    updated = await ui.ensure_persistent_rich_menu()
    await inspect_line_rich_menu(app)
    actions = app.state.richmenu_check.get("actions") or []
    texts = {action.get("text") for action in actions}
    found = "檢視賠率" in texts
    app.state.richmenu_check["odds_entry_ready"] = found
    log.info("line_richmenu_odds_entry configured=%s present=%s", updated, found)
    if not found:
        log.warning("line_richmenu_odds_entry missing_after_setup")


async def verify_line_webhook(app):
    """Inspect LINE webhook configuration and repair a stale endpoint if possible."""
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token:
        log.warning("line_webhook_check unavailable=missing_access_token")
        app.state.webhook_check = {"status": "missing_token"}
        return
    url = "https://api.line.me/v2/bot/channel/webhook/endpoint"
    expected = os.getenv("LINE_WEBHOOK_URL", "").strip() or (
        "https://line-sports-ai-bot.onrender.com/webhooks/line")
    headers = {"Authorization": "Bearer " + token}
    try:
        response = await app.state.line_client.get(url, headers=headers)
        if response.status_code != 200:
            log.warning("line_webhook_check status=%s", response.status_code)
            app.state.webhook_check = {"status": "api_error", "http": response.status_code}
            return
        details = response.json()
        existing = str(details.get("endpoint") or "")
        active = details.get("active") is True
        changed = False
        if existing.rstrip("/") != expected.rstrip("/"):
            update = await app.state.line_client.put(
                url, headers=headers, json={"endpoint": expected})
            changed = update.status_code == 200
            log.warning("line_webhook_endpoint_mismatch repair_status=%s",
                        update.status_code)
            if not changed:
                app.state.webhook_check = {"status": "repair_failed", "http": update.status_code,
                                           "active": active}
                return
        test_ok = False
        test_http = 0
        test_reason = "unknown"
        # The free Render instance can be cold during its first LINE probe.
        # A single failed probe must not be mistaken for an inactive channel.
        # Retry only the standard API test, never any customer message.
        for attempt in range(2):
            try:
                test = await app.state.line_client.post(
                    "https://api.line.me/v2/bot/channel/webhook/test",
                    headers=headers, json={"endpoint": expected})
                test_http = test.status_code
                payload = test.json() if test.content else {}
                test_ok = test_http == 200 and payload.get("success") is True
                test_reason = str(payload.get("reason") or payload.get("message")
                                  or payload.get("error") or "unknown")[:140]
                if test_ok:
                    break
                log.warning("line_webhook_test_attempt attempt=%s http=%s reason=%s",
                            attempt + 1, test_http, test_reason)
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                test_reason = type(exc).__name__
                log.warning("line_webhook_test_attempt attempt=%s failure=%s",
                            attempt + 1, test_reason)
            if attempt == 0:
                await asyncio.sleep(8)
        app.state.webhook_check = {
            "status": "ready" if active and test_ok else
                      "inactive" if not active else "test_failed",
            "active": active, "endpoint_matches": True,
            "fixed_endpoint": changed, "test_success": test_ok,
            "test_http": test_http, "test_reason": test_reason if not test_ok else None,
        }
        log.warning("line_webhook_check active=%s endpoint_matches=%s updated=%s test=%s http=%s",
                    active, True, changed, test_ok, test_http)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        app.state.webhook_check = {"status": "exception", "error": type(exc).__name__}
        log.warning("line_webhook_check exception=%s", type(exc).__name__)


async def dated_flow_probe(app):
    """Smoke-test the complete dated fixture -> LINE button -> model flow (no user messages)."""
    await asyncio.sleep(15)
    ctl = app.state.controller
    # June is within the 2026 NPB/KBO/MLB regular seasons.
    selected = datetime(2026, 6, 20, tzinfo=TZ).date()
    for league, key in (("MLB", "baseball"), ("NPB", "baseball_npb"),
                        ("KBO", "baseball_kbo"), ("CPBL", "baseball_cpbl"),
                        ("MiLB", "baseball_milb")):
        try:
            snap = await asyncio.wait_for(ctl.fixtures.on_date(key, selected), timeout=12)
            if not snap.games:
                log.warning("dated_analysis_probe league=%s date=%s status=no_games errors=%s",
                            league, selected, snap.errors[:3])
                app.state.date_probe_status[league] = "no_games"
                continue
            game = snap.games[0]
            card = fixture_cards(key, Snapshot([game], snap.fetched, snap.errors,
                                                snap.successes, snap.stale),
                                 ui.team_zh, on_date=selected)
            if game.state == "final":
                bubble = card["contents"]["contents"][0]
                if "footer" in bubble:
                    raise AssertionError("completed fixture still offers analysis")
                body = str(bubble["body"]["contents"])
                expected = (f"{game.home_score}：{game.away_score}"
                            if game.home_score is not None and game.away_score is not None
                            else "比分未提供")
                if expected not in body or "推薦" in body or "勝率" in body:
                    raise AssertionError("completed fixture must show final score only")
                command = (f"分析{league} {selected.isoformat()} "
                           f"{game.home} vs {game.away}")
                analysis = await asyncio.wait_for(ctl.reply(command), timeout=16)
                result = analysis[0] if analysis else {}
                if (result.get("type") != "flex" or
                        expected not in str(result.get("contents", {})) or
                        "footer" in result.get("contents", {})):
                    raise AssertionError("old analysis action must return score only")
            else:
                command = card["contents"]["contents"][0]["footer"]["contents"][0]["action"]["text"]
                if selected.isoformat() not in command:
                    raise AssertionError("analysis button lost its fixture date")
                analysis = await asyncio.wait_for(ctl.reply(command), timeout=16)
                if not (analysis and analysis[0].get("type") == "flex"):
                    raise AssertionError("future fixture missing analysis response")
            token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
            if token:
                response = await app.state.line_client.post(
                    "https://api.line.me/v2/bot/message/validate/reply",
                    headers={"Authorization": "Bearer " + token},
                    json={"messages": holographic_messages([card, *analysis[:1]])})
                if response.status_code != 200:
                    log.warning("dated_analysis_line_validation league=%s status=%s detail=%s",
                                league, response.status_code, response.text[:350])
                    raise AssertionError("LINE rejected dated fixture / analysis card")
            app.state.date_probe_status[league] = "passed"
            log.info("dated_analysis_probe league=%s date=%s status=passed games=%s",
                     league, selected, len(snap.games))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            app.state.date_probe_status[league] = "failed:" + type(exc).__name__
            log.warning("dated_analysis_probe league=%s date=%s status=failed kind=%s",
                        league, selected, type(exc).__name__)


REFRESH_KEYS = (
    "soccer",
    "soccer_epl",
    "soccer_laliga",
    "soccer_bundesliga",
    "soccer_seriea",
    "soccer_ligue1",
    "soccer_ucl",
    "basketball",
    "basketball_ncaa",
    "basketball_nbl",
    "basketball_tpbl",
    "baseball",
    "baseball_npb",
    "baseball_kbo",
    "baseball_cpbl",
    "baseball_milb",
)


async def continuous_refresh(app):
    """Schedule data refresh independently from the every-minute health pulse."""
    await asyncio.sleep(10)
    last_webhook_check = time.monotonic()
    while True:
        started = time.monotonic()
        try:
            now = datetime.now(TZ)
            # Keep a health heartbeat even when no 3x/day fixture slot is due.
            app.state.last_background_refresh = time.time()
            # Reading GitHub's public odds-cache does NOT spend The Odds API credits.
            # The cache writer runs hourly, not once per ten minutes.
            await app.state.controller.odds.refresh_remote_reference()
            if platform_refresh_window(now):
                day = platform_day(now)
                if app.state.platform_scheduled_attempt_day != day:
                    app.state.platform_scheduled_attempt_day = day
                    if app.state.controller.odds.daily.get("day") != day:
                        await app.state.controller.odds.refresh_platform_daily()
                    else:
                        log.info("scheduled_platform_odds day=%s already_cached=true", day)
            # Explicit user-requested one-off recovery on 2026-11-20.
            # The routine requires a shared saved attempt marker BEFORE any
            # chargeable calls and refuses to fetch if today's record exists.
            manual_day = os.getenv("PLATFORM_ODDS_MANUAL_DAY", "").strip()
            if (manual_day == "2026-11-20"
                    and now.date().isoformat() == manual_day
                    and app.state.platform_scheduled_attempt_day != "manual:" + manual_day):
                app.state.platform_scheduled_attempt_day = "manual:" + manual_day
                await app.state.controller.odds.restore_shared_daily()
                if app.state.controller.odds.daily.get("day") != manual_day:
                    log.warning("platform_manual_recovery start day=%s", manual_day)
                    await app.state.controller.odds.refresh_platform_daily()
                else:
                    log.info("platform_manual_recovery already_saved day=%s", manual_day)
            if time.monotonic() - last_webhook_check >= 600:
                last_webhook_check = time.monotonic()
                await verify_line_webhook(app)
            slot = latest_analysis_slot(now)
            if app.state.last_fixture_refresh_slot == slot:
                await asyncio.sleep(60)
                continue
            app.state.last_fixture_refresh_slot = slot
            log.info("scheduled_fixture_refresh start slot=%s", slot)
            results = await asyncio.gather(
                *(app.state.controller.fixtures.refresh(key) for key in REFRESH_KEYS),
                return_exceptions=True,
            )
            counts = {}
            source_successes = {}
            source_errors = {}
            failures = 0
            for key, result in zip(REFRESH_KEYS, results):
                if isinstance(result, Snapshot):
                    counts[key] = len(result.games)
                    source_successes[key] = result.successes
                    source_errors[key] = result.errors[:8]
                    failures += len(result.errors)
                else:
                    counts[key] = -1
                    source_successes[key] = 0
                    source_errors[key] = [type(result).__name__]
                    failures += 1
            app.state.last_background_refresh = time.time()
            app.state.background_refresh_counts = counts
            app.state.background_refresh_successes = source_successes
            app.state.background_refresh_errors = source_errors
            app.state.last_fixture_refresh_epoch = time.time()
            if not getattr(app.state, "team_translation_checked", False):
                app.state.team_translation_checked = True
                for key in REFRESH_KEYS:
                    snap = app.state.controller.fixtures.last.get(key)
                    if snap and snap.games:
                        names = sorted({name for game in snap.games
                                        for name in (game.home, game.away)})
                        # Already-Chinese names do not need translation.
                        def needs_chinese(name):
                            if any("\u3400" <= ch <= "\u9fff" for ch in name):
                                return False
                            if not any("a" <= ch.lower() <= "z" for ch in name):
                                return False
                            return ui.team_display(name) == name
                        missing = [name for name in names if needs_chinese(name)]
                        log.info("team_translation_coverage league=%s translated=%s total=%s missing=%s",
                                 key, len(names) - len(missing), len(names), missing[:16])
                        if missing:
                            for offset in range(0, len(missing), 24):
                                log.info("team_name_gap league=%s group=%s names=%s",
                                         key, offset // 24, missing[offset:offset + 24])
                        # Source metadata coverage is not an image-load test;
                        # log gaps separately so a missing URL is not confused
                        # with a missing Chinese-name mapping.
                        no_logo = sorted({name for game in snap.games
                            for name, url in ((game.home, game.home_logo),
                                              (game.away, game.away_logo))
                            if not (isinstance(url, str) and url.startswith("https://"))})
                        log.info("team_logo_source_coverage league=%s present=%s total=%s missing=%s",
                                 key, len(names) - len(no_logo), len(names), no_logo[:16])
                        if no_logo:
                            for offset in range(0, len(no_logo), 24):
                                log.info("team_logo_gap league=%s group=%s names=%s",
                                         key, offset // 24, no_logo[offset:offset + 24])
            # Once per startup, validate real soccer fixture and analysis cards
            # against the LINE payload schema; this sends no user-facing messages.
            if not app.state.soccer_validation_done:
                app.state.soccer_validation_done = True
                token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
                if token:
                    headers = {"Authorization": "Bearer " + token}
                    labels = ("英超", "西甲", "德甲", "義甲", "法甲", "歐冠")
                    soccer_keys = dict(zip(labels, REFRESH_KEYS[1:7]))
                    for label, key in soccer_keys.items():
                        snapshot = app.state.controller.fixtures.last.get(key)
                        if not snapshot or not snapshot.games:
                            log.warning("soccer_analysis_validation league=%s status=no_games", label)
                            continue
                        game = next((g for g in snapshot.games
                                     if g.state == "live" or g.start >= datetime.now(TZ)), None)
                        if game is None:
                            log.warning("soccer_analysis_validation league=%s status=no_upcoming", label)
                            continue
                        try:
                            card = fixture_cards(key, snapshot, ui.team_zh)
                            response = await app.state.line_client.post(
                                "https://api.line.me/v2/bot/message/validate/reply",
                                headers=headers, json={"messages": [card]})
                            log.info("soccer_fixture_validation league=%s status=%s", label, response.status_code)
                            analysis = await app.state.controller.reply(
                                f"分析{label} {game.home} vs {game.away}")
                            response = await app.state.line_client.post(
                                "https://api.line.me/v2/bot/message/validate/reply",
                                headers=headers, json={"messages": holographic_messages(analysis)})
                            log.info("soccer_analysis_validation league=%s status=%s", label, response.status_code)
                            if response.status_code != 200:
                                log.warning("soccer_analysis_payload_error league=%s details=%s",
                                            label, response.text[:800])
                            if label == "歐冠":
                                # Test the EXACT button text produced by LINE's
                                # fixture card, including fixture date/teams.
                                one_game = fixture_cards(
                                    key, Snapshot([game], snapshot.fetched,
                                                  snapshot.errors, snapshot.successes),
                                    ui.team_zh)
                                cmd = one_game["contents"]["contents"][0][
                                    "footer"]["contents"][0]["action"]["text"]
                                assert cmd.startswith("分析歐冠 ") and game.start.strftime(
                                    "%Y-%m-%d") in cmd
                                direct = await asyncio.wait_for(
                                    app.state.controller.reply(cmd), timeout=13)
                                if not direct or direct[0].get("type") != "flex":
                                    raise AssertionError("UCL button returned no analysis card")
                                checked = await app.state.line_client.post(
                                    "https://api.line.me/v2/bot/message/validate/reply",
                                    headers=headers, json={
                                        "messages": holographic_messages(direct)})
                                log.info("ucl_exact_button_validation status=%s date=%s",
                                         checked.status_code, game.start.date())
                                if checked.status_code != 200:
                                    log.warning("ucl_exact_button_error status=%s reason=%s",
                                                checked.status_code, checked.text[:450])
                        except Exception as exc:
                            log.exception("soccer_analysis_validation league=%s error=%s",
                                      label, type(exc).__name__)
            if not getattr(app.state, "verified_stats_probe_done", False):
                app.state.verified_stats_probe_done = True
                checks = (("英超", "soccer_epl", "足球"),
                          ("NBA", "basketball", "籃球"),
                          ("MLB", "baseball", "棒球"),
                          ("NPB", "baseball_npb", "棒球"),
                          ("KBO", "baseball_kbo", "棒球"),
                          ("CPBL", "baseball_cpbl", "棒球"),
                          ("MiLB", "baseball_milb", "棒球"))
                for league, key, sport_name in checks:
                    snapshot = app.state.controller.fixtures.last.get(key)
                    if league == "CPBL":
                        home, away = "中信兄弟", "統一7-ELEVEn獅"
                    elif league == "NPB":
                        home, away = "Yomiuri Giants", "Hanshin Tigers"
                    elif league == "KBO":
                        home, away = "KIA Tigers", "SSG Landers"
                    elif snapshot and snapshot.games:
                        game = snapshot.games[0]
                        home, away = game.home, game.away
                    else:
                        log.warning("verified_stats_probe league=%s result=no_games", league)
                        continue
                    try:
                        context = await app.state.controller.team_details(
                            sport_name, home, away, league)
                        log.info("verified_stats_probe league=%s form=%s roster=%s recommendation=%s verified=%s",
                                 league, "勝率" in context[0],
                                 "球員" in context[1], bool(context[2]),
                                 "尚無可核實" not in context[0])
                    except Exception as exc:
                        log.error("verified_stats_probe league=%s exception=%s",
                                  league, type(exc).__name__)
            log.info(
                "background_refresh version=%s keys=%s failures=%s elapsed_ms=%d",
                VERSION, len(REFRESH_KEYS), failures,
                int((time.monotonic() - started) * 1000),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("background_refresh_error kind=%s", type(exc).__name__)
        await asyncio.sleep(60)


async def check_platform_quota_once(app):
    """Read account credit balance via free The Odds API /sports endpoint.

    Never emit API keys, response bodies, URLs with credentials, or exception
    strings in logs. This is diagnostic only: no odds are purchased.
    """
    key = (os.getenv("ODDS_API_KEY") or
           os.getenv("THE_ODDS_API_KEY") or "").strip()
    if not key:
        log.warning("the_odds_quota configured=no remaining=unknown used=unknown")
        return {"configured": False, "remaining": None, "used": None}

    try:
        response = await app.state.controller.odds.client.get(
            "https://api.the-odds-api.com/v4/sports/",
            headers={"x-api-key": key}, timeout=8)
        def clean_count(name):
            raw = response.headers.get(name, "")
            return int(raw) if raw.isdecimal() else None
        balance = clean_count("x-requests-remaining")
        used = clean_count("x-requests-used")
        cost = clean_count("x-requests-last")
        log.info("the_odds_quota configured=yes http_status=%s remaining=%s used=%s last=%s",
                 response.status_code, balance if balance is not None else "unknown",
                 used if used is not None else "unknown",
                 cost if cost is not None else "unknown")
        return {"configured": True, "http_status": response.status_code,
                "remaining": balance, "used": used, "last": cost}
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        log.warning("the_odds_quota configured=yes error_kind=%s remaining=unknown",
                    type(exc).__name__)
        return {"configured": True, "remaining": None, "used": None}


@asynccontextmanager
async def lifespan(app):
    app.state.pending = set()
    app.state.events = {}
    app.state.line_validation = None
    app.state.last_background_refresh = None
    app.state.last_fixture_refresh_slot = None
    app.state.last_fixture_refresh_epoch = None
    app.state.platform_scheduled_attempt_day = None
    app.state.background_refresh_counts = {}
    app.state.background_refresh_successes = {}
    app.state.background_refresh_errors = {}
    app.state.bot_started_epoch = time.time()
    app.state.soccer_validation_done = False
    app.state.date_probe_status = {}
    app.state.webhook_check = {"status": "checking"}
    app.state.richmenu_check = {"status": "checking"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=2.5, pool=6),
                                 follow_redirects=True,
                                 limits=httpx.Limits(max_connections=48,
                                                     max_keepalive_connections=24)) as source_client, \
               httpx.AsyncClient(timeout=httpx.Timeout(5, connect=2)) as line_client:
        app.state.controller = Controller(ui, source_client)
        await app.state.controller.odds.restore_shared_daily()
        app.state.team_logo_client = source_client
        # Log boolean readiness only. Never expose trial token or tester user IDs.
        log.info("jbot_trial_setup key_present=%s tester_ids_configured=%s",
                 bool(os.getenv("JBOT_API_TOKEN", "").strip()),
                 bool(os.getenv("JBOT_TRIAL_USER_IDS", "").strip()))
        app.state.line_client = line_client
        platform_quota_probe = asyncio.create_task(check_platform_quota_once(app))
        probe = asyncio.create_task(startup_probe(app))
        refresher = asyncio.create_task(continuous_refresh(app))
        dated_probe = asyncio.create_task(dated_flow_probe(app))
        webhook_probe = asyncio.create_task(verify_line_webhook(app))
        richmenu_task = asyncio.create_task(configure_and_verify_rich_menu(app))
        yield
        platform_quota_probe.cancel()
        probe.cancel()
        refresher.cancel()
        dated_probe.cancel()
        webhook_probe.cancel()
        richmenu_task.cancel()
        active = list(app.state.pending)
        if active:
            done, unfinished = await asyncio.wait(active, timeout=4)
            for task in unfinished:
                task.cancel()
        await asyncio.gather(platform_quota_probe, probe, refresher, dated_probe,
                             webhook_probe, richmenu_task,
                             *active, return_exceptions=True)
        await app.state.controller.close()
    # Versioned rich menu is reused on subsequent deploys; old menu retained on failure.


app = FastAPI(title="LINE Sports AI", version=VERSION, lifespan=lifespan)


@app.get("/team-logo")
async def serve_team_logo(name: str = "隊伍", src: str = "", sport: str = ""):
    """Return a validated club image or a locally rendered team badge.

    LINE always receives actual image/png on our stable HTTPS URL; the public
    endpoint never redirects to an unreliable CDN or returns SVG/HTML.
    """
    png = await rendered_team_logo(app.state.team_logo_client, name, src, sport)
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=600",
                             "X-Content-Type-Options": "nosniff"})



@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "ok", "service": "line-sports-ai", "version": VERSION,
            "commit": os.getenv("RENDER_GIT_COMMIT", "")[:12]}


@app.get("/health/features")
async def feature_health():
    ctl = app.state.controller
    platform = ctl.odds.daily
    return {"version": VERSION, "bot_started_epoch": app.state.bot_started_epoch,
            "line_message_validation": app.state.line_validation,
            "platform_odds": {
                "day": platform.get("day"),
                "status": platform.get("state", "not_attempted"),
                "cached_events": sum(len(v) for v in platform.get("leagues", {}).values()
                                     if isinstance(v, list)),
                "shared_cache_enabled": ctl.odds.shared_redis is not None,
                "storage_error": bool(platform.get("storage_error")),
                "attempted_leagues": len(platform.get("attempted_leagues", [])),
                "deferred_leagues": len(platform.get("deferred_leagues", [])),
                "schedule_taipei": "00:30",
            },
            "taiwan_odds": {
                "source": "verified_official_or_labeled_third_party",
                "remote_status": ctl.odds.tw_remote_status.get("status", "not_checked"),
                "last_remote_day": ctl.odds.tw_remote.get("day"),
                "last_checked_at": ctl.odds.tw_remote_status.get("checked_at"),
                "schedule_taipei": "hourly",
            },
            "providers": {sport: {"cached_games": len(item[1].games),
                         "updated": item[1].fetched.isoformat() if item[1].fetched else None}
                          for sport, item in ctl.fixtures.cache.items()},
            "background_refresh": {
                "active": True,
                "interval_seconds": 60,
                "fixture_schedule_taipei": ["09:00", "15:00", "20:00"],
                "last_fixture_slot": app.state.last_fixture_refresh_slot,
                "last_fixture_refresh_epoch": app.state.last_fixture_refresh_epoch,
                "platform_schedule_taipei": "00:30",
                "platform_last_attempt_day": app.state.platform_scheduled_attempt_day,
                "last_run_epoch": app.state.last_background_refresh,
                "counts": app.state.background_refresh_counts,
                "source_successes": app.state.background_refresh_successes,
                "source_errors": app.state.background_refresh_errors,
            },
            "pending_events": len(app.state.pending),
            "dated_analysis_probe": app.state.date_probe_status,
            "line_webhook": app.state.webhook_check,
            "richmenu": app.state.richmenu_check}


async def _push_fallback(event, app, token: str, text_value: str) -> bool:
    """Best-effort second channel when a reply token/send path fails."""
    source = event.get("source", {}) if isinstance(event, dict) else {}
    target = source.get("userId") or source.get("groupId") or source.get("roomId")
    if not isinstance(target, str) or not target:
        log.warning("line_fallback unavailable=no_target")
        return False
    payload = {"to": target, "messages": [{"type": "text", "text": str(text_value)[:5000]}]}
    try:
        response = await app.state.line_client.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": "Bearer " + token},
            json=payload,
        )
        log.info("line_fallback_push version=%s status=%s", VERSION, response.status_code)
        return 200 <= response.status_code < 300
    except httpx.HTTPError as exc:
        log.error("line_fallback_push transport_error=%s", type(exc).__name__)
        return False


async def process_event(event, app):
    """Every accepted command gets either a reply or a best-effort push fallback."""
    started = time.monotonic()
    raw = "\u9078\u55ae"
    if event.get("type") == "message":
        raw = event.get("message", {}).get("text", "")
    elif event.get("type") == "postback":
        postback = event.get("postback", {})
        raw = postback.get("data", "").removeprefix("cmd:")
        params = postback.get("params", {})
        if raw == SPECIFIED and isinstance(params, dict):
            selected_date = params.get("date")
            if isinstance(selected_date, str):
                raw = SPECIFIED + selected_date[:10]

    # Route labels contain no team names, user IDs or chat content.
    if raw.startswith("分析"):
        safe_route = "analysis"
    elif raw.startswith("今日賽事"):
        safe_route = "today"
    elif raw.startswith("未來賽事"):
        safe_route = "upcoming"
    elif raw.startswith("指定日期賽事"):
        safe_route = "custom_date"
    elif raw.startswith("賠率") or raw in ("檢視賠率", "台灣運彩賠率", "娛樂平台賠率"):
        safe_route = "odds"
    elif raw in ("足球", "籃球", "棒球"):
        safe_route = "sport_menu"
    else:
        safe_route = "menu_or_other"
    fallback_text = "系統剛剛忙碌或資料來源暫時無法回應，請再點一次原本的按鈕。"
    preparation_ms = 0
    try:
        # Pass only the sender's LINE user ID to authorize private JBot tests.
        # Never log or persist the ID in the odds cache.
        sender = event.get("source") or {}
        user_id = (sender.get("userId") if isinstance(sender, dict)
                   and sender.get("type") == "user" else None)
        messages = await asyncio.wait_for(
            app.state.controller.reply(raw, user_id=user_id), timeout=18.0)
        messages = holographic_messages(messages)
        if not isinstance(messages, list) or not messages:
            raise ValueError("empty_messages")
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        log.warning("command_timeout raw=%s", str(raw)[:40])
        messages = [{"type": "text", "text": fallback_text, "quickReply": quick_menu()}]
    except Exception as exc:
        log.warning("command_error kind=%s raw=%s", type(exc).__name__, str(raw)[:40])
        messages = [{"type": "text", "text": fallback_text, "quickReply": quick_menu()}]

    preparation_ms = int((time.monotonic() - started) * 1000)
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    payload = {"replyToken": event["replyToken"], "messages": messages[:5]}
    reply_ok = False
    fallback_ok = False
    try:
        response = await app.state.line_client.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={"Authorization": "Bearer " + token},
            json=payload,
        )
        # Invalid Flex/card payload: retry once as plain text while the reply token
        # is still usable.
        if response.status_code == 400:
            try:
                details = response.json().get("details", [])
            except ValueError:
                details = []
            schema_error = any(
                "messages" in str(d.get("property", ""))
                for d in details if isinstance(d, dict)
            )
            if schema_error:
                payload["messages"] = [{
                    "type": "text",
                    "text": "卡片顯示異常，已改用文字回覆。請再點一次原本的按鈕。",
                    "quickReply": quick_menu(),
                }]
                response = await app.state.line_client.post(
                    "https://api.line.me/v2/bot/message/reply",
                    headers={"Authorization": "Bearer " + token},
                    json=payload,
                )
        reply_ok = 200 <= response.status_code < 300
        log.info(
            "line_reply version=%s status=%s elapsed_ms=%d messages=%d route=%s prepare_ms=%d send_ms=%d",
            VERSION, response.status_code,
            int((time.monotonic() - started) * 1000),
            len(payload["messages"]), safe_route, preparation_ms,
            int((time.monotonic() - started) * 1000) - preparation_ms,
        )
        if not reply_ok:
            fallback_ok = await _push_fallback(
                event, app, token,
                "剛剛的操作沒有成功送出回覆，系統已自動補送。請再點一次原本的按鈕。"
            )
    except httpx.HTTPError as exc:
        log.error("line_reply transport_error=%s", type(exc).__name__)
        fallback_ok = await _push_fallback(
            event, app, token,
            "剛剛 LINE 回覆連線失敗，系統已自動補送。請再點一次原本的按鈕。"
        )
    return reply_ok or fallback_ok


@app.post("/webhooks/line")
async def webhook(request: Request):
    secret = os.getenv("LINE_CHANNEL_SECRET", "").strip()
    if not secret:
        raise HTTPException(503, "LINE credentials unavailable")
    raw = await request.body()
    if len(raw) > 256 * 1024:
        raise HTTPException(413, "Payload too large")
    expected = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
    signature = request.headers.get("x-line-signature", "")
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "Invalid signature")
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(body, dict) or not isinstance(body.get("events"), list):
        raise HTTPException(400, "Invalid events")
    events = body["events"]
    log.info("line_webhook_received version=%s events=%s", VERSION, len(events))
    if not events:
        return {"ok": True}
    if not os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip():
        raise HTTPException(503, "LINE reply credentials unavailable")
    now = time.monotonic()
    seen = app.state.events
    for key in [key for key, expiry in seen.items() if expiry <= now]:
        seen.pop(key, None)
    if len(app.state.pending) + len(events) > 64:
        raise HTTPException(503, "Busy; retry later")
    for event in events:
        if not isinstance(event, dict) or event.get("mode") == "standby":
            continue
        kind = event.get("type")
        if kind not in ("message", "follow", "postback"):
            continue
        if kind == "message" and event.get("message", {}).get("type") != "text":
            continue
        reply_token = event.get("replyToken")
        if not isinstance(reply_token, str) or not reply_token or set(reply_token) == {"0"}:
            continue
        key = str(event.get("webhookEventId") or hashlib.sha256(reply_token.encode()).hexdigest())
        if key in seen:
            log.info("line_webhook_duplicate kind=%s key=%s", kind, key[:12])
            continue
        if len(seen) >= 4096:
            seen.pop(next(iter(seen)))
        seen[key] = now + 30
        log.info("line_command_received version=%s kind=%s key=%s", VERSION, kind, key[:12])
        task = asyncio.create_task(process_event(event, app))
        app.state.pending.add(task)
        def _done(done, event_key=key):
            app.state.pending.discard(done)
            if done.cancelled():
                seen.pop(event_key, None)
                log.warning("line_event_canceled key=%s", event_key[:12])
                return
            try:
                success = done.result()
            except asyncio.CancelledError:
                seen.pop(event_key, None)
                return
            except Exception as exc:
                seen.pop(event_key, None)
                log.error("event_task_unhandled kind=%s", type(exc).__name__)
                return
            if success:
                seen[event_key] = time.monotonic() + 1200
            else:
                seen.pop(event_key, None)
                log.warning("line_event_not_delivered key=%s", event_key[:12])
        task.add_done_callback(_done)
    return {"ok": True}
