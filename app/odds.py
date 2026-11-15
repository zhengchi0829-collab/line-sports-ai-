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
            "league":str(event.get("league") or ""),
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

def verified_taiwan_rows(rows, date):
    """Exclude missing-date, unrelated-day, and empty quotes from TW displays."""
    verified = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        when = parse_time(row.get("start"))
        if when is None or when.strftime("%Y-%m-%d") != date:
            continue
        if not row.get("home") or not row.get("away"):
            continue
        if not any(number(row.get(field)) is not None and number(row.get(field)) > 1
                   for field in ("home_price", "away_price", "draw_price",
                                 "spread_home_price", "spread_away_price",
                                 "over_price", "under_price")):
            continue
        verified.append(row)
    return verified


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

# Read-only public Blob format documented by community projects. Not an
# authenticated or guaranteed official API. Never bypass a blocked response.
BLOB_SPORTS = {"NBA": 442, "MLB": 443}


def sportslottery_blob_normalize(payload, league):
    """Conservatively parse verified team IDs and explicit numeric outcomes."""
    if isinstance(payload, dict):
        data = payload.get("result", payload)
        if isinstance(data, dict):
            payload = data.get("registerOn") or data.get("liveOn") or []
    if not isinstance(payload, list):
        return []
    records = []
    for event in payload:
        if not isinstance(event, dict) or event.get("si") is None:
            continue
        try:
            if int(event["si"]) != BLOB_SPORTS.get(league):
                continue
        except (ValueError, TypeError):
            continue
        home = event.get("htn")
        away = event.get("atn")
        home = home[0] if isinstance(home, list) and home else home
        away = away[0] if isinstance(away, list) and away else away
        if not isinstance(home, str) or not isinstance(away, str) or not home or not away:
            continue
        try:
            start = datetime.fromtimestamp(int(event["kdt"]) / 1000, timezone.utc).isoformat()
        except (TypeError, ValueError, OSError, KeyError):
            continue
        row = {"source": "台灣運彩公開資料（未授權 API）", "book": "台灣運彩",
               "home": home, "away": away, "start": start,
               "updated": "", "home_price": None, "away_price": None,
               "draw_price": None, "spread": None, "spread_home_price": None,
               "spread_away_price": None, "total": None,
               "over_price": None, "under_price": None}
        for market in event.get("ms") or []:
            if not isinstance(market, dict):
                continue
            try:
                kind = int(market.get("id"))
            except (TypeError, ValueError):
                continue
            for group in market.get("cs") or []:
                if not isinstance(group, list):
                    continue
                bypos = {}
                for cell in group:
                    if isinstance(cell, dict):
                        try:
                            pos = int(cell.get("p"))
                        except (ValueError, TypeError):
                            continue
                        price = number(cell.get("o"))
                        if price is not None and price > 1:
                            bypos[pos] = (price, number(cell.get("h")))
                if kind == 1 and 1 in bypos and (3 in bypos or len(bypos) == 2):
                    # Do not assume 1/2 positions mean home/away in a
                    # three-way sport. Limit to NBA/MLB 2-way markets.
                    if len(bypos) == 2:
                        second = 3 if 3 in bypos else 2
                        row["home_price"], row["away_price"] = bypos[1][0], bypos[second][0]
                elif kind == 3 and 1 in bypos and 3 in bypos:
                    h, a = bypos[1], bypos[3]
                    if h[1] is not None and a[1] is not None and abs(h[1] + a[1]) < .01:
                        row["spread"], row["spread_home_price"], row["spread_away_price"] = h[1], h[0], a[0]
                elif kind == 5 and len(bypos) == 2:
                    first, second = sorted(bypos)
                    over, under = bypos[first], bypos[second]
                    if over[1] is not None and under[1] is not None and abs(over[1] - under[1]) < .01:
                        row["total"], row["over_price"], row["under_price"] = over[1], over[0], under[0]
        if any(row[k] is not None for k in ("home_price", "spread", "total")):
            records.append(row)
    return records


