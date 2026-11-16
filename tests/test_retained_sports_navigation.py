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
