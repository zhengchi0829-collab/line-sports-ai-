"""Read only directly accessible official Taiwan Sports Lottery odds.

One GET, no login, proxy, anti-bot bypass or invented bookmaker prices.
"""
import argparse
import json
import re
import sys
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

URL = "https://www.sportslottery.com.tw/sportsbook/daily-coupons"
TZ = ZoneInfo("Asia/Taipei")
ALIASES = {
    "date": ("日期", "賽事日期", "比賽日期"),
    "time": ("時間", "開賽時間", "比賽時間"),
    "home": ("主隊", "主場球隊", "主場隊伍", "主場"),
    "away": ("客隊", "客場球隊", "客場隊伍", "客場"),
    "home_price": ("主勝賠率", "主隊賠率", "不讓分主勝", "主勝"),
    "away_price": ("客勝賠率", "客隊賠率", "不讓分客勝", "客勝"),
    "draw_price": ("和局賠率", "和局", "和"),
    "league": ("聯賽", "聯盟", "賽事名稱"),
    "sport": ("運動", "運動項目", "球種", "球類"),
    "event_id": ("賽事編號", "場次編號", "場次"),
}
KNOWN = ("英超", "西甲", "德甲", "義甲", "法甲", "歐冠",
         "NBA", "NCAA", "NBL", "TPBL", "MLB", "NPB", "KBO", "CPBL", "MiLB")


class FetchFailure(Exception):
    pass


class Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.rows = None
        self.row = None
        self.cell = None
        self.script = None
        self.tables = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            if self.depth == 0:
                self.rows = []
            self.depth += 1
        elif tag == "tr" and self.depth:
            self.row = []
        elif tag in ("td", "th") and self.depth and self.row is not None:
            self.cell = []
        elif tag == "script" and (
                attrs.get("type") == "application/json" or
                attrs.get("id") == "__NEXT_DATA__"):
            self.script = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)
        if self.script is not None:
            self.script.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None and self.rows is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None
        elif tag == "table" and self.depth:
            self.depth -= 1
            if not self.depth and self.rows is not None:
                self.tables.append(self.rows)
                self.rows = None
        elif tag == "script" and self.script is not None:
            self.scripts.append("".join(self.script))
            self.script = None


def odds_number(value):
    value = str(value or "").strip()
    if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3})?", value):
        return None
    result = float(value)
    return result if 1 < result < 100 else None


def league_name(league, sport):
    label = str(league or "") + " " + str(sport or "")
    for known in KNOWN:
        if re.search(r"(?<![a-z])" + re.escape(known) + r"(?![a-z])", label, re.I):
            return known
    return "足球" if "足球" in label else None


def kickoff(date_value, time_value, today):
    raw = str(time_value or "").strip()
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        value = value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)
        return value if value.date() == today else None
    except ValueError:
        pass
    raw = str(date_value or "") + " " + raw
    found = re.search(
        r"(?:(20\d{2})\s*[-/]\s*)?(\d{1,2})\s*[-/]\s*(\d{1,2})"
        r".{0,18}?(\d{1,2}):(\d{2})", raw)
    if not found:
        return None
    yy, mm, dd, hour, minute = found.groups()
    try:
        value = datetime(int(yy or today.year), int(mm), int(dd),
                         int(hour), int(minute), tzinfo=TZ)
        return value if value.date() == today else None
    except ValueError:
        return None


def verified_row(data, today):
    home, away = (str(data.get(k) or "").strip() for k in ("home", "away"))
    hprice, aprice = odds_number(data.get("home_price")), odds_number(data.get("away_price"))
    if (not home or not away or home == away or len(home) > 70 or len(away) > 70
            or not any(c.isalpha() for c in home + away)
            or hprice is None or aprice is None):
        return None
    league = league_name(data.get("league"), data.get("sport"))
    when = kickoff(data.get("date"), data.get("time") or data.get("start"), today)
    if not league or when is None:
        return None
    row = {"home": home, "away": away, "start": when.isoformat(),
           "home_price": hprice, "away_price": aprice,
           "draw_price": odds_number(data.get("draw_price")),
           "spread": None, "spread_home_price": None, "spread_away_price": None,
           "total": None, "over_price": None, "under_price": None}
    if str(data.get("event_id") or "").strip():
        row["event_id"] = str(data["event_id"])[:50]
    return league, row


