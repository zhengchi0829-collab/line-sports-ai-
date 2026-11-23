"""Authorized sportsbook odds sources. Never claim model numbers are actual odds."""
import asyncio
import difflib
import json
import os
import re
import time
import logging
from scripts.update_tw_official_odds import parse_public_html, URL as TW_PUBLIC_URL
from .schedule import platform_credit_budget, platform_market_plan

log = logging.getLogger("sports_bot")
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import redis.asyncio as redis_async
from redis.exceptions import RedisError

TZ = ZoneInfo("Asia/Taipei")
TW_CACHE_URL = ("https://raw.githubusercontent.com/zhengchi0829-collab/"
                "line-sports-ai-/odds-cache/tw_third_party_odds.json")
KEYS = {
    "英超": "soccer_epl", "西甲": "soccer_spain_la_liga",
    "德甲": "soccer_germany_bundesliga", "義甲": "soccer_italy_serie_a",
    "法甲": "soccer_france_ligue_one", "歐冠": "soccer_uefa_champs_league",
    "NBA": "basketball_nba", "NCAA": "basketball_ncaab",
    "MLB": "baseball_mlb",
}
JSPORTS = {
    "足球": "FTB", "英超": "FTB", "西甲": "FTB", "德甲": "FTB", "義甲": "FTB",
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


def odds_market_insights(book, *, soccer=False):
    """Explain only observed sportsbook prices, not invented model forecasts.

    Every number comes from the same actual quoted market in `book`.
    Normalization removes the bookmaker's summed implied probability; it
    cannot be interpreted as an independently estimated team win rate.
    """
    def probability(value):
        price = number(value)
        return 1.0 / price if price is not None and price > 1 else None

    def normalize(pairs):
        raw = [(label, probability(price)) for label, price in pairs]
        if any(value is None for _, value in raw):
            return None
        total = sum(value for _, value in raw)
        if total <= 0:
            return None
        return [(label, value / total) for label, value in raw]

    result = []
    takeaways = []
    winner = [
        ("主隊", book.get("home_price")),
        ("客隊", book.get("away_price")),
    ]
    if soccer:
        winner.append(("和局", book.get("draw_price")))
    elif probability(book.get("draw_price")) is not None:
        winner.append(("和局", book.get("draw_price")))

    outcomes = normalize(winner)
    if outcomes:
        ordered = sorted(outcomes, key=lambda pair: pair[1], reverse=True)
        leader, share = ordered[0]
        gap = share - ordered[1][1]
        if gap < 0.03:
            result.append("🏆 獨贏分析：市場價格接近，暫無明顯傾向")
            takeaways.append("獨贏接近均勢")
        else:
            result.append(f"🏆 獨贏分析：市場較看好{leader}（去抽水約 {share*100:.1f}%）")
            takeaways.append("獨贏盤偏向" + leader)
        implied = [probability(price) for _, price in winner]
        total = sum(implied)
        if total >= 0.999:
            result.append(f"📊 獨贏盤口總隱含機率 {total*100:.1f}%（含莊家水位）")
    elif soccer and probability(book.get("home_price")) and probability(book.get("away_price")):
        result.append("🏆 獨贏分析：缺少和局賠率，無法完整比較三項市場")
    else:
        result.append("🏆 獨贏分析：缺少完整雙方賠率")

    handicap = number(book.get("spread"))
    handicap_prices = normalize([
        ("主隊", book.get("spread_home_price")),
        ("客隊", book.get("spread_away_price")),
    ]) if handicap is not None else None
    if handicap_prices:
        h_label, h_prob = max(handicap_prices, key=lambda pair: pair[1])
        gap = abs(handicap_prices[0][1] - handicap_prices[1][1])
        if gap >= 0.03:
            result.append(
                f"⚖️ 讓分分析：主隊 {handicap:+g}，市場水位略偏{h_label}"
                f"（去抽水約 {h_prob*100:.1f}%）")
            takeaways.append("讓分水位偏" + h_label)
        else:
            result.append(f"⚖️ 讓分分析：主隊 {handicap:+g}，雙方水位接近")
    else:
        result.append("⚖️ 讓分分析：尚無完整實際盤口")

    total_line = number(book.get("total"))
    total_prices = normalize([
        ("大分", book.get("over_price")),
        ("小分", book.get("under_price")),
    ]) if total_line is not None and total_line > 0 else None
    if total_prices:
        t_label, t_prob = max(total_prices, key=lambda pair: pair[1])
        gap = abs(total_prices[0][1] - total_prices[1][1])
        if gap >= 0.03:
            result.append(
                f"📈 大小分分析：{total_line:g}，水位略偏{t_label}"
                f"（去抽水約 {t_prob*100:.1f}%）")
            takeaways.append("大小分水位偏" + t_label)
        else:
            result.append(f"📈 大小分分析：{total_line:g}，大小分水位接近")
    else:
        result.append("📈 大小分分析：尚無完整實際盤口")

    result.append("🧭 盤口結論：" + ("；".join(takeaways) if takeaways
                                      else "目前無法得出可靠的市場傾向"))
    result.append("⚠️ 僅反映當時賠率與相對市場定價；非獨立勝率預測或保證獲利推薦")
    return result


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
        shared_url = os.getenv("PLATFORM_ODDS_REDIS_URL", "").strip()
        # A separate free Key Value instance survives web deployments;
        # it is still in-memory and may lose data if Key Value itself restarts.
        self.shared_redis = (redis_async.from_url(
            shared_url, socket_connect_timeout=1.2, socket_timeout=1.2,
            retry_on_timeout=False) if shared_url else None)
        self.public_blob_cache = {}
        self.official_snapshot_file = Path(__file__).resolve().parent / "tw_official_odds.json"
        self.third_party_snapshot_file = Path(__file__).resolve().parent / "tw_third_party_odds.json"
        # Reference data is published on a separate non-deploy GitHub branch.
        self.tw_remote = {}
        self.tw_remote_status = {"status": "not_checked"}
        self.tw_remote_checked = 0.0
        self.tw_remote_lock = asyncio.Lock()
        self.tw_live = {}
        self.tw_probe = {}
        self.tw_last_request = 0.0
        self.tw_probe_lock = asyncio.Lock()
        self.tw_probe_task: asyncio.Task | None = None
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


    @staticmethod
    def _shared_key(day: str) -> str:
        return "line-sports-ai:platform-odds:" + day

    async def restore_shared_daily(self) -> bool:
        """Restore same-day quotes after a web deploy without spending API credits."""
        client = self.shared_redis
        if client is None:
            return False
        day = datetime.now(TZ).date().isoformat()
        try:
            payload = await asyncio.wait_for(client.get(self._shared_key(day)),
                                             timeout=1.7)
            if payload:
                if len(payload) > 1_500_000:
                    raise ValueError("Invalid odds-cache payload size")
                saved = json.loads(payload)
                if (isinstance(saved, dict) and saved.get("day") == day
                        and isinstance(saved.get("leagues"), dict)
                        and saved.get("state") in {
                            "ready", "attempted", "quota", "no_events",
                            "missing_key", "catalog_error", "storage_unavailable"}):
                    if (self.daily.get("day") != day or
                            (self.daily.get("state") != "ready" and
                             saved.get("state") == "ready")):
                        self.daily = saved
                        self._save_daily(saved)
                    count = sum(len(v) for v in
                                self.daily.get("leagues", {}).values()
                                if isinstance(v, list))
                    log.info("platform_cache_restore status=%s day=%s fixtures=%s",
                             self.daily.get("state"), day, count)
                    return True
            # A same-day local snapshot can exist when shared storage was
            # provisioned after the morning batch. Seed without API spending.
            if (self.daily.get("day") == day
                    and self.daily.get("state") in {"ready", "attempted",
                        "quota", "no_events", "missing_key", "catalog_error"}
                    and isinstance(self.daily.get("leagues"), dict)):
                seeded = await self.save_shared_daily(self.daily)
                log.info("platform_cache_seed day=%s saved=%s state=%s",
                         day, seeded, self.daily.get("state"))
                return seeded
            log.info("platform_cache_restore status=empty day=%s", day)
            return False
        except (RedisError, OSError, TimeoutError, ValueError, TypeError) as exc:
            log.warning("platform_cache_restore status=error kind=%s",
                        type(exc).__name__)
            return False

    async def save_shared_daily(self, record: dict) -> bool:
        """Write the attempt marker and completed rows to the shared cache."""
        if self.shared_redis is None:
            return True  # Local tests and deployments without shared storage
        try:
            payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            if len(payload) > 1_500_000:
                raise ValueError("Odds cache exceeds storage limit")
            success = await asyncio.wait_for(
                self.shared_redis.set(
                    self._shared_key(record["day"]), payload, ex=48 * 3600),
                timeout=1.7)
            if not success:
                return False
            log.info("platform_cache_write day=%s state=%s fixtures=%s",
                     record["day"], record.get("state"),
                     sum(len(v) for v in record.get("leagues", {}).values()
                         if isinstance(v, list)))
            return True
        except (RedisError, OSError, TimeoutError, TypeError, ValueError) as exc:
            log.warning("platform_cache_write status=error kind=%s",
                        type(exc).__name__)
            return False

    def daily_status(self, league):
        day = datetime.now(TZ).strftime("%Y-%m-%d")
        record = self.daily
        if record.get("day") != day:
            return "not_cached"
        if record.get("state") == "quota":
            return "quota"
        if record.get("state") == "storage_unavailable":
            return "storage_unavailable"
        if record.get("state") == "attempted":
            return "unavailable"
        if league in record.get("deferred_leagues", []):
            return "deferred"
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

    async def refresh_platform_daily(self):
        """Scheduled 00:30 Taiwan: one shared cost-controlled daily batch."""
        await self._daily_platform_feed("NBA")
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        summary = self.daily if self.daily.get("day") == today else {}
        log.info(
            "platform_odds_daily day=%s state=%s leagues=%s planned_credits=%s remaining=%s",
            today, summary.get("state", "unavailable"),
            len(summary.get("attempted_leagues", [])),
            summary.get("credits_planned", 0),
            summary.get("remaining", "unknown"),
        )
        return summary

    async def _daily_platform_feed(self, league):
        """Daily 00:30 batch only; user taps never call this method.

        Read /v4/sports (no credit cost), budget remaining free credits across
        remaining calendar days, and stage a durable-on-instance attempt marker
        BEFORE all chargeable requests. On free Render, /tmp can reset on a
        redeploy; strict cross-deployment exactly-once requires durable storage.
        """
        now = datetime.now(TZ)
        day = now.strftime("%Y-%m-%d")
        async with self.daily_lock:
            if self.daily.get("day") != day:
                await self.restore_shared_daily()
            if self.daily.get("day") == day:
                return self.stored_platform_rows(league)
            key = (os.getenv("ODDS_API_KEY") or os.getenv("THE_ODDS_API_KEY") or "").strip()
            if not key:
                record = {"day": day, "state": "missing_key", "leagues": {}}
                self._save_daily(record)
                await self.save_shared_daily(record)
                return []
            headers = {"x-api-key": key}
            try:
                catalog_response = await self.client.get(
                    "https://api.the-odds-api.com/v4/sports/",
                    headers=headers, timeout=6)
                catalog_response.raise_for_status()
                catalog = catalog_response.json()
                if not isinstance(catalog, list):
                    raise ValueError("Sports catalog must be an array")
                raw = catalog_response.headers.get("x-requests-remaining", "")
                if not raw.isdecimal():
                    raise ValueError("Quota headers unavailable")
                remaining = int(raw)
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                record = {
                    "day": day, "state": "catalog_error",
                    "leagues": {}, "error_type": type(exc).__name__,
                }
                self._save_daily(record)
                await self.save_shared_daily(record)
                return []

            active = {row.get("key") for row in catalog
                      if isinstance(row, dict) and row.get("active")}
            supported = [(label, sport_key) for label, sport_key in KEYS.items()
                         if sport_key in active]
            not_offered = [label for label, sport_key in KEYS.items()
                           if sport_key not in active]
            budget = platform_credit_budget(remaining, now)
            plan = platform_market_plan(supported, budget, now)
            planned = sum(len(markets.split(",")) for _, _, markets in plan)
            deferred = [label for label, _ in supported
                        if label not in {label for label, _, _ in plan}]
            if not plan:
                record = {
                    "day": day, "state": "quota" if supported else "no_events",
                    "leagues": {}, "remaining": remaining,
                    "credits_budget": budget, "credits_planned": 0,
                    "deferred_leagues": deferred, "not_offered": not_offered,
                }
                self._save_daily(record)
                await self.save_shared_daily(record)
                return []

            # Never make chargeable calls unless an attempt marker is saved.
            pending = {
                "day": day, "state": "attempted",
                "captured_at": now.isoformat(),
                "leagues": {}, "remaining": remaining,
                "credits_budget": budget, "credits_planned": planned,
                "attempted_leagues": [name for name, _, _ in plan],
                "deferred_leagues": deferred,
                "not_offered": not_offered,
            }
            if not self._save_daily(pending):
                return []
            # Shared pending marker must be committed BEFORE spending credits.
            if not await self.save_shared_daily(pending):
                self._save_daily({**pending, "state": "storage_unavailable"})
                return []

            async def fetch_one(label, sport_key, markets):
                try:
                    response = await self.client.get(
                        f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
                        headers=headers,
                        params={"regions": "us", "markets": markets,
                                "oddsFormat": "decimal"},
                        timeout=8)
                    if response.status_code != 200:
                        return label, [], None, "api_error"
                    all_rows = theodds_normalize(response.json())
                    # Prefer a complete, newer quotation from one bookmaker;
                    # never blend unmatched odds from different bookmakers.
                    unique = {}
                    def quote_rank(row):
                        h2h = number(row.get("home_price")) and number(row.get("away_price"))
                        spreads = (number(row.get("spread")) is not None and
                                   number(row.get("spread_home_price")) and
                                   number(row.get("spread_away_price")))
                        totals = (number(row.get("total")) is not None and
                                  number(row.get("over_price")) and
                                  number(row.get("under_price")))
                        return (int(bool(h2h)) + int(bool(spreads)) +
                                int(bool(totals)), str(row.get("updated") or ""))
                    for row in all_rows:
                        if (not row["home"] or not row["away"] or
                                not parse_time(row.get("start"))):
                            continue
                        identity = (row["home"], row["away"], row["start"])
                        if (identity not in unique or
                                quote_rank(row) > quote_rank(unique[identity])):
                            unique[identity] = row
                    rows = sorted(unique.values(),
                                  key=lambda row: row.get("start") or "")
                    balance = response.headers.get("x-requests-remaining")
                    return label, rows, (
                        int(balance) if balance is not None and balance.isdecimal()
                        else None), ("ok" if rows else "no_events")
                except (httpx.HTTPError, ValueError, TypeError, KeyError):
                    return label, [], None, "api_error"

            # Sequential requests avoid sudden concurrent consumption if a
            # provider quota changes underneath our budget calculation.
            outcomes = []
            for label, sport_key, markets in plan:
                outcome = await fetch_one(label, sport_key, markets)
                outcomes.append(outcome)
                if outcome[2] is not None and outcome[2] < 1:
                    break
            leagues = {label: rows for label, rows, _, _ in outcomes}
            balances = [balance for _, _, balance, _ in outcomes
                        if balance is not None]
            attempted = [label for label, _, _, _ in outcomes]
            result = {
                "day": day, "state": "ready",
                "captured_at": now.isoformat(), "leagues": leagues,
                "remaining": min(balances) if balances else remaining,
                "credits_budget": budget, "credits_planned": planned,
                "attempted_leagues": attempted, "not_offered": not_offered,
                "deferred_leagues": deferred + [
                    label for label, _, _ in plan if label not in attempted],
                "empty_leagues": [label for label, _, _, state in outcomes
                                  if state == "no_events"],
                "failed_leagues": [label for label, _, _, state in outcomes
                                   if state == "api_error"],
            }
            self._save_daily(result)
            if not await self.save_shared_daily(result):
                # Keep today's quotes available in memory, but expose the
                # persistence failure instead of silently claiming success.
                result["storage_error"] = True
                self._save_daily(result)
                log.error("platform_cache_final_save_failed day=%s", day)
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

    async def refresh_remote_reference(self, now=None):
        """Read the latest hourly data branch without restarting Render.

        GitHub Actions writes this publicly accessible JSON into odds-cache.
        Never trust it without date/source/time/event and price checks.
        """
        now = now or datetime.now(TZ)
        day = now.strftime("%Y-%m-%d")
        async with self.tw_remote_lock:
            if (self.tw_remote_checked and
                    time.monotonic() - self.tw_remote_checked < 270 and
                    self.tw_remote_status.get("day") == day):
                return self.tw_remote_status
            self.tw_remote_checked = time.monotonic()
            state = {"day": day, "checked_at": now.isoformat(),
                     "status": "unavailable", "count": 0}
            try:
                response = await self.client.get(
                    TW_CACHE_URL,
                    # A new hourly odds-cache commit may still be hidden behind
                    # GitHub raw CDN's earlier cached response. A short-lived
                    # cache-busting key asks for the newest published snapshot.
                    params={"check": str(int(now.timestamp() // 300))},
                    headers={"Accept": "application/json", "Cache-Control": "no-cache"},
                    timeout=4.0)
                if response.status_code != 200:
                    state["status"] = "http_" + str(response.status_code)
                elif len(response.content) > 750000:
                    state["status"] = "too_large"
                else:
                    data = response.json()
                    stamp = parse_time(data.get("captured_at")) if isinstance(data, dict) else None
                    state["source_captured_at"] = stamp.isoformat() if stamp else None
                    source_url = (
                        "https://www.playsport.cc/gamesData/result?"
                        "allianceid=4&gametime=" + day.replace("-", ""))
                    if (not isinstance(data, dict) or data.get("day") != day
                            or data.get("capture_method") != "third_party_playsport"
                            or data.get("source_url") != source_url or stamp is None):
                        state["status"] = "invalid_source"
                    elif not timedelta(minutes=-5) <= now - stamp <= timedelta(minutes=90):
                        state["status"] = "stale_snapshot"
                    else:
                        raw_leagues = data.get("leagues")
                        if not isinstance(raw_leagues, dict):
                            state["status"] = "bad_rows"
                        else:
                            valid = {
                                name: verified_taiwan_rows(rows, day)
                                for name, rows in raw_leagues.items()
                                if name in JSPORTS and isinstance(rows, list)
                            }
                            count = sum(map(len, valid.values()))
                            if count:
                                data["leagues"] = valid
                                self.tw_remote = data
                                state.update(status="ok", count=count,
                                             captured_at=stamp.isoformat())
                            else:
                                state["status"] = "no_verified_events"
            except (httpx.HTTPError, TimeoutError) as exc:
                state["status"] = "network_" + type(exc).__name__
            except (ValueError, KeyError, TypeError) as exc:
                state["status"] = "invalid_" + type(exc).__name__
            self.tw_remote_status = state
            log.info("tw_odds_remote_cache status=%s count=%s day=%s source_at=%s",
                     state["status"], state["count"], day,
                     state.get("source_captured_at") or "unknown")
            return state

    def schedule_tw_public_probe(self) -> bool:
        """Queue one free official-source probe without holding the LINE reply.

        The public official page often returns HTTP 403 after a long timeout.
        One background attempt per 30 minutes avoids repeated slow user taps.
        """
        if (self.tw_last_request and
                time.monotonic() - self.tw_last_request < 1800):
            return False
        if self.tw_probe_task and not self.tw_probe_task.done():
            return False
        task = asyncio.create_task(self.check_tw_public_now())
        self.tw_probe_task = task

        def report_error(done: asyncio.Task):
            if done.cancelled():
                return
            try:
                done.result()
            except Exception as exc:
                log.warning("tw_odds_background_error kind=%s",
                            type(exc).__name__)

        task.add_done_callback(report_error)
        return True

    async def close(self):
        task = self.tw_probe_task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.shared_redis is not None:
            await self.shared_redis.aclose()

    async def check_tw_public_now(self, now=None):
        """One bounded public GET shared across LINE users; never bypass 403."""
        now = now or datetime.now(TZ)
        day = now.strftime("%Y-%m-%d")
        async with self.tw_probe_lock:
            if (self.tw_probe.get("day") == day and self.tw_last_request
                    and time.monotonic() - self.tw_last_request < 1800):
                return self.tw_probe
            self.tw_last_request = time.monotonic()
            status = {"day": day, "at": now.strftime("%Y-%m-%d %H:%M"),
                      "status": "unavailable", "count": 0}
            try:
                resp = await self.client.get(
                    TW_PUBLIC_URL, headers={"Accept": "text/html"}, timeout=7.0)
                if resp.status_code != 200:
                    status["status"] = "http_" + str(resp.status_code)
                elif resp.url.host not in ("www.sportslottery.com.tw",
                                           "sportslottery.com.tw"):
                    status["status"] = "bad_redirect"
                elif "html" not in resp.headers.get("content-type", "").lower():
                    status["status"] = "not_html"
                elif len(resp.content) > 2_000_000:
                    status["status"] = "too_large"
                else:
                    leagues = parse_public_html(resp.text, now.date())
                    total = sum(len(x) for x in leagues.values())
                    if total:
                        self.tw_live = {
                            "day": day, "captured_at": now.isoformat(),
                            "source_url": TW_PUBLIC_URL,
                            "capture_method": "official_public_html",
                            "leagues": leagues}
                        status.update(status="ok", count=total)
                    else:
                        status["status"] = "no_verified_prices"
            except (httpx.HTTPError, TimeoutError) as exc:
                status["status"] = "network_" + type(exc).__name__
            except (ValueError, KeyError, TypeError) as exc:
                status["status"] = "parse_" + type(exc).__name__
            self.tw_probe = status
            log.warning("tw_odds_on_demand status=%s count=%s checked=%s",
                        status["status"], status["count"], status["at"])
            return status

    def official_daily_rows(self, league, day, *, now=None):
        """Read a verified, date-stamped sportslottery.com.tw odds snapshot.

        The scheduled assistant may update this repository JSON ONLY after
        successfully reading authorized official data. An empty placeholder
        must never be presented as real quotes.
        """
        now = now or datetime.now(TZ)
        try:
            # Prefer the newest verified snapshot by capture time.
            # Remote odds branch gets refreshed without deploying bot code.
            official_url = "https://www.sportslottery.com.tw/sportsbook/daily-coupons"
            third_url = ("https://www.playsport.cc/gamesData/result?"
                         "allianceid=4&gametime=" + day.replace("-", ""))
            candidates = [self.tw_live, self.tw_remote]
            for path in (self.official_snapshot_file, self.third_party_snapshot_file):
                try:
                    candidates.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError, TypeError):
                    continue
            matching = []
            for candidate in candidates:
                if not isinstance(candidate, dict) or candidate.get("day") != day:
                    continue
                is_third_party = (
                    candidate.get("capture_method") == "third_party_playsport"
                    and candidate.get("source_url") == third_url)
                if candidate.get("source_url") != official_url and not is_third_party:
                    continue
                captured = parse_time(candidate.get("captured_at"))
                if captured is None or captured.strftime("%Y-%m-%d") != day:
                    continue
                # Never show expired third-party quotes in the live daily menu.
                if (is_third_party and day == now.date().isoformat()
                        and not timedelta(minutes=-5) <= now - captured <= timedelta(minutes=90)):
                    continue
                leagues = candidate.get("leagues")
                if not isinstance(leagues, dict) or not isinstance(leagues.get(league), list):
                    continue
                if not leagues[league]:
                    continue
                matching.append((captured, candidate, is_third_party))
            if not matching:
                return []
            # A newly verified official-page response outranks third-party
            # references even if those were captured a few minutes later.
            live = next((item for item in matching
                         if item[1] is self.tw_live), None)
            captured_at, record, third_party = (
                live if live is not None else max(matching, key=lambda x: x[0]))
            leagues = record.get("leagues")
            if not isinstance(leagues, dict) or league not in JSPORTS:
                return []
            raw_rows = leagues.get(league)
            if not isinstance(raw_rows, list):
                return []
            output = []
            for row in raw_rows:
                if not isinstance(row, dict):
                    continue
                # Published pre-match prices are not usable after kickoff.
                if third_party and day == now.date().isoformat():
                    kickoff = parse_time(row.get("start"))
                    if kickoff is None or kickoff <= now:
                        continue
                # Keep the external snapshot isolated from provider labels.
                clean = {
                    "source": ("台彩盤口（玩運彩第三方轉載，非官網核實）"
                               if third_party else
                               "台灣運彩官網截圖（人工核對）"
                               if record.get("capture_method") == "user_screenshot"
                               else "台灣運彩官網賽事表（每日快照）"),
                    "book": "台灣運彩",
                    "updated": captured_at.isoformat(),
                    "home": str(row.get("home") or "").strip(),
                    "away": str(row.get("away") or "").strip(),
                    "start": str(row.get("start") or ""),
                    "home_price": number(row.get("home_price")),
                    "away_price": number(row.get("away_price")),
                    "draw_price": number(row.get("draw_price")),
                    "spread": number(row.get("spread")),
                    "spread_home_price": number(row.get("spread_home_price")),
                    "spread_away_price": number(row.get("spread_away_price")),
                    "total": number(row.get("total")),
                    "over_price": number(row.get("over_price")),
                    "under_price": number(row.get("under_price")),
                }
                # Accept complete paired markets only; never synthesize a price.
                if (not clean["home_price"] or clean["home_price"] <= 1
                        or not clean["away_price"] or clean["away_price"] <= 1):
                    clean["home_price"] = clean["away_price"] = clean["draw_price"] = None
                if (clean["spread"] is None
                        or not clean["spread_home_price"] or clean["spread_home_price"] <= 1
                        or not clean["spread_away_price"] or clean["spread_away_price"] <= 1):
                    clean["spread"] = clean["spread_home_price"] = clean["spread_away_price"] = None
                if (clean["total"] is None or clean["total"] <= 0
                        or not clean["over_price"] or clean["over_price"] <= 1
                        or not clean["under_price"] or clean["under_price"] <= 1):
                    clean["total"] = clean["over_price"] = clean["under_price"] = None
                output.append(clean)
            result = verified_taiwan_rows(output, day)
            self.cache[("tw", league, day)] = (time.monotonic(), result)
            return result
        except (OSError, ValueError, TypeError):
            return []

    async def feed(self, provider, league, date):
        if provider != "tw":
            # Browsing LINE menus must never spend the account's shared quota.
            # Quotes are populated by the 00:30 Taiwan background job only.
            return self.stored_platform_rows(league)
        # Daily official snapshot takes priority over JBot trial quotes.
        official = self.official_daily_rows(league, date)
        if official:
            return official
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
