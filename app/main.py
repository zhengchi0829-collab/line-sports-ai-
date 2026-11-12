import base64
import hashlib
import hmac
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI(title="體育AI分析系統", version="0.6.3")
log = logging.getLogger("sports_bot")
TZ = ZoneInfo("Asia/Taipei")
MENU = ("歡迎使用【體育AI分析系統】！\n\n"
        "請點選下方快捷按鈕，或輸入：\n"
        "今日賽事｜足球｜籃球｜棒球｜賽事分析｜會員中心")
BUTTONS = ["今日賽事", "未來賽事", "足球", "籃球", "棒球", "賽事分析", "會員中心", "選單"]
SPORTS = {
    "足球": [("英超", "soccer/eng.1"), ("西甲", "soccer/esp.1"), ("歐冠", "soccer/uefa.champions"), ("國際足球", "soccer/fifa.friendly"), ("德甲", "soccer/ger.1"), ("義甲", "soccer/ita.1")],
    "籃球": [("NBA", "basketball/nba")],
    "棒球": [("MLB", "baseball/mlb")],
}

def quick_replies():
    return {"items": [
        {"type": "action", "action": {"type": "message", "label": name, "text": name}}
        for name in BUTTONS
    ]}


def menu_card():
    """LINE Flex Message: polished two-column navigation card."""
    entries = [
        ("📅 今日賽事", "今日賽事", "#2563EB"),
        ("⚽ 足球", "足球", "#0F766E"),
        ("🏀 籃球", "籃球", "#C2410C"),
        ("⚾ 棒球", "棒球", "#7C3AED"),
        ("📊 賽事分析", "賽事分析", "#334155"),
        ("🗓 未來賽事", "未來賽事", "#334155"),
    ]
    def button(label, command, color):
        return {
            "type": "button", "style": "primary", "color": color, "height": "sm",
            "flex": 1, "action": {"type": "message", "label": label, "text": command}
        }
    rows = []
    for i in range(0, len(entries), 2):
        rows.append({
            "type": "box", "layout": "horizontal", "spacing": "sm",
            "contents": [button(*entries[i]), button(*entries[i + 1])]
        })
    return {
        "type": "flex", "altText": "體育AI分析系統｜功能選單",
        "contents": {
            "type": "bubble", "size": "mega",
            "header": {
                "type": "box", "layout": "vertical", "backgroundColor": "#0F172A",
                "paddingAll": "22px", "spacing": "md",
                "contents": [
                    {"type": "text", "text": "SPORTS  /  DATA", "size": "xs",
                     "color": "#93C5FD", "weight": "bold"},
                    {"type": "text", "text": "體育 AI 分析系統", "size": "xl",
                     "weight": "bold", "color": "#FFFFFF"},
                    {"type": "text", "text": "掌握賽況 · 探索數據", "size": "sm",
                     "color": "#CBD5E1"}
                ]
            },
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "18px",
                "spacing": "md",
                "contents": [
                    {"type": "text", "text": "選擇你想查看的功能", "size": "sm",
                     "weight": "bold", "color": "#334155"},
                    *rows,
                    {"type": "separator", "margin": "md", "color": "#E2E8F0"},
                    {"type": "text", "text": "賽事資訊可能延遲｜分析不代表預測",
                     "size": "xxs", "color": "#64748B", "wrap": True}
                ]
            }
        }
    }

async def official_events(client, league, date):
    if league == "baseball/mlb":
        day = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        r = await client.get("https://statsapi.mlb.com/api/v1/schedule", params={"sportId": 1, "date": day}, timeout=2)
        r.raise_for_status()
        output = []
        for group in r.json().get("dates", []):
            for game in group.get("games", []):
                sides = game.get("teams", {})
                away, home = sides.get("away", {}), sides.get("home", {})
                state = game.get("status", {}).get("abstractGameState", "")
                output.append({"competitions": [{"competitors": [
                    {"team": {"displayName": away.get("team", {}).get("name", "客隊")}, "score": str(away.get("score", "-"))},
                    {"team": {"displayName": home.get("team", {}).get("name", "主隊")}, "score": str(home.get("score", "-"))}
                ]}], "status": {"type": {"state": "post" if state == "Final" else ("in" if state == "Live" else "pre"),
                    "shortDetail": game.get("status", {}).get("detailedState", "未開賽")}}})
        return output
    if league == "basketball/nba":
        r = await client.get("https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json", timeout=2)
        r.raise_for_status()
        output = []
        for game in r.json().get("scoreboard", {}).get("games", []):
            if str(game.get("gameTimeUTC", ""))[:10].replace("-", "") != date:
                continue
            away, home = game.get("awayTeam", {}), game.get("homeTeam", {})
            status = game.get("gameStatus", 1)
            output.append({"competitions": [{"competitors": [
                {"team": {"displayName": away.get("teamName", "客隊")}, "score": str(away.get("score", "-"))},
                {"team": {"displayName": home.get("teamName", "主隊")}, "score": str(home.get("score", "-"))}
            ]}], "status": {"type": {"state": "post" if status == 3 else ("in" if status == 2 else "pre"),
                "shortDetail": game.get("gameStatusText", "未開賽")}}})
        return output
    return []

