"""Read-only tenant catalog regression tests. All network calls are mocked."""
import json
import os
import unittest
from datetime import datetime
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx

from app.integration_catalog import (IntegrationCatalog, safe_base, signature,
                                     classify_error, summarize, sports_only_game, TZ)


NOW = datetime(2026, 11, 16, 21, 32, tzinfo=TZ)
ENV = {
    "GAME_INTEGRATION_BASE_URL": "https://backend.bofu58.com/records-api/platform",
    "GAME_INTEGRATION_TOKEN": "test-tenant-token",
    "GAME_INTEGRATION_SECRET": "test-secret-not-real",
}


class SignatureTests(unittest.TestCase):
    def test_official_get_signature_test_vector(self):
        self.assertEqual(signature(
            "test_api_secret", "1794000000",
            "abcdef0123456789abcdef0123456789",
            "GET", "/records-api/platform/games?per_page=20"),
            "6e24206dcbae0f14f514121dfad8b31b9e0dc9d1bd63fb64086f2e209aadbc72")

    def test_base_is_pinned_to_expected_https_origin(self):
        self.assertEqual(safe_base(ENV["GAME_INTEGRATION_BASE_URL"]),
                         ENV["GAME_INTEGRATION_BASE_URL"])
        for bad in ("http://backend.bofu58.com/records-api/platform",
                    "https://evil.example/records-api/platform",
                    "https://backend.bofu58.com.evil.example/records-api/platform",
                    "https://backend.bofu58.com/records-api/platform?redirect=1"):
            self.assertIsNone(safe_base(bad))

    def test_errors_do_not_relay_sensitive_provider_response(self):
        self.assertEqual(classify_error(401, {"message": "IP not in whitelist"}),
                         "ip_not_whitelisted")
        self.assertEqual(classify_error(401, {"message": "Invalid signature"}),
                         "invalid_signature")


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_game_catalog_signature_and_no_mutating_calls(self):
        seen = []
        def handler(req):
            seen.append(req)
            self.assertEqual(req.method, "GET")
            self.assertEqual(req.url.path, "/records-api/platform/games")
            self.assertEqual(req.url.params.get("per_page"), "200")
            self.assertEqual(req.headers.get("X-API-Token"), "test-tenant-token")
            ts = req.headers["X-Timestamp"]
            nonce = req.headers["X-Nonce"]
            uri = req.url.raw_path.decode("ascii")
            self.assertEqual(req.headers["X-Signature"],
                             signature(ENV["GAME_INTEGRATION_SECRET"],
                                       ts, nonce, "GET", uri))
            return httpx.Response(200, json={
                "code": 200, "data": {
                    "games": [
                        {"vendor_code": "BL", "name": "遊戲A",
                         "category": "電子", "is_active": True},
                        {"vendor_code": "SP", "name": "Sports Demo",
                         "category": "體育", "is_active": True},
                    ],
                    "pagination": {"total": 2, "total_pages": 1},
                }
            })
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.dict(os.environ, ENV):
                catalog = IntegrationCatalog(client)
                status = await catalog.check(NOW)
                self.assertEqual(status["status"], "ok")
                self.assertEqual(status["vendor_count"], 1)
                self.assertEqual(status["records_read"], 1)
                self.assertEqual(status["sports_count"], 1)
                self.assertEqual(status["catalog_scanned"], 2)
                self.assertEqual([x["name"] for x in status["sports_entries"]],
                                 ["Sports Demo"])
                self.assertFalse(status["sports_odds_api"])
                self.assertIn("不是台彩體育賠率 API", catalog.line_summary())
                self.assertNotIn("遊戲A", catalog.line_summary())
                self.assertNotIn("電子", str(status["sports_entries"]))
                await catalog.check(NOW)
                self.assertEqual(len(seen), 1)

    async def test_whitelist_denial_is_sanitized(self):
        def handler(req):
            return httpx.Response(401, json={
                "code": 401, "message": "IP not in whitelist",
                "token": "SHOULD_NEVER_RELAY",
            })
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.dict(os.environ, ENV):
                catalog = IntegrationCatalog(client)
                state = await catalog.check(NOW)
                self.assertEqual(state["status"], "ip_not_whitelisted")
                self.assertNotIn("SHOULD_NEVER_RELAY", json.dumps(state))
                self.assertIn("出口 IP", catalog.line_summary())

    async def test_o8_selection_is_not_faked(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={
                    "code": 200, "data": {
                        "selection_required": True, "games": "not-an-array"}
                }))) as client:
            with patch.dict(os.environ, ENV):
                state = await IntegrationCatalog(client).check(NOW)
                self.assertEqual(state["status"], "selection_requires_member")

    async def test_no_keys_does_not_send_network_request(self):
        def handler(req):
            self.fail("No network call is allowed when secrets are absent")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.dict(os.environ, {k: "" for k in ENV}):
                state = await IntegrationCatalog(client).check(NOW)
                self.assertEqual(state["status"], "not_configured")


if __name__ == "__main__":
    unittest.main()


class SportsOnlyFilteringTests(unittest.TestCase):
    def test_keep_football_basketball_baseball_and_real_sportsbooks(self):
        for name in ("足球體育", "NBA 籃球", "MLB 棒球",
                     "Super Sportsbook", "台灣運彩", "European football"):
            self.assertTrue(sports_only_game({
                "name": name, "category": "未分類", "vendor_code": "TEST"
            }), name)

    def test_exclude_all_casino_atg_and_esports(self):
        for name in ("ATG電子", "真人百家樂", "Baccarat Table",
                     "ATG Sportsbook", "百家樂體育場",
                     "Slots 体育", "電競Esports", "捕魚", "輪盤"):
            self.assertFalse(sports_only_game({
                "name": name, "category": "未分類", "vendor_code": "TEST"
            }), name)

    def test_exclude_casino_category_even_if_title_says_sports(self):
        self.assertFalse(sports_only_game({
            "name": "SPORTS CLUB", "category": "真人娛樂",
            "vendor_code": "GCTEST"}))
        self.assertFalse(sports_only_game({
            "name": "Football", "category": "未分類",
            "vendor_code": "ATGTEST"}))

    def test_no_non_sport_vendor_or_example_in_summary(self):
        original = [
            {"vendor_code": "ATGTEST", "name": "ATG slots", "category": "電子"},
            {"vendor_code": "BACCAT", "name": "真人百家樂", "category": "真人"},
            {"vendor_code": "TQTEST", "name": "體育賽事", "category": "未分類"},
            {"vendor_code": "FBTEST", "name": "Football", "category": "體育"},
            {"vendor_code": "FBTEST", "name": "Slot Machine", "category": "電子"},
        ]
        record = summarize(original, len(original), False)
        self.assertEqual(record["sports_count"], 2)
        self.assertEqual(record["records_read"], 2)
        self.assertEqual(record["vendor_codes"], ["FBTEST", "TQTEST"])
        as_text = json.dumps(record, ensure_ascii=False)
        self.assertNotIn("ATGTEST", as_text)
        self.assertNotIn("BACCAT", as_text)
        self.assertNotIn("真人百家", as_text)
        self.assertNotIn("Slot Machine", as_text)

    def test_unverified_casino_returns_empty_sports_result(self):
        data = summarize([{"vendor_code": "ATGTEST", "name": "百家樂體育",
                           "category": "真人"}], 1, False)
        self.assertEqual(data["sports_entries"], [])
        self.assertEqual(data["vendor_codes"], [])
