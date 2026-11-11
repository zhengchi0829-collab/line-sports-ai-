import base64
import hashlib
import hmac
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI(title="體育AI分析系統", version="0.3.3")
log = logging.getLogger("sports_bot")
TZ = ZoneInfo("Asia/Taipei")
MENU = ("歡迎使用【體育AI分析系統】！\n\n"
        "請點選下方快捷按鈕，或輸入：\n"
        "今日賽事｜足球｜籃球｜棒球｜賽事分析｜會員中心")
BUTTONS = ["今日賽事", "未來賽事", "足球", "籃球", "棒球", "賽事分析", "會員中心", "選單"]
SPORTS = {
    "足球": [("英超", "soccer/eng.1"), ("西甲", "soccer/esp.1"), ("歐冠", "soccer/uefa.champions")],
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

async def get_events(client, league, date):
    """Primary ESPN feed, with a separate free public sports-data fallback."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/{league}/scoreboard"
    try:
        response = await client.get(url, params={"dates": date}, timeout=2)
        response.raise_for_status()
        events = response.json().get("events", [])
        if events:
            return events
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("ESPN %s unavailable: %s", league, str(exc)[:140])
    # TheSportsDB free endpoint uses YYYY-MM-DD and returns a different schema.
    sport = "Soccer" if league.startswith("soccer/") else ("Basketball" if "basketball/" in league else "Baseball")
    day = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    response = await client.get(
        "https://www.thesportsdb.com/api/v1/json/123/eventsday.php",
        params={"d": day, "s": sport}, timeout=3)
    response.raise_for_status()
    raw = response.json().get("events") or []
    keyword = {"soccer/eng.1": "English Premier League",
               "soccer/esp.1": "Spanish La Liga",
               "soccer/uefa.champions": "Champions League",
               "basketball/nba": "NBA", "baseball/mlb": "MLB"}.get(league, "")
    matches = [e for e in raw if keyword.lower() in (e.get("strLeague") or "").lower()]
    events = []
    for e in matches:
        home, away = e.get("strHomeTeam"), e.get("strAwayTeam")
        if not home or not away:
            continue
        played = e.get("intHomeScore") is not None and e.get("intAwayScore") is not None
        events.append({"competitions": [{"competitors": [
            {"team": {"displayName": home, "shortDisplayName": home}, "score": str(e.get("intHomeScore") or "-")},
            {"team": {"displayName": away, "shortDisplayName": away}, "score": str(e.get("intAwayScore") or "-")}
        ]}], "status": {"type": {"state": "post" if played else "pre",
              "shortDetail": e.get("strStatus") or e.get("strTime") or "賽程待確認"}}})
    return events

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

async def sports_report(sport, analysis=False, future=False):
    now = datetime.now(TZ)
    date = (now + timedelta(days=1)).strftime("%Y%m%d") if future else now.strftime("%Y%m%d")
    leagues = SPORTS.get(sport, [])
    lines = [f"【{sport}{'賽事分析' if analysis else '賽事'}】{now:%Y/%m/%d}（台灣時間）"]
    total = 0
    errors = 0
    async with httpx.AsyncClient(headers={"User-Agent": "SportsAI-LineBot/0.2"}) as client:
        for title, league in leagues:
            try:
                events = await get_events(client, league, date)
                if not events:
                    continue
                lines.append(f"\n〔{title}〕")
                for event in events[:4]:
                    summary = event_line(event)
                    if summary:
                        lines.append(summary)
                        total += 1
                        if analysis:
                            competition = (event.get("competitions") or [{}])[0]
                            competitors = competition.get("competitors") or []
                            records = []
                            for team in competitors:
                                rec = team.get("records") or []
                                if rec:
                                    records.append(f"{team.get('team', {}).get('shortDisplayName', '球隊')}：{rec[0].get('summary', '資料不足')}")
                            if records:
                                lines.append("公開戰績：" + "；".join(records))
                            if sport != "足球":
                                lines.append(estimate_match(competitors))
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                errors += 1
                log.warning("Sports data unavailable for %s: %s", league, str(exc)[:160])
    if not total:
        if errors:
            return f"【{sport}】目前無法取得即時資料，請稍後再試。"
        return f"【{sport}】今天查無已收錄賽事。可稍後再查詢。"
    if analysis:
        lines.append("\n【數據說明】以上為公開戰績與簡易統計估算，並非訓練完成的 AI 模型預測，也不保證比賽結果。")
    else:
        lines.append("\n資料來源：ESPN 公開賽事資訊；可能有更新延遲。")
    result = "\n".join(lines)
    return result[:4800]

async def reply_for(text):
    cmd = text.strip().replace(" ", "")
    if cmd in ("你好", "嗨", "開始", "選單", "幫助", "help"):
        return MENU
    if cmd in SPORTS:
        return await sports_report(cmd)
    if cmd in ("今日賽事", "未來賽事"):
        results = []
        for sport in SPORTS:
            results.append(await sports_report(sport, future=(cmd == "未來賽事")))
        return "\n\n".join(results)[:4800]
    if cmd in ("賽事分析", "AI分析", "AI預測", "預測戰績"):
        return ("【賽事數據分析】\n請輸入「分析足球」、「分析籃球」或「分析棒球」，"
                "查看今天已收錄賽事及公開戰績。\n目前提供簡易統計估算，尚非 AI 模型預測。")
    if cmd.startswith("分析") and cmd[2:] in SPORTS:
        return await sports_report(cmd[2:], analysis=True)
    if cmd in ("會員中心", "VIP"):
        return "【會員中心】\n目前賽事查詢與公開數據分析免費使用。\n尚未開放註冊、登入、付費或訂閱。"
    return MENU

@app.get("/")
@app.get("/health")
async def health():
    return {"status": "ok", "service": "line-sports-ai", "version": "0.3.3"}

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
                        menu_card() if message == MENU else {"type": "text", "text": message, "quickReply": quick_replies()}
                    ]},
                )
                result.raise_for_status()
            except httpx.HTTPError as exc:
                log.error("LINE reply failed: %s", type(exc).__name__)
                raise HTTPException(502, "LINE reply failed") from exc
    return {"ok": True}
