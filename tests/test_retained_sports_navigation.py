"""End-to-end routing checks for retained LINE sports/odds features."""
import json
import unittest
from datetime import datetime
from unittest.mock import patch

import httpx

from app import legacy
from app.reliability import Controller, odds_category_menu


class RetainedSportsNavigationTests(unittest.TestCase):
    def test_fixed_rich_menu_has_sports_and_quotations_button(self):
        entries = legacy.RICH_MENU_ACTIONS
        self.assertEqual(len(entries), 6)
        self.assertEqual([action for _, action, _ in entries],
                         ["足球", "籃球", "棒球",
                          "今日賽事", "未來賽事", "檢視賠率"])
        all_labels = " ".join(x for entry in entries for x in entry)
        for forbidden in ("ATG", "試玩", "百家樂", "SLOT", "真人"):
            self.assertNotIn(forbidden, all_labels)


    def test_platform_menu_displays_only_actual_saved_quotes_as_buttons(self):
        from app.reliability import odds_league_menu
        from datetime import datetime
        from app.reliability import TZ
        today = datetime.now(TZ).date().isoformat()
        saved = {
            "day": today, "state": "ready",
            "captured_at": today + "T00:30:00+08:00",
            "leagues": {"NBA": [{"home": "Home", "away": "Away"}]},
            "deferred_leagues": ["NCAA"], "not_offered": ["NBL", "TPBL"],
        }
        panel = odds_league_menu("娛樂平台賠率", "籃球",
                                 platform_daily=saved)
        body = panel["contents"]["body"]["contents"]
        actions = [row.get("action", {}).get("text") for row in body]
        self.assertIn("賠率娛樂平台NBA", actions)
        for not_available in ("NCAA", "NBL", "TPBL"):
            self.assertNotIn("賠率娛樂平台" + not_available, actions)
        info = json.dumps(body, ensure_ascii=False)
        self.assertIn("NBA｜1 場", info)
        self.assertIn("NCAA｜免費額度未排程", info)
        self.assertIn("NBL｜來源未提供", info)
        self.assertIn("TPBL｜來源未提供", info)

    def test_missing_platform_cache_is_labeled_not_empty_leagues(self):
        from app.reliability import odds_league_menu
        menu = odds_league_menu("娛樂平台賠率", platform_daily={})
        shown = json.dumps(menu, ensure_ascii=False)
        self.assertIn("快取目前不存在", shown)
        self.assertIn("00:30", shown)

    def test_flex_menu_and_odds_sources_are_connected(self):
        menu = json.dumps(legacy.menu_card(), ensure_ascii=False)
        data = json.dumps(odds_category_menu(), ensure_ascii=False)
        self.assertIn("檢視賠率", menu)
        for command in ("台灣運彩賠率", "娛樂平台賠率"):
            self.assertIn(command, data)
        self.assertNotIn("賠率資料狀態", data)



class RetainedControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_analysis_odds_and_sports_only_commands(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(503))) as client:
            ctl = Controller(legacy, client)
            ctl.odds.official_snapshot_file = __import__("pathlib").Path("/invalid/no_tw_official.json")
            ctl.odds.third_party_snapshot_file = __import__("pathlib").Path("/invalid/no_tw_third.json")
            for command, expected in (
                ("檢視賠率", "台灣運彩賠率"),
                ("賽事分析", "今日賽事"),
                ("體育分析", "今日賽事"),
            ):
                messages = await ctl.reply(command)
                self.assertTrue(messages)
                self.assertEqual(messages[0]["type"], "flex")
                self.assertIn(expected, json.dumps(messages, ensure_ascii=False))
            self.assertNotIn("賠率資料狀態",
                             json.dumps(await ctl.reply("檢視賠率"), ensure_ascii=False))
            await ctl.close()


if __name__ == "__main__":
    unittest.main()
