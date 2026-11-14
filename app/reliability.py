"""Independent data sources, bounded caches and resilient LINE navigation.

Only public fixture metadata is cached. No LINE tokens, messages or user IDs are
stored in the data cache. An empty successful feed is NOT a connection failure.
"""
from __future__ import annotations

import asyncio
import os
from .odds import OddsGateway, format_book
import hashlib
import math
from .baseball_analysis import verified_baseball_analysis
from .legacy import team_display
import logging
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo

import httpx

VERSION = "0.7.5"
TZ = ZoneInfo("Asia/Taipei")
SPORTS = {"soccer": "\u8db3\u7403", "basketball": "\u7c43\u7403", "baseball": "\u68d2\u7403"}
KEYS = {v: k for k, v in SPORTS.items()}
SOCCER_LEAGUES = {
    "英超": ("soccer_epl", "eng.1"),
    "西甲": ("soccer_laliga", "esp.1"),
    "德甲": ("soccer_bundesliga", "ger.1"),
    "義甲": ("soccer_seriea", "ita.1"),
    "法甲": ("soccer_ligue1", "fra.1"),
    "歐冠": ("soccer_ucl", "uefa.champions"),
}
SOCCER_LEAGUE_LABELS = {key: label for label, (key, _) in SOCCER_LEAGUES.items()}
SOCCER_SLUG_LABELS = {slug: label for label, (_, slug) in SOCCER_LEAGUES.items()}
KEYS.update({label: key for label, (key, _) in SOCCER_LEAGUES.items()})
KEYS.update({"NBA": "basketball", "NCAA": "basketball_ncaa", "NBL": "basketball_nbl", "TPBL": "basketball_tpbl", "MLB": "baseball", "NPB": "baseball_npb", "KBO": "baseball_kbo", "CPBL": "baseball_cpbl", "MiLB": "baseball_milb"})
DEMO_PAIRS = {
    "soccer": ("\u62dc\u4ec1\u6155\u5c3c\u9ed1", "\u591a\u7279\u8499\u5fb7"),
    "basketball": ("\u6d1b\u6749\u78ef\u6e56\u4eba", "\u91d1\u5dde\u52c7\u58eb"),
    "baseball": ("\u6d1b\u6749\u78ef\u9053\u5947", "\u7d10\u7d04\u6d0b\u57fa"),
}
MENU = "\u9078\u55ae"
TODAY = "\u4eca\u65e5\u8cfd\u4e8b"
FUTURE = "\u672a\u4f86\u8cfd\u4e8b"
SPECIFIED = "指定日期賽事"
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
    home_score: int | None = None
    away_score: int | None = None


def final_score(value) -> int | None:
    """Accept only actual nonnegative integer scores, not predicted points."""
    if isinstance(value, dict):
        value = value.get("value", value.get("score"))
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return int(number) if number >= 0 and number.is_integer() else None


def score_only_card(game: Game, team_name) -> dict:
    """Completed-event response with final score only; no analysis actions."""
    home, away = team_name(game.home), team_name(game.away)
    result = (f"{game.home_score}：{game.away_score}"
              if game.home_score is not None and game.away_score is not None
              else "比分未提供")
    when = game.start.strftime("%Y/%m/%d %H:%M") if game.time_known else game.start.strftime("%Y/%m/%d")
    return {"type": "flex", "altText": f"{when}｜{home} VS {away}｜{result}"[:400],
            "contents": {"type": "bubble", "size": "kilo",
                "header": {"type": "box", "layout": "vertical", "paddingAll": "14px",
                           "backgroundColor": "#071A2D", "spacing": "xs",
                           "contents": [
                               text(when, "sm", "#8EF8FF", "bold"),
                               text(f"{home} VS {away}", "md", "#FFFFFF", "bold")]},
                "body": {"type": "box", "layout": "vertical", "paddingAll": "18px",
                         "backgroundColor": "#061425", "spacing": "sm",
                         "contents": [
                             text("最終比分" if result != "比分未提供" else "賽果", "sm",
                                  "#55EFFF", "bold"),
                             text(result, "xxl", "#FFFFFF", "bold")]}}}