async def openliga_next(client, now):
    """Free no-key Bundesliga fixture feed, independent of ESPN."""
    games = []
    for season in (now.year - 1, now.year):
        response = await client.get(f"https://api.openligadb.de/getmatchdata/bl1/{season}", timeout=3)
        response.raise_for_status()
        games.extend(response.json())
    upcoming = []
    for game in games:
        try:
            raw = game.get("matchDateTimeUTC") or game.get("matchDateTime")
            start = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=ZoneInfo("UTC"))
            start = start.astimezone(TZ)
            if start >= now:
                upcoming.append((start, game))
        except (ValueError, TypeError, AttributeError):
            continue
    if not upcoming:
        return None
    start, game = min(upcoming, key=lambda x: x[0])
    return f"【足球｜德甲下一場】\\n台灣時間：{start:%Y/%m/%d %H:%M}\\n{game.get('team1', {}).get('teamName', '主隊')} vs {game.get('team2', {}).get('teamName', '客隊')}\\n來源：OpenLigaDB 免費公開賽程"

async def nba_next(client, now):
    """Official NBA scoreboard is only today's games; never claim it is a future calendar."""
    response = await client.get("https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json", timeout=3)
    response.raise_for_status()
    upcoming = []
    for game in response.json().get("scoreboard", {}).get("games", []):
        try:
            start = datetime.fromisoformat(game["gameTimeUTC"].replace("Z", "+00:00")).astimezone(TZ)
            if start >= now and game.get("gameStatus") == 1:
                away, home = game.get("awayTeam", {}), game.get("homeTeam", {})
                upcoming.append((start, away, home))
        except (ValueError, KeyError, TypeError):
            continue
    if not upcoming:
        return None
    start, away, home = min(upcoming, key=lambda x: x[0])
    return f"【NBA 下一場】\\n台灣時間：{start:%Y/%m/%d %H:%M}\\n{away.get('teamCity', '')} {away.get('teamName', '客隊')} vs {home.get('teamCity', '')} {home.get('teamName', '主隊')}\\n來源：NBA 官方今日賽程"

async def get_events(client, league, date):
    """Prefer official public feeds for NBA and MLB."""
    if league in ("baseball/mlb", "basketball/nba"):
        try:
            events = await official_events(client, league, date)
            if events:
                return events
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log.warning("Official feed %s failed: %s", league, str(exc)[:120])
    url = f"https://site.api.espn.com/apis/site/v2/sports/{league}/scoreboard"
    try:
        response = await client.get(url, params={"dates": date}, timeout=2)
        response.raise_for_status()
        events = response.json().get("events", [])
        if events:
            return events
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("ESPN %s unavailable: %s", league, str(exc)[:140])
    # TheSportsDB is rate limited (429) on the free Render instance.
    # Do not hammer it or misreport a failed request as a day without games.
    raise httpx.HTTPStatusError("ESPN unavailable; no verified football fallback", request=httpx.Request("GET", url), response=httpx.Response(503, request=httpx.Request("GET", url)))

def event_line(event):
    competition = (event.get("competitions") or [{}])[0]
    teams = competition.get("competitors") or []
    names = [t.get("team", {}).get("displayName", "未知球隊") for t in teams]
    if len(names) < 2:
        return None
    status = event.get("status", {}).get("type", {})
    state = status.get("state", "pre")
    details = status.get("shortDetail") or status.get("detail") or ""
    if state in ("in", "post"):
        score = " - ".join(t.get("score", "-") for t in teams)
        outcome = f"比分 {score}｜{details}"
    else:
        outcome = details
    return f"{names[0]} vs {names[1]}\n{outcome}"

