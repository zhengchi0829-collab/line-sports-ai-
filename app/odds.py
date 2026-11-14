"""Authorized sportsbook odds sources. Never claim model numbers are actual odds."""
import asyncio
import difflib
import json
import os
import re
import time
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

TZ = ZoneInfo("Asia/Taipei")
KEYS = {
    "英超": "soccer_epl", "西甲": "soccer_spain_la_liga",
    "德甲": "soccer_germany_bundesliga", "義甲": "soccer_italy_serie_a",
    "法甲": "soccer_france_ligue_one", "歐冠": "soccer_uefa_champs_league",
    "NBA": "basketball_nba", "NCAA": "basketball_ncaab",
    "MLB": "baseball_mlb",
}
JSPORTS = {
    "英超": "FTB", "西甲": "FTB", "德甲": "FTB", "義甲": "FTB",
    "法甲": "FTB", "歐冠": "FTB", "NBA": "NBA", "NCAA": "BSK",
    "NBL": "BSK", "TPBL": "BSK", "MLB": "MLB",
    "NPB": "NPB", "KBO": "KBO", "CPBL": "CPB", "MiLB": "BSE",
}

def number(v):
    try:
        x = float(v)
        return x if -10000 < x < 10000 else None
    except (ValueError, TypeError):
        return None

def namekey(s):
    return re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]", "", str(s)).casefold()

def similarity(x, y, translator=None):
    x, y = str(x), str(y)
    aliases = [x, y]
    if translator:
        aliases += [translator(x), translator(y)]
    a, b = namekey(x), namekey(y)
    return 1.0 if a and a == b else max(
        difflib.SequenceMatcher(None, namekey(v), namekey(w)).ratio()
        for v in (x, translator(x) if translator else x)
        for w in (y, translator(y) if translator else y)
        if namekey(v) and namekey(w)
    )

def choose_primary(lines):
    if not isinstance(lines, dict):
        return None
    entries = [(k,v) for k,v in lines.items() if isinstance(v,dict)]
    if not entries:
        return None
    key, data = next(((k,v) for k,v in entries if v.get("m") is True), entries[0])
    return number(key), data

def jbot_normalize(payload):
    if not isinstance(payload, dict) or payload.get("status") != "OK":
        return []
    output=[]
    for event in payload.get("data", []):
        if not isinstance(event,dict) or not isinstance(event.get("odds"),list) or not event["odds"]:
            continue
        quote=event["odds"][-1]
        if not isinstance(quote,dict):
            continue
        normal=quote.get("normal") or {}
        spread=choose_primary(quote.get("handi"))
        total=choose_primary(quote.get("total"))
        book={
            "source":"台灣運彩（JBot 第三方記錄）","book":"台灣運彩","updated":str(quote.get("update") or ""),
            "home":str(event.get("home") or ""), "away":str(event.get("away") or ""),
            "start":str(event.get("time") or ""),
            "home_price":number(normal.get("h")), "away_price":number(normal.get("a")),
            "draw_price":number(normal.get("d")),
            "spread":spread[0] if spread else None,
            "spread_home_price":number(spread[1].get("h")) if spread else None,
            "spread_away_price":number(spread[1].get("a")) if spread else None,
            "total":total[0] if total else None,
            "over_price":number(total[1].get("o")) if total else None,
            "under_price":number(total[1].get("u")) if total else None,
        }
        if book["home"] and book["away"]:
            output.append(book)
    return output