def parse_feed(kind: str, data: Any, include_final: bool = False) -> list[Game]:
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
    elif kind in ("npb_archive", "kbo_archive"):
        if not isinstance(data, list):
            raise ValueError("Asian baseball archive must be a list")
        rows = data
    elif kind in ("mlb", "npb", "kbo", "milb"):
        if not isinstance(data, dict) or "dates" not in data:
            raise ValueError("Baseball dates missing")
        rows = [g for d in data["dates"] for g in d.get("games", [])]
    elif kind == "cpbl":
        rows = []
        def walk(value):
            if isinstance(value, dict):
                if {"date", "home", "away"}.issubset(value):
                    rows.append(value)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(data)
    else:
        if not isinstance(data, dict) or "events" not in data:
            raise ValueError("ESPN events missing")
        rows = data["events"]
    out = []
    for g in rows:
        try:
            if kind == "openliga":
                if g.get("matchIsFinished") and not include_final:
                    continue
                raw = g.get("matchDateTimeUTC")
                dt = instant(raw) if raw else instant(g["matchDateTime"], ZoneInfo("Europe/Berlin"))
                h, a = g["team1"], g["team2"]
                outcomes = g.get("matchResults") or []
                full_time = next(
                    (result for result in outcomes
                     if result.get("resultTypeID") == 2), None)
                if full_time is None and g.get("matchIsFinished") and outcomes:
                    full_time = outcomes[-1]
                item = Game(str(g["matchID"]), "soccer", "\u5fb7\u7532", dt,
                            h["teamName"], a["teamName"], h.get("teamIconUrl", ""),
                            a.get("teamIconUrl", ""), "OpenLigaDB",
                            "final" if g.get("matchIsFinished") else "scheduled",
                            True,
                            final_score(full_time.get("pointsTeam1")) if full_time else None,
                            final_score(full_time.get("pointsTeam2")) if full_time else None)
            elif kind.startswith("nba"):
                status = int(g.get("gameStatus", 1))
                if status not in ((1, 2, 3) if include_final else (1, 2)):
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
                            "final" if status == 3 else ("live" if status == 2 else "scheduled"),
                            not bool(g.get("isTimeTBD")),
                            final_score(h.get("score")) if status == 3 else None,
                            final_score(a.get("score")) if status == 3 else None)
            elif kind in ("mlb", "npb", "kbo", "milb", "npb_archive", "kbo_archive"):
                status = g.get("status", {})
                state = str(status.get("abstractGameState", ""))
                # Public NPB/KBO archives use P/F/L; MLB Stats uses Preview/Final/Live.
                is_preview = state in ("Preview", "P", "Scheduled")
                is_live = state in ("Live", "L", "In Progress")
                is_final = state in ("Final", "F") or str(status.get("detailedState", "")).lower().startswith("final")
                if not (is_preview or is_live or (include_final and is_final)) or any(
                        w in str(status.get("detailedState", "")).lower()
                        for w in ("cancel", "postpon")):
                    continue
                h, a = g["teams"]["home"]["team"], g["teams"]["away"]["team"]
                if kind == "milb":
                    # The MiLB schedule feed may contain MLB exhibition opponents.
                    # All MLB franchise IDs are within 100-199; exclude cross-league games.
                    home_id, away_id = int(h["id"]), int(a["id"])
                    if 100 <= home_id < 200 or 100 <= away_id < 200:
                        continue
                league_name = {"mlb": "MLB", "npb": "NPB", "kbo": "KBO", "milb": "MiLB",
                               "npb_archive": "NPB", "kbo_archive": "KBO"}[kind]
                item = Game(str(g["gamePk"]), "baseball", league_name, instant(g["gameDate"]), h["name"], a["name"],
                            ("" if kind.endswith("_archive") else f"https://www.mlbstatic.com/team-logos/{h['id']}.svg"),
                            ("" if kind.endswith("_archive") else f"https://www.mlbstatic.com/team-logos/{a['id']}.svg"),
                            ("Public league score archive" if kind.endswith("_archive") else "MLB Stats API"),
                            "final" if is_final else ("live" if is_live else "scheduled"),
                            not status.get("startTimeTBD", False),
                            final_score((g["teams"]["home"]).get("score")) if is_final else None,
                            final_score((g["teams"]["away"]).get("score")) if is_final else None)
            elif kind == "cpbl":
                cpbl_status = str(g.get("status", "")).lower()
                if cpbl_status in ("postponed", "cancelled", "canceled") or (
                        cpbl_status in ("final", "finished") and not include_final):
                    continue
                team_map = {"AAA": "味全龍", "AEO": "富邦悍將", "AKP": "台鋼雄鷹",
                            "ACN": "中信兄弟", "AJL": "樂天桃猿", "ADD": "統一7-ELEVEn獅"}
                raw_date = str(g["date"])
                raw_time = str(g.get("time") or "18:35")
                dt = datetime.strptime(raw_date + " " + raw_time, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
                home_name = team_map.get(str(g["home"]), str(g["home"]))
                away_name = team_map.get(str(g["away"]), str(g["away"]))
                item = Game("cpbl-" + str(g.get("no", raw_date + raw_time)), "baseball", "CPBL", dt,
                            home_name, away_name,
                            fallback_team_logo("baseball_cpbl", home_name),
                            fallback_team_logo("baseball_cpbl", away_name),
                            "CPBL 2026 data",
                            "final" if cpbl_status in ("final", "finished") else "scheduled",
                            bool(g.get("time")),
                            final_score(g.get("hs", g.get("home_score", g.get("homeScore"))))
                            if cpbl_status in ("final", "finished") else None,
                            final_score(g.get("as", g.get("away_score", g.get("awayScore"))))
                            if cpbl_status in ("final", "finished") else None)
            else:
                comp = g["competitions"][0]
                status = comp.get("status", g.get("status", {})).get("type", {})
                is_final = bool(status.get("completed")) or status.get("state") == "post"
                if (is_final and not include_final) or any(
                        word in status.get("name", "").lower() for word in ("cancel", "postpon")):
                    continue
                h = next(c["team"] for c in comp["competitors"] if c.get("homeAway") == "home")
                a = next(c["team"] for c in comp["competitors"] if c.get("homeAway") == "away")
                is_soccer = kind.startswith("soccer_")
                label = SOCCER_SLUG_LABELS.get(kind.removeprefix("soccer_"), "足球") if is_soccer else "NBA"
                def espn_logo(team):
                    logos = team.get("logos", [])
                    return team.get("logo") or (logos[0].get("href", "") if logos else "")
                item = Game(str(g["id"]), "soccer" if is_soccer else "basketball", label,
                            instant(g["date"]), h["displayName"], a["displayName"],
                            espn_logo(h), espn_logo(a), "ESPN",
                            "final" if is_final else (
                                "live" if status.get("state") == "in" else "scheduled"),
                            comp.get("timeValid") is not False,
                            final_score(next((c.get("score") for c in comp["competitors"]
                                              if c.get("homeAway") == "home"), None))
                            if is_final else None,
                            final_score(next((c.get("score") for c in comp["competitors"]
                                              if c.get("homeAway") == "away"), None))
                            if is_final else None)
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
        self.date_cache: dict[tuple[str, date], tuple[float, Snapshot]] = {}

    async def source(self, name: str, url: str, params: dict, kind: str,
                     include_final: bool = False) -> tuple[list[Game], str | None]:
        if time.monotonic() < self.cooldown.get(name, 0):
            return [], name + ":cooldown"
        try:
            async with asyncio.timeout(4.0):
                response = await self.client.get(url, params=params)
                response.raise_for_status()
                if kind == "tpbl_html":
                    html = response.text
                    game_no = re.search(r"GAME\s*(\d+)", html, re.I)
                    date_match = re.search(r"(\d{2})/(\d{2})\s*\([^)]*\)\s*(\d{2}:\d{2})", html)
                    teams = re.findall(r"<h4[^>]*>([^<]{2,30})</h4>", html)
                    if not date_match:
                        # Fallback to visible text extraction patterns used by official game pages.
                        text_body = re.sub(r"<[^>]+>", " ", html)
                        date_match = re.search(r"(\d{2})/(\d{2})\s*\([^)]*\)\s*(\d{2}:\d{2})", text_body)
                        if not teams:
                            names = ["臺北台新戰神", "新北中信特攻", "新北國王", "桃園台啤永豐雲豹",
                                     "新竹御嵿攻城獅", "福爾摩沙夢想家", "高雄全家海神"]
                            teams = [name for name in names if name in text_body]
                    if not date_match or len(teams) < 2:
                        raise ValueError("TPBL game page parse failed")
                    year = datetime.now(TZ).year
                    month, day, hm = date_match.groups()
                    # 2026-27 season pages currently in late 2026/early 2027.
                    y = year if int(month) >= 7 else year + 1
                    dt = datetime.strptime(f"{y}-{month}-{day} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
                    # Official page text is away first, home second.
                    away, home = teams[0], teams[1]
                    away_logo = nearby_html_image(html, away, url)
                    home_logo = nearby_html_image(html, home, url)
                    game = Game("tpbl-" + (game_no.group(1) if game_no else url.rsplit("/",1)[-1]),
                                "basketball", "TPBL", dt, home, away, home_logo, away_logo,
                                "TPBL 官方", "scheduled", True)
                    return [game], None
                parsed = parse_feed(kind, response.json(), include_final=include_final)
                if kind == "milb":
                    level = name.removeprefix("milb_").split("_date_")[0]
                    parsed = [replace(game, source="MiLB:" + level) for game in parsed]
                return parsed, None
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
        for label, (key, slug) in SOCCER_LEAGUES.items():
            if sport == key:
                # ESPN often rejects long YYYYMMDD-YYYYMMDD ranges with HTTP 400.
                # Monthly YYYYMM requests are supported and cover the next fixtures.
                first = now.replace(day=1)
                second = (first + timedelta(days=32)).replace(day=1)
                months = (first, second)
                feeds = [
                    (f"{key}_espn_{month:%Y%m}",
                     f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard",
                     {"dates": month.strftime("%Y%m"), "limit": 250}, "soccer_" + slug)
                    for month in months
                ]
                if key == "soccer_bundesliga":
                    season = now.year if now.month >= 7 else now.year - 1
                    feeds.append(("openliga_backup", f"https://api.openligadb.de/getmatchdata/bl1/{season}", {}, "openliga"))
                return feeds
        if sport == "basketball":
            return [
                ("nba_schedule", "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json", {}, "nba_schedule"),
                ("nba_espn_today", "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
                 {"dates": now.strftime("%Y%m%d")}, "espn"),
                ("nba_espn_next", "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
                 {"dates": (now + timedelta(days=1)).strftime("%Y%m%d")}, "espn"),
            ]
        if sport == "basketball_ncaa":
            return [
                ("ncaa_today", "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard",
                 {"dates": now.strftime("%Y%m%d"), "groups": "50", "limit": "100"}, "espn"),
                ("ncaa_next", "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard",
                 {"dates": (now + timedelta(days=1)).strftime("%Y%m%d"), "groups": "50", "limit": "100"}, "espn"),
            ]
        if sport == "basketball_nbl":
            return [
                ("nbl_today", "https://site.api.espn.com/apis/site/v2/sports/basketball/nbl/scoreboard",
                 {"dates": now.strftime("%Y%m%d")}, "espn"),
                ("nbl_next", "https://site.api.espn.com/apis/site/v2/sports/basketball/nbl/scoreboard",
                 {"dates": (now + timedelta(days=1)).strftime("%Y%m%d")}, "espn"),
            ]
        if sport == "basketball_tpbl":
            return [
                ("tpbl_official_13", "https://tpbl.basketball/schedule/27554", {}, "tpbl_html"),
                ("tpbl_official_15", "https://tpbl.basketball/schedule/27556", {}, "tpbl_html"),
                ("tpbl_official_17", "https://tpbl.basketball/schedule/27558", {}, "tpbl_html"),
                ("tpbl_official_19", "https://tpbl.basketball/schedule/27560", {}, "tpbl_html"),
                ("tpbl_official_20", "https://tpbl.basketball/schedule/27561", {}, "tpbl_html"),
            ]
        if sport == "baseball":
            return [("mlb", "https://statsapi.mlb.com/api/v1/schedule",
                     {"sportId": 1, "startDate": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
                      "endDate": (now + timedelta(days=240)).strftime("%Y-%m-%d")}, "mlb")]
        if sport == "baseball_npb":
            return [("npb_archive",
                     f"https://raw.githubusercontent.com/dentearl/npb-data/main/data/npb/{now.year}.json",
                     {}, "npb_archive")]
        if sport == "baseball_kbo":
            return [("kbo_archive",
                     f"https://raw.githubusercontent.com/dentearl/kbo-data/main/data/kbo/{now.year}.json",
                     {}, "kbo_archive")]
        if sport == "baseball_milb":
            # MiLB requires level-specific schedules (AAA/AA/High-A/A/Rookie).
            return [
                (f"milb_{sid}", "https://statsapi.mlb.com/api/v1/schedule",
                 {"sportId": sid, "startDate": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
                  "endDate": (now + timedelta(days=180)).strftime("%Y-%m-%d")}, "milb")
                for sid in (11, 12, 13, 14, 16)
            ]
        if sport == "baseball_cpbl":
            return [("cpbl", "https://raw.githubusercontent.com/pochih/cpbl2026/main/cpbl_data.json", {}, "cpbl")]
        return []

    def specs_for_date(self, sport: str, selected: date) -> list[tuple]:
        """Query the requested Taiwan calendar date; do not reuse today's fixture window."""
        day = datetime.combine(selected, datetime.min.time(), tzinfo=TZ)
        dates = ((day - timedelta(days=1)).strftime("%Y%m%d"), day.strftime("%Y%m%d"))
        if sport == "basketball":
            base = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
            result = [(f"nba_date_{d}", base, {"dates": d, "limit": 250}, "espn")
                      for d in dates]
        elif sport in ("basketball_ncaa", "basketball_nbl"):
            slug = "mens-college-basketball" if sport == "basketball_ncaa" else "nbl"
            base = f"https://site.api.espn.com/apis/site/v2/sports/basketball/{slug}/scoreboard"
            result = [(f"{sport}_{d}", base, {"dates": d, "limit": 250}, "espn")
                      for d in dates]
        elif sport in SOCCER_LEAGUE_LABELS:
            league = SOCCER_LEAGUE_LABELS[sport]
            slug = SOCCER_LEAGUES[league][1]
            base = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard"
            months = sorted({day.strftime("%Y%m"), (day - timedelta(days=1)).strftime("%Y%m")})
            result = [(f"{sport}_{month}", base, {"dates": month, "limit": 250},
                       "soccer_" + slug) for month in months]
            if sport == "soccer_bundesliga":
                season = day.year if day.month >= 7 else day.year - 1
                result.append(("openliga_date_backup",
                               f"https://api.openligadb.de/getmatchdata/bl1/{season}", {},
                               "openliga"))
        else:
            result = self.specs(sport, day)
            if sport in ("baseball", "baseball_milb"):
                start = (day - timedelta(days=1)).strftime("%Y-%m-%d")
                end = (day + timedelta(days=1)).strftime("%Y-%m-%d")
                result = [(name, url, {**params, "startDate": start, "endDate": end}, kind)
                          for name, url, params, kind in result]
        return [(f"{name}_date_{selected.isoformat()}", url, params, kind)
                for name, url, params, kind in result]

    async def on_date(self, sport: str, selected: date) -> Snapshot:
        """Fetch an exact-date snapshot without contaminating live/upcoming caches."""
        key = (sport, selected)
        cached = self.date_cache.get(key)
        if cached and time.monotonic() - cached[0] < 240:
            return cached[1]
        specs = self.specs_for_date(sport, selected)
        if not specs:
            return Snapshot(errors=["no_source_for_date"])
        results = await asyncio.gather(
            *(self.source(*spec, include_final=True) for spec in specs),
            return_exceptions=True)
        unique = {}
        errors = []
        successes = 0
        for result in results:
            if isinstance(result, BaseException):
                errors.append("source_exception")
                continue
            games, error = result
            if error:
                errors.append(error)
            else:
                successes += 1
            for game in games:
                if game.start.date() != selected:
                    continue
                signature = (game.start.isoformat(), game.home.casefold(), game.away.casefold())
                unique.setdefault(signature, game)
        snap = Snapshot(sorted(unique.values(), key=lambda game: game.start),
                        datetime.now(TZ), errors, successes)
        if len(self.date_cache) >= 120:
            self.date_cache.pop(next(iter(self.date_cache)))
        self.date_cache[key] = (time.monotonic(), snap)
        log.info("dated_fixture_result sport=%s date=%s count=%s successes=%s failures=%s",
                 sport, selected, len(snap.games), successes, len(errors))
        return snap

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
            return await asyncio.wait_for(asyncio.shield(task), timeout=3.2)
        except (TimeoutError, httpx.HTTPError):
            if old and time.monotonic() - old[0] < 21600:
                return Snapshot(old[1].games, old[1].fetched, ["refresh_timeout"], 0, True)
            return Snapshot(errors=["refresh_timeout"])

    async def close(self):
        for task in self.jobs.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)


def select_games(games: list[Game], now: datetime, today: bool = False,
                 future: bool = False, on_date: date | None = None) -> list[Game]:
    if on_date is not None:
        valid = [g for g in games if g.start.date() == on_date]
    elif today:
        # Today's calendar day in Asia/Taipei. No fallback to tomorrow.
        valid = [g for g in games if g.start.date() == now.date()]
    elif future:
        # Strictly exclude today, even games starting later tonight.
        valid = [g for g in games if g.start.date() > now.date()]
    else:
        valid = [g for g in games if g.state == "live" or g.start >= now or
                 (not g.time_known and g.start.date() >= now.date())]
    return sorted(valid, key=lambda g: (g.state != "live", g.start))[:8]


def text(value: str, size="sm", color="#334155", weight="regular") -> dict:
    return {"type": "text", "text": str(value)[:1000] or "-", "size": size,
            "color": color, "weight": weight, "wrap": True}


def button(label: str, command: str, color="#0F766E") -> dict:
    return {"type": "button", "style": "primary", "height": "sm", "color": color,
            "action": {"type": "message", "label": label[:20], "text": command[:300]}}


def quick_menu() -> dict:
    labels = [
        ("⚽ 足球", "足球"), ("🏀 籃球", "籃球"), ("⚾ 棒球", "棒球"),
        ("📅 今日賽事", TODAY), ("📆 指定日期", SPECIFIED),
        ("🗓️ 未來賽事", FUTURE), ("📊 賠率分析", "檢視賠率"), ("🏠 主選單", MENU),
    ]
    return {"items": [{"type": "action", "action": {"type": "message", "label": label, "text": cmd}}
                      for label, cmd in labels]}


CATEGORY_SOURCES = {
    "足球": ("soccer_epl", "soccer_laliga", "soccer_bundesliga",
             "soccer_seriea", "soccer_ligue1", "soccer_ucl"),
    "籃球": ("basketball", "basketball_ncaa", "basketball_nbl", "basketball_tpbl"),
    "棒球": ("baseball", "baseball_npb", "baseball_kbo", "baseball_cpbl", "baseball_milb"),
}


LEAGUE_CATEGORIES = {
    "足球": tuple(SOCCER_LEAGUES),
    "籃球": ("NBA", "NCAA", "NBL", "TPBL"),
    "棒球": ("MLB", "NPB", "KBO", "CPBL", "MiLB"),
}


def calendar_action() -> dict:
    now = datetime.now(TZ).date()
    return {"type": "datetimepicker", "label": "📆 從日曆選日期",
            "data": "cmd:" + SPECIFIED, "mode": "date",
            "initial": now.isoformat(),
            "min": (now - timedelta(days=365)).isoformat(),
            "max": (now + timedelta(days=365)).isoformat()}


def date_category_menu(mode: str, selected: date | None = None) -> dict:
    if mode == SPECIFIED and selected is None:
        title = "指定日期賽事"
        desc = "選擇日期，再挑選球類與聯盟；只列出該日真實賽程"
        actions = [
            {"type": "button", "style": "primary", "color": "#7C3AED",
             "height": "sm", "action": calendar_action()},
            text("也可直接輸入：指定日期賽事2026-06-20", "xs", "#64748B"),
            button("📅 回到今日賽事", TODAY, "#0F766E"),
            button("🏠 回主選單", MENU, "#334155"),
        ]
    else:
        title = (str(selected) + " 賽事") if mode == SPECIFIED else (
            "今日賽事" if mode == TODAY else "未來賽事")
        desc = ("只顯示台灣時間 " + str(selected) + " 的賽事" if selected else (
            "僅顯示台灣時間今日賽事" if mode == TODAY else
            "僅顯示明天起的賽事，不包含今日"))
        prefix = (SPECIFIED + selected.isoformat()) if mode == SPECIFIED else mode
        actions = [button("⚽ 足球", prefix + "足球", "#0F766E"),
                   button("🏀 籃球", prefix + "籃球", "#C2410C"),
                   button("⚾ 棒球", prefix + "棒球", "#7C3AED"),
                   {"type": "button", "style": "secondary", "height": "sm",
                    "action": calendar_action()},
                   button("🏠 回主選單", MENU, "#334155")]
    return {"type": "flex", "altText": title + "｜選擇賽事",
            "quickReply": quick_menu(),
            "contents": {"type": "bubble", "size": "kilo",
                "header": {"type": "box", "layout": "vertical",
                           "backgroundColor": "#0F172A", "paddingAll": "16px",
                           "contents": [text("📅 " + title, "lg", "#FFFFFF", "bold"),
                                        text(desc, "sm", "#CBD5E1")]},
                "body": {"type": "box", "layout": "vertical", "spacing": "md",
                         "contents": actions}}}


def date_league_menu(mode: str, category: str, selected: date) -> dict:
    prefix = SPECIFIED + selected.isoformat() if mode == SPECIFIED else TODAY
    options = LEAGUE_CATEGORIES[category]
    palette = "#7C3AED" if category == "棒球" else "#0F766E"
    contents = [button("🔎 " + label, prefix + label, palette) for label in options]
    contents.append(button("📆 更換日期", SPECIFIED, "#334155"))
    return {"type": "flex",
            "altText": f"{selected}｜{category}｜選擇聯盟",
            "quickReply": quick_menu(),
            "contents": {"type": "bubble", "size": "kilo",
                "header": {"type": "box", "layout": "vertical",
                           "backgroundColor": "#0F172A", "paddingAll": "16px",
                           "contents": [text(f"{selected}｜{category}", "lg", "#FFFFFF", "bold"),
                                        text("選擇聯盟後僅顯示指定日期的實際對戰", "sm", "#CBD5E1")]},
                "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                         "contents": contents}}}



def odds_fixture_picker(provider: str, league: str, records: list[dict]) -> dict:
    """A single LINE carousel card per fixture; details are opened on demand."""
    origin = "娛樂平台" if provider == "platform" else "台灣運彩"
    heading = f"{league}｜選擇賽事"
    bubbles = []
    for index, item in enumerate(records[:30]):
        start = item.get("start") or ""
        try:
            when = instant(start).astimezone(TZ).strftime("%m/%d %H:%M")
        except (ValueError, TypeError):
            when = str(start)[:16] or "時間未提供"
        home = team_display(str(item.get("home") or "-"))
        away = team_display(str(item.get("away") or "-"))
        command = f"賠率單場{origin}{league}#{index}"
        bubble = {
            "type": "bubble", "size": "kilo",
            "header": {"type": "box", "layout": "vertical",
                       "backgroundColor": "#0F172A", "paddingAll": "16px",
                       "contents": [text("📊 " + heading, "lg", "#FFFFFF", "bold"),
                                    text(when + "｜台灣時間", "sm", "#CBD5E1")]},
            "body": {"type": "box", "layout": "vertical", "spacing": "md",
                     "paddingAll": "14px", "contents": [
                         text(home, "md", "#0F172A", "bold"),
                         text("VS", "xs", "#64748B", "bold"),
                         text(away, "md", "#0F172A", "bold"),
                         text("點選下方按鈕查看這場賠率", "xs", "#64748B")]},
            "footer": {"type": "box", "layout": "vertical",
                       "paddingAll": "12px", "contents": [
                           button("📈 查看本場賠率", command,
                                  "#C2410C" if league in ("NBA", "NCAA", "NBL", "TPBL")
                                  else "#7C3AED" if league in ("MLB", "NPB", "KBO", "CPBL", "MiLB")
                                  else "#0F766E"),
                           button("↩ 返回聯盟選單", origin + "賠率", "#334155")]}}
        bubbles.append(bubble)
    if len(bubbles) == 1:
        contents = bubbles[0]
    else:
        contents = {"type": "carousel", "contents": bubbles[:12]}
    return {"type": "flex", "altText": heading + "｜左右滑動選擇比賽",
            "quickReply": quick_menu(), "contents": contents}


def odds_single_match_card(provider: str, league: str, item: dict) -> dict:
    """Show only the selected matchup with its verified API quote."""
    source = "娛樂平台" if provider == "platform" else "台灣運彩"
    body_lines = format_book(item).split("\n")
    original_matchup = str(item.get("home") or "-") + " vs " + str(item.get("away") or "-")
    translated_matchup = team_display(item.get("home") or "-") + " VS " + team_display(item.get("away") or "-")
    body_lines = [translated_matchup if line == original_matchup else line for line in body_lines]
    rows = [text(line, "sm", "#334155") for line in body_lines]
    rows.append(text("當日賠率資料｜實際盤口以平台為準",
                     "xs", "#64748B"))
    return {"type": "flex", "altText": (league + "｜" + team_display(item.get("home") or "-") +
            " VS " + team_display(item.get("away") or "-") + " 賠率分析")[:400],
            "quickReply": quick_menu(),
            "contents": {"type": "bubble", "size": "kilo",
                "header": {"type": "box", "layout": "vertical", "paddingAll": "16px",
                           "backgroundColor": "#0F172A",
                           "contents": [text("📊 " + league + "｜賠率分析", "lg", "#FFFFFF", "bold"),
                                        text(team_display(item.get("home") or "-") + " VS " + team_display(item.get("away") or "-"),
                                             "sm", "#CBD5E1")]},
                "body": {"type": "box", "layout": "vertical", "spacing": "md",
                         "paddingAll": "14px", "contents": rows},
                "footer": {"type": "box", "layout": "vertical", "spacing": "sm",
                           "contents": [button("↩ 選擇其他賽事", "賠率" + source + league, "#334155"),
                                        button("🏠 回主選單", MENU, "#334155")]}}}


def odds_category_menu() -> dict:
    return {"type": "flex", "altText": "賠率分析｜選擇來源",
            "quickReply": quick_menu(),
            "contents": {"type": "bubble", "size": "kilo",
                "header": {"type": "box", "layout": "vertical",
                           "backgroundColor": "#0F172A", "paddingAll": "16px",
                           "contents": [text("📊 賠率分析", "lg", "#FFFFFF", "bold"),
                                        text("選擇資料來源；不使用模擬賠率", "sm", "#CBD5E1")]},
                "body": {"type": "box", "layout": "vertical", "spacing": "md",
                         "contents": [button("🇹🇼 台灣運彩賠率", "台灣運彩賠率", "#334155"),
                                      button("🎮 娛樂平台賠率", "娛樂平台賠率", "#334155"),
                                      button("🏠 回主選單", MENU, "#334155")]}}}


def odds_league_menu(provider: str) -> dict:
    """Always display actionable leagues even when the API token is missing.

    The next step reports the actual data availability instead of silently
    stopping at a credential warning. Both sources remain odds-menu only.
    """
    prefix = "台灣運彩" if provider == "台灣運彩賠率" else "娛樂平台"
    configured = (bool(os.getenv("JBOT_API_TOKEN", "").strip()) if prefix == "台灣運彩"
                  else bool((os.getenv("ODDS_API_KEY") or os.getenv("THE_ODDS_API_KEY") or "").strip()))
    if prefix == "台灣運彩":
        leagues = ("NBA", "MLB", "英超", "西甲", "德甲", "義甲", "法甲",
                   "歐冠", "NPB", "KBO", "CPBL")
    else:
        leagues = ("NBA", "MLB", "英超", "西甲", "德甲", "義甲", "法甲", "歐冠")
    return {"type": "flex", "altText": provider + "｜選擇聯盟",
            "quickReply": quick_menu(),
            "contents": {"type": "bubble", "size": "kilo",
                "header": {"type": "box", "layout": "vertical",
                           "backgroundColor": "#0F172A", "paddingAll": "16px",
                           "spacing": "sm", "contents": [
                               text("📊 " + provider, "lg", "#FFFFFF", "bold"),
                               text("請選擇要查詢的聯盟", "sm", "#CBD5E1")]},
                "body": {"type": "box", "layout": "vertical",
                         "spacing": "sm", "paddingAll": "14px",
                         "contents": [
                             *(button("📈 " + league, "賠率" + prefix + league,
                                      "#C2410C" if league in ("NBA", "NCAA", "NBL", "TPBL") else
                                      "#7C3AED" if league in ("MLB", "NPB", "KBO", "CPBL", "MiLB") else
                                      "#0F766E") for league in leagues),
                             text("尚未設定授權 API；點選聯盟可查看原因。" if not configured
                                  else "僅顯示有來源、可核實的賠率資料。",
                                  "xs", "#CBD5E1"),
                             button("↩ 返回賠率分類", "檢視賠率", "#334155"),
                         ]}}}


def odds_source_card(provider: str) -> dict:
    official = provider == "台灣運彩賠率"
    desc = ("台灣運彩即時盤口需在 Render 設定 JBOT_API_TOKEN；目前尚未完成授權資料接入。"
            "請由官方賽事表查看當下價格；不會顯示猜測值。"
            if official else
            "娛樂平台盤口需在 Render 設定 ODDS_API_KEY；目前尚未完成授權資料接入。"
            "不同平台盤口與賠率不同，不能套用其他平台的數據。")
    controls = [button("🔄 重新查詢來源", provider, "#0F766E"),
                button("↩ 返回賠率分類", "檢視賠率", "#334155")]
    if official:
        controls.insert(0, {"type": "button", "style": "primary", "height": "sm",
                            "color": "#2563EB",
                            "action": {"type": "uri", "label": "🔎 官方賠率頁",
                                       "uri": "https://www.sportslottery.com.tw/"}})
    return {"type": "flex", "altText": provider + "｜尚未設定授權賠率 API",
            "quickReply": quick_menu(),
            "contents": {"type": "bubble", "size": "kilo",
                "header": {"type": "box", "layout": "vertical", "backgroundColor": "#0F172A",
                           "paddingAll": "16px", "contents": [
                               text("📊 " + provider, "lg", "#FFFFFF", "bold")]},
                "body": {"type": "box", "layout": "vertical", "spacing": "md",
                         "contents": [text("目前無可驗證賠率", "md", "#B45309", "bold"),
                                      text(desc, "sm"),
                                      text("賠率隨時可能變動；僅顯示經來源核實的資料。", "xs")]},
                "footer": {"type": "box", "layout": "vertical", "spacing": "sm",
                           "contents": controls}}}


def soccer_menu() -> dict:
    """Six popular football competitions with separate schedules and analysis."""
    options = [
        ("🏴 英超｜英格蘭", "英超", "#0F766E"),
        ("🇪🇸 西甲｜西班牙", "西甲", "#B91C1C"),
        ("🇩🇪 德甲｜德國", "德甲", "#C2410C"),
        ("🇮🇹 義甲｜義大利", "義甲", "#0369A1"),
        ("🇫🇷 法甲｜法國", "法甲", "#7C3AED"),
        ("🏆 歐冠｜冠軍聯賽", "歐冠", "#334155"),
    ]
    return {"type": "flex", "altText": "足球聯賽｜英超、西甲、德甲、義甲、法甲、歐冠",
            "quickReply": quick_menu(), "contents": {"type": "bubble", "size": "kilo",
                "header": {"type": "box", "layout": "vertical", "backgroundColor": "#0F172A",
                           "paddingAll": "16px", "contents": [
                               text("⚽ 足球賽事", "lg", "#FFFFFF", "bold"),
                               text("選擇聯賽，查看賽程及立即分析", "sm", "#CBD5E1")]},
                "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                         "paddingAll": "14px", "contents": [
                             *(button(label, cmd, color) for label, cmd, color in options),
                             button("🏠 返回主選單", MENU, "#334155"),
                         ]}}}


def league_menu() -> dict:
    """Second-level league chooser opened from the basketball button."""
    return {"type": "flex", "altText": "選擇 NBA、NCAA、NBL 或 TPBL；TPBL 可查看賽程並立即分析", "quickReply": quick_menu(),
            "contents": {"type": "bubble", "size": "kilo",
                "header": {"type": "box", "layout": "vertical", "backgroundColor": "#0F172A",
                           "paddingAll": "16px", "contents": [
                               text("🏀 籃球入口", "lg", "#FFFFFF", "bold"),
                               text("選擇要查看的聯盟", "sm", "#CBD5E1")]},
                "body": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "14px",
                         "contents": [
                             button("🏀 NBA｜美國職籃", "NBA", "#C2410C"),
                             button("🏀 NCAA｜美國大學籃球", "NCAA", "#B45309"),
                             button("🏀 NBL｜澳洲職籃", "NBL", "#0369A1"),
                             button("🏀 TPBL｜台灣職籃｜賽程＋分析", "TPBL", "#7C3AED"),
                             button("🏠 回主選單", MENU, "#334155")
                         ]}}}


