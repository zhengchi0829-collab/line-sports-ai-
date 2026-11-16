"""Read-only integration gateway for the documented tenant game catalog.

Strictly GET /platform/games; no game launch, wager, balance, member, callback,
or transfer API is implemented. Credentials remain in Render environment only.
No odds data is inferred from the game catalog.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import datetime
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("sports_bot")
TZ = ZoneInfo("Asia/Taipei")
DEFAULT_BASE = "https://backend.bofu58.com/records-api/platform"
ALLOWED_HOST = "backend.bofu58.com"
MAX_PAGES = 3
PER_PAGE = 200


def signature(secret: str, timestamp: str, nonce: str, method: str,
              request_uri: str, body: str = "") -> str:
    """Exact 5-line HMAC-SHA256 signature in the provided specification."""
    canonical = "\n".join((timestamp, nonce, method.upper(), request_uri, body))
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def safe_base(value: str) -> str | None:
    """Only the user's configured HTTPS tenant hostname; reject redirects."""
    raw = (value or DEFAULT_BASE).strip().rstrip("/")
    try:
        u = urlsplit(raw)
    except ValueError:
        return None
    if (u.scheme != "https" or u.hostname != ALLOWED_HOST
            or u.port not in (None, 443) or u.username or u.password
            or u.query or u.fragment or not u.path.endswith("/records-api/platform")):
        return None
    return raw


def classify_error(http_status: int, payload) -> str:
    """Do not leak arbitrary provider responses (may include private data)."""
    if http_status == 401:
        msg = str(payload.get("message", "") if isinstance(payload, dict) else "").lower()
        if "whitelist" in msg:
            return "ip_not_whitelisted"
        if "signature" in msg:
            return "invalid_signature"
        if "disabled" in msg:
            return "tenant_disabled"
        return "unauthorized"
    if http_status == 403:
        return "tenant_not_permitted"
    if http_status == 404:
        return "api_path_not_found"
    if http_status == 429:
        return "rate_limited"
    if http_status >= 500:
        return "upstream_unavailable"
    return "http_" + str(http_status)


SPORTS_TERMS = (
    "體育", "体育", "運彩", "足彩", "足彩彩票", "足球", "籃球", "篮球",
    "棒球", "網球", "网球", "排球", "冰球", "羽球", "羽毛球",
    "高爾夫", "高尔夫", "賽馬", "赛马", "sportsbook", "sports",
    "football", "soccer", "basketball", "baseball", "tennis",
    "volleyball", "hockey", "golf", "cricket",
)
EXCLUDED_TERMS = (
    "atg", "百家", "百家乐", "baccarat", "真人", "casino", "livecasino",
    "slots", "slot", "老虎機", "老虎机", "電子", "电子", "捕魚", "捕鱼",
    "棋牌", "輪盤", "轮盘", "roulette", "骰寶", "骰宝", "sicbo",
    "麻將", "麻将", "blackjack", "二十一點", "二十一点", "鬥雞", "斗鸡",
    "電競", "电竞", "esports", "e-sports",
)


def sports_only_game(row: object) -> bool:
    """Allow only recognizable physical sports; deny casino/ATG first.

    Provider codes are not trusted as proof that a particular *game* is a
    sports offering. A mixed supplier may provide both casino and sports.
    """
    if not isinstance(row, dict):
        return False
    raw = " ".join(str(row.get(key) or "")
                   for key in ("vendor_code", "name", "category"))
    token = raw.casefold().replace(" ", "").replace("-", "")
    if not token or any(term in token for term in EXCLUDED_TERMS):
        return False
    if row.get("is_active") is False or row.get("in_maintenance") is True:
        return False
    searchable = " ".join(str(row.get(key) or "")
                          for key in ("name", "category")).casefold()
    return any(term in searchable for term in SPORTS_TERMS)


