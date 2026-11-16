"""Bounded zero-code-change redeploy for strictly restartable bot faults.

Never redeploy due to upstream API errors, odds 403, invalid tokens, or wrong
data. Do not run more than once per 90 minutes or during a recent deployment.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

EXPECTED_REPO = "zhengchi0829-collab/line-sports-ai-"
PREFIX = "[auto-recovery] Relaunch LINE bot"
DEPLOY_GRACE_SECONDS = 360
RECOVERY_COOLDOWN_SECONDS = 5400


def git(*args):
    return subprocess.check_output(["git", *args], text=True,
                                   stderr=subprocess.STDOUT).strip()


def policy(diagnosis, history, now):
    """Return a safe decision without changing the repository."""
    if diagnosis.get("ok") or diagnosis.get("restartable") is not True:
        return False, "Not a restartable application fault"
    if not history:
        return False, "No trusted git history"
    latest = max(stamp for stamp, _ in history)
    if now - latest < DEPLOY_GRACE_SECONDS:
        return False, "A recent deployment may still be starting"
    for stamp, subject in history:
        if subject.startswith(PREFIX) and now - stamp < RECOVERY_COOLDOWN_SECONDS:
            return False, "Auto-redeploy cooldown (90 minutes)"
    return True, "Recoverable bot process failure"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnostics", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    out = Path(os.environ["GITHUB_OUTPUT"])
    def mark(redeployed, message):
        out.open("a", encoding="utf-8").write(
            "redeployed=" + ("true" if redeployed else "false") + "\n")
        args.report.open("a", encoding="utf-8").write(
            "Auto-recovery: " + message + "\n")
        print(message)

    if os.getenv("GITHUB_REPOSITORY") != EXPECTED_REPO:
        raise ValueError("Unexpected repository; deployment aborted")
    diagnosis = json.loads(args.diagnostics.read_text(encoding="utf-8"))
    if not diagnosis.get("restartable"):
        mark(False, "Skipped: External provider/configuration fault")
        return 0
    git("fetch", "--depth=100", "origin", "main")
    raw = git("log", "--format=%ct%x09%s", "-80", "origin/main")
    history = []
    for row in raw.splitlines():
        try:
            stamp, subject = row.split("\t", 1)
            history.append((int(stamp), subject))
        except ValueError:
            continue
    allowed, reason = policy(diagnosis, history, time.time())
    if not allowed:
        mark(False, "Skipped: " + reason)
        return 0
    # No application source is modified; Render's GitHub auto-deploy listens
    # for main pushes. Concurrent changes fail safely on a non-force push.
    git("checkout", "-B", "main", "origin/main")
    git("-c", "user.name=github-actions[bot]",
        "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit", "--allow-empty", "-m", PREFIX + ": " + str(diagnosis.get("code")))
    git("push", "origin", "HEAD:main")
    mark(True, "Redeploy requested by safe empty commit to main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