def baseball_menu() -> dict:
    """Compact 6-button baseball menu matching the original screenshot proportions.

    Reuse LINE Flex message actions and retain the global cyan holographic palette.
    """
    return {"type": "flex", "altText": "棒球入口｜MLB、NPB、KBO、CPBL、MiLB",
            "quickReply": quick_menu(),
            "contents": {"type": "bubble", "size": "kilo",
                "header": {"type": "box", "layout": "vertical",
                           "backgroundColor": "#0F172A", "paddingAll": "16px",
                           "contents": [
                               text("⚾ 棒球入口", "lg", "#FFFFFF", "bold"),
                               text("選擇要查看的聯盟", "sm", "#CBD5E1")]},
                "body": {"type": "box", "layout": "vertical",
                         "spacing": "xs", "paddingAll": "12px",
                         "contents": [
                             button("⚾ MLB｜美國職棒", "MLB"),
                             button("⚾ NPB｜日本職棒", "NPB"),
                             button("⚾ KBO｜韓國職棒", "KBO"),
                             button("⚾ CPBL｜中華職棒", "CPBL"),
                             button("⚾ MiLB｜美國小聯盟", "MiLB"),
                             button("🏠 返回主選單", MENU, "#263D56"),
                         ]}}}



def notice(sport: str, snapshot: Snapshot) -> dict:
    if sport.startswith("baseball"):
        league = BASEBALL_LEAGUE_LABELS.get(sport, "MLB")
        info = ("目前無即將開賽的賽程；仍可查詢歷史五場分析。"
                if not snapshot.errors else
                "賽程資料暫時無法連線；可手動輸入球隊進行歷史五場分析。")
        return {"type":"flex","altText":league+"｜棒球賽程與分析",
                "quickReply":quick_menu(),
                "contents":{"type":"bubble","size":"kilo",
                    "header":{"type":"box","layout":"vertical","backgroundColor":"#0F172A",
                        "contents":[text("⚾ "+league,"lg","#FFFFFF","bold")]},
                    "body":{"type":"box","layout":"vertical","spacing":"md",
                        "contents":[text(info),text("輸入格式：分析"+league+" 主隊 vs 客隊","sm"),
                                    text("僅使用該聯盟已核實的歷史比分，資料不足不虛構推薦。","xs")]},
                    "footer":{"type":"box","layout":"vertical","spacing":"sm",
                        "contents":[button("📅 查詢今日聯盟賽事", TODAY + league, "#0F766E"),
                                    button("📆 指定日期賽事", SPECIFIED, "#7C3AED"),
                                    button("🔄 查詢近期賽程", league, "#334155"),
                                    button("⚾ 返回棒球分類", "棒球", "#334155")]}}}
    name = SOCCER_LEAGUE_LABELS.get(sport) or SPORTS.get(sport, "\u8cfd\u4e8b")
    failed = bool(snapshot.errors)
    a, b = DEMO_PAIRS.get(sport, DEMO_PAIRS["soccer"])
    demo = "\u6a21\u64ec\u5206\u6790" + SPORTS.get(sport, SPORTS["soccer"]) + " " + a + " vs " + b
    detail = ("\u8cc7\u6599\u4f86\u6e90\u66ab\u6642\u7121\u6cd5\u9023\u7dda\uff0c\u4e26\u4e0d\u4ee3\u8868\u6c92\u6709\u6bd4\u8cfd\u3002" if failed else
              "\u4f86\u6e90\u5df2\u56de\u61c9\uff0c\u76ee\u524d\u672a\u6536\u9304\u65b0\u7684\u672a\u958b\u8cfd\u8cfd\u7a0b\u3002")
    if sport in SOCCER_LEAGUE_LABELS:
        demo = "模擬分析" + name + " 曼城 vs 利物浦"
        detail += "\n可輸入「分析" + name + " 主隊 vs 客隊」進行指定對戰情境分析。"

    return {"type": "flex", "altText": name + "\u8cfd\u7a0b\u72c0\u614b", "quickReply": quick_menu(),
            "contents": {"type": "bubble", "size": "kilo",
             "body": {"type": "box", "layout": "vertical", "spacing": "md",
                      "contents": [text(name + "\u8cfd\u7a0b", "lg", weight="bold"), text(detail),
                                   text("\u53ef\u91cd\u65b0\u67e5\u8a62\u6216\u5207\u63db\u5176\u4ed6\u7403\u7a2e\u3002", "xs")]},
             "footer": {"type": "box", "layout": "vertical", "spacing": "sm",
                        "contents": [button("\u91cd\u65b0\u67e5\u8a62", name), button("\u6a21\u64ec\u5c0d\u6230\u5206\u6790", demo, "#475569"), button("\u5176\u4ed6\u904b\u52d5", MENU, "#334155")]}}}