def record_rate(team):
    for rec in team.get("records") or []:
        parts = str(rec.get("summary", "")).split("-")
        if len(parts) >= 2:
            try:
                w, l = float(parts[0]), float(parts[1])
                t = float(parts[2]) if len(parts) > 2 else 0
                n = w + l + t
                if n >= 3:
                    return (w + t * 0.5) / n, int(n)
            except (ValueError, TypeError):
                continue
    return None

def estimate_match(teams):
    if len(teams) != 2:
        return "勝率估算：資料不足"
    a, b = record_rate(teams[0]), record_rate(teams[1])
    if a is None or b is None:
        return "勝率估算：戰績樣本不足"
    p = max(0.05, min(0.95, (a[0] + 1 - b[0]) / 2))
    n1 = teams[0].get("team", {}).get("shortDisplayName", "隊伍一")
    n2 = teams[1].get("team", {}).get("shortDisplayName", "隊伍二")
    return f"戰績基準估算：{n1} {p:.0%}｜{n2} {1-p:.0%}（樣本 {a[1]}/{b[1]} 場）"

async def mlb_next_game(now):
    """Show next scheduled MLB game and teams in Taiwan time."""
    async with httpx.AsyncClient() as client:
        r = await client.get("https://statsapi.mlb.com/api/v1/schedule",
            params={"sportId": 1, "startDate": now.strftime("%Y-%m-%d"),
                    "endDate": (now + timedelta(days=180)).strftime("%Y-%m-%d")}, timeout=4)
        r.raise_for_status()
        games = [g for d in r.json().get("dates", []) for g in d.get("games", [])]
        upcoming = []
        for game in games:
            try:
                start = datetime.fromisoformat(game["gameDate"].replace("Z", "+00:00")).astimezone(TZ)
                if start > now and game.get("status", {}).get("abstractGameState") == "Preview":
                    upcoming.append((start, game))
            except (ValueError, KeyError, TypeError):
                continue
        if not upcoming:
            return "【棒球】目前查不到已公布的下一場 MLB 賽程。"
        start, game = min(upcoming, key=lambda x: x[0])
        teams = game.get("teams", {})
        away = teams.get("away", {}).get("team", {}).get("name", "客隊")
        home = teams.get("home", {}).get("team", {}).get("name", "主隊")
        return f"【MLB 下一場比賽】\\n台灣時間：{start:%Y/%m/%d %H:%M}\\n{away} vs {home}\\n資料來源：MLB 官方賽程"

