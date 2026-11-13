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

from . import legacy as ui
from .reliability import (Controller, Game, Snapshot, SPORTS, TZ, VERSION,
                          fixture_cards, notice, quick_menu, date_category_menu,
                          date_league_menu, odds_category_menu, odds_source_card,
                          select_games, SPECIFIED)

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
        log.info("date_filter_checks=passed version=%s", VERSION)
        log.info("startup_ui_checks=passed version=%s", VERSION)
        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        if token:
            headers = {"Authorization": "Bearer " + token}
            response = await app.state.line_client.post(
                "https://api.line.me/v2/bot/message/validate/reply",
                headers=headers, json={"messages": samples})
            app.state.line_validation = response.status_code
            log.info("line_message_validation status=%s version=%s", response.status_code, VERSION)
            new_cards = [date_category_menu("今日賽事"),
                         date_category_menu("未來賽事"),
                         odds_category_menu(), odds_source_card("台灣運彩賠率"),
                         odds_source_card("娛樂平台賠率")]
            checked = await app.state.line_client.post(
                "https://api.line.me/v2/bot/message/validate/reply",
                headers=headers, json={"messages": new_cards})
            log.info("line_new_menus_validation status=%s version=%s", checked.status_code, VERSION)
            if checked.status_code != 200:
                log.warning("line_new_menus_detail %s", checked.text[:800])
            date_cards = [date_category_menu(SPECIFIED),
                          date_category_menu(SPECIFIED, check_now.date()),
                          date_league_menu("今日賽事", "棒球", check_now.date())]
            check_date = await app.state.line_client.post(
                "https://api.line.me/v2/bot/message/validate/reply",
                headers=headers, json={"messages": date_cards})
            log.info("line_date_menus_validation status=%s version=%s",
                     check_date.status_code, VERSION)
            if check_date.status_code != 200:
                log.warning("line_date_menus_detail %s", check_date.text[:900])
            for sport, snap in zip(SPORTS, results):
                if snap.games:
                    card = fixture_cards(sport, snap, ui.team_zh)
                    checked = await app.state.line_client.post(
                        "https://api.line.me/v2/bot/message/validate/reply",
                        headers=headers, json={"messages": [card]})
                    log.info("line_fixture_validation sport=%s status=%s", sport, checked.status_code)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.error("startup_probe failed=%s", type(exc).__name__)


async def dated_flow_probe(app):
    """Smoke-test the complete dated fixture -> LINE button -> model flow (no user messages)."""
    await asyncio.sleep(15)
    ctl = app.state.controller
    # June is within the 2026 NPB/KBO/MLB regular seasons.
    selected = datetime(2026, 6, 20, tzinfo=TZ).date()
    for league, key in (("MLB", "baseball"), ("NPB", "baseball_npb"),
                        ("KBO", "baseball_kbo")):
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
            command = card["contents"]["contents"][0]["footer"]["contents"][0]["action"]["text"]
            if selected.isoformat() not in command:
                raise AssertionError("analysis button lost its fixture date")
            analysis = await asyncio.wait_for(ctl.reply(command), timeout=16)
            ok = bool(analysis and analysis[0].get("type") == "flex"
                      and selected.strftime("%Y/%m/%d")
                      in str(analysis[0].get("contents", {}).get("header", {})))
            if not ok:
                raise AssertionError("dated analysis did not return correct fixture card")
            token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
            if token:
                response = await app.state.line_client.post(
                    "https://api.line.me/v2/bot/message/validate/reply",
                    headers={"Authorization": "Bearer " + token},
                    json={"messages": [card, *analysis[:1]]})
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
    """Keep fixture caches warm while the service is running."""
    # Give startup validation a short head start, then continuously refresh every 5 minutes.
    await asyncio.sleep(10)
    while True:
        started = time.monotonic()
        try:
            results = await asyncio.gather(
                *(app.state.controller.fixtures.refresh(key) for key in REFRESH_KEYS),
                return_exceptions=True,
            )
            counts = {}
            failures = 0
            for key, result in zip(REFRESH_KEYS, results):
                if isinstance(result, Snapshot):
                    counts[key] = len(result.games)
                    failures += len(result.errors)
                else:
                    counts[key] = -1
                    failures += 1
            app.state.last_background_refresh = time.time()
            app.state.background_refresh_counts = counts
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
                                headers=headers, json={"messages": analysis})
                            log.info("soccer_analysis_validation league=%s status=%s", label, response.status_code)
                            if response.status_code != 200:
                                log.warning("soccer_analysis_payload_error league=%s details=%s",
                                            label, response.text[:800])
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
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app):
    app.state.pending = set()
    app.state.events = {}
    app.state.line_validation = None
    app.state.last_background_refresh = None
    app.state.background_refresh_counts = {}
    app.state.soccer_validation_done = False
    app.state.date_probe_status = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(3.5, connect=2), follow_redirects=True,
                                 limits=httpx.Limits(max_connections=12)) as source_client, \
               httpx.AsyncClient(timeout=httpx.Timeout(5, connect=2)) as line_client:
        app.state.controller = Controller(ui, source_client)
        app.state.line_client = line_client
        probe = asyncio.create_task(startup_probe(app))
        refresher = asyncio.create_task(continuous_refresh(app))
        dated_probe = asyncio.create_task(dated_flow_probe(app))
        yield
        probe.cancel()
        refresher.cancel()
        dated_probe.cancel()
        active = list(app.state.pending)
        if active:
            done, unfinished = await asyncio.wait(active, timeout=4)
            for task in unfinished:
                task.cancel()
        await asyncio.gather(probe, refresher, dated_probe,
                             *active, return_exceptions=True)
        await app.state.controller.close()
    # Keep the existing LINE default rich menu. Recreating it on every deploy
    # needlessly downloads a large font and risks switching to an unfinished menu.


app = FastAPI(title="LINE Sports AI", version=VERSION, lifespan=lifespan)


@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "ok", "service": "line-sports-ai", "version": VERSION,
            "commit": os.getenv("RENDER_GIT_COMMIT", "")[:12]}


@app.get("/health/features")
async def feature_health():
    ctl = app.state.controller
    return {"version": VERSION, "line_message_validation": app.state.line_validation,
            "providers": {sport: {"cached_games": len(item[1].games),
                         "updated": item[1].fetched.isoformat() if item[1].fetched else None}
                          for sport, item in ctl.fixtures.cache.items()},
            "background_refresh": {
                "active": True,
                "interval_seconds": 300,
                "last_run_epoch": app.state.last_background_refresh,
                "counts": app.state.background_refresh_counts,
            },
            "pending_events": len(app.state.pending),
            "dated_analysis_probe": app.state.date_probe_status}


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

    fallback_text = "系統剛剛忙碌或資料來源暫時無法回應，請再點一次原本的按鈕。"
    try:
        messages = await asyncio.wait_for(app.state.controller.reply(raw), timeout=18.0)
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
            "line_reply version=%s status=%s elapsed_ms=%d messages=%d",
            VERSION, response.status_code,
            int((time.monotonic() - started) * 1000),
            len(payload["messages"]),
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