BASKETBALL_LEAGUE_LABELS = {
    "basketball": "NBA",
    "basketball_ncaa": "NCAA",
    "basketball_nbl": "NBL",
    "basketball_tpbl": "TPBL",
}


def basketball_league_label(sport: str) -> str:
    return BASKETBALL_LEAGUE_LABELS.get(sport, "籃球")


BASEBALL_LEAGUE_LABELS = {
    "baseball": "MLB",
    "baseball_npb": "NPB",
    "baseball_kbo": "KBO",
    "baseball_cpbl": "CPBL",
    "baseball_milb": "MiLB",
}


def baseball_league_label(sport: str) -> str:
    return BASEBALL_LEAGUE_LABELS.get(sport, "棒球")


CPBL_TEAM_DOMAINS = {
    "味全龍": "wdragons.com",
    "富邦悍將": "fubonguardians.com",
    "台鋼雄鷹": "tsghawks.com",
    "中信兄弟": "brothers.tw",
    "樂天桃猿": "monkeys.rakuten.com.tw",
    "統一7-ELEVEn獅": "uni-lions.com.tw",
    "統一獅": "uni-lions.com.tw",
}


def fallback_team_logo(sport: str, raw_name: str, current: str = "") -> str:
    """Return a stable logo when the fixture provider did not include one."""
    if isinstance(current, str) and current.startswith("https://"):
        return current
    name = str(raw_name).strip()
    if sport == "baseball_cpbl":
        domain = CPBL_TEAM_DOMAINS.get(name)
        if domain:
            # Official team-site favicon. Small but stable and branded; used only
            # when the schedule source has no direct team-logo field.
            return "https://www.google.com/s2/favicons?domain=" + domain + "&sz=128"
    return ""