def theodds_normalize(payload):
    if not isinstance(payload,list):
        return []
    result=[]
    for event in payload:
        if not isinstance(event,dict):
            continue
        for bookmaker in event.get("bookmakers",[]):
            if not isinstance(bookmaker,dict):
                continue
            row={"source":"The Odds API","book":str(bookmaker.get("title") or bookmaker.get("key") or ""),
                 "updated":str(bookmaker.get("last_update") or ""),
                 "home":str(event.get("home_team") or ""), "away":str(event.get("away_team") or ""),
                 "start":str(event.get("commence_time") or ""),
                 "home_price":None,"away_price":None,"draw_price":None,
                 "spread":None,"spread_home_price":None,"spread_away_price":None,
                 "total":None,"over_price":None,"under_price":None}
            for market in bookmaker.get("markets",[]):
                k=market.get("key")
                outcomes=market.get("outcomes",[])
                if k=="h2h":
                    for o in outcomes:
                        n=str(o.get("name") or "")
                        if n==row["home"]: row["home_price"]=number(o.get("price"))
                        elif n==row["away"]: row["away_price"]=number(o.get("price"))
                        elif n.lower() in ("draw","tie"):row["draw_price"]=number(o.get("price"))
                elif k=="spreads":
                    for o in outcomes:
                        n=str(o.get("name") or "")
                        if n==row["home"]:
                            row["spread"]=number(o.get("point"))
                            row["spread_home_price"]=number(o.get("price"))
                        elif n==row["away"]:
                            row["spread_away_price"]=number(o.get("price"))
                elif k=="totals":
                    for o in outcomes:
                        n=str(o.get("name") or "").lower()
                        if n=="over":
                            row["total"]=number(o.get("point"))
                            row["over_price"]=number(o.get("price"))
                        elif n=="under":
                            row["under_price"]=number(o.get("price"))
            if row["book"] and (row["home_price"] is not None or
                                row["spread"] is not None or row["total"] is not None):
                result.append(row)
    return result

def parse_time(raw):
    try:
        date=datetime.fromisoformat(str(raw).replace("Z","+00:00"))
        return date.replace(tzinfo=TZ) if date.tzinfo is None else date.astimezone(TZ)
    except ValueError:
        return None

def format_book(book, predicted=None):
    parts=[book["source"] + "｜" + book["book"],
           book["home"]+" vs "+book["away"]]
    if book.get("home_price") and book.get("away_price"):
        h=f"主 {book['home_price']:.2f}／客 {book['away_price']:.2f}"
        if book.get("draw_price"):h+=f"／和 {book['draw_price']:.2f}"
        parts.append("獨贏賠率 "+h)
    # Market-implied probabilities, not model predictions.
    choices=[("主",book.get("home_price")),("客",book.get("away_price"))]
    if book.get("draw_price") is not None:
        choices.append(("和",book["draw_price"]))
    if all(isinstance(p,(int,float)) and p>1 for _,p in choices):
        implied=[(label,1/p) for label,p in choices]
        total_probability=sum(p for _,p in implied)
        if total_probability>0:
            parts.append("盤口隱含機率（去除抽水） "+
                         "／".join(f"{label} {p/total_probability*100:.1f}%"
                                  for label,p in implied))
    if book.get("total") is not None:
        line=book["total"]
        text=f"大小盤 {line:g}｜大 {book['over_price']:.2f}" if book.get("over_price") else f"大小盤 {line:g}"
        if book.get("under_price"):text+=f"／小 {book['under_price']:.2f}"
        if predicted and predicted.get("expected_total") is not None:
            val=predicted["expected_total"]
            text+=f"｜歷史模型 {val:.1f} → "+("偏大" if val>line+0.2 else "偏小" if val<line-0.2 else "接近盤口")
        parts.append(text)
    if book.get("spread") is not None:
        text=f"讓球／讓分 主隊 {book['spread']:+g}"
        if book.get("spread_home_price"):text+=f" {book['spread_home_price']:.2f}"
        if book.get("spread_away_price"):text+=f"／客隊 {-book['spread']:+g} {book['spread_away_price']:.2f}"
        if predicted and predicted.get("expected_margin") is not None:
            v=predicted["expected_margin"]+book["spread"]
            text+="｜模型 "+("偏主隊" if v>0.3 else "偏客隊" if v< -0.3 else "接近盤口")
        parts.append(text)
    if book.get("updated"):parts.append("盤口更新："+book["updated"][:19])
    return "\n".join(parts)