def parse_public_html(html, today):
    parser = Tables()
    parser.feed(html)
    found = []
    for table in parser.tables:
        for i, headers in enumerate(table[:5]):
            columns = {}
            for index, value in enumerate(headers):
                for name, aliases in ALIASES.items():
                    if name not in columns and re.sub(r"\s+", "", value) in aliases:
                        columns[name] = index
            if not all(k in columns for k in ("home", "away", "home_price", "away_price")):
                continue
            if not (("time" in columns or "date" in columns)
                    and ("sport" in columns or "league" in columns)):
                continue
            for cells in table[i + 1:]:
                if len(cells) <= max(columns.values()):
                    continue
                row = verified_row({k: cells[v] for k, v in columns.items()}, today)
                if row:
                    found.append(row)
            break

    def walk(node, depth=0):
        if depth > 16:
            return
        if isinstance(node, dict):
            if {"home", "away", "home_price", "away_price"}.issubset(node):
                row = verified_row(node, today)
                if row:
                    found.append(row)
            for value in node.values():
                if isinstance(value, (list, dict)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:2000]:
                walk(item, depth + 1)

    for script in parser.scripts:
        try:
            walk(json.loads(script))
        except (ValueError, TypeError, RecursionError):
            continue
    output, seen = {}, set()
    for league, row in found:
        identity = (league, row["home"], row["away"], row["start"])
        if identity not in seen:
            output.setdefault(league, []).append(row)
            seen.add(identity)
    return output


def direct_fetch():
    request = Request(URL, headers={
        "User-Agent": "line-sports-ai-public-odds-check/1.0",
        "Accept": "text/html",
    })
    try:
        with urlopen(request, timeout=22) as response:
            if response.status != 200:
                raise FetchFailure("HTTP_" + str(response.status))
            if urlparse(response.geturl()).hostname not in {
                    "www.sportslottery.com.tw", "sportslottery.com.tw"}:
                raise FetchFailure("UNEXPECTED_REDIRECT")
            if "html" not in response.headers.get("Content-Type", "").lower():
                raise FetchFailure("NON_HTML_RESPONSE")
            body = response.read(2_000_001)
    except HTTPError as exc:
        raise FetchFailure("HTTP_" + str(exc.code)) from exc
    except URLError as exc:
        raise FetchFailure("NETWORK_FAILURE") from exc
    if len(body) > 2_000_000:
        raise FetchFailure("RESPONSE_TOO_LARGE")
    return body.decode("utf-8", errors="replace")


def refresh(output, status_path, now=None, fetch=direct_fetch):
    now = now or datetime.now(TZ)
    status = {"checked_at": now.isoformat(), "day": now.date().isoformat(),
              "source_url": URL, "ok": False}
    try:
        leagues = parse_public_html(fetch(), now.date())
        count = sum(len(v) for v in leagues.values())
        if not count:
            raise FetchFailure("NO_VERIFIABLE_ODDS_OR_UNSUPPORTED_LAYOUT")
        snapshot = {"day": now.date().isoformat(),
                    "captured_at": now.isoformat(),
                    "source_url": URL,
                    "capture_method": "official_public_html",
                    "source_note": "Direct public page snapshot; not live or guaranteed latest prices.",
                    "leagues": leagues}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2)
                          + "\n", encoding="utf-8")
        status.update(ok=True, verified_rows=count,
                      leagues={k: len(v) for k, v in leagues.items()})
    except FetchFailure as exc:
        status["error"] = str(exc)[:120]
    except Exception as exc:
        status["error"] = "UNEXPECTED_" + type(exc).__name__
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2)
                           + "\n", encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False))
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    cli = argparse.ArgumentParser()
    cli.add_argument("--out", type=Path, default=Path("app/tw_official_odds.json"))
    cli.add_argument("--status", type=Path, required=True)
    args = cli.parse_args()
    sys.exit(refresh(args.out, args.status))
