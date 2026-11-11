import base64
import hashlib
import hmac
import os

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI(title="LINE Sports AI", version="0.1.0")
MENU = "歡迎使用體育AI分析系統！請輸入：選單、今日賽事、足球、籃球、棒球、AI預測、會員中心。"

def reply_for(text: str) -> str:
    cmd = text.strip().replace(" ", "")
    if cmd in ("你好", "嗨", "開始", "選單", "幫助", "help"):
        return MENU
    if cmd in ("今日賽事", "未來賽事", "足球", "籃球", "棒球"):
        return "賽事查詢功能正在建置，目前尚未接入即時資料，請稍後再試。"
    if cmd in ("AI預測", "預測戰績"):
        return "AI預測功能尚未上線，目前不提供未驗證的勝率或預測。"
    if cmd in ("會員中心", "VIP"):
        return "會員功能正在建置，目前尚未開通付費服務。"
    return MENU

@app.get("/")
@app.get("/health")
async def health():
    return {"status": "ok", "service": "line-sports-ai"}

@app.post("/webhooks/line")
async def webhook(request: Request, x_line_signature: str | None = Header(default=None)):
    secret = os.getenv("LINE_CHANNEL_SECRET", "")
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    raw = await request.body()
    if not secret:
        raise HTTPException(503, "LINE channel secret not configured")
    expected = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
    if not x_line_signature or not hmac.compare_digest(expected, x_line_signature):
        raise HTTPException(401, "Invalid signature")
    if not token:
        raise HTTPException(503, "LINE channel access token not configured")
    payload = await request.json()
    for event in payload.get("events", []):
        reply_token = event.get("replyToken")
        if not reply_token:
            continue
        if event.get("type") == "follow":
            message = MENU
        elif event.get("type") == "message" and event.get("message", {}).get("type") == "text":
            message = reply_for(event["message"].get("text", ""))
        else:
            continue
        async with httpx.AsyncClient(timeout=8) as client:
            result = await client.post(
                "https://api.line.me/v2/bot/message/reply",
                headers={"Authorization": f"Bearer {token}"},
                json={"replyToken": reply_token, "messages": [{"type": "text", "text": message}]},
            )
            result.raise_for_status()
    return {"ok": True}