class OddsGateway:
    """Only one The Odds API lookup and one matchup per Taiwan day.

    Filesystem cache avoids repeat charges during ordinary requests/restarts
    on the same instance. Render free ephemeral disk can reset on redeploy;
    a durable database is required for a strict cross-deployment quota.
    """
    def __init__(self, client):
        self.client = client
        self.cache = {}
        self.locks = {}
        self.last_jbot_call = 0.0
        self.daily_lock = asyncio.Lock()
        self.daily_file = Path(os.getenv("ODDS_DAILY_CACHE_PATH", "/tmp/line_sports_odds_daily.json"))
        self.daily = self._read_daily()

    def _read_daily(self):
        try:
            data = json.loads(self.daily_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_daily(self, record):
        """Commit an attempted call BEFORE the network request to avoid retries."""
        try:
            self.daily_file.parent.mkdir(parents=True, exist_ok=True)
            path = self.daily_file.with_name(self.daily_file.name + ".tmp")
            path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            path.replace(self.daily_file)
            self.daily = record
            return True
        except OSError:
            # Never spend paid API credits without being able to remember it.
            return False

    def daily_status(self, league):
        day = datetime.now(TZ).strftime("%Y-%m-%d")
        record = self.daily
        if record.get("day") != day:
            return "available"
        if record.get("league") != league:
            return "other_league"
        if record.get("state") == "ready":
            return "cached"
        return "unavailable"

    async def _daily_platform_feed(self, league):
        day = datetime.now(TZ).strftime("%Y-%m-%d")
        async with self.daily_lock:
            record = self.daily
            if record.get("day") == day:
                return record.get("rows", []) if record.get("league") == league else []
            sport = KEYS.get(league)
            key = (os.getenv("ODDS_API_KEY") or os.getenv("THE_ODDS_API_KEY") or "").strip()
            if not key or not sport:
                return []
            # This marker is stored before the request: even errors do not retry.
            pending = {"day": day, "league": league, "state": "attempted", "rows": []}
            if not self._save_daily(pending):
                return []
            rows = []
            try:
                response = await self.client.get(
                    f"https://api.the-odds-api.com/v4/sports/{sport}/odds",
                    params={"apiKey": key, "regions": "us",
                            "markets": "h2h,spreads,totals", "oddsFormat": "decimal"},
                    timeout=5,
                )
                if response.status_code == 200:
                    all_rows = theodds_normalize(response.json())
                    # A single matchup (not four unrelated games).
                    if all_rows:
                        first = all_rows[0]
                        rows = [row for row in all_rows
                                if row["home"] == first["home"]
                                and row["away"] == first["away"]
                                and row["start"] == first["start"]][:1]
            except (httpx.HTTPError, ValueError, TypeError, KeyError):
                rows = []
            self._save_daily({"day": day, "league": league,
                              "state": "ready" if rows else "unavailable",
                              "rows": rows})
            return rows

    async def feed(self, provider, league, date):
        if provider != "tw":
            return await self._daily_platform_feed(league)
        cache_key = (provider, league, date)
        cache = self.cache.get(cache_key)
        if cache and time.monotonic() - cache[0] < 300:
            return cache[1]
        lock = self.locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cache = self.cache.get(cache_key)
            if cache and time.monotonic() - cache[0] < 300:
                return cache[1]
            data = []
            key = os.getenv("JBOT_API_TOKEN", "").strip()
            kind = JSPORTS.get(league)
            if not key or not kind:
                return []
            if time.monotonic() - self.last_jbot_call < 5.5:
                return []
            self.last_jbot_call = time.monotonic()
            try:
                response = await self.client.get(
                    "https://api.sportsbot.tech/v2/odds",
                    params={"sport": kind, "date": date, "mode": "close"},
                    headers={"X-JBot-Token": key}, timeout=5,
                )
                if response.status_code == 200:
                    data = jbot_normalize(response.json())
            except (httpx.HTTPError, ValueError, TypeError, KeyError):
                data = []
            if data:
                self.cache[cache_key] = (time.monotonic(), data)
            return data

    async def match(self, provider, league, home, away, kickoff, translate=None):
        if not kickoff:return None
        date=kickoff.astimezone(TZ).strftime("%Y-%m-%d")
        data=await self.feed(provider,league,date)
        possible=[]
        for row in data:
            a=similarity(row["home"],home,translate)
            b=similarity(row["away"],away,translate)
            start=parse_time(row.get("start"))
            if start is None or abs((start-kickoff).total_seconds()) > 16*3600:
                continue
            if min(a,b)<0.75:continue
            possible.append((min(a,b),(row["updated"],row["book"]),row))
        if not possible:return None
        possible.sort(key=lambda p:(p[0],p[1]),reverse=True)
        return possible[0][2]
