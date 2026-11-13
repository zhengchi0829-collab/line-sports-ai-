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
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo

import httpx

VERSION = "0.6.42"
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
            elif kind in ("mlb", "npb", "kbo", "milb"):
                status = g.get("status", {})
                state = status.get("abstractGameState")
                if state not in ("Preview", "Live") or any(w in status.get("detailedState", "").lower()
                                                           for w in ("cancel", "postpon")):
                    continue
                h, a = g["teams"]["home"]["team"], g["teams"]["away"]["team"]
                league_name = {"mlb": "MLB", "npb": "NPB", "kbo": "KBO", "milb": "MiLB"}[kind]
                item = Game(str(g["gamePk"]), "baseball", league_name, instant(g["gameDate"]), h["name"], a["name"],
                            f"https://www.mlbstatic.com/team-logos/{h['id']}.svg",
                            f"https://www.mlbstatic.com/team-logos/{a['id']}.svg", "MLB Stats API",
                            "live" if state == "Live" else "scheduled", not status.get("startTimeTBD", False))
            elif kind == "cpbl":
                if str(g.get("status", "")).lower() in ("final", "postponed", "cancelled", "canceled"):
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
                            "CPBL 2026 data", "scheduled", bool(g.get("time")))
            else:
                comp = g["competitions"][0]
                status = comp.get("status", g.get("status", {})).get("type", {})
                if status.get("completed") or status.get("state") == "post" or any(
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
                            "live" if status.get("state") == "in" else "scheduled", comp.get("timeValid") is not False)
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
            return [("npb", "https://statsapi.mlb.com/api/v1/schedule",
                     {"sportId": 31, "startDate": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
                      "endDate": (now + timedelta(days=180)).strftime("%Y-%m-%d")}, "npb")]
        if sport == "baseball_kbo":
            return [("kbo", "https://statsapi.mlb.com/api/v1/schedule",
                     {"sportId": 32, "startDate": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
                      "endDate": (now + timedelta(days=180)).strftime("%Y-%m-%d")}, "kbo")]
        if sport == "baseball_milb":
            return [("milb", "https://statsapi.mlb.com/api/v1/schedule",
                     {"sportId": 21, "startDate": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
                      "endDate": (now + timedelta(days=180)).strftime("%Y-%m-%d")}, "milb")]
        if sport == "baseball_cpbl":
            return [("cpbl", "https://raw.githubusercontent.com/pochih/cpbl2026/main/cpbl_data.json", {}, "cpbl")]
        return []

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
    labels = [
        ("⚽ 足球", "足球"), ("🏀 籃球", "籃球"), ("⚾ 棒球", "棒球"),
        ("📅 今日賽事", TODAY), ("🗓️ 未來賽事", FUTURE), ("🏠 主選單", MENU),
    ]
    return {"items": [{"type": "action", "action": {"type": "message", "label": label, "text": cmd}}
                      for label, cmd in labels]}


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
    """Second-level league chooser opened from the baseball button."""
    return {"type": "flex", "altText": "選擇 MLB、NPB、KBO、CPBL 或 MiLB", "quickReply": quick_menu(),
            "contents": {"type": "bubble", "size": "kilo",
                "header": {"type": "box", "layout": "vertical", "backgroundColor": "#0F172A",
                           "paddingAll": "16px", "contents": [
                               text("⚾ 棒球入口", "lg", "#FFFFFF", "bold"),
                               text("選擇要查看的聯盟", "sm", "#CBD5E1")]},
                "body": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "14px",
                         "contents": [
                             button("⚾ MLB｜美國職棒", "MLB", "#7C3AED"),
                             button("⚾ NPB｜日本職棒", "NPB", "#B91C1C"),
                             button("⚾ KBO｜韓國職棒", "KBO", "#0369A1"),
                             button("⚾ CPBL｜中華職棒", "CPBL", "#047857"),
                             button("⚾ MiLB｜美國小聯盟", "MiLB", "#92400E"),
                             button("🏠 回主選單", MENU, "#334155")
                         ]}}}


def notice(sport: str, snapshot: Snapshot) -> dict:
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


