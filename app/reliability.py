"""Independent data sources, bounded caches and resilient LINE navigation.

Only public fixture metadata is cached. No LINE tokens, messages or user IDs are
stored in the data cache. An empty successful feed is NOT a connection failure.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

VERSION = "0.6.22"
TZ = ZoneInfo("Asia/Taipei")
SPORTS = {"soccer": "\u8db3\u7403", "basketball": "\u7c43\u7403", "baseball": "\u68d2\u7403"}
KEYS = {v: k for k, v in SPORTS.items()}
DEMO_PAIRS = {
    "soccer": ("\u62dc\u4ec1\u6155\u5c3c\u9ed1", "\u591a\u7279\u8499\u5fb7"),
    "basketball": ("\u6d1b\u6749\u78ef\u6e56\u4eba", "\u91d1\u5dde\u52c7\u58eb"),
    "baseball": ("\u6d1b\u6749\u78ef\u9053\u5947", "\u7d10\u7d04\u6d0b\u57fa"),
}
MENU = "\u9078\u55ae"
TODAY = "\u4eca\u65e5\u8cfd\u4e8b"
FUTURE = "\u672a\u4f86\u8cfd\u4e8b"
log = logging.getLogger("sports_bot")


def instant(raw: str, assumed_tz=timezone.utc) -> datetime:
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return (dt.replace(tzinfo=assumed_tz) if dt.tzinfo is None else dt).astimezone(TZ)


@dataclass(frozen=True)
class Game:
    ident: str
    sport: str
    league: str
    start: datetime
    home: str
    away: str
    home_logo: str = ""
    away_logo: str = ""
    source: str = ""
    state: str = "scheduled"
    time_known: bool = True


def parse_feed(kind: str, data: Any) -> list[Game]:
    """Normalize providers; a malformed game cannot discard the other games."""
    if kind == "openliga":
        if not isinstance(data, list):
            raise ValueError("OpenLigaDB payload is not a list")
        rows = data
    elif kind.startswith("nba"):
        if not isinstance(data, dict):
            raise ValueError("NBA payload is not an object")
        if kind == "nba_schedule":
            if "leagueSchedule" not in data:
                raise ValueError("NBA season schedule missing")
            rows = [g for d in data["leagueSchedule"].get("gameDates", []) for g in d.get("games", [])]
        else:
            if "scoreboard" not in data:
                raise ValueError("NBA scoreboard missing")
            rows = data["scoreboard"].get("games", [])
    elif kind == "mlb":
        if not isinstance(data, dict) or "dates" not in data:
            raise ValueError("MLB dates missing")
        rows = [g for d in data["dates"] for g in d.get("games", [])]
    else:
        if not isinstance(data, dict) or "events" not in data:
            raise ValueError("ESPN events missing")
        rows = data["events"]
    out = []
    for g in rows:
        try:
            if kind == "openliga":
                if g.get("matchIsFinished"):
                    continue
                raw = g.get("matchDateTimeUTC")
                dt = instant(raw) if raw else instant(g["matchDateTime"], ZoneInfo("Europe/Berlin"))
                h, a = g["team1"], g["team2"]
                item = Game(str(g["matchID"]), "soccer", "\u5fb7\u7532", dt,
                            h["teamName"], a["teamName"], h.get("teamIconUrl", ""),
                            a.get("teamIconUrl", ""), "OpenLigaDB")
            elif kind.startswith("nba"):
                status = int(g.get("gameStatus", 1))
                if status not in (1, 2):
                    continue
                h, a = g["homeTeam"], g["awayTeam"]
                raw = g.get("gameDateTimeUTC") or g.get("gameTimeUTC")
                if not raw:
                    continue
                def nba_name(t):
                    return (t.get("teamCity", "") + " " + t.get("teamName", "")).strip()
                def nba_logo(t):
                    return f"https://cdn.nba.com/logos/nba/{t['teamId']}/primary/L/logo.svg" if t.get("teamId") else ""
                item = Game(str(g["gameId"]), "basketball", "NBA", instant(raw), nba_name(h),
                            nba_name(a), nba_logo(h), nba_logo(a), "NBA",
                            "live" if status == 2 else "scheduled", not bool(g.get("isTimeTBD")))
            elif kind == "mlb":
                status = g.get("status", {})
                state = status.get("abstractGameState")
                if state not in ("Preview", "Live") or any(w in status.get("detailedState", "").lower()
                                                           for w in ("cancel", "postpon")):
                    continue
                h, a = g["teams"]["home"]["team"], g["teams"]["away"]["team"]
                item = Game(str(g["gamePk"]), "baseball", "MLB", instant(g["gameDate"]), h["name"], a["name"],
                            f"https://www.mlbstatic.com/team-logos/{h['id']}.svg",
                            f"https://www.mlbstatic.com/team-logos/{a['id']}.svg", "MLB",
                            "live" if state == "Live" else "scheduled", not status.get("startTimeTBD", False))
            else:
                comp = g["competitions"][0]
                status = comp.get("status", g.get("status", {})).get("type", {})
                if status.get("completed") or status.get("state") == "post" or any(
                        word in status.get("name", "").lower() for word in ("cancel", "postpon")):
                    continue
                h = next(c["team"] for c in comp["competitors"] if c.get("homeAway") == "home")
                a = next(c["team"] for c in comp["competitors"] if c.get("homeAway") == "away")
                item = Game(str(g["id"]), "basketball", "NBA", instant(g["date"]), h["displayName"],
                            a["displayName"], h.get("logo", ""), a.get("logo", ""), "ESPN",
                            "live" if status.get("state") == "in" else "scheduled", not comp.get("timeValid") is False)
            if item.home.strip() and item.away.strip() and item.home != item.away:
                out.append(item)
        except (KeyError, TypeError, ValueError, IndexError, AttributeError, StopIteration):
            continue
    return out


@dataclass
class Snapshot:
    games: list[Game] = field(default_factory=list)
    fetched: datetime | None = None
    errors: list[str] = field(default_factory=list)
    successes: int = 0
    stale: bool = False


class Fixtures:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.cache: dict[str, tuple[float, Snapshot]] = {}
        self.jobs: dict[str, asyncio.Task] = {}
        self.cooldown: dict[str, float] = {}
        self.retry_after: dict[str, float] = {}
        self.last: dict[str, Snapshot] = {}

    async def source(self, name: str, url: str, params: dict, kind: str) -> tuple[list[Game], str | None]:
        if time.monotonic() < self.cooldown.get(name, 0):
            return [], name + ":cooldown"
        try:
            async with asyncio.timeout(4.0):
                response = await self.client.get(url, params=params)
                response.raise_for_status()
                return parse_feed(kind, response.json()), None
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (401, 403, 429):
                delay = 300
                try:
                    delay = max(delay, int(exc.response.headers.get("Retry-After", "300")))
                except ValueError:
                    pass
                self.cooldown[name] = time.monotonic() + min(delay, 86400)
            log.warning("fixture_source source=%s status=%s", name, status)
            return [], f"{name}:http_{status}"
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError) as exc:
            self.cooldown[name] = time.monotonic() + 30
            log.warning("fixture_source source=%s error=%s", name, type(exc).__name__)
            return [], name + ":" + type(exc).__name__

    def specs(self, sport: str, now: datetime) -> list[tuple]:
        if sport == "soccer":
            season = now.year if now.month >= 7 else now.year - 1
            return [("openliga", f"https://api.openligadb.de/getmatchdata/bl1/{season}", {}, "openliga")]
        if sport == "basketball":
            return [
                ("nba_schedule", "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json", {}, "nba_schedule"),
                # The scoreboard date-range query returns HTTP 400 for this feed.
                # Query separate valid calendar dates; never retry a 403 with a proxy.
                ("nba_espn_today", "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
                 {"dates": now.strftime("%Y%m%d")}, "espn"),
                ("nba_espn_next", "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
                 {"dates": (now + timedelta(days=1)).strftime("%Y%m%d")}, "espn"),
            ]
        return [("mlb", "https://statsapi.mlb.com/api/v1/schedule",
                 {"sportId": 1, "startDate": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
                  "endDate": (now + timedelta(days=240)).strftime("%Y-%m-%d")}, "mlb")]

    async def refresh(self, sport: str) -> Snapshot:
        now = datetime.now(TZ)
        results = await asyncio.gather(*(self.source(*spec) for spec in self.specs(sport, now)))
        errors = [error for _, error in results if error]
        # Source errors are independent: a failed NBA source cannot skip the others.
        unique: dict[tuple, Game] = {}
        for games, error in results:
            for g in games:
                key = (g.start.isoformat(), g.home.casefold(), g.away.casefold())
                unique.setdefault(key, g)
        snap = Snapshot(sorted(unique.values(), key=lambda g: g.start), now, errors,
                        sum(error is None for _, error in results))
        old = self.cache.get(sport)
        self.retry_after[sport] = time.monotonic() + (60 if errors else 300)
        if not snap.games and errors and old and old[1].games and time.monotonic() - old[0] < 21600:
            cached_result = Snapshot(old[1].games, old[1].fetched, errors, snap.successes, True)
            self.last[sport] = cached_result
            return cached_result
        self.last[sport] = snap
        if snap.successes:
            self.cache[sport] = (time.monotonic(), snap)
        log.info("fixture_result sport=%s count=%s successes=%s failures=%s",
                 sport, len(snap.games), snap.successes, len(errors))
        return snap

    async def get(self, sport: str) -> Snapshot:
        old = self.cache.get(sport)
        if sport in self.last and time.monotonic() < self.retry_after.get(sport, 0):
            return self.last[sport]
        task = self.jobs.get(sport)
        if not task or task.done():
            task = asyncio.create_task(self.refresh(sport))
            self.jobs[sport] = task
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=4.4)
        except (TimeoutError, httpx.HTTPError):
            if old and time.monotonic() - old[0] < 21600:
                return Snapshot(old[1].games, old[1].fetched, ["refresh_timeout"], 0, True)
            return Snapshot(errors=["refresh_timeout"])

    async def close(self):
        for task in self.jobs.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)


def select_games(games: list[Game], now: datetime, today: bool = False) -> list[Game]:
    valid = [g for g in games if g.state == "live" or g.start >= now or
             (not g.time_known and g.start.date() >= now.date())]
    if today:
        current = [g for g in valid if g.start.date() == now.date()]
        if current:
            valid = current
    return sorted(valid, key=lambda g: (g.state != "live", g.start))[:8]


def text(value: str, size="sm", color="#334155", weight="regular") -> dict:
    return {"type": "text", "text": str(value)[:1000] or "-", "size": size,
            "color": color, "weight": weight, "wrap": True}


def button(label: str, command: str, color="#0F766E") -> dict:
    return {"type": "button", "style": "primary", "height": "sm", "color": color,
            "action": {"type": "message", "label": label[:20], "text": command[:300]}}


def quick_menu() -> dict:
    return {"items": [{"type": "action", "action": {"type": "message", "label": name, "text": name}}
                      for name in [*SPORTS.values(), TODAY, FUTURE, MENU]]}


def notice(sport: str, snapshot: Snapshot) -> dict:
    name = SPORTS.get(sport, "\u8cfd\u4e8b")
    failed = bool(snapshot.errors)
    a, b = DEMO_PAIRS.get(sport, DEMO_PAIRS["soccer"])
    demo = "\u6a21\u64ec\u5206\u6790" + SPORTS.get(sport, SPORTS["soccer"]) + " " + a + " vs " + b
    detail = ("\u8cc7\u6599\u4f86\u6e90\u66ab\u6642\u7121\u6cd5\u9023\u7dda\uff0c\u4e26\u4e0d\u4ee3\u8868\u6c92\u6709\u6bd4\u8cfd\u3002" if failed else
              "\u4f86\u6e90\u5df2\u56de\u61c9\uff0c\u76ee\u524d\u672a\u6536\u9304\u65b0\u7684\u672a\u958b\u8cfd\u8cfd\u7a0b\u3002")
    return {"type": "flex", "altText": name + "\u8cfd\u7a0b\u72c0\u614b", "quickReply": quick_menu(),
            "contents": {"type": "bubble", "size": "kilo",
             "body": {"type": "box", "layout": "vertical", "spacing": "md",
                      "contents": [text(name + "\u8cfd\u7a0b", "lg", weight="bold"), text(detail),
                                   text("\u53ef\u91cd\u65b0\u67e5\u8a62\u6216\u5207\u63db\u5176\u4ed6\u7403\u7a2e\u3002", "xs")]},
             "footer": {"type": "box", "layout": "vertical", "spacing": "sm",
                        "contents": [button("\u91cd\u65b0\u67e5\u8a62", name), button("\u6a21\u64ec\u5c0d\u6230\u5206\u6790", demo, "#475569"), button("\u5176\u4ed6\u904b\u52d5", MENU, "#334155")]}}}


def fixture_cards(sport: str, snapshot: Snapshot, team_name, today=False, now=None) -> dict:
    now = now or datetime.now(TZ)
    games = select_games(snapshot.games, now, today)
    if not games:
        return notice(sport, snapshot)
    colors = {"soccer": "#0F766E", "basketball": "#C2410C", "baseball": "#4F46E5"}
    accent = colors[sport]
    bubbles = []
    for g in games:
        home, away = team_name(g.home), team_name(g.away)
        label = g.start.strftime("%m/%d %H:%M") if g.time_known else g.start.strftime("%m/%d") + " \u6642\u9593\u5f85\u5b9a"
        subtitle = "\u9032\u884c\u4e2d" if g.state == "live" else "\u5373\u5c07\u958b\u8cfd"
        if today and g.start.date() != now.date():
            subtitle = "\u4eca\u65e5\u7121\u5f85\u958b\u8cfd\uff5c\u4e0b\u4e00\u5834"
        def team_row(name, logo, role):
            content = []
            if isinstance(logo, str) and logo.startswith("https://"):
                image = "https://wsrv.nl/?url=" + quote(logo, safe="") + "&w=128&h=128&fit=contain&output=png"
                content.append({"type": "image", "url": image, "size": "48px", "flex": 0,
                                "aspectMode": "fit", "aspectRatio": "1:1"})
            content.append({"type": "box", "layout": "vertical", "flex": 1,
                            "contents": [text(role, "xxs", "#64748B"), text(name, "md", weight="bold")]})
            return {"type": "box", "layout": "horizontal", "spacing": "sm", "alignItems": "center", "contents": content}
        stamp = snapshot.fetched.strftime("%m/%d %H:%M") if snapshot.fetched else "-"
        bubbles.append({"type": "bubble", "size": "kilo",
            "header": {"type": "box", "layout": "vertical", "backgroundColor": "#0F172A", "paddingAll": "14px",
                       "spacing": "xs", "contents": [text(g.league + " | " + SPORTS[sport], "xs", "#CBD5E1"),
                       text(label, "xl", "#FFFFFF", "bold"), text("\u53f0\u7063\u6642\u9593 | " + subtitle, "xs", "#CBD5E1")]},
            "body": {"type": "box", "layout": "vertical", "backgroundColor": "#F4F7FB", "paddingAll": "14px",
                     "spacing": "sm", "contents": [team_row(home, g.home_logo, "\u4e3b\u968a"), text("VS", color=accent, weight="bold"),
                     team_row(away, g.away_logo, "\u5ba2\u968a"), text(g.source + " | " + stamp +
                     (" | \u5feb\u53d6\u8cc7\u6599" if snapshot.stale else ""), "xxs", "#64748B")]},
            "footer": {"type": "box", "layout": "vertical", "paddingAll": "10px", "contents": [
                button("\u7acb\u5373\u5206\u6790", f"\u5206\u6790{SPORTS[sport]} {home} vs {away}", accent)]}})
    return {"type": "flex", "altText": SPORTS[sport] + "\u8fd1\u671f\u8cfd\u4e8b\uff5c\u9ede\u9078\u5206\u6790",
            "quickReply": quick_menu(), "contents": {"type": "carousel", "contents": bubbles}}


class Controller:
    def __init__(self, ui, client):
        self.ui = ui
        self.fixtures = Fixtures(client)
        self.details: dict[tuple, tuple] = {}
        self.jobs: dict[tuple, asyncio.Task] = {}

    async def team_details(self, sport, a, b):
        key = sport, a, b
        cached = self.details.get(key)
        if cached and time.monotonic() - cached[0] < (1800 if cached[1] or cached[2] else 120):
            return cached[1:]
        async def refresh():
            try:
                result = await asyncio.wait_for(self.ui.winner_team_details(sport, a, b), 10)
            except (Exception,):
                result = (None, None)
            if len(self.details) >= 128:
                self.details.pop(next(iter(self.details)))
            self.details[key] = (time.monotonic(), *result)
            return result
        job = self.jobs.get(key)
        if job is None or job.done():
            job = asyncio.create_task(refresh())
            self.jobs[key] = job
            job.add_done_callback(lambda done: self.jobs.pop(key, None) if self.jobs.get(key) is done else None)
        try:
            return await asyncio.wait_for(asyncio.shield(job), 1.5)
        except TimeoutError:
            return cached[1:] if cached and time.monotonic() - cached[0] < 7200 else (None, None)

    async def reply(self, raw: str) -> list[dict]:
        raw = str(raw).strip()[:300]
        demo = raw.startswith("\u6a21\u64ec\u5206\u6790")
        if demo:
            raw = raw[2:]
        compact = re.sub(r"\s+", "", raw)
        if compact in KEYS:
            key = KEYS[compact]
            return [fixture_cards(key, await self.fixtures.get(key), self.ui.team_zh)]
        if compact in (TODAY, FUTURE):
            snapshots = await asyncio.gather(*(self.fixtures.get(key) for key in SPORTS), return_exceptions=True)
            return [fixture_cards(key, snap if isinstance(snap, Snapshot) else Snapshot(errors=["internal_error"]),
                                  self.ui.team_zh, today=compact == TODAY) for key, snap in zip(SPORTS, snapshots)]
        match = re.match(r"^\s*\u5206\u6790\s*(\u8db3\u7403|\u7c43\u7403|\u68d2\u7403)\s*(.*)$", raw, re.S)
        if match:
            sport, matchup = match.groups()
            if not matchup:
                key = KEYS[sport]
                return [fixture_cards(key, await self.fixtures.get(key), self.ui.team_zh)]
            parts = re.split(r"\s*(?:\bvs\.?\b|\u5c0d\u6230|\u5c0d|\uff0d|\u2014)\s*", matchup, maxsplit=1, flags=re.I)
            if len(parts) == 2 and all(p.strip() for p in parts):
                a, b = (p.strip().lstrip(".").strip()[:35] for p in parts)
                history, players = (None, None) if demo else await self.team_details(sport, a, b)
                card = self.ui.offline_analysis(sport, a + " vs " + b, history, players)
                if isinstance(card, dict):
                    if demo:
                        card["contents"]["header"]["contents"].append(text("\u6a21\u64ec\u5c0d\u6230 | \u975e\u771f\u5be6\u8cfd\u7a0b", "xs", "#FDE68A"))
                    card["quickReply"] = quick_menu()
                    return [card]
            return [{"type": "text", "text": "\u8acb\u9ede\u9078\u8cfd\u4e8b\u5361\u7247\u7684\u300c\u7acb\u5373\u5206\u6790\u300d\u3002", "quickReply": quick_menu()}]
        if compact in ("\u6703\u54e1\u4e2d\u5fc3", "VIP"):
            return [{"type": "text", "text": "\u76ee\u524d\u4f7f\u7528\u514d\u8cbb\u529f\u80fd\uff0c\u672a\u555f\u7528\u4ed8\u8cbb\u8a02\u95b1\u3002", "quickReply": quick_menu()}]
        card = self.ui.menu_card()
        card["quickReply"] = quick_menu()
        return [card]

    async def close(self):
        await self.fixtures.close()
        tasks = list(self.jobs.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