def nearby_html_image(html: str, team_name: str, base_url: str) -> str:
    """Find an official team image near the team name in a TPBL game page."""
    if not html or not team_name:
        return ""
    pos = html.find(team_name)
    if pos < 0:
        return ""
    start = max(0, pos - 1600)
    end = min(len(html), pos + 1600)
    chunk = html[start:end]
    candidates = re.findall(
        r'(?:src|data-src|data-original)=["\']([^"\']+)["\']',
        chunk, re.I
    )
    # Prefer image-like assets and skip generic tracking/icons.
    for src in reversed(candidates):
        low = src.lower()
        if any(ext in low for ext in (".png", ".svg", ".webp", ".jpg", ".jpeg")):
            return urljoin(base_url, src)
    return ""


def fixture_cards(sport: str, snapshot: Snapshot, team_name, today=False, now=None,
                  on_date: date | None = None) -> dict:
    now = now or datetime.now(TZ)
    games = select_games(snapshot.games, now, today, on_date=on_date)
    if not games:
        return notice(sport if sport.startswith("soccer_") else ("basketball" if sport.startswith("basketball_") else ("baseball" if sport.startswith("baseball_") else sport)), snapshot)
    colors = {"soccer": "#0F766E", "soccer_epl": "#0F766E", "soccer_laliga": "#B91C1C", "soccer_bundesliga": "#C2410C", "soccer_seriea": "#0369A1", "soccer_ligue1": "#7C3AED", "soccer_ucl": "#334155", "basketball": "#C2410C", "basketball_ncaa": "#B45309",
              "basketball_nbl": "#0369A1", "basketball_tpbl": "#7C3AED", "baseball": "#4F46E5", "baseball_npb": "#B91C1C",
              "baseball_kbo": "#0369A1", "baseball_cpbl": "#047857", "baseball_milb": "#92400E"}
    accent = colors[sport]
    bubbles = []
    for g in games:
        if g.state == "final":
            bubbles.append(score_only_card(g, team_name)["contents"])
            continue
        if g.start < now and g.state != "live":
            # Do not offer pregame recommendations for a past, unverified result.
            when = g.start.strftime("%Y/%m/%d %H:%M")
            bubbles.append({"type": "bubble", "size": "kilo",
                "header": {"type": "box", "layout": "vertical", "paddingAll": "14px",
                           "backgroundColor": "#071A2D",
                           "contents": [text(when, "sm", "#8EF8FF", "bold"),
                                        text(f"{team_name(g.home)} VS {team_name(g.away)}",
                                             "md", "#FFFFFF", "bold")]},
                "body": {"type": "box", "layout": "vertical", "paddingAll": "15px",
                         "contents": [text("尚未取得正式賽果", "sm", "#CBD5E1")]}})
            continue
        home, away = team_display(g.home), team_display(g.away)
        home_logo = fallback_team_logo(sport, g.home, g.home_logo)
        away_logo = fallback_team_logo(sport, g.away, g.away_logo)
        label = g.start.strftime("%m/%d %H:%M") if g.time_known else g.start.strftime("%m/%d") + " \u6642\u9593\u5f85\u5b9a"
        subtitle = ("已結束｜歷史賽事" if g.state == "final" else
                    "進行中" if g.state == "live" else
                    "即將開賽" if g.start >= now else "時間已過｜狀態未核實")
        if today and g.start.date() != now.date():
            subtitle = "\u4eca\u65e5\u7121\u5f85\u958b\u8cfd\uff5c\u4e0b\u4e00\u5834"
        def team_row(name, logo, role):
            content = []
            if isinstance(logo, str) and logo.startswith("https://"):
                image = "https://wsrv.nl/?url=" + quote(logo, safe="") + "&w=128&h=128&fit=contain&output=png"
                content.append({"type": "image", "url": image, "size": "48px", "flex": 0,
                                "aspectMode": "fit", "aspectRatio": "1:1"})
            else:
                # Never leave the logo slot visually blank.
                content.append({"type": "box", "layout": "vertical", "width": "48px", "height": "48px",
                                "backgroundColor": "#E2E8F0", "cornerRadius": "24px",
                                "justifyContent": "center", "alignItems": "center",
                                "contents": [text((str(name).strip() or "?")[0], "sm", "#334155", "bold")]})
            content.append({"type": "box", "layout": "vertical", "flex": 1,
                            "contents": [text(role, "xxs", "#64748B"), text(name, "md", weight="bold")]})
            return {"type": "box", "layout": "horizontal", "spacing": "sm", "alignItems": "center", "contents": content}
        stamp = snapshot.fetched.strftime("%m/%d %H:%M") if snapshot.fetched else "-"
        bubbles.append({"type": "bubble", "size": "kilo",
            "header": {"type": "box", "layout": "vertical", "backgroundColor": "#0F172A", "paddingAll": "14px",
                       "spacing": "xs", "contents": [text((basketball_league_label(sport) if sport.startswith("basketball") else (baseball_league_label(sport) if sport.startswith("baseball") else g.league)) + " | " + ("籃球" if sport.startswith("basketball") else ("棒球" if sport.startswith("baseball") else ("足球" if sport.startswith("soccer") else SPORTS[sport]))), "xs", "#CBD5E1"),
                       text(label, "xl", "#FFFFFF", "bold"), text("\u53f0\u7063\u6642\u9593 | " + subtitle, "xs", "#CBD5E1")]},
            "body": {"type": "box", "layout": "vertical", "backgroundColor": "#F4F7FB", "paddingAll": "14px",
                     "spacing": "sm", "contents": [team_row(home, home_logo, "\u4e3b\u968a"), text("VS", color=accent, weight="bold"),
                     team_row(away, away_logo, "\u5ba2\u968a"), text(g.source + " | " + stamp +
                     (" | \u5feb\u53d6\u8cc7\u6599" if snapshot.stale else ""), "xxs", "#64748B")]},
            "footer": {"type": "box", "layout": "vertical", "paddingAll": "10px", "contents": [
                button("立即分析", "分析" + (
                    basketball_league_label(sport) if sport.startswith("basketball") else
                    baseball_league_label(sport) if sport.startswith("baseball") else
                    SOCCER_LEAGUE_LABELS.get(sport, SPORTS.get(sport, sport))) +
                    f" {g.start:%Y-%m-%d} {g.home} vs {g.away}", accent)]}})
    return {"type": "flex", "altText": (basketball_league_label(sport) if sport.startswith("basketball") else (baseball_league_label(sport) if sport.startswith("baseball") else SOCCER_LEAGUE_LABELS.get(sport, SPORTS.get(sport, sport)))) + "｜賽事比分與賽程",
            "quickReply": quick_menu(), "contents": {"type": "carousel", "contents": bubbles}}



# Shared holographic visual theme. This changes presentation, never actions,
# original team identities, match dates, or numeric analysis values.
HUD_BG = "#061425"
HUD_HEADER = "#071A2D"
HUD_PANEL = "#10304F"
HUD_BLUE = "#075491"
HUD_BORDER = "#12DCFF"
HUD_LIGHT = "#E9F9FF"
HUD_MUTED = "#9ABBD0"


def holographic_theme(message: dict) -> dict:
    """Style every LINE Flex UI while preserving all actionable payloads.

    Uses only supported LINE Flex elements; unlike an image mock-up, its
    navigation stays tappable and readable on Android and iOS.
    """
    if not isinstance(message, dict) or message.get("type") != "flex":
        return message
    # The baseball entry is already the approved first-preview implementation.
    if "全息科技選單" in message.get("altText", ""):
        return message

    # League-specific alignment: basketball left, football center.
    # All other buttons retain the shared centered HUD style.
    alt = message.get("altText", "")
    basketball_leagues = alt.startswith("選擇 NBA、NCAA、NBL 或 TPBL")
    soccer_leagues = alt.startswith("足球聯賽｜")
    def transform(value):
        if isinstance(value, list):
            return [transform(item) for item in value]
        if not isinstance(value, dict):
            return value
        node = {key: transform(item) for key, item in value.items()}
        kind = node.get("type")
        if kind == "button":
            action = node.get("action", {})
            if action.get("type") == "message":
                label = action.get("label", "選擇")
                is_basketball_league = basketball_leagues and action.get("text") in (
                    "NBA", "NCAA", "NBL", "TPBL")
                center = soccer_leagues or not is_basketball_league
                return {
                    "type": "box", "layout": "horizontal", "flex": node.get("flex", 1),
                    "height": "42px", "alignItems": "center",
                    "justifyContent": "center" if center else "flex-start",
                    "cornerRadius": "8px", "backgroundColor": (
                        "#263D56" if action.get("text") == MENU else HUD_BLUE),
                    "borderColor": HUD_BORDER, "borderWidth": "1px",
                    "paddingStart": "12px" if is_basketball_league else "7px",
                    "paddingEnd": "7px",
                    "action": action,
                    "contents": [{"type": "text", "text": label, "size": "sm",
                                  "color": "#FFFFFF", "weight": "bold",
                                  "wrap": True,
                                  "align": "start" if is_basketball_league else "center"}],
                }
            # Date picker and external links keep their native LINE button
            # representation, which has better platform compatibility.
            if node.get("style") == "secondary":
                node["style"] = "primary"
            node["color"] = HUD_BLUE
        elif kind == "box":
            original = node.get("backgroundColor")
            if original:
                if original.upper() in ("#FFFFFF", "#F4F7FB", "#F1F5F9",
                                        "#F5F7FA", "#E2E8F0"):
                    node["backgroundColor"] = HUD_PANEL
                elif original.upper() in ("#0F172A", "#061425", "#071A2D"):
                    node["backgroundColor"] = HUD_HEADER
                elif original.upper() in ("#263D56", "#103B5E", "#075491"):
                    node["backgroundColor"] = original
                else:
                    node["backgroundColor"] = HUD_BLUE
            if node.get("borderColor"):
                node["borderColor"] = HUD_BORDER
        elif kind == "text":
            original = str(node.get("color", "#334155")).upper()
            if original in ("#334155", "#0F172A", "#111827", "#475569",
                            "#1F2937", "#FFFFFF", "#F8FAFC", "#E2E8F0"):
                node["color"] = HUD_LIGHT
            elif original in ("#64748B", "#94A3B8", "#CBD5E1", "#9CA3AF"):
                node["color"] = HUD_MUTED
            elif original not in ("#FDE68A", "#FBBF24", "#F87171"):
                node["color"] = "#55EFFF"
        elif kind == "separator":
            node["color"] = "#175876"
        elif kind == "bubble":
            node["styles"] = {
                "header": {"backgroundColor": HUD_HEADER},
                "body": {"backgroundColor": HUD_BG},
                "footer": {"backgroundColor": HUD_BG},
            }
            for section in ("header", "body", "footer"):
                box = node.get(section)
                if isinstance(box, dict):
                    box["backgroundColor"] = (HUD_HEADER if section == "header"
                                              else HUD_BG)
            if isinstance(node.get("header"), dict) and "對戰分析" not in message.get("altText", ""):
                header = node["header"]
                texts = header.get("contents")
                if isinstance(texts, list) and texts and len(texts) < 9:
                    texts.insert(0, {"type": "text", "text": "H Z  /  SPORTS AI",
                                     "size": "xxs", "color": HUD_BORDER,
                                     "weight": "bold"})
        return node

    result = transform(message)
    result["altText"] = str(result.get("altText", "SPORTS AI"))[:400]
    return result