async def sports_report(sport, analysis=False, future=False):
    now = datetime.now(TZ)
    if sport == "足球":
        try:
            async with httpx.AsyncClient() as client:
                result = await openliga_next(client, now)
                if result:
                    return result
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            log.warning("OpenLigaDB unavailable: %s", str(exc)[:140])
    if sport == "籃球":
        try:
            async with httpx.AsyncClient() as client:
                result = await nba_next(client, now)
                if result:
                    return result
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            log.warning("NBA official next unavailable: %s", str(exc)[:140])
    if sport == "棒球":
        try:
            return await mlb_next_game(now)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log.warning("MLB next game unavailable: %s", str(exc)[:120])
            return "【棒球】MLB 官方賽程暫時無法連線，請稍後再試。"
    date = (now + timedelta(days=1)).strftime("%Y%m%d") if future else now.strftime("%Y%m%d")
    leagues = SPORTS.get(sport, [])
    lines = [f"【{sport}{'賽事分析' if analysis else '賽事'}】{now:%Y/%m/%d}（台灣時間）"]
    total = 0
    errors = 0
    import asyncio
    async with httpx.AsyncClient(headers={"User-Agent": "SportsAI-LineBot/0.4"}) as client:
        async def fetch(title, league, offset):
            day = (now + timedelta(days=offset)).strftime("%Y%m%d")
            try:
                events = await asyncio.wait_for(get_events(client, league, day), timeout=2.3)
                return title, offset, events, False
            except (httpx.HTTPError, ValueError, KeyError, TypeError, asyncio.TimeoutError) as exc:
                log.warning("Sports feed %s %s: %s", league, day, str(exc)[:100])
                return title, offset, [], True
        offset = 1 if future else 0
        found = await asyncio.gather(*(fetch(title, league, offset) for title, league in leagues))
        errors = sum(1 for _, _, _, failed in found if failed)
        if not any(events for _, _, events, _ in found):
            # Search two nearby dates in parallel; bounded to protect LINE reply time.
            offsets = (2, 7) if future else (1, 7)
            nearby = await asyncio.gather(*(fetch(title, league, d) for d in offsets for title, league in leagues))
            for d in offsets:
                candidate = [x for x in nearby if x[1] == d]
                if any(x[2] for x in candidate):
                    found = candidate
                    lines.append(f"今日無已收錄賽事，顯示近期賽程：{(now + timedelta(days=d)):%Y/%m/%d}")
                    break
        for title, day_offset, events, _ in found:
            if not events:
                continue
            lines.append(f"\\n〔{title}｜{(now + timedelta(days=day_offset)):%Y/%m/%d}〕")
            for event in events[:4]:
                summary = event_line(event)
                if not summary:
                    continue
                lines.append(summary)
                total += 1
                if analysis and sport != "足球":
                    competitors = (event.get("competitions") or [{}])[0].get("competitors") or []
                    lines.append(estimate_match(competitors))
    if not total:
        if errors:
            return f"【{sport}】賽事資料來源目前連線失敗，無法確認下一場比賽；不是沒有賽事。"
        return f"【{sport}】查詢日期內沒有已收錄比賽；下一場賽程尚待資料來源確認。"
    if analysis:
        lines.append("\n【數據說明】以上為公開戰績與簡易統計估算，並非訓練完成的 AI 模型預測，也不保證比賽結果。")
    else:
        lines.append("\n資料來源：ESPN 公開賽事資訊；可能有更新延遲。")
    result = "\n".join(lines)
    return result[:4800]