def fixture_cards(sport: str, snapshot: Snapshot, team_name, today=False, now=None) -> dict:
    now = now or datetime.now(TZ)
    games = select_games(snapshot.games, now, today)
    if not games:
        return notice(sport if sport.startswith("soccer_") else ("basketball" if sport.startswith("basketball_") else ("baseball" if sport.startswith("baseball_") else sport)), snapshot)
    colors = {"soccer": "#0F766E", "soccer_epl": "#0F766E", "soccer_laliga": "#B91C1C", "soccer_bundesliga": "#C2410C", "soccer_seriea": "#0369A1", "soccer_ligue1": "#7C3AED", "soccer_ucl": "#334155", "basketball": "#C2410C", "basketball_ncaa": "#B45309",
              "basketball_nbl": "#0369A1", "basketball_tpbl": "#7C3AED", "baseball": "#4F46E5", "baseball_npb": "#B91C1C",
              "baseball_kbo": "#0369A1", "baseball_cpbl": "#047857", "baseball_milb": "#92400E"}
    accent = colors[sport]
    bubbles = []
    for g in games:
        home, away = team_name(g.home), team_name(g.away)
        home_logo = fallback_team_logo(sport, g.home, g.home_logo)
        away_logo = fallback_team_logo(sport, g.away, g.away_logo)
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
                button("\u7acb\u5373\u5206\u6790", (f"\u5206\u6790{basketball_league_label(sport)} {home} vs {away}" if sport.startswith("basketball") else (f"\u5206\u6790{baseball_league_label(sport)} {home} vs {away}" if sport.startswith("baseball") else f"\u5206\u6790{SOCCER_LEAGUE_LABELS.get(sport, SPORTS.get(sport, sport))} {home} vs {away}")), accent)]}})
    return {"type": "flex", "altText": (basketball_league_label(sport) if sport.startswith("basketball") else (baseball_league_label(sport) if sport.startswith("baseball") else SOCCER_LEAGUE_LABELS.get(sport, SPORTS.get(sport, sport)))) + "\u8fd1\u671f\u8cfd\u4e8b\uff5c\u9ede\u9078\u5206\u6790",
            "quickReply": quick_menu(), "contents": {"type": "carousel", "contents": bubbles}}


class Controller:
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
        if compact == "足球":
            return [soccer_menu()]
        if compact == "籃球":
            return [league_menu()]
        if compact == "棒球":
            return [baseball_menu()]
        if compact in KEYS:
            key = KEYS[compact]
            return [fixture_cards(key, await self.fixtures.get(key), self.ui.team_zh)]
        if compact in (TODAY, FUTURE):
            snapshots = await asyncio.gather(*(self.fixtures.get(key) for key in SPORTS), return_exceptions=True)
            return [fixture_cards(key, snap if isinstance(snap, Snapshot) else Snapshot(errors=["internal_error"]),
                                  self.ui.team_zh, today=compact == TODAY) for key, snap in zip(SPORTS, snapshots)]
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
            parts = re.split(r"\s*(?:\bvs\.?\b|\u5c0d\u6230|\u5c0d|\uff0d|\u2014)\s*", matchup, maxsplit=1, flags=re.I)
            if len(parts) == 2 and all(p.strip() for p in parts):
                a, b = (p.strip().lstrip(".").strip()[:35] for p in parts)
                history, players = (None, None) if demo else await self.team_details(base_sport, a, b)
                card = self.ui.offline_analysis(base_sport, a + " vs " + b, history, players, league=league)
                if isinstance(card, dict):
                    if league:
                        card["contents"]["header"]["contents"][-1]["text"] = f"{league} · 對戰情境分析"
                        buttons = card["contents"].get("footer", {}).get("contents", [])
                        if buttons:
                            buttons[0]["action"]["label"] = f"🔎 其他{league}賽事"
                            buttons[0]["action"]["text"] = league
                    if soccer_league and not demo:
                        fixture_key = SOCCER_LEAGUES[soccer_league][0]
                        snapshot = self.fixtures.last.get(fixture_key)
                        if snapshot is not None:
                            for game in snapshot.games:
                                norm = lambda n: re.sub(r"[^\\w]", "", str(n)).casefold()
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
                                            "justifyContent": "spaceAround", "spacing": "lg",
                                            "contents": pictures})
                                    break
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
