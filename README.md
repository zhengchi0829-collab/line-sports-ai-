# 體育AI分析系統 — LINE Bot

免費 Render 測試版：支援 LINE Webhook 驗簽、文字回覆、基本選單及健康檢查。足球、籃球、棒球即時資料、AI 預測與會員資料庫尚未接入，不能宣稱已上線。

Build: `pip install -r requirements.txt`

Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

Webhook: `https://<render-host>/webhooks/line`

請在 Render Environment 設定 `LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN`，不要提交到 GitHub。