def summarize(rows, reported_total: int | None, more_pages: bool):
    """Keep ONLY sports-related game titles and provider labels in memory.

    We must scan the tenant's mixed catalog, but Baccarat/ATG/slots/casino
    records are immediately discarded and cannot enter LINE output.
    """
    vendors, categories, entries = set(), {}, []
    seen = set()
    for row in rows:
        if not sports_only_game(row):
            continue
        vendor = " ".join(str(row.get("vendor_code") or "").split())[:25]
        category = " ".join(str(row.get("category") or "未分類").split())[:40]
        name = " ".join(str(row.get("name") or "").split())[:65]
        if not name:
            continue
        identity = (vendor, name, category)
        if identity in seen:
            continue
        seen.add(identity)
        if vendor:
            vendors.add(vendor)
        categories[category] = categories.get(category, 0) + 1
        # A bounded sports-only subset; no private user/transaction records.
        if len(entries) < 40:
            entries.append({"vendor": vendor, "name": name,
                            "category": category})
    return {
        "status": "ok",
        "records_read": len(entries),
        "catalog_scanned": len(rows),
        "catalog_truncated": bool(more_pages),
        "sports_count": len(seen),
        "vendor_count": len(vendors),
        "vendor_codes": sorted(vendors)[:30],
        "categories": dict(sorted(categories.items(),
                                  key=lambda p: (-p[1], p[0]))[:20]),
        "sports_entries": entries,
        "sports_odds_api": False,
        "scope": "sports_games_read_only",
    }


