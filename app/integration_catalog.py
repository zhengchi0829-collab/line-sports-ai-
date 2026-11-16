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


def summarize(rows, reported_total: int | None, more_pages: bool):
    """Whitelist only public game-catalog fields. Never relay raw JSON."""
    vendors, categories, examples = set(), {}, []
    sport_candidates = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        vendor = str(row.get("vendor_code") or "").strip()[:25]
        category = str(row.get("category") or "未分類").strip()[:40]
        name = str(row.get("name") or "").strip()[:65]
        if vendor:
            vendors.add(vendor)
        categories[category] = categories.get(category, 0) + 1
        if len(examples) < 8 and name:
            examples.append({"vendor": vendor, "name": name,
                             "category": category})
        joined = (vendor + " " + category + " " + name).casefold()
        if any(x in joined for x in ("sport", "體育", "運彩", "lottery", "彩票")):
            if len(sport_candidates) < 12:
                sport_candidates.append({"vendor": vendor,
                                         "name": name, "category": category})
    return {
        "status": "ok",
        "records_read": len(rows),
        "reported_total": reported_total if isinstance(reported_total, int) else None,
        "partial": bool(more_pages),
        "vendor_count": len(vendors),
        "vendor_codes": sorted(vendors)[:30],
        "categories": dict(sorted(categories.items(),
                                  key=lambda p: (-p[1], p[0]))[:20]),
        "examples": examples,
        "sport_candidates": sport_candidates,
        "sports_odds_api": False,
        "scope": "game_catalog_only",
    }


class IntegrationCatalog:
    """On-demand read-only check with 60-second cache and capped pagination."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.lock = asyncio.Lock()
        self.last_check = 0.0
        self.summary = {"status": "not_checked", "scope": "game_catalog_only",
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
            state = {"status": "not_configured", "scope": "game_catalog_only",
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
            log.info("integration_catalog status=%s http=%s records=%s vendors=%s partial=%s",
                     state["status"], state.get("http_status", 200),
                     state.get("records_read", 0),
                     state.get("vendor_count", 0), state.get("partial", False))
            return state

    def line_summary(self) -> str:
        """No credential or raw customer fields are included in LINE messages."""
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
                "selection_requires_member": "需要會員帳號的個人化遊戲選單（未取會員資料）",
            }
            return "🔎 集成平台唯讀檢查\n狀態：" + notes.get(status, status) + "\n現有體育賠率不受影響。"
        codes = ", ".join(info.get("vendor_codes", [])[:16]) or "無"
        category_lines = "、".join(
            f"{name} {count}" for name, count in
            list(info.get("categories", {}).items())[:8]) or "無"
        sports = ("有體育類型關鍵字，尚待確認是不是賠率供應商"
                  if info.get("sport_candidates") else "未發現明確體育類型")
        return (
            "🔎 集成平台唯讀檢查\n"
            f"遊戲清單：{info.get('records_read', 0)} 筆\n"
            f"遊戲商：{info.get('vendor_count', 0)} 家（{codes}）\n"
            f"分類：{category_lines}\n"
            f"體育線索：{sports}\n"
            "注意：這不是台彩體育賠率 API；既有賠率功能未變更。"
        )