def holographic_messages(messages: list[dict]) -> list[dict]:
    return [holographic_theme(message) for message in messages]



class Controller:
    async def category_fixtures(self, mode: str, category: str,
                                selected: date | None = None,
                                league: str | None = None) -> dict:
        now = datetime.now(TZ)
        requested = selected or (now.date() if mode == TODAY else None)
        keys = (KEYS[league],) if league else CATEGORY_SOURCES[category]
        if requested is not None:
            results = await asyncio.gather(
                *(self.fixtures.on_date(key, requested) for key in keys),
                return_exceptions=True)
        else:
            results = await asyncio.gather(
                *(self.fixtures.get(key) for key in keys),
                return_exceptions=True)
        options = []
        had_error = False
        for key, result in zip(keys, results):
            if not isinstance(result, Snapshot):
                had_error = True
                continue
            had_error = had_error or bool(result.errors)
            for game in result.games:
                if ((requested is not None and game.start.date() == requested) or
                    (requested is None and mode == FUTURE and game.start.date() > now.date())):
                    options.append((game, key, result))
        options.sort(key=lambda item: (item[0].start, item[1], item[0].ident))
        bubbles = []
        used = set()
        for game, key, snap in options:
            unique = (game.start.isoformat(), game.home.casefold(), game.away.casefold())
            if unique in used:
                continue
            used.add(unique)
            single = Snapshot([game], snap.fetched, snap.errors, snap.successes, snap.stale)
            result = fixture_cards(key, single, self.ui.team_zh,
                                   on_date=requested, now=now)
            bubbles.extend(result["contents"].get("contents", []))
            if len(bubbles) >= 8:
                break
        caption = f"{requested}｜{league or category}" if requested else f"{mode}｜{league or category}"
        if not bubbles:
            info = caption + " 沒有查到該日實際賽事。"
            if had_error:
                info += "\n部分賽程資料來源未回應，無法確認賽程是否完整。"
            return {"type": "text", "text": info, "quickReply": quick_menu()}
        return {"type": "flex", "altText": caption + "｜點選實際賽事進行分析",
                "quickReply": quick_menu(),
                "contents": {"type": "carousel", "contents": bubbles}}

    def baseball_league_entry(self, league: str, zh: str) -> dict:
        return {"type": "flex", "altText": f"{league}｜{zh} 對戰分析", "quickReply": quick_menu(),
                "contents": {"type": "bubble", "size": "kilo",
                    "header": {"type": "box", "layout": "vertical", "backgroundColor": "#0F172A",
                               "paddingAll": "16px", "contents": [
                                   text(f"⚾ {league}", "lg", "#FFFFFF", "bold"),
                                   text(zh, "sm", "#CBD5E1")]},
                    "body": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "14px",
                             "contents": [
                                 text(f"輸入：分析{league} A隊 vs B隊", "sm"),
                                 text("系統會使用棒球分析模型產生對戰重點與模擬推薦方向。", "xs", "#64748B"),
                                 button("🏠 回棒球選單", "棒球", "#334155")
                             ]}}}

    def tpbl_analysis_entry(self) -> dict:
        return {"type": "flex", "altText": "TPBL 台灣職業籃球大聯盟｜對戰分析", "quickReply": quick_menu(),
                "contents": {"type": "bubble", "size": "kilo",
                    "header": {"type": "box", "layout": "vertical", "backgroundColor": "#4C1D95",
                               "paddingAll": "16px", "contents": [
                                   text("🏀 TPBL", "lg", "#FFFFFF", "bold"),
                                   text("台灣職業籃球大聯盟", "sm", "#E9D5FF")]},
                    "body": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "14px",
                             "contents": [
                                 text("輸入：分析TPBL A隊 vs B隊", "sm"),
                                 text("系統會使用籃球分析模型產生對戰重點與模擬推薦方向。", "xs", "#64748B"),
                                 {"type": "button", "style": "primary", "height": "sm", "color": "#7C3AED",
                                  "action": {"type": "uri", "label": "查看 TPBL 官方賽程",
                                             "uri": "https://tpbl.basketball/schedule"}},
                                 button("🏠 回籃球選單", "籃球", "#334155")
                             ]}}}

    def __init__(self, ui, client):
        self.ui = ui
        self.fixtures = Fixtures(client)
        self.odds = OddsGateway(client)
        self.details: dict[tuple, tuple] = {}
        self.jobs: dict[tuple, asyncio.Task] = {}

    async def team_details(self, sport, a, b, league=None,
                           as_of: date | None = None, fixture: Game | None = None):
        """Verified recent form and registered players from the selected competition."""
        key = (sport, league, a, b, as_of)
        cached = self.details.get(key)
        if cached and time.monotonic() - cached[0] < 1200:
            return cached[1]
        if sport == "棒球":
            # League-specific score and roster lookup; do not use MLB results for CPBL.
            team_ids = None
            milb_level = None
            if league == "MiLB":
                # Fixture logos encode the official Stats API team IDs; they are more
                # reliable than a generic team search when the next season is scheduled.
                snapshot = (Snapshot([fixture]) if fixture else
                            self.fixtures.last.get("baseball_milb"))
                def norm_team(value):
                    return re.sub(r"[^\w]", "", str(value)).casefold()
                if snapshot:
                    for game in snapshot.games:
                        if (norm_team(a) in (norm_team(game.home), norm_team(self.ui.team_zh(game.home)))
                            and norm_team(b) in (norm_team(game.away), norm_team(self.ui.team_zh(game.away)))):
                            home_id = re.search(r"/(\d+)\.svg$", game.home_logo)
                            away_id = re.search(r"/(\d+)\.svg$", game.away_logo)
                            if home_id and away_id:
                                team_ids = (home_id.group(1), away_id.group(1))
                                if game.source.startswith("MiLB:"):
                                    try:
                                        milb_level = int(game.source.split(":", 1)[1])
                                    except ValueError:
                                        milb_level = None
                            break
            result = await verified_baseball_analysis(
                self.fixtures.client, league or "MLB", a, b, self.ui.team_zh,
                team_ids=team_ids, sport_id=milb_level, as_of=as_of)
            if len(self.details) >= 128:
                self.details.pop(next(iter(self.details)))
            self.details[key] = (time.monotonic(), result)
            return result
        paths = {
            "英超": "soccer/eng.1", "西甲": "soccer/esp.1",
            "德甲": "soccer/ger.1", "義甲": "soccer/ita.1",
            "法甲": "soccer/fra.1", "歐冠": "soccer/uefa.champions",
            "NBA": "basketball/nba", "NCAA": "basketball/mens-college-basketball",
            "NBL": "basketball/nbl", "MLB": "baseball/mlb",
        }
        fallback = ("⚠️ 近五場真實賽果不足，暫無可核實勝率。", "⚠️ 尚未取得可核實的主力球員資料。", None, None)
        path = paths.get(league or {"足球":"德甲","籃球":"NBA","棒球":"MLB"}.get(sport))
        if not path:
            return fallback
        import difflib
        import re as regex
        base = "https://site.api.espn.com/apis/site/v2/sports/" + path
        def norm(value):
            return regex.sub(r"[^\w]", "", str(value)).casefold()
        def score_team(team, requested):
            names = [team.get(k, "") for k in ("displayName", "name", "shortDisplayName", "abbreviation")]
            names.extend(self.ui.team_zh(n) for n in list(names))
            names.extend([team.get("location", "") + " " + team.get("name", "")])
            rname = norm(requested)
            return max((1.0 if norm(n) == rname else difflib.SequenceMatcher(None, norm(n), rname).ratio()
                        for n in names if n), default=0.0)
        async def fetch():
            try:
                response = await self.fixtures.client.get(base + "/teams", params={"limit": "500"})
                response.raise_for_status()
                entries = response.json().get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
                teams = [x.get("team", {}) for x in entries if isinstance(x, dict)]
                matches = []
                for name in (a, b):
                    if not teams:
                        return fallback
                    team = max(teams, key=lambda x: score_team(x, name))
                    if score_team(team, name) < 0.72 or not team.get("id"):
                        return fallback
                    matches.append(team)
                if str(matches[0]["id"]) == str(matches[1]["id"]):
                    return fallback
                ids = [str(team["id"]) for team in matches]
                responses = await asyncio.gather(
                    *(self.fixtures.client.get(base + "/teams/" + ident + "/schedule") for ident in ids),
                    return_exceptions=True)
                records = []
                scores = []
                historical_games = []
                for ident, response in zip(ids, responses):
                    played = []
                    if isinstance(response, httpx.Response) and response.status_code == 200:
                        for event in response.json().get("events", []):
                            comps = event.get("competitions") or []
                            if not comps:
                                continue
                            comp = comps[0]
                            status = comp.get("status") or event.get("status") or {}
                            if not status.get("type", {}).get("completed"):
                                continue
                            competitors = comp.get("competitors") or []
                            mine = next((c for c in competitors if str(c.get("team", {}).get("id")) == ident), None)
                            if mine is None:
                                continue
                            if mine.get("winner") is True:
                                outcome = "W"
                            elif any(c.get("winner") is True for c in competitors):
                                outcome = "L"
                            else:
                                # Ties are common in football and count as played, not wins.
                                outcome = "D" if sport == "足球" else "U"
                            if outcome != "U":
                                def real_score(value):
                                    if isinstance(value, dict):
                                        value = value.get("value", value.get("displayValue"))
                                    try:
                                        return float(value)
                                    except (TypeError, ValueError):
                                        return None
                                mine_score = real_score(mine.get("score"))
                                opponent = next((c for c in competitors if c is not mine), None)
                                other_score = real_score(opponent.get("score")) if opponent else None
                                played.append((event.get("date") or "", outcome, mine_score, other_score))
                    played.sort(reverse=True)
                    # Only the last five completed games, newest first.
                    recent_games = played[:5]
                    historical_games.append(recent_games)
                    recent = [outcome for _, outcome, _, _ in recent_games]
                    wins = recent.count("W")
                    draws = recent.count("D")
                    losses = recent.count("L")
                    records.append((wins, draws, losses, len(recent)))
                    scores.append([(for_score, against_score)
                                   for _, _, for_score, against_score in recent_games
                                   if for_score is not None and against_score is not None])
                # A real five-game form sample, not a random pick or betting probability.
                # Both clubs require five verified finished games before a comparison.
                if any(rec[3] != 5 for rec in records):
                    return ("雙方歷史最近五場資料不完整，暫無可驗證的五場勝率。",
                            "球員資料待核實；不根據不足樣本推薦。", None, None)
                rates = [rec[0] / 5 for rec in records]
                margins_avg = [
                    sum(x - y for x, y in values) / len(values) if values else None
                    for values in scores
                ]
                # Compare wins first, then net scores as a transparent tie-break.
                if rates[0] == rates[1]:
                    if any(val is None for val in margins_avg) or margins_avg[0] == margins_avg[1]:
                        return ("兩隊歷史五場勝率相同，且分差不足以區分；暫不推薦。",
                                "球員資料待核實。", None, None)
                    chosen_index = 0 if margins_avg[0] > margins_avg[1] else 1
                else:
                    chosen_index = 0 if rates[0] > rates[1] else 1
                chosen = (a, b)[chosen_index]
                wins, draws, losses, n = records[chosen_index]
                history = (f"{chosen}｜歷史最近五場：{wins}勝、{draws}和、{losses}敗"
                           f"｜勝率 {wins / 5 * 100:.1f}%")
                metrics = None
                if len(scores[0]) == 5 and len(scores[1]) == 5:
                    # Team-specific expected scoring from each team's last five scored
                    # and conceded points/goals. A simple form estimator, not market odds.
                    # Played games are newest first. Weight the most recent result
                    # highest for projection while retaining the five-game sample.
                    weights = (5, 4, 3, 2, 1)
                    weight_sum = sum(weights)
                    avg_for = [sum(weights[i] * x for i, (x, _) in enumerate(group)) / weight_sum
                               for group in scores]
                    avg_against = [sum(weights[i] * y for i, (_, y) in enumerate(group)) / weight_sum
                                   for group in scores]
                    expected_home = (avg_for[0] + avg_against[1]) / 2
                    expected_away = (avg_for[1] + avg_against[0]) / 2
                    expected_total = expected_home + expected_away
                    expected_margin = expected_home - expected_away
                    historical_totals = [x + y for group in scores for x, y in group]
                    historical_mean = sum(historical_totals) / len(historical_totals)
                    # Internal reference threshold derived from history, not an offered line.
                    # Half-point model lines (x.5): avoid integer push outcomes.
                    reference = math.floor(historical_mean) + 0.5
                    total_direction = ("大" if expected_total > reference + 0.15 else
                                       ("小" if expected_total < reference - 0.15 else "接近參考線"))
                    unit = "球" if sport == "足球" else "分"
                    total_text = (f"{total_direction} {reference:.1f} {unit}（歷史模型參考線）"
                                  f"｜預估合計 {expected_total:.1f} {unit}")
                    if sport == "足球":
                        handicap_line = 0.5
                    elif sport == "棒球":
                        handicap_line = 1.5
                    else:
                        handicap_line = math.floor(abs(expected_margin)) + 0.5
                    # Choose a side only if projected difference exceeds the internal line.
                    favored_index = 0 if expected_margin > 0 else 1
                    favored = (a, b)[favored_index]
                    projected = abs(expected_margin)
                    if projected > handicap_line + (0.1 if sport == "足球" else 0.3):
                        handicap_text = f"{favored} -{handicap_line:.1f} {unit}（模型參考）"
                    else:
                        handicap_text = f"讓分差距不足｜參考 {favored} -{handicap_line:.1f} {unit}"
                    metrics = {"total": total_text, "handicap": handicap_text,
                               "expected_total": expected_total, "expected_margin": expected_margin}
                selected_id = ids[0 if chosen == a else 1]
                roster_response, leaders_response = await asyncio.gather(
                    self.fixtures.client.get(base + "/teams/" + selected_id + "/roster"),
                    self.fixtures.client.get(base + "/teams/" + selected_id + "/leaders"),
                    return_exceptions=True)
                leaders = []
                if isinstance(leaders_response, httpx.Response) and leaders_response.status_code == 200:
                    for category in leaders_response.json().get("leaders", []):
                        if not isinstance(category, dict):
                            continue
                        for entry in category.get("leaders", [])[:2]:
                            if not isinstance(entry, dict):
                                continue
                            athlete = entry.get("athlete") or {}
                            name = athlete.get("displayName") or athlete.get("fullName")
                            if name and name not in leaders:
                                leaders.append(name)
                names = []
                if isinstance(roster_response, httpx.Response) and roster_response.status_code == 200:
                    athletes = roster_response.json().get("athletes", [])
                    if isinstance(athletes, dict):
                        athletes = list(athletes.values())
                    for group in athletes:
                        items = group.get("items", []) if isinstance(group, dict) else []
                        if not items and isinstance(group, dict):
                            items = [group]
                        for player in items:
                            if not isinstance(player, dict):
                                continue
                            athlete = player.get("athlete", player)
                            if not isinstance(athlete, dict):
                                continue
                            name = athlete.get("displayName") or athlete.get("fullName")
                            if name and name not in names:
                                names.append(name)
                # Prefer actual statistical leaders to arbitrary roster ordering.
                player_text = (f"{chosen}｜ESPN 球隊數據領先球員（非確定先發）："
                               + "、".join(leaders[:5])) if leaders else (
                               (f"{chosen}｜ESPN 現役登錄球員（尚未核實主力／先發）："
                                + "、".join(names[:5])) if names else
                               f"{chosen}｜尚無可驗證的主力球員名單。")
                return (history, player_text, chosen, metrics)
            except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
                log.warning("verified_team_stats_failed league=%s reason=%s", league, type(exc).__name__)
                return fallback
        job = self.jobs.get(key)
        if job is None or job.done():
            job = asyncio.create_task(fetch())
            self.jobs[key] = job
            job.add_done_callback(lambda done: self.jobs.pop(key, None) if self.jobs.get(key) is done else None)
        try:
            result = await asyncio.wait_for(asyncio.shield(job), 6.0)
        except TimeoutError:
            return cached[1] if cached else fallback
        if len(self.details) >= 128:
            self.details.pop(next(iter(self.details)))
        self.details[key] = (time.monotonic(), result)
        return result

    async def reply(self, raw: str) -> list[dict]:
        raw = str(raw).strip()[:300]
        demo = raw.startswith("\u6a21\u64ec\u5206\u6790")
        if demo:
            raw = raw[2:]
        compact = re.sub(r"\s+", "", raw)
        if compact == "足球":
            return [soccer_menu()]
        if compact == "籃球":
            return [league_menu()]
        if compact == "棒球":
            return [baseball_menu()]
        if compact in (TODAY, FUTURE, SPECIFIED):
            return [date_category_menu(compact)]
        specific = re.fullmatch(
            rf"{re.escape(SPECIFIED)}(\d{{4}}-\d{{2}}-\d{{2}})(.*)", compact)
        if specific:
            date_text, suffix = specific.groups()
            try:
                requested = date.fromisoformat(date_text)
            except ValueError:
                return [{"type": "text", "text": "日期格式不正確，請用 YYYY-MM-DD。",
                         "quickReply": quick_menu()}]
            if not (datetime.now(TZ).date() - timedelta(days=365) <= requested <=
                    datetime.now(TZ).date() + timedelta(days=365)):
                return [{"type": "text", "text": "請選擇過去一年至未來一年內的日期。",
                         "quickReply": quick_menu()}]
            if not suffix:
                return [date_category_menu(SPECIFIED, requested)]
            if suffix in CATEGORY_SOURCES:
                return [date_league_menu(SPECIFIED, suffix, requested)]
            if suffix in LEAGUE_CATEGORIES["足球"] + LEAGUE_CATEGORIES["籃球"] + LEAGUE_CATEGORIES["棒球"]:
                return [await self.category_fixtures(
                    SPECIFIED, ("足球" if suffix in LEAGUE_CATEGORIES["足球"] else
                                "籃球" if suffix in LEAGUE_CATEGORIES["籃球"] else "棒球"),
                    selected=requested, league=suffix)]
        if compact.startswith(TODAY):
            suffix = compact[len(TODAY):]
            if suffix in CATEGORY_SOURCES:
                return [date_league_menu(TODAY, suffix, datetime.now(TZ).date())]
            if suffix in LEAGUE_CATEGORIES["足球"] + LEAGUE_CATEGORIES["籃球"] + LEAGUE_CATEGORIES["棒球"]:
                return [await self.category_fixtures(
                    TODAY, ("足球" if suffix in LEAGUE_CATEGORIES["足球"] else
                            "籃球" if suffix in LEAGUE_CATEGORIES["籃球"] else "棒球"),
                    selected=datetime.now(TZ).date(), league=suffix)]
        if compact.startswith(FUTURE) and compact[len(FUTURE):] in CATEGORY_SOURCES:
            return [await self.category_fixtures(FUTURE, compact[len(FUTURE):])]
        if compact == "檢視賠率":
            return [odds_category_menu()]
        if compact == "賽事分析":
            return [date_category_menu(TODAY)]
        if compact in ("台灣運彩賠率", "娛樂平台賠率"):
            return [odds_league_menu(compact)]
        selected_odds = re.fullmatch(
            r"賠率單場(台灣運彩|娛樂平台)(英超|西甲|德甲|義甲|法甲|歐冠|NBA|NCAA|NBL|TPBL|MLB|NPB|KBO|CPBL|MiLB)#([0-9]{1,2})",
            compact)
        if selected_odds:
            source, league_name, slot = selected_odds.groups()
            provider = "tw" if source == "台灣運彩" else "platform"
            # No API call for platform detail clicks: read only the daily snapshot.
            if provider == "platform":
                daily = self.odds.daily
                if daily.get("day") != datetime.now(TZ).strftime("%Y-%m-%d"):
                    return [{"type": "text", "text": "本日賠率快取尚未建立或已過期，請重新選擇聯盟。",
                             "quickReply": quick_menu()}]
                records = self.odds.stored_platform_rows(league_name)
            else:
                records = self.odds.cache.get(("tw", league_name, datetime.now(TZ).strftime("%Y-%m-%d")), (0, []))[1]
            index = int(slot)
            if not isinstance(records, list) or index >= len(records):
                return [{"type": "text", "text": "本場賠率已失效，請回到賠率選單重新查詢。",
                         "quickReply": quick_menu()}]
            return [odds_single_match_card(provider, league_name, records[index])]
        match_odds = re.fullmatch(r"賠率(台灣運彩|娛樂平台)(英超|西甲|德甲|義甲|法甲|歐冠|NBA|NCAA|NBL|TPBL|MLB|NPB|KBO|CPBL|MiLB)", compact)
        if match_odds:
            source, league_name = match_odds.groups()
            provider = "tw" if source == "台灣運彩" else "platform"
            configured = (bool(os.getenv("JBOT_API_TOKEN", "").strip()) if provider == "tw"
                          else bool((os.getenv("ODDS_API_KEY") or os.getenv("THE_ODDS_API_KEY") or "").strip()))
            if not configured:
                return [odds_source_card("台灣運彩賠率" if provider=="tw" else "娛樂平台賠率")]
            now = datetime.now(TZ)
            data = await self.odds.feed(provider, league_name, now.strftime("%Y-%m-%d"))
            if not data:
                if provider == "platform":
                    status = self.odds.daily_status(league_name)
                    reason = ("免費賠率額度不足以同步全部聯盟，今天不會進行額外收費查詢。"
                              if status == "quota" else
                              "本次同步沒有取得該聯盟的可用賠率，當天不會對該聯盟重複查詢。"
                              if status == "unavailable" else
                              "賠率來源暫時無法連線，請稍後再試。")
                    return [{"type":"text","text":f"{source}｜{league_name}：{reason}","quickReply":quick_menu()}]
                return [{"type":"text","text":f"{source}｜{league_name} 今日尚無可核實賠率資料（或資料源暫時無法連線）。","quickReply":quick_menu()}]
            return [odds_fixture_picker(provider, league_name, data)]
        if compact in KEYS:
            key = KEYS[compact]
            return [fixture_cards(key, await self.fixtures.get(key), self.ui.team_zh)]
        match = re.match(r"^\s*分析\s*(英超|西甲|德甲|義甲|法甲|歐冠|足球|籃球|棒球|NBA|NCAA|NBL|TPBL|MLB|NPB|KBO|CPBL|MiLB)\s*(.*)$", raw, re.S)
        if match:
            sport, matchup = match.groups()
            basketball_league = sport if sport in ("NBA", "NCAA", "NBL", "TPBL") else None
            baseball_league = sport if sport in ("MLB", "NPB", "KBO", "CPBL", "MiLB") else None
            soccer_league = sport if sport in SOCCER_LEAGUES else None
            league = basketball_league or baseball_league or soccer_league
            base_sport = "籃球" if basketball_league else ("棒球" if baseball_league else ("足球" if soccer_league else sport))
            if not matchup:
                key = {"NBA": "basketball", "NCAA": "basketball_ncaa", "NBL": "basketball_nbl", "TPBL": "basketball_tpbl",
                       "MLB": "baseball", "NPB": "baseball_npb", "KBO": "baseball_kbo",
                       "CPBL": "baseball_cpbl", "MiLB": "baseball_milb", **{name: data[0] for name, data in SOCCER_LEAGUES.items()}}.get(league) if league else KEYS[sport]
                return [fixture_cards(key, await self.fixtures.get(key), self.ui.team_zh)]
            matchup = matchup.strip()
            requested_date = None
            date_prefix = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(.+)$", matchup, re.S)
            if date_prefix:
                try:
                    requested_date = date.fromisoformat(date_prefix.group(1))
                except ValueError:
                    return [{"type": "text", "text": "賽事日期格式錯誤，請重新選擇。",
                             "quickReply": quick_menu()}]
                matchup = date_prefix.group(2).strip()
            parts = re.split(r"\s*(?:\bvs\.?\b|\u5c0d\u6230|\u5c0d|\uff0d|\u2014)\s*",
                             matchup, maxsplit=1, flags=re.I)
            if len(parts) == 2 and all(p.strip() for p in parts):
                a, b = (p.strip().lstrip(".").strip()[:35] for p in parts)
                fixture_key = {
                    "NBA": "basketball", "NCAA": "basketball_ncaa",
                    "NBL": "basketball_nbl", "TPBL": "basketball_tpbl",
                    "MLB": "baseball", "NPB": "baseball_npb",
                    "KBO": "baseball_kbo", "CPBL": "baseball_cpbl",
                    "MiLB": "baseball_milb",
                    **{label: key for label, (key, _) in SOCCER_LEAGUES.items()},
                }.get(league)
                def comparable(given, actual):
                    return re.sub(r"[^\w]", "", str(given)).casefold() == (
                        re.sub(r"[^\w]", "", str(actual)).casefold())
                matched_game = None
                kickoff = None
                if requested_date:
                    if not fixture_key:
                        return [{"type": "text",
                                 "text": "請先選擇聯盟與日期，再點選當天的實際對戰。",
                                 "quickReply": quick_menu()}]
                    if not (datetime.now(TZ).date() - timedelta(days=365)
                            <= requested_date <= datetime.now(TZ).date() + timedelta(days=365)):
                        return [{"type": "text", "text": "只提供過去一年至未來一年的日期查詢。",
                                 "quickReply": quick_menu()}]
                    snapshot = await self.fixtures.on_date(fixture_key, requested_date)
                else:
                    snapshot = self.fixtures.last.get(fixture_key) if fixture_key else None
                if snapshot:
                    for game in snapshot.games:
                        if requested_date and game.start.date() != requested_date:
                            continue
                        if ((comparable(game.home, a) or
                             comparable(self.ui.team_zh(game.home), a))
                            and (comparable(game.away, b) or
                                 comparable(self.ui.team_zh(game.away), b))):
                            matched_game = game
                            kickoff = game.start
                            break
                if requested_date and matched_game is None:
                    message = (f"{requested_date}｜{league} 找不到"
                               f"「{a} vs {b}」的已核實賽程，請重新選日期及賽事。")
                    if snapshot and snapshot.errors:
                        message += "\n資料來源部分無法連線，賽事可能尚未更新。"
                    return [{"type": "text", "text": message,
                             "quickReply": quick_menu()}]
                if matched_game is not None and matched_game.state == "final":
                    return [score_only_card(matched_game, self.ui.team_zh)]
                if (matched_game is not None and matched_game.start < datetime.now(TZ)
                        and matched_game.state != "live"):
                    return [{"type": "text",
                             "text": "此場比賽尚未取得已核實的最終比分，暫不進行其他分析。",
                             "quickReply": quick_menu()}]
                retrospective_other_sport = bool(
                    requested_date and base_sport != "棒球" and
                    (requested_date < datetime.now(TZ).date()
                     or (matched_game and matched_game.state == "final")))
                if demo:
                    history, players, recommended, metrics = None, None, None, None
                elif retrospective_other_sport:
                    history = "該場已結束，但沒有可核實的賽前近五場資料；不使用事後戰績冒充預測。"
                    players = "沒有可核實的該場賽前先發與傷停資料。"
                    recommended, metrics = None, None
                else:
                    history, players, recommended, metrics = await self.team_details(
                        base_sport, a, b, league, as_of=requested_date,
                        fixture=matched_game)
                # This is an analysis-only view: no sportsbook API request, prices,
                # bookmakers or live odds. Both authorized feeds belong exclusively
                # to the separate "檢視賠率" menu.
                card = self.ui.offline_analysis(base_sport, a + " vs " + b, history, players,
                                                league=league, recommended_team=recommended,
                                                recent_metrics=metrics)
                if isinstance(card, dict):
                    panel = card["contents"]["body"]["contents"][-1]
                    cells = panel.get("contents", [])
                    if league:
                        header_contents = card["contents"]["header"]["contents"]
                        header_contents[-1]["text"] = f"{league} · 已選賽事分析"
                        buttons = card["contents"].get("footer", {}).get("contents", [])
                        if buttons:
                            buttons[0]["action"]["label"] = f"🔎 其他{league}賽事"
                            buttons[0]["action"]["text"] = (
                                SPECIFIED + requested_date.isoformat() + league
                                if requested_date else league)
                        if requested_date and matched_game:
                            header_contents.append(
                                text("📅 台灣時間 " + matched_game.start.strftime("%Y/%m/%d %H:%M"),
                                     "xs", "#CBD5E1"))
                            if matched_game.state == "final":
                                header_contents.append(
                                    text("已結束｜賽前資料回測，非賽前預測",
                                         "xs", "#FDE68A"))
                            elif matched_game.state == "scheduled":
                                header_contents.append(
                                    text("按該場日期分析｜歷史戰績不代表預測勝率",
                                         "xs", "#CBD5E1"))
                    if soccer_league and not demo and not requested_date:
                        fixture_key = SOCCER_LEAGUES[soccer_league][0]
                        snapshot = self.fixtures.last.get(fixture_key)
                        if snapshot is not None:
                            for game in snapshot.games:
                                norm = lambda n: re.sub(r"[^\w]", "", str(n)).casefold()
                                requested = {norm(a), norm(b)}
                                localized = {norm(self.ui.team_zh(game.home)), norm(self.ui.team_zh(game.away))}
                                original = {norm(game.home), norm(game.away)}
                                if requested == localized or requested == original:
                                    header = card["contents"]["header"]["contents"]
                                    header.append(text("🕒 台灣時間 " + game.start.strftime("%m/%d %H:%M"), "xs", "#CBD5E1"))
                                    logos = [(game.home, game.home_logo), (game.away, game.away_logo)]
                                    pictures = []
                                    for title, logo in logos:
                                        if isinstance(logo, str) and logo.startswith("https://"):
                                            pictures.append({"type": "image",
                                                "url": "https://wsrv.nl/?url=" + quote(logo, safe="") +
                                                       "&w=100&h=100&fit=contain&output=png",
                                                "size": "38px", "aspectRatio": "1:1", "aspectMode": "fit",
                                                "flex": 0})
                                        else:
                                            pictures.append(text(self.ui.team_zh(title)[:4], "sm", "#334155"))
                                    card["contents"]["body"]["contents"].insert(
                                        0, {"type": "box", "layout": "horizontal", "alignItems": "center",
                                            "justifyContent": "center", "spacing": "lg",
                                            "contents": pictures})
                                    break
                    # Keep title to actual date/time and two short Chinese team names.
                    event_time = matched_game.start if matched_game else kickoff
                    stamp = (event_time.strftime("%Y/%m/%d %H:%M")
                             if event_time else
                             (requested_date.strftime("%Y/%m/%d") + " 時間待確認"
                              if requested_date else "日期／時間待確認"))
                    card["contents"]["header"]["contents"] = [
                        text(stamp, "sm", "#8EF8FF", "bold"),
                        text(f"{self.ui.team_zh(a)} VS {self.ui.team_zh(b)}",
                             "md", "#FFFFFF", "bold"),
                    ]
                    if matched_game and matched_game.state == "final":
                        card["contents"]["body"]["contents"].insert(
                            0, text("歷史回測｜非賽前即時預測", "xs", "#FDE68A"))
                    if demo:
                        card["contents"]["body"]["contents"].insert(
                            0, text("模擬分析｜非真實賽程", "xs", "#FDE68A"))
                    # Rounded to 0.5 units. This is a model estimate, not a result.
                    if metrics is not None:
                        total_value = metrics.get("expected_total")
                        margin_value = metrics.get("expected_margin")
                        if isinstance(total_value, (int, float)) and isinstance(margin_value, (int, float)):
                            home_est = max(0.0, round(total_value + margin_value) / 2)
                            away_est = max(0.0, round(total_value - margin_value) / 2)
                            cells.insert(1, text(
                                f"模型預估比分 {home_est:.1f}：{away_est:.1f}",
                                "md", "#FFFFFF", "bold"))
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
