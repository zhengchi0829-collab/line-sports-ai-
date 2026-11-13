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
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo

import httpx

VERSION = "0.6.65"
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


def select_games(games: list[Game], now: datetime, today: bool = False,
                 future: bool = False) -> list[Game]:
    if today:
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
        ("📅 今日賽事", TODAY), ("🗓️ 未來賽事", FUTURE), ("📊 檢視賠率", "檢視賠率"), ("🏠 主選單", MENU),
    ]
    return {"items": [{"type": "action", "action": {"type": "message", "label": label, "text": cmd}}
                      for label, cmd in labels]}


CATEGORY_SOURCES = {
    "足球": ("soccer_epl", "soccer_laliga", "soccer_bundesliga",
             "soccer_seriea", "soccer_ligue1", "soccer_ucl"),
    "籃球": ("basketball", "basketball_ncaa", "basketball_nbl", "basketball_tpbl"),
    "棒球": ("baseball", "baseball_npb", "baseball_kbo", "baseball_cpbl", "baseball_milb"),
}


def date_category_menu(mode: str) -> dict:
    title = "今日賽事" if mode == TODAY else "未來賽事"
    desc = ("僅顯示台灣時間今日賽事" if mode == TODAY
            else "僅顯示明天起的賽事，不包含今日")
    return {"type": "flex", "altText": title + "｜請選擇運動種類",
            "quickReply": quick_menu(),
            "contents": {"type": "bubble", "size": "kilo",
                "header": {"type": "box", "layout": "vertical",
                           "backgroundColor": "#0F172A", "paddingAll": "16px",
                           "contents": [text("📅 " + title, "lg", "#FFFFFF", "bold"),
                                        text(desc, "sm", "#CBD5E1")]},
                "body": {"type": "box", "layout": "vertical", "spacing": "md",
                         "contents": [button("⚽ 足球", mode + "足球", "#0F766E"),
                                      button("🏀 籃球", mode + "籃球", "#C2410C"),
                                      button("⚾ 棒球", mode + "棒球", "#7C3AED"),
                                      button("🏠 回主選單", MENU, "#334155")]}}}


def odds_category_menu() -> dict:
    return {"type": "flex", "altText": "檢視賠率｜選擇來源",
            "quickReply": quick_menu(),
            "contents": {"type": "bubble", "size": "kilo",
                "header": {"type": "box", "layout": "vertical",
                           "backgroundColor": "#0F172A", "paddingAll": "16px",
                           "contents": [text("📊 檢視賠率", "lg", "#FFFFFF", "bold"),
                                        text("選擇資料來源；不使用模擬賠率", "sm", "#CBD5E1")]},
                "body": {"type": "box", "layout": "vertical", "spacing": "md",
                         "contents": [button("🇹🇼 台灣運彩賠率", "台灣運彩賠率", "#0F766E"),
                                      button("🎮 娛樂平台賠率", "娛樂平台賠率", "#7C3AED"),
                                      button("🏠 回主選單", MENU, "#334155")]}}}


def odds_league_menu(provider: str) -> dict:
    prefix = "台灣運彩" if provider == "台灣運彩賠率" else "娛樂平台"
    key = "JBOT_API_TOKEN" if prefix == "台灣運彩" else "THE_ODDS_API_KEY"
    if not os.getenv(key,"").strip():
        return odds_source_card(provider)
    if prefix=="台灣運彩":
        leagues = ("NBA","MLB","英超","西甲","德甲","義甲","法甲","歐冠","NPB","KBO","CPBL")
    else:
        leagues = ("NBA","MLB","英超","西甲","德甲","義甲","法甲","歐冠")
    return {"type":"flex","altText":provider+"｜選擇聯賽","quickReply":quick_menu(),
        "contents":{"type":"bubble","size":"kilo",
            "header":{"type":"box","layout":"vertical","backgroundColor":"#0F172A",
                "contents":[text("📊 "+provider,"lg","#FFFFFF","bold")]},
            "body":{"type":"box","layout":"vertical","spacing":"sm",
                "contents":[button("📈 "+lg,"賠率"+prefix+lg,"#0F766E") for lg in leagues[:12]]}}}


