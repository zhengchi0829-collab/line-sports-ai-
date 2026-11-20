"""Scheduled read-only production health probe with structured triage.

Restartable failures are separate from external data/API failures; the monitor
never claims that restarting can repair a blocked odds or fixture provider.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")
from urllib.request import Request, urlopen

BASE = "https://line-sports-ai-bot.onrender.com"


class MonitorError(ValueError):
    def __init__(self, code, message, restartable=False):
        super().__init__(message)
        self.code = code
        self.restartable = restartable


def get_json(base, path, timeout=80):
    request = Request(base.rstrip("/") + path, headers={
        "User-Agent": "line-sports-ai-health/2.0",
        "Accept": "application/json",
    })
    with urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError("%s HTTP %s" % (path, response.status))
        data = json.loads(response.read(120000).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(path + " returned non-object JSON")
    return data


def check(base):
    try:
        health = get_json(base, "/health")
        features = get_json(base, "/health/features")
    except Exception as exc:
        raise MonitorError("service_unreachable",
                           "Health endpoints unavailable: " + type(exc).__name__,
                           True) from exc
    if health.get("status") != "ok" or health.get("service") != "line-sports-ai":
        raise MonitorError("service_not_ready", "Bot health endpoint is not ready", True)
    if features.get("version") != health.get("version"):
        raise MonitorError("version_mismatch", "Service version mismatch", True)
    refresh = features.get("background_refresh") or {}
    if refresh.get("active") is not True:
        raise MonitorError("background_inactive", "Background fixture refresher inactive", True)
    age = time.time()
    last = refresh.get("last_run_epoch")
    started = features.get("bot_started_epoch")
    if isinstance(last, (int, float)) and age - last > 1200:
        raise MonitorError("stale_refresh", "Background fixture refresh stale > 20 min", True)
    if last is None and isinstance(started, (int, float)) and age - started > 300:
        raise MonitorError("refresh_never_started",
                           "Background refresh did not start within five minutes", True)
    counts = refresh.get("counts") or {}
    if any(isinstance(val, int) and val < 0 for val in counts.values()):
        raise MonitorError("worker_failure", "At least one fixture worker crashed", True)
    successes = refresh.get("source_successes") or {}
    missing = [s for s in ("soccer_ucl", "soccer_epl", "basketball")
               if s in successes and successes[s] == 0]
    if missing:
        raise MonitorError("upstream_failed", "Core fixture sources failed: " + ", ".join(missing))
    errors = refresh.get("source_errors") or {}
    failure_count = sum(len(values) for values in errors.values()
                        if isinstance(values, list))
    if failure_count >= 8:
        raise MonitorError("upstream_failed",
                           "Many external fixture sources failed: %s" % failure_count)
    webhook = features.get("line_webhook") or {}
    if webhook.get("status") in {
        "inactive", "missing_token", "repair_failed", "api_error",
        "test_failed", "exception",
    }:
        raise MonitorError("webhook_external",
                           "LINE webhook status: " + str(webhook.get("status")))
    if (webhook.get("status") == "checking" and
            isinstance(started, (int, float)) and age - started > 300):
        raise MonitorError("webhook_external", "LINE webhook validation timed out")
    if isinstance(started, (int, float)) and age - started > 300:
        validation = features.get("line_message_validation")
        if validation != 200:
            raise MonitorError("line_validation_invalid",
                               "LINE message validation missing or non-200")
    # Only check the new slots when the running bot advertises that schedule.
    current_slot = refresh.get("last_fixture_slot")
    if current_slot is not None:
        now = datetime.now(TZ)
        passed = [(hour, minute) for hour, minute in ((9, 0), (15, 0), (20, 0))
                  if (now.hour, now.minute) >= (hour, minute)]
        if passed:
            hour, minute = passed[-1]
            expected = now.replace(hour=hour, minute=minute,
                                   second=0, microsecond=0)
            if (now - expected).total_seconds() >= 40 * 60:
                last_completed = refresh.get("last_fixture_refresh_epoch")
                if (current_slot != expected.isoformat() or
                        not isinstance(last_completed, (int, float)) or
                        last_completed < expected.timestamp()):
                    raise MonitorError(
                        "analysis_update_missed",
                        "Analysis scheduled slot did not complete: " +
                        expected.isoformat())
    # Platform quota/bad key needs an incident, NOT repeated billed retries.
    platform = features.get("platform_odds") or {}
    if platform:
        now = datetime.now(TZ)
        # The 00:30 batch can catch up until 03:00 on a sleeping Free server.
        # Missing daily data after that window is a fault, not "no games".
        # Reporting is read-only: never trigger chargeable odds API retries.
        if now.hour >= 3 and platform.get("day") != now.date().isoformat():
            raise MonitorError(
                "platform_cache_missing",
                "Platform odds scheduled snapshot unavailable after 03:00; "
                "check shared free Key Value and today's 00:30 batch")
        if platform.get("day") == now.date().isoformat() and (
                platform.get("status") in {
                    "catalog_error", "missing_key", "storage_unavailable",
                    "attempted"}):
            if now.hour >= 3:
                raise MonitorError(
                    "platform_update_failed",
                    "Daily platform odds update: " +
                    str(platform.get("status")))
    richmenu = features.get("richmenu") or {}
    if richmenu.get("action_count") == 0:
        raise MonitorError("richmenu_missing", "LINE rich menu has no actions", True)
    return ("OK version=" + str(health.get("version")) +
            " cached_sports=" + str(len(features.get("providers") or {})) +
            " webhook=" + str(webhook.get("status", "checking")) +
            " source_warnings=" + str(failure_count))


def run(base, report, max_attempts=3, sleep=time.sleep, diagnostics=None):
    errors = []
    last_error = None
    for attempt in range(max_attempts):
        try:
            summary = check(base)
            result = ("Checked UTC: " + datetime.now(timezone.utc).isoformat() +
                      "\n" + summary + "\n")
            report.write_text(result, encoding="utf-8")
            if diagnostics:
                diagnostics.write_text(json.dumps({"ok": True, "restartable": False,
                                                    "code": "healthy"}), encoding="utf-8")
            print(result)
            return 0
        except MonitorError as exc:
            last_error = exc
            errors.append(exc.code + ": " + str(exc)[:240])
        except Exception as exc:
            last_error = MonitorError("unexpected_probe_error",
                                      type(exc).__name__, False)
            errors.append("unexpected_probe_error: " + type(exc).__name__)
        if attempt + 1 < max_attempts:
            sleep(12)
    result = ("Checked UTC: " + datetime.now(timezone.utc).isoformat() +
              "\nFAILED after " + str(max_attempts) + " attempts\n" +
              "\n".join(errors) + "\n" +
              "Auto-redeploy permitted: " +
              str(bool(last_error and last_error.restartable)) + "\n")
    report.write_text(result, encoding="utf-8")
    if diagnostics:
        diagnostics.write_text(json.dumps({
            "ok": False,
            "code": last_error.code if last_error else "unknown",
            "restartable": bool(last_error and last_error.restartable),
            "errors": errors,
        }, ensure_ascii=False), encoding="utf-8")
    print(result, file=sys.stderr)
    return 1


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--base", default=BASE)
    args.add_argument("--report", type=Path, required=True)
    args.add_argument("--diagnostics", type=Path)
    opt = args.parse_args()
    sys.exit(run(opt.base, opt.report, diagnostics=opt.diagnostics))
