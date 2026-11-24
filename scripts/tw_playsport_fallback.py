"""Conservative public third-party Taiwan Sports Lottery odds reference.

Data from 玩運彩 '運彩盤', *not* official real-time Taiwan Lottery quotes.
Respect robots.txt, read only the public current-day football results table,
parse labeled teams/odds and retain provenance. Never bypass access controls.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib import robotparser
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")
ORIGIN = "https://www.playsport.cc"
ROBOTS = ORIGIN + "/robots.txt"
USER_AGENT = "line-sports-ai-public-odds-reference/1.0"


class OddsSourceError(ValueError):
    pass


class TableRows(HTMLParser):
    """Capture rows only from the visible public predictgame-table."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.row = None
        self.cell = None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "table":
            if self.depth:
                self.depth += 1
            elif "predictgame-table" in values.get("class", ""):
                self.depth = 1
        if not self.depth:
            return
        if tag == "tr" and self.row is None:
            self.row = {"id": str(values.get("gameid") or ""),
                        "class": values.get("class", ""), "cells": []}
        elif tag == "td" and self.row is not None and self.cell is None:
            self.cell = {"class": values.get("class", ""), "parts": []}

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["parts"].append(data)

    def handle_endtag(self, tag):
        if not self.depth:
            return
        if tag == "td" and self.cell is not None and self.row is not None:
            self.row["cells"].append({
                "class": self.cell["class"],
                "text": re.sub(r"\s+", " ", "".join(self.cell["parts"])).strip(),
            })
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row["cells"]:
                self.rows.append(self.row)
            self.row = None
        elif tag == "table":
            self.depth -= 1


def groups(rows):
    """One published fixture can repeat its gameid on all three market rows."""
    current = []
    for row in rows:
        if "gaprow" in row["class"]:
            if current:
                yield current
            current = []
        elif row["id"]:
            if current and current[0]["id"] == row["id"]:
                current.append(row)
            else:
                if current:
                    yield current
                current = [row]
        elif current:
            current.append(row)
    if current:
        yield current


def numeric(value):
    try:
        price = float(value)
        return price if 1 < price < 100 else None
    except (TypeError, ValueError):
        return None


def market(cells, label):
    for cell in cells:
        if not any(x in cell["class"] for x in (
                "td-bank-bet03", "td-bank-bet01", "td-bank-bet02")):
            continue
        found = re.search(
            re.escape(label) + r"\s*(\d{1,3}(?:\.\d{1,3})?)",
            cell["text"])
        if found:
            return numeric(found.group(1))
    return None


def total_market(cells, direction):
    for cell in cells:
        if not any(x in cell["class"] for x in ("td-bank-bet02", "td-bank-bet04")):
            continue
        found = re.search(
            re.escape(direction) + r"\s*(\d{1,3}(?:\.\d{1,3})?)"
            r"\s*[,，/]\s*(\d{1,3}(?:\.\d{1,3})?)",
            cell["text"])
        if found:
            line = float(found.group(1))
            price = numeric(found.group(2))
            if 0 < line < 30 and price:
                return line, price
    return None


def league_label(info):
    if "英格蘭超級" in info or "英超" in info:
        return "英超"
    if "西班牙甲級" in info or re.search(r"(?<![巴墨])西甲", info):
        return "西甲"
    if "德國甲級" in info or "德甲" in info:
        return "德甲"
    if "義大利甲級" in info or "義甲" in info:
        return "義甲"
    if "法國甲級" in info or "法甲" in info:
        return "法甲"
    if "歐洲冠軍" in info or "歐冠" in info:
        return "歐冠"
    return "足球"