def offline_analysis(sport, matchup=None):
    """Offline hypothetical analysis; do not present invented data as real."""
    icons = {"足球": "⚽", "籃球": "🏀", "棒球": "⚾"}
    if not matchup:
        return f"{icons[sport]}【{sport}離線分析】\n請輸入：分析{sport} A隊 vs B隊\n例如：分析{sport} 曼城 vs 利物浦"
    import re
    parts = re.split(r"\s*(?:vs\.?|對戰|對|－|—)\s*", matchup, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2 or not all(x.strip() for x in parts):
        return f"格式：分析{sport} A隊 vs B隊\n例如：分析{sport} 曼城 vs 利物浦"
    home, away = [x.strip()[:35] for x in parts]
    if sport == "足球":
        odds = f"{home} 35%｜平局 30%｜{away} 35%"
        focus = "① 主場優勢與控球節奏\n② 兩隊攻防轉換與定位球\n③ 先發傷停與賽程密度"
        scenarios = f"若{home}能掌控中場，主隊勝面提高；若{away}反擊效率較高，客隊更有機會。"
        result = "暫無足夠依據判斷優勢方；建議關注先發與近期戰績。"
    elif sport == "籃球":
        odds = f"{home} 50%｜{away} 50%"
        focus = "① 進攻與防守效率\n② 三分球命中率與失誤\n③ 籃板、傷停與輪休"
        scenarios = f"若{home}控制籃板和失誤，勝面增加；若{away}外線發揮，局勢可能反轉。"
        result = "雙方暫以五五波看待，關鍵是先發陣容與比賽節奏。"
    else:
        odds = f"{home} 50%｜{away} 50%"
        focus = "① 先發投手與近期投球負荷\n② 牛棚可用性\n③ 打線、守備與球場條件"
        scenarios = f"若{home}先發投手壓制成功，主隊較有利；若{away}提早攻破先發，客隊機會提高。"
        result = "暫無足夠依據判斷優勢方；先發投手資訊最重要。"
    # Stable demo pick; no fabricated win probabilities or market odds.
    import hashlib
    pick = int(hashlib.sha256((sport + "|" + home + "|" + away).encode()).hexdigest()[:8], 16)
    winner = home if pick % 2 == 0 else away
    if sport == "足球":
        total = "小 2.5 球" if pick % 3 else "大 2.5 球"
    elif sport == "籃球":
        total = "小 220.5 分" if pick % 3 else "大 220.5 分"
    else:
        total = "小 8.5 分" if pick % 3 else "大 8.5 分"
    suggestion = f"獨贏：{winner}\\n大小分：{total}\\n（模擬選擇，非即時盤口）"
    return (f"{icons[sport]}【{sport}對戰分析】\n{home} VS {away}\n\n"
            f"📊 無資料基準：{odds}\n\n"
            f"🔍 分析重點：\n{focus}\n\n"
            f"📈 比賽情境：{scenarios}\n\n"
            f"🎯 分析結論：{result}\n\n"
            f"💡 推薦方向：\n{suggestion}\n\n"
            "⚠️ 離線情境推演；未取得即時數據，百分比是中立起始值，不是真實預測勝率。")

TEAM_ZH = {
    "Bayern Munich": "拜仁慕尼黑", "FC Bayern München": "拜仁慕尼黑", "Borussia Dortmund": "多特蒙德",
    "Bayer 04 Leverkusen": "勒沃庫森", "Bayer Leverkusen": "勒沃庫森", "RB Leipzig": "萊比錫",
    "Eintracht Frankfurt": "法蘭克福", "VfB Stuttgart": "斯圖加特", "VfL Wolfsburg": "沃夫斯堡",
    "SC Freiburg": "弗萊堡", "1. FC Union Berlin": "柏林聯合", "1. FSV Mainz 05": "美因茨",
    "Borussia Mönchengladbach": "門興格拉德巴赫", "Werder Bremen": "雲達不萊梅",
    "SV Werder Bremen": "雲達不萊梅", "FC Augsburg": "奧格斯堡", "TSG Hoffenheim": "霍芬海姆",
    "1. FC Heidenheim 1846": "海登海姆", "FC St. Pauli": "聖保利",
    "Hamburger SV": "漢堡", "1. FC Köln": "科隆",
    "Atlanta Hawks": "亞特蘭大老鷹", "Boston Celtics": "波士頓塞爾提克",
    "Brooklyn Nets": "布魯克林籃網", "Charlotte Hornets": "夏洛特黃蜂",
    "Chicago Bulls": "芝加哥公牛", "Cleveland Cavaliers": "克里夫蘭騎士",
    "Dallas Mavericks": "達拉斯獨行俠", "Denver Nuggets": "丹佛金塊",
    "Detroit Pistons": "底特律活塞", "Golden State Warriors": "金州勇士",
    "Houston Rockets": "休士頓火箭", "Indiana Pacers": "印第安納溜馬",
    "LA Clippers": "洛杉磯快艇", "Los Angeles Clippers": "洛杉磯快艇",
    "Los Angeles Lakers": "洛杉磯湖人", "Memphis Grizzlies": "曼菲斯灰熊",
    "Miami Heat": "邁阿密熱火", "Milwaukee Bucks": "密爾瓦基公鹿",
    "Minnesota Timberwolves": "明尼蘇達灰狼", "New Orleans Pelicans": "紐奧良鵜鶘",
    "New York Knicks": "紐約尼克", "Oklahoma City Thunder": "奧克拉荷馬雷霆",
    "Orlando Magic": "奧蘭多魔術", "Philadelphia 76ers": "費城七六人",
    "Phoenix Suns": "鳳凰城太陽", "Portland Trail Blazers": "波特蘭拓荒者",
    "Sacramento Kings": "沙加緬度國王", "San Antonio Spurs": "聖安東尼奧馬刺",
    "Toronto Raptors": "多倫多暴龍", "Utah Jazz": "猶他爵士",
    "Washington Wizards": "華盛頓巫師",
    "Arizona Diamondbacks": "亞利桑那響尾蛇", "Athletics": "運動家",
    "Atlanta Braves": "亞特蘭大勇士", "Baltimore Orioles": "巴爾的摩金鶯",
    "Boston Red Sox": "波士頓紅襪", "Chicago Cubs": "芝加哥小熊",
    "Chicago White Sox": "芝加哥白襪", "Cincinnati Reds": "辛辛那提紅人",
    "Cleveland Guardians": "克里夫蘭守護者", "Colorado Rockies": "科羅拉多洛磯",
    "Detroit Tigers": "底特律老虎", "Houston Astros": "休士頓太空人",
    "Kansas City Royals": "堪薩斯市皇家", "Los Angeles Angels": "洛杉磯天使",
    "Los Angeles Dodgers": "洛杉磯道奇", "Miami Marlins": "邁阿密馬林魚",
    "Milwaukee Brewers": "密爾瓦基釀酒人", "Minnesota Twins": "明尼蘇達雙城",
    "New York Mets": "紐約大都會", "New York Yankees": "紐約洋基",
    "Philadelphia Phillies": "費城費城人", "Pittsburgh Pirates": "匹茲堡海盜",
    "San Diego Padres": "聖地牙哥教士", "San Francisco Giants": "舊金山巨人",
    "Seattle Mariners": "西雅圖水手", "St. Louis Cardinals": "聖路易紅雀",
    "Tampa Bay Rays": "坦帕灣光芒", "Texas Rangers": "德州遊騎兵",
    "Toronto Blue Jays": "多倫多藍鳥", "Washington Nationals": "華盛頓國民",
}

def team_zh(name):
    return TEAM_ZH.get(name, name)

async def upcoming_match_cards(sport):
    """Return a tappable LINE carousel with real dated fixtures."""
    now = datetime.now(TZ)
    games = []
    async with httpx.AsyncClient(timeout=4) as client:
        try:
            if sport == "足球":
                for season in (now.year - 1, now.year):
                    try:
                        r = await client.get(f"https://api.openligadb.de/getmatchdata/bl1/{season}")
                        r.raise_for_status()
                        for g in r.json():
                            raw = g.get("matchDateTimeUTC")
                            if not raw:
                                continue
                            dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(TZ)
                            if dt >= now:
                                games.append((dt, g.get("team1", {}).get("teamName", ""), g.get("team2", {}).get("teamName", ""), "德甲"))
                    except (httpx.HTTPError, ValueError, TypeError, KeyError):
                        continue
            elif sport == "籃球":
                r = await client.get("https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json")
                r.raise_for_status()
                for g in r.json().get("scoreboard", {}).get("games", []):
                    dt = datetime.fromisoformat(g["gameTimeUTC"].replace("Z", "+00:00")).astimezone(TZ)
                    if dt >= now and g.get("gameStatus") == 1:
                        a, h = g.get("awayTeam", {}), g.get("homeTeam", {})
                        games.append((dt, f"{a.get('teamCity', '')} {a.get('teamName', '')}".strip(), f"{h.get('teamCity', '')} {h.get('teamName', '')}".strip(), "NBA"))
            elif sport == "棒球":
                r = await client.get("https://statsapi.mlb.com/api/v1/schedule", params={"sportId": 1, "startDate": now.strftime("%Y-%m-%d"), "endDate": (now + timedelta(days=90)).strftime("%Y-%m-%d")})
                r.raise_for_status()
                for d in r.json().get("dates", []):
                    for g in d.get("games", []):
                        dt = datetime.fromisoformat(g["gameDate"].replace("Z", "+00:00")).astimezone(TZ)
                        if dt >= now:
                            teams = g.get("teams", {})
                            games.append((dt, teams.get("away", {}).get("team", {}).get("name", ""), teams.get("home", {}).get("team", {}).get("name", ""), "MLB"))
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log.warning("Fixture cards unavailable for %s: %s", sport, str(exc)[:100])
    games = sorted((x for x in games if x[1] and x[2]), key=lambda x: x[0])[:8]
    if not games:
        return f"【{sport}】暫時無法取得近期真實賽程，請稍後點選「{sport}」重新整理。"
    bubbles = []
    palette = {"足球": ("#0B3B36", "#12A594", "⚽"), "籃球": ("#47230D", "#EA580C", "🏀"), "棒球": ("#1F2459", "#6366F1", "⚾")}
    dark, accent, emoji = palette[sport]
    for dt, away, home, league in games:
        away_zh, home_zh = team_zh(away), team_zh(home)
        command = f"分析{sport} {away_zh} vs {home_zh}"
        if len(command) > 300:
            continue
        bubbles.append({
            "type": "bubble", "size": "kilo",
            "header": {"type": "box", "layout": "vertical", "backgroundColor": dark,
                "paddingAll": "20px", "spacing": "sm", "contents": [
                    {"type": "text", "text": f"{emoji}  {league}  /  MATCH CENTER", "size": "xs", "weight": "bold", "color": "#D1FAE5"},
                    {"type": "text", "text": dt.strftime("%m/%d  %H:%M"), "size": "xl", "weight": "bold", "color": "#FFFFFF"},
                    {"type": "text", "text": "台灣時間  •  即將開賽", "size": "xs", "color": "#CBD5E1"}
                ]},
            "body": {"type": "box", "layout": "vertical", "backgroundColor": "#F4F7FB",
                "paddingAll": "20px", "spacing": "lg", "contents": [
                    {"type": "text", "text": "近期焦點對戰", "size": "xs", "weight": "bold", "color": accent},
                    {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                        {"type": "text", "text": away_zh, "weight": "bold", "size": "lg", "color": "#0F172A", "wrap": True},
                        {"type": "text", "text": "V S", "weight": "bold", "size": "md", "align": "center", "color": accent},
                        {"type": "text", "text": home_zh, "weight": "bold", "size": "lg", "color": "#0F172A", "wrap": True}
                    ]},
                    {"type": "separator", "color": "#CBD5E1"},
                    {"type": "text", "text": "點擊下方按鈕查看對戰情境分析", "size": "xs", "color": "#64748B", "wrap": True}
                ]},
            "footer": {"type": "box", "layout": "vertical", "backgroundColor": "#FFFFFF",
                "paddingAll": "16px", "contents": [
                    {"type": "button", "style": "primary", "color": accent, "height": "md",
                     "action": {"type": "message", "label": "📊 立即分析", "text": command}}
                ]}
        })
    return {"type": "flex", "altText": f"{sport}近期賽事｜中文隊名｜點選立即分析",
            "contents": {"type": "carousel", "contents": bubbles}}