class IntegrationCatalog:
    """On-demand read-only check with 60-second cache and capped pagination."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.lock = asyncio.Lock()
        self.last_check = 0.0
        self.summary = {"status": "not_checked", "scope": "sports_games_read_only",
                        "sports_odds_api": False}

    def configured(self) -> bool:
        return bool(os.getenv("GAME_INTEGRATION_TOKEN", "").strip() and
                    os.getenv("GAME_INTEGRATION_SECRET", "").strip() and
                    os.getenv("GAME_INTEGRATION_BASE_URL", "").strip())

    async def check(self, now: datetime | None = None) -> dict:
        """One check may fetch up to 3 read-only pages; no other HTTP methods."""
        async with self.lock:
            if self.last_check and time.monotonic() - self.last_check < 60:
                return self.summary
            self.last_check = time.monotonic()
            now = now or datetime.now(TZ)
            checked = now.astimezone(TZ).isoformat()
            state = {"status": "not_configured", "scope": "sports_games_read_only",
                     "sports_odds_api": False, "checked_at": checked}
            if not self.configured():
                self.summary = state
                log.info("integration_catalog status=not_configured")
                return state
            base = safe_base(os.getenv("GAME_INTEGRATION_BASE_URL", ""))
            if base is None:
                state["status"] = "invalid_base_url"
                self.summary = state
                log.warning("integration_catalog status=invalid_base_url")
                return state

            token = os.environ["GAME_INTEGRATION_TOKEN"].strip()
            secret = os.environ["GAME_INTEGRATION_SECRET"].strip()
            all_rows = []
            total = None
            partial = False
            for page in range(1, MAX_PAGES + 1):
                query = urlencode({"per_page": PER_PAGE, "page": page})
                url = base + "/games?" + query
                path = urlsplit(url).path + "?" + query
                ts = str(int(now.timestamp()))
                nonce = secrets.token_hex(16)
                headers = {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-API-Token": token,
                    "X-Timestamp": ts,
                    "X-Nonce": nonce,
                    "X-Signature": signature(secret, ts, nonce, "GET", path),
                }
                try:
                    response = await self.client.get(
                        url, headers=headers, timeout=15,
                        follow_redirects=False)
                except (httpx.HTTPError, TimeoutError) as exc:
                    state["status"] = "network_" + type(exc).__name__
                    break
                if response.status_code != 200:
                    payload = {}
                    try:
                        payload = response.json()
                    except (ValueError, TypeError):
                        pass
                    state["status"] = classify_error(response.status_code, payload)
                    state["http_status"] = response.status_code
                    break
                if len(response.content) > 2_000_000:
                    state["status"] = "response_too_large"
                    break
                try:
                    payload = response.json()
                except (ValueError, TypeError):
                    state["status"] = "invalid_json"
                    break
                if not isinstance(payload, dict) or payload.get("code") != 200:
                    state["status"] = "api_code_" + str(payload.get("code", "unknown"))[:5] \
                        if isinstance(payload, dict) else "invalid_payload"
                    break
                data = payload.get("data")
                if not isinstance(data, dict):
                    state["status"] = "invalid_payload"
                    break
                games = data.get("games")
                if not isinstance(games, list):
                    state["status"] = ("selection_requires_member"
                                       if data.get("selection_required")
                                       else "unsupported_games_format")
                    break
                all_rows.extend(games)
                pagination = data.get("pagination") or {}
                if isinstance(pagination, dict):
                    try:
                        total = int(pagination["total"])
                    except (ValueError, TypeError, KeyError):
                        pass
                    try:
                        pages = int(pagination["total_pages"])
                    except (ValueError, TypeError, KeyError):
                        pages = page
                    if pages <= page:
                        break
                elif len(games) < PER_PAGE:
                    break
                if page == MAX_PAGES:
                    partial = True

            if state["status"] == "not_configured":
                state.update(summarize(all_rows, total, partial))
                state["checked_at"] = checked
            else:
                state["records_read"] = len(all_rows)
            self.summary = state
            log.info("integration_catalog status=%s http=%s sports_records=%s sports_vendors=%s scan_partial=%s",
                     state["status"], state.get("http_status", 200),
                     state.get("records_read", 0),
                     state.get("vendor_count", 0), state.get("partial", False))
            if state["status"] == "ok":
                log.info("integration_sports_only vendors=%s games=%s",
                         ",".join(state.get("vendor_codes", [])),
                         " | ".join(
                             f"{row['vendor']}:{row['name']}"
                             for row in state.get("sports_entries", [])[:20])))
            return state

    def line_summary(self) -> str:
        """Display only verified category/name matches for sports games.

        A game listing is not an odds API or evidence of sportsbook prices.
        """
        info = self.summary
        status = info.get("status", "not_checked")
        if status != "ok":
            notes = {
                "not_checked": "尚未檢查",
                "not_configured": "尚未設定集成平台憑證",
                "ip_not_whitelisted": "集成平台尚未放行 Render 出口 IP",
                "invalid_signature": "簽章驗證失敗",
                "unauthorized": "API Token 無效或租戶沒有權限",
                "tenant_disabled": "租戶已停用",
                "network_ConnectError": "無法連線到集成平台",
                "selection_requires_member": "需會員帳號才能取得個人化選單（未讀取會員資料）",
            }
            return ("🏟️ 體育集成｜唯讀檢查\n"
                    "狀態：" + notes.get(status, status) +
                    "\n原本台彩與娛樂平台賠率功能未變更。")
        entries = info.get("sports_entries", [])
        if not entries:
            return ("🏟️ 體育集成｜唯讀清單\n"
                    "目前在已授權的遊戲清單中，沒有找到可明確認定的體育遊戲。\n"
                    "百家樂、ATG、電子及其他非體育項目均不顯示。\n"
                    "這份遊戲清單不是台彩體育賠率 API；現有賠率功能不變。")
        lines = [
            "🏟️ 體育集成｜唯讀清單",
            f"體育類項目：{info.get('sports_count', len(entries))} 筆",
            f"體育類供應商：{info.get('vendor_count', 0)} 家",
        ]
        for entry in entries[:18]:
            lines.append(f"• {entry['name']}（{entry['vendor']}）")
        if info.get("sports_count", len(entries)) > 18:
            lines.append("其他體育項目已省略")
        lines.extend((
            "已排除百家樂、ATG、電子及其他非體育遊戲。",
            "注意：僅是體育遊戲清單，不是台彩體育賠率 API。",
            "原本的體育賽事分析與賠率功能維持不變。",
        ))
        return "\n".join(lines)[:4500]