def parse_football(html, today, cutoff=None):
    parser = TableRows()
    parser.feed(html)
    result = {}
    seen = set()
    for group in groups(parser.rows):
        first = group[0]
        if not first["id"].startswith(today.strftime("%Y%m%d")):
            continue
        info = next((c["text"] for c in first["cells"]
                     if "td-gameinfo" in c["class"]), "")
        tm = re.search(r"(AM|PM)\s*(\d{1,2}):(\d{2})", info, re.I)
        if not tm:
            continue
        meridiem, hour, minute = tm.groups()
        h = int(hour) % 12 + (12 if meridiem.upper() == "PM" else 0)
        try:
            start = datetime(today.year, today.month, today.day,
                             h, int(minute), tzinfo=TZ)
        except ValueError:
            continue
        # Avoid presenting completed matches as actionable prices.
        if cutoff is not None and start <= cutoff:
            continue
        teams = [c["text"] for row in group for c in row["cells"]
                 if "td-teaminfo" in c["class"] and c["text"]]
        if len(teams) < 2 or teams[0] == teams[1]:
            continue
        away, home = teams[:2]
        if max(len(away), len(home)) > 70:
            continue
        cells = [c for row in group for c in row["cells"]]
        away_price = market(cells, "客")
        home_price = market(cells, "主")
        draw = market(cells, "和")
        over, under = total_market(cells, "大"), total_market(cells, "小")
        total = over[0] if over and under and over[0] == under[0] else None
        if not (away_price and home_price) and not total:
            continue
        league = league_label(info)
        key = (league, start.isoformat(), away, home)
        if key in seen:
            continue
        seen.add(key)
        row = {
            "event_id": first["id"][-4:],
            "away": away, "home": home, "start": start.isoformat(),
            "away_price": away_price, "home_price": home_price,
            "draw_price": draw,
            "spread": None, "spread_home_price": None,
            "spread_away_price": None, "total": total,
            "over_price": over[1] if total else None,
            "under_price": under[1] if total else None,
        }
        result.setdefault(league, []).append(row)
    return result


def fetch_public_football(day, fetch=None):
    url = (ORIGIN + "/gamesData/result?allianceid=4&gametime=" +
           day.strftime("%Y%m%d"))
    def fetch_url(url):
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=16) as response:
            if response.status != 200 or urlparse(response.url).hostname != "www.playsport.cc":
                raise OddsSourceError("NON_SUCCESS_OR_REDIRECT")
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise OddsSourceError("PAGE_TOO_LARGE")
            return raw.decode("utf-8", errors="replace")
    get = fetch or fetch_url
    try:
        policy = robotparser.RobotFileParser()
        policy.parse(get(ROBOTS).splitlines())
        if not policy.can_fetch(USER_AGENT, url):
            raise OddsSourceError("ROBOTS_DISALLOW")
        return url, get(url)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise OddsSourceError("SOURCE_NETWORK_" + type(exc).__name__) from exc


def refresh(output, status_path, now=None, fetch=None):
    now = now or datetime.now(TZ)
    day = now.date()
    status = {"day": day.isoformat(), "checked_at": now.isoformat(),
              "status": "failed", "source": "玩運彩第三方運彩盤"}
    try:
        url, html = fetch_public_football(day, fetch=fetch)
        leagues = parse_football(html, day, cutoff=now)
        count = sum(map(len, leagues.values()))
        if not count:
            raise OddsSourceError("NO_VERIFIABLE_UPCOMING_PRICES")
        snapshot = {
            "day": day.isoformat(),
            "captured_at": now.isoformat(),
            "source_url": url,
            "capture_method": "third_party_playsport",
            "source_note": ("Third-party published Taiwan Sports Lottery reference odds. "
                            "NOT verified with Taiwan Sports Lottery's live website. "
                            "Please confirm against the official market before betting."),
            "leagues": leagues,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2)
                          + "\n", encoding="utf-8")
        status.update(status="ok", count=count,
                      leagues={k: len(v) for k, v in leagues.items()})
    except OddsSourceError as exc:
        status["error"] = str(exc)
    except Exception as exc:
        status["error"] = "UNEXPECTED_" + type(exc).__name__
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2)
                           + "\n", encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False))
    return 0 if status["status"] == "ok" else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path,
                        default=Path("app/tw_third_party_odds.json"))
    parser.add_argument("--status", type=Path, required=True)
    args = parser.parse_args()
    return refresh(args.out, args.status)


if __name__ == "__main__":
    sys.exit(main())