async def reply_for(text):
    raw = text.strip()
    cmd = raw.replace(" ", "")
    if cmd in ("你好", "嗨", "開始", "選單", "幫助", "help"):
        return MENU
    if cmd in SPORTS:
        return await upcoming_match_cards(cmd)
    if cmd in ("今日賽事", "未來賽事"):
        results = []
        for sport in SPORTS:
            results.append(await sports_report(sport, future=(cmd == "未來賽事")))
        return "\n\n".join(results)[:4800]
    if cmd in ("賽事分析", "AI分析", "AI預測", "預測戰績"):
        return MENU
    if raw.startswith("分析"):
        rest = raw[2:].strip()
        for sport in SPORTS:
            if rest.startswith(sport):
                return offline_analysis(sport, rest[len(sport):].strip())
    if cmd in ("會員中心", "VIP"):
        return "【會員中心】\n目前賽事查詢與公開數據分析免費使用。\n尚未開放註冊、登入、付費或訂閱。"
    return MENU

@app.get("/")
@app.get("/health")
async def health():
    return {"status": "ok", "service": "line-sports-ai", "version": "0.6.3"}

@app.post("/webhooks/line")
async def webhook(request: Request, x_line_signature: str | None = Header(default=None)):
    secret = os.getenv("LINE_CHANNEL_SECRET", "")
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    raw = await request.body()
    if not secret or not token:
        raise HTTPException(503, "LINE credentials not configured")
    expected = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
    if not x_line_signature or not hmac.compare_digest(expected, x_line_signature):
        raise HTTPException(401, "Invalid signature")
    payload = await request.json()
    async with httpx.AsyncClient(timeout=9) as client:
        for event in payload.get("events", []):
            reply_token = event.get("replyToken")
            if not reply_token:
                continue
            if event.get("type") == "follow":
                message = MENU
            elif event.get("type") == "message" and event.get("message", {}).get("type") == "text":
                try:
                    import asyncio
                    message = await asyncio.wait_for(reply_for(event["message"].get("text", "")), timeout=7)
                except Exception as exc:
                    log.error("Sports reply timed out or failed: %s", str(exc)[:160])
                    message = "【體育AI分析系統】目前賽事資料暫時無法取得，機器人已恢復回覆。請稍後再試或輸入「選單」。"
            else:
                continue
            try:
                result = await client.post(
                    "https://api.line.me/v2/bot/message/reply",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"replyToken": reply_token, "messages": [
                        menu_card() if message == MENU else (message if isinstance(message, dict) else {"type": "text", "text": message, "quickReply": quick_replies()})
                    ]},
                )
                result.raise_for_status()
            except httpx.HTTPError as exc:
                log.error("LINE reply failed: %s", type(exc).__name__)
                raise HTTPException(502, "LINE reply failed") from exc
    return {"ok": True}