class OddsGateway:
    """Only one The Odds API lookup per Taiwan day, caching all returned fixtures.

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
        self.public_blob_cache = {}
        # Free JBot trial is shared across ALL developers. Only manual requests;
        # durable-on-instance daily cache avoids spending on repeated taps.
        self.jbot_trial_file = Path(os.getenv("JBOT_TRIAL_CACHE_PATH", "/tmp/line_sports_jbot_trial.json"))
        self.jbot_trial_lock = asyncio.Lock()
        self.jbot_trial = self._read_jbot_trial()
        self.jbot_status = {}

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
        if record.get("state") == "quota":
            return "quota"
        if record.get("state") == "attempted":
            return "unavailable"
        rows = record.get("leagues", {}).get(league, [])
        if rows:
            return "cached"
        if league in record.get("not_offered", []):
            return "not_offered"
        if league in record.get("empty_leagues", []):
            return "no_events"
        if league in record.get("failed_leagues", []):
            return "api_error"
        return "unavailable"

    def stored_platform_rows(self, league):
        record = self.daily
        if record.get("day") != datetime.now(TZ).strftime("%Y-%m-%d"):
            return []
        return record.get("leagues", {}).get(league, [])

    async def _daily_platform_feed(self, league):
        """The first tap preloads every available league, then all taps use cache.

        Each sport requires its own The Odds API request. We check the remaining
        free-plan credits via the zero-cost /sports endpoint before a daily batch.
        """
        day = datetime.now(TZ).strftime("%Y-%m-%d")
        async with self.daily_lock:
            record = self.daily
            if record.get("day") == day:
                return self.stored_platform_rows(league)
            key = (os.getenv("ODDS_API_KEY") or os.getenv("THE_ODDS_API_KEY") or "").strip()
            if not key or league not in KEYS:
                return []
            headers = {"x-api-key": key}
            try:
                catalog_response = await self.client.get(
                    "https://api.the-odds-api.com/v4/sports/",
                    headers=headers, timeout=5)
                catalog_response.raise_for_status()
                catalog = catalog_response.json()
                if not isinstance(catalog, list):
                    return []
                available_keys = {sport.get("key") for sport in catalog
                                  if isinstance(sport, dict) and sport.get("active")}
                supported = [(label, sport_key) for label, sport_key in KEYS.items()
                             if sport_key in available_keys]
                not_offered = [label for label in KEYS if label not in {name for name, _ in supported}]
                # Three markets in one US region consume at most 3 credits
                # per league when data is present. Check quota before spending.
                required_credits = 3 * len(supported)
                remaining = int(catalog_response.headers.get("x-requests-remaining", "-1"))
            except (httpx.HTTPError, ValueError, TypeError, KeyError):
                return []
            if remaining < required_credits:
                self._save_daily({"day": day, "state": "quota",
                                  "leagues": {}, "remaining": remaining})
                return []
            # Save before requesting odds so repeated clicks cannot spend twice.
            pending = {"day": day, "state": "attempted", "leagues": {},
                       "remaining": remaining}
            if not self._save_daily(pending):
                return []

            async def fetch_one(label, sport_key):
                try:
                    response = await self.client.get(
                        f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
                        headers=headers,
                        params={"regions": "us", "markets": "h2h,spreads,totals",
                                "oddsFormat": "decimal"},
                        timeout=6)
                    if response.status_code != 200:
                        return label, [], None, "api_error"
                    all_rows = theodds_normalize(response.json())
                    seen = set()
                    rows = []
                    for row in all_rows:
                        identity = (row["home"], row["away"], row["start"])
                        if identity not in seen:
                            seen.add(identity)
                            rows.append(row)
                    balance = response.headers.get("x-requests-remaining")
                    return label, rows, int(balance) if balance is not None else None, ("ok" if rows else "no_events")
                except (httpx.HTTPError, ValueError, TypeError, KeyError):
                    return label, [], None, "api_error"

            outcomes = await asyncio.gather(
                *(fetch_one(label, sport_key) for label, sport_key in supported))
            leagues = {label: rows for label, rows, _, _ in outcomes}
            balances = [balance for _, _, balance, _ in outcomes if balance is not None]
            result = {"day": day, "state": "ready", "leagues": leagues,
                      "remaining": min(balances) if balances else remaining,
                      "attempted_leagues": [label for label, _ in supported],
                      "not_offered": not_offered,
                      "empty_leagues": [label for label, _, _, state in outcomes if state == "no_events"],
                      "failed_leagues": [label for label, _, _, state in outcomes if state == "api_error"]}
            self._save_daily(result)
            return leagues.get(league, [])

    async def _public_blob_feed(self, league, date):
        """One daily attempt for publicly reachable data, never bypass 403."""
        key = (league, date)
        cached = self.public_blob_cache.get(key)
        if cached is not None:
            return cached
        if league not in BLOB_SPORTS:
            return []
        output = []
        # Explicitly public locations; no cookies, tokens, browser spoofing,
        # proxy rotation, or retry on forbidden responses.
        for path in ("Register/On.json", "Live/On.json"):
            try:
                resp = await self.client.get(
                    "https://blob.sportslottery.com.tw/apidata/" + path,
                    headers={"Accept": "application/json"}, timeout=5)
                if resp.status_code in (401, 403):
                    break
                if resp.status_code != 200:
                    continue
                payload = resp.json()
                output = sportslottery_blob_normalize(payload, league)
                if output:
                    break
            except (httpx.HTTPError, ValueError, TypeError):
                continue
        self.public_blob_cache[key] = output
        return output

    def _read_jbot_trial(self):
        try:
            data = json.loads(self.jbot_trial_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_jbot_trial(self, data):
        # Save a pending attempt before calling the API, never on failure retry.
        try:
            self.jbot_trial_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.jbot_trial_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.jbot_trial_file)
            self.jbot_trial = data
            return True
        except OSError:
            return False

    @staticmethod
    def _jbot_league_rows(rows, league):
        # JBot FTB and BSK return all football/other basketball competitions.
        # Only label a quote as belonging to a league if its API league matches.
        names = {
            "英超": ("英超", "英格蘭超級", "premier league"),
            "西甲": ("西甲", "西班牙甲級", "la liga"),
            "德甲": ("德甲", "德國甲級", "bundesliga"),
            "義甲": ("義甲", "義大利甲級", "serie a"),
            "法甲": ("法甲", "法國甲級", "ligue 1"),
            "歐冠": ("歐冠", "歐洲冠軍", "champions league"),
            "NCAA": ("ncaa", "美國大學", "大學籃球"),
            "NBL": ("nbl", "澳洲職籃", "澳洲國家籃球"),
            "TPBL": ("tpbl", "台灣職籃", "台灣職業籃球"),
            "MiLB": ("milb", "小聯盟", "minor league"),
        }
        aliases = names.get(league)
        if not aliases:
            return rows
        return [row for row in rows
                if any(alias.casefold() in str(row.get("league") or "").casefold()
                       for alias in aliases)]

    def trial_status(self, league):
        return self.jbot_status.get(league, "unavailable")

    async def _jbot_trial_feed(self, league, date, key):
        sport = JSPORTS.get(league)
        if not sport:
            self.jbot_status[league] = "not_supported"
            return []
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        request_key = f"{sport}|{date}"
        async with self.jbot_trial_lock:
            record = self.jbot_trial if self.jbot_trial.get("day") == today else {
                "day": today, "attempts": {}, "last_call_at": 0}
            attempts = record.get("attempts", {})
            previous = attempts.get(request_key)
            if isinstance(previous, dict):
                self.jbot_status[league] = previous.get("status", "unavailable")
                matching = self._jbot_league_rows(previous.get("rows") or [], league)
                self.cache[("tw", league, date)] = (time.monotonic(), matching)
                return matching
            # Extremely small allowance, irrespective of the public trial's
            # remaining shared credits. No automatic background refresh.
            if len(attempts) >= 2:
                self.jbot_status[league] = "local_limit"
                return []
            now = time.time()
            if now - float(record.get("last_call_at") or 0) < 6:
                self.jbot_status[league] = "cooldown"
                return []
            attempts[request_key] = {"status": "attempted", "rows": []}
            record["attempts"] = attempts
            record["last_call_at"] = now
            if not self._save_jbot_trial(record):
                self.jbot_status[league] = "cache_unavailable"
                return []
            status, rows = "api_error", []
            try:
                response = await self.client.get(
                    "https://api.sportsbot.tech/v2/odds",
                    params={"sport": sport, "date": date, "mode": "close"},
                    headers={"X-JBot-Token": key}, timeout=7)
                if response.status_code in (401, 403):
                    status = "unauthorized"
                elif response.status_code == 429:
                    status = "quota"
                elif response.status_code == 200:
                    payload = response.json()
                    if isinstance(payload, dict) and payload.get("status") == "OK":
                        rows = verified_taiwan_rows(jbot_normalize(payload), date)
                        status = "ok" if rows else "no_events"
                    else:
                        status = "quota_or_error"
            except (httpx.HTTPError, ValueError, TypeError, KeyError):
                status = "api_error"
            attempts[request_key] = {"status": status, "rows": rows}
            self._save_jbot_trial(record)
            self.jbot_status[league] = status
            matching = self._jbot_league_rows(rows, league)
            self.cache[("tw", league, date)] = (time.monotonic(), matching)
            return matching

    async def feed(self, provider, league, date):
        if provider != "tw":
            return await self._daily_platform_feed(league)
        key = os.getenv("JBOT_API_TOKEN", "").strip()
        if not key:
            self.jbot_status[league] = "missing_key"
            return []
        # Never schedule free trial requests. Each button tap reads the same
        # persistent per-sport/day record after the first manual attempt.
        return await self._jbot_trial_feed(league, date, key)

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
