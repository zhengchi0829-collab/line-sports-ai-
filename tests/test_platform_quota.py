"""Zero-cost The Odds API quota check uses installed key without leaking it."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app.main import check_platform_quota_once


class QuotaTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_reports_unknown_not_zero(self):
        with patch.dict("os.environ", {"ODDS_API_KEY": "", "THE_ODDS_API_KEY": ""}):
            with self.assertLogs("sports_bot", level="WARNING") as records:
                result = await check_platform_quota_once(SimpleNamespace())
        self.assertEqual(result["configured"], False)
        self.assertIsNone(result["remaining"])
        self.assertIn("remaining=unknown", records.output[0])

    async def test_account_remaining_is_read_from_response_headers(self):
        seen = []
        def mock(request):
            seen.append(request)
            return httpx.Response(200, json=[{"key": "soccer_epl"}], headers={
                "x-requests-remaining": "475",
                "x-requests-used": "25",
                "x-requests-last": "0",
            })
        async with httpx.AsyncClient(transport=httpx.MockTransport(mock)) as client:
            app = SimpleNamespace(state=SimpleNamespace(
                controller=SimpleNamespace(odds=SimpleNamespace(client=client))))
            with patch.dict("os.environ", {"ODDS_API_KEY": "secret-do-not-log"}):
                with self.assertLogs("sports_bot", level="INFO") as records:
                    result = await check_platform_quota_once(app)
        self.assertEqual(result["remaining"], 475)
        self.assertEqual(result["used"], 25)
        self.assertEqual(result["last"], 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].url.path, "/v4/sports/")
        self.assertEqual(seen[0].headers["x-api-key"], "secret-do-not-log")
        self.assertNotIn("secret-do-not-log", " ".join(records.output))

    async def test_invalid_key_does_not_invent_balance(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(401, json={"message": "unauthorized"}))) as client:
            app = SimpleNamespace(state=SimpleNamespace(
                controller=SimpleNamespace(odds=SimpleNamespace(client=client))))
            with patch.dict("os.environ", {"ODDS_API_KEY": "redacted"}):
                result = await check_platform_quota_once(app)
        self.assertEqual(result["http_status"], 401)
        self.assertIsNone(result["remaining"])


if __name__ == "__main__":
    unittest.main()
