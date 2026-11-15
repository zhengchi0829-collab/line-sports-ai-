"""GitHub Actions health probe. No secrets or paid APIs are required."""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

BASE = "https://line-sports-ai-bot.onrender.com"


def get_json(base, path, timeout=85):
    request = Request(base.rstrip("/") + path, headers={
        "User-Agent": "line-sports-ai-monitor/1.0",
        "Accept": "application/json",
    })
    with urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError(f"{path} HTTP {response.status}")
        payload = json.loads(response.read(100000).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} returned non-object JSON")
    return payload


def check(base):
    health = get_json(base, "/health")
    if health.get("status") != "ok" or health.get("service") != "line-sports-ai":
        raise ValueError("Health endpoint did not report ready")
    features = get_json(base, "/health/features")
    if features.get("version") != health.get("version"):
        raise ValueError("Version disagreement between health endpoints")
    refresh = features.get("background_refresh") or {}
    if refresh.get("active") is not True:
        raise ValueError("Background fixture refresh is inactive")
    last = refresh.get("last_run_epoch")
    if isinstance(last, (float, int)) and time.time() - last > 1200:
        raise ValueError("Background fixture refresh has been stale for over 20 minutes")
    counts = refresh.get("counts") or {}
    if any(isinstance(value, int) and value < 0 for value in counts.values()):
        raise ValueError("One or more fixture providers failed to refresh")
    webhook = features.get("line_webhook") or {}
    if webhook.get("status") in {
        "inactive", "missing_token", "repair_failed", "api_error",
        "test_failed", "exception",
    }:
        raise ValueError("LINE webhook check failed: " + str(webhook.get("status")))
    richmenu = features.get("richmenu") or {}
    if richmenu.get("action_count") == 0:
        raise ValueError("LINE rich menu has no actions")
    return ("OK " + str(health.get("version")) +
            "; cached providers=" + str(len(features.get("providers") or {})) +
            "; webhook=" + str(webhook.get("status", "checking")))


def run(base, report, max_attempts=3, sleep=time.sleep):
    errors = []
    for attempt in range(max_attempts):
        try:
            result = check(base)
        except Exception as exc:
            errors.append(type(exc).__name__ + ": " + str(exc)[:400])
            if attempt + 1 < max_attempts:
                sleep(12)
            continue
        output = ("Time UTC: " + datetime.now(timezone.utc).isoformat() +
                  "\n" + result + "\n")
        report.write_text(output, encoding="utf-8")
        print(output)
        return 0
    output = ("Time UTC: " + datetime.now(timezone.utc).isoformat() +
              "\nFAILED after " + str(max_attempts) + " attempts\n" +
              "\n".join(errors) + "\n")
    report.write_text(output, encoding="utf-8")
    print(output, file=sys.stderr)
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=BASE)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    sys.exit(run(args.base, args.report))
