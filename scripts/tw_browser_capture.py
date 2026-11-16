"""Fetch the visible official daily-coupons page in a real Chrome browser.

This uses only normal public page rendering. It never bypasses 403,
authentication, CAPTCHA, geo-restrictions or blocked access controls.
Parsed rows still pass the strict date/team/actual-odds validation.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

from scripts.update_tw_official_odds import FetchFailure, URL, refresh

BROWSER_NAMES = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
)
ALLOWED_HOSTS = {"sportslottery.com.tw", "www.sportslottery.com.tw"}


def find_chrome(which=shutil.which):
    for name in BROWSER_NAMES:
        result = which(name)
        if result:
            return result
    return None


def browser_fetch():
    executable = find_chrome()
    if not executable:
        raise FetchFailure("CHROME_NOT_INSTALLED")
    try:
        from playwright.sync_api import sync_playwright, Error as PlaywrightError
    except ImportError as exc:
        raise FetchFailure("PLAYWRIGHT_NOT_INSTALLED") from exc

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=executable,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                context = browser.new_context(
                    locale="zh-TW",
                    timezone_id="Asia/Taipei",
                    viewport={"width": 1366, "height": 900},
                )
                try:
                    page = context.new_page()
                    response = page.goto(
                        URL, wait_until="domcontentloaded", timeout=30000,
                    )
                    if response is None:
                        raise FetchFailure("CHROME_NO_RESPONSE")
                    print("Chrome HTTP status:", response.status)
                    if response.status != 200:
                        raise FetchFailure("CHROME_HTTP_" + str(response.status))
                    if urlparse(page.url).hostname not in ALLOWED_HOSTS:
                        raise FetchFailure("CHROME_UNEXPECTED_REDIRECT")

                    # Wait for the official page's own JavaScript to populate
                    # any public, visible odds table. Do not interact with
                    # anti-bot checks or blocked pages.
                    page.wait_for_timeout(4500)
                    html = page.content()
                    print("Chrome title:", page.title()[:120])
                    print("Chrome visible table count:", page.locator("table").count())
                    if len(html.encode("utf-8")) > 2_000_000:
                        raise FetchFailure("CHROME_RESPONSE_TOO_LARGE")
                    return html
                finally:
                    context.close()
            finally:
                browser.close()
    except PlaywrightError as exc:
        message = str(exc).lower()
        if "err_blocked_by_administrator" in message:
            raise FetchFailure("CHROME_BLOCKED_BY_ADMINISTRATOR") from exc
        if "err_access_denied" in message:
            raise FetchFailure("CHROME_ACCESS_DENIED") from exc
        raise FetchFailure("CHROME_BROWSER_ERROR") from exc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path,
                        default=Path("app/tw_official_odds.json"))
    parser.add_argument("--status", type=Path, required=True)
    args = parser.parse_args()
    return refresh(args.out, args.status, fetch=browser_fetch)


if __name__ == "__main__":
    sys.exit(main())
