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

import httpx
from fastapi import FastAPI, HTTPException, Request

from . import legacy as ui
from .reliability import Controller, Game, Snapshot, SPORTS, TZ, VERSION, fixture_cards, notice, quick_menu

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
        log.info("startup_ui_checks=passed version=%s", VERSION)
        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        if token:
            headers = {"Authorization": "Bearer " + token}
            response = await app.state.line_client.post(
                "https://api.line.me/v2/bot/message/validate/reply",
                headers=headers, json={"messages": samples})
            app.state.line_validation = response.status_code
            log.info("line_message_validation status=%s version=%s", response.status_code, VERSION)
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


@asynccontextmanager
async def lifespan(app):
    app.state.pending = set()
    app.state.events = {}
    app.state.line_validation = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(3.5, connect=2), follow_redirects=True,
                                 limits=httpx.Limits(max_connections=12)) as source_client, \
               httpx.AsyncClient(timeout=httpx.Timeout(5, connect=2)) as line_client:
        app.state.controller = Controller(ui, source_client)
        app.state.line_client = line_client
        probe = asyncio.create_task(startup_probe(app))
        yield
        probe.cancel()
        for task in list(app.state.pending):
            task.cancel()
        await asyncio.gather(probe, *app.state.pending, return_exceptions=True)
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
            "pending_events": len(app.state.pending)}


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
        raw = event.get("postback", {}).get("data", "").removeprefix("cmd:")

    fallback_text = "系統剛剛忙碌或資料來源暫時無法回應，請再點一次原本的按鈕。"
    try:
        messages = await asyncio.wait_for(app.state.controller.reply(raw), timeout=5.0)
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
            await _push_fallback(
                event, app, token,
                "剛剛的操作沒有成功送出回覆，系統已自動補送。請再點一次原本的按鈕。"
            )
    except httpx.HTTPError as exc:
        log.error("line_reply transport_error=%s", type(exc).__name__)
        await _push_fallback(
            event, app, token,
            "剛剛 LINE 回覆連線失敗，系統已自動補送。請再點一次原本的按鈕。"
        )


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
            continue
        if len(seen) >= 4096:
            seen.pop(next(iter(seen)))
        seen[key] = now + 1200
        task = asyncio.create_task(process_event(event, app))
        app.state.pending.add(task)
        def _done(done):
            app.state.pending.discard(done)
            if done.cancelled():
                return
            try:
                exc = done.exception()
            except asyncio.CancelledError:
                return
            if exc:
                log.error("event_task_unhandled kind=%s", type(exc).__name__)
        task.add_done_callback(_done)
    return {"ok": True}