def odds_source_card(provider: str) -> dict:
    official = provider == "台灣運彩賠率"
    desc = ("台灣運彩即時盤口需在 Render 設定 JBOT_API_TOKEN；目前尚未完成授權資料接入。"
            "請由官方賽事表查看當下價格；不會顯示猜測值。"
            if official else
            "娛樂平台盤口需在 Render 設定 THE_ODDS_API_KEY；目前尚未完成授權資料接入。"
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
    async def category_fixtures(self, mode: str, category: str) -> dict:
        now = datetime.now(TZ)
        keys = CATEGORY_SOURCES[category]
        results = await asyncio.gather(
            *(self.fixtures.get(key) for key in keys), return_exceptions=True)
        options = []
        had_error = False
        for key, result in zip(keys, results):
            if not isinstance(result, Snapshot):
                had_error = True
                continue
            had_error = had_error or bool(result.errors)
            for game in result.games:
                if ((mode == TODAY and game.start.date() == now.date()) or
                    (mode == FUTURE and game.start.date() > now.date())):
                    options.append((game, key, result))
        options.sort(key=lambda item: (item[0].start, item[1], item[0].ident))
        bubbles = []
        used = set()
        for game, key, snap in options:
            unique = (game.start.isoformat(), game.home.casefold(), game.away.casefold())
            if unique in used:
                continue
            used.add(unique)
            selected = Snapshot([game], snap.fetched, snap.errors, snap.successes, snap.stale)
            result = fixture_cards(key, selected, self.ui.team_zh, today=(mode == TODAY), now=now)
            bubbles.extend(result["contents"].get("contents", []))
            if len(bubbles) >= 8:
                break
        if not bubbles:
            text_value = (("當日無其他" + category + "賽事") if mode == TODAY
                          else ("目前無未來" + category + "賽事"))
            if had_error:
                text_value += "\n部分賽程資料來源暫時無法連線，請稍後再試。"
            return {"type": "text", "text": text_value, "quickReply": quick_menu()}
        return {"type": "flex", "altText": mode + "｜" + category + "｜選擇立即分析",
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

    async def team_details(self, sport, a, b, league=None):
        """Verified recent form and registered players from the selected competition."""
        key = (sport, league, a, b)
        cached = self.details.get(key)
        if cached and time.monotonic() - cached[0] < 1200:
            return cached[1]
        if sport == "棒球":
            # League-specific score and roster lookup; do not use MLB results for CPBL.
            result = await verified_baseball_analysis(
                self.fixtures.client, league or "MLB", a, b, self.ui.team_zh)
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
        if compact in (TODAY, FUTURE):
            return [date_category_menu(compact)]
        for period in (TODAY, FUTURE):
            if compact.startswith(period) and compact[len(period):] in CATEGORY_SOURCES:
                return [await self.category_fixtures(period, compact[len(period):])]
        if compact in ("賽事分析", "檢視賠率"):
            return [odds_category_menu()]
        if compact in ("台灣運彩賠率", "娛樂平台賠率"):
            return [odds_league_menu(compact)]
        match_odds = re.fullmatch(r"賠率(台灣運彩|娛樂平台)(英超|西甲|德甲|義甲|法甲|歐冠|NBA|NCAA|NBL|TPBL|MLB|NPB|KBO|CPBL|MiLB)", compact)
        if match_odds:
            source, league_name = match_odds.groups()
            provider = "tw" if source == "台灣運彩" else "platform"
            configured = bool(os.getenv("JBOT_API_TOKEN" if provider=="tw" else "THE_ODDS_API_KEY","").strip())
            if not configured:
                return [odds_source_card("台灣運彩賠率" if provider=="tw" else "娛樂平台賠率")]
            now = datetime.now(TZ)
            data = await self.odds.feed(provider, league_name, now.strftime("%Y-%m-%d"))
            if not data:
                return [{"type":"text","text":f"{source}｜{league_name} 今日尚無可核實賠率資料（或資料源暫時無法連線）。","quickReply":quick_menu()}]
            lines=[format_book(row) for row in data[:4]]
            return [{"type":"text","text":"\n\n".join(lines)[:4700],"quickReply":quick_menu()}]
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
            parts = re.split(r"\s*(?:\bvs\.?\b|\u5c0d\u6230|\u5c0d|\uff0d|\u2014)\s*", matchup, maxsplit=1, flags=re.I)
            if len(parts) == 2 and all(p.strip() for p in parts):
                a, b = (p.strip().lstrip(".").strip()[:35] for p in parts)
                history, players, recommended, metrics = ((None, None, None, None) if demo else
                                                         await self.team_details(base_sport, a, b, league))
                # Match live market odds to both teams and a verified kickoff;
                # never substitute the historical model line for a sportsbook line.
                actual_market = None
                fixture_key = {"NBA":"basketball","NCAA":"basketball_ncaa","NBL":"basketball_nbl","TPBL":"basketball_tpbl",
                               "MLB":"baseball","NPB":"baseball_npb","KBO":"baseball_kbo","CPBL":"baseball_cpbl",
                               "MiLB":"baseball_milb", **{label:key for label,(key,_) in SOCCER_LEAGUES.items()}}.get(league)
                kickoff = None
                if fixture_key:
                    snapshot = self.fixtures.last.get(fixture_key)
                    if snapshot:
                        for game in snapshot.games:
                            def comparable(given, actual):
                                return re.sub(r"[^\w]","",str(given)).casefold() == re.sub(r"[^\w]","",str(actual)).casefold()
                            if ((comparable(game.home,a) or comparable(self.ui.team_zh(game.home),a))
                                and (comparable(game.away,b) or comparable(self.ui.team_zh(game.away),b))):
                                kickoff = game.start
                                break
                if not demo and league and kickoff:
                    configured_sources = [
                        source for source, name in (("tw","JBOT_API_TOKEN"),("platform","THE_ODDS_API_KEY"))
                        if os.getenv(name,"").strip()]
                    if configured_sources:
                        quotes = await asyncio.gather(
                            *(self.odds.match(source,league,a,b,kickoff,self.ui.team_zh)
                              for source in configured_sources), return_exceptions=True)
                        candidates = [q for q in quotes if isinstance(q,dict)]
                        # Prefer the provider that actually has h2h, totals AND handicap.
                        # Never fill missing markets using an unrelated bookmaker's line.
                        if candidates:
                            actual_market = max(candidates,key=lambda q: (
                                sum(q.get(field) is not None for field in
                                    ("home_price","away_price","total","spread")),
                                q.get("source","").startswith("台灣運彩")))
                card = self.ui.offline_analysis(base_sport, a + " vs " + b, history, players,
                                                league=league, recommended_team=recommended,
                                                recent_metrics=metrics)
                if isinstance(card,dict):
                    panel = card["contents"]["body"]["contents"][-1]
                    cells = panel.get("contents",[])
                    if actual_market:
                        source_title=actual_market["source"]+"｜"+actual_market["book"]
                        cells[0]["text"]="✦ 真實盤口｜"+source_title
                        if actual_market.get("home_price") and actual_market.get("away_price"):
                            cells[1]["text"]=("🏆 獨贏賠率  主 "+f"{actual_market['home_price']:.2f}"+
                                              "／客 "+f"{actual_market['away_price']:.2f}"+
                                              (("／和 "+f"{actual_market['draw_price']:.2f}") if actual_market.get("draw_price") else ""))
                        else:
                            cells[1]["text"]="🏆 獨贏賠率  此來源未提供"
                        if actual_market.get("total") is not None:
                            line=actual_market["total"]
                            projected=(metrics or {}).get("expected_total")
                            direction=("偏大" if projected>line+0.2 else "偏小" if projected<line-0.2 else "接近盤口") if projected is not None else "模型資料不足"
                            over=actual_market.get("over_price")
                            under=actual_market.get("under_price")
                            prices=(f" 大 {over:.2f}" if over is not None else "") + (f"／小 {under:.2f}" if under is not None else "")
                            cells[3]["text"]=f"📊 大小盤 {line:g}{prices}｜"+direction
                        else:
                            cells[3]["text"]="📊 大小盤  此來源未提供"
                        if actual_market.get("spread") is not None:
                            line=actual_market["spread"]
                            projected=(metrics or {}).get("expected_margin")
                            direction=("偏主隊" if projected+line>0.3 else "偏客隊" if projected+line< -0.3 else "接近盤口") if projected is not None else "模型資料不足"
                            sh=actual_market.get("spread_home_price")
                            sa=actual_market.get("spread_away_price")
                            prices=(f" 主 {sh:.2f}" if sh is not None else "")+(f"／客 {sa:.2f}" if sa is not None else "")
                            cells[5]["text"]=f"⚖️ 讓球 主隊 {line:+g}{prices}｜"+direction
                        else:
                            cells[5]["text"]="⚖️ 讓球  此來源未提供"
                        cells.append(text("盤口更新："+str(actual_market.get("updated") or "未提供")[:19],"xs","#CBD5E1"))
                    else:
                        cells[0]["text"]="✦ 歷史模型參考｜無已核實盤口"
                        cells[3]["text"]="📊 大小分 "+((metrics or {}).get("total") or "未取得實際盤口")
                        cells[5]["text"]="⚖️ 讓球 "+((metrics or {}).get("handicap") or "未取得實際盤口")
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
