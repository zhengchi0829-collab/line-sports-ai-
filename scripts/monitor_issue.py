"""Keep one GitHub incident open while a bot monitor check is unhealthy."""
import json
import os
import subprocess
from pathlib import Path

TITLE = "[LINE Sports AI] Health monitor incident"


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True,
                                   stderr=subprocess.STDOUT).strip()


def main():
    healthy = os.environ.get("MONITOR_RESULT", "").lower() in ("true", "success")
    report = Path(os.environ["MONITOR_REPORT"])
    repo = os.environ["GITHUB_REPOSITORY"]
    incidents = json.loads(gh("issue", "list", "-R", repo, "--state", "open",
                              "--limit", "100", "--json", "number,title"))
    matched = [str(item["number"]) for item in incidents
               if item.get("title") == TITLE]
    if not healthy:
        link = ("https://github.com/" + repo + "/actions/runs/" +
                os.environ["GITHUB_RUN_ID"])
        notes = (report.read_text(encoding="utf-8") if report.exists()
                 else "Health probe failed without a report.\n")
        notes += ("\nWorkflow: " + link +
                  "\nSafe automatic redeploy: " +
                  os.environ.get("RECOVERY_RESULT", "skipped") +
                  "\nExternal API failures require source repair; no invented odds.\n")
        report.write_text(notes, encoding="utf-8")
        if matched:
            gh("issue", "edit", matched[0], "-R", repo, "--body-file", str(report))
            print("Incident updated", matched[0])
        else:
            print(gh("issue", "create", "-R", repo, "--title", TITLE,
                     "--body-file", str(report)))
    else:
        for num in matched:
            gh("issue", "close", num, "-R", repo,
               "--comment", "Latest monitor is healthy; issue automatically resolved.")
            print("Incident resolved", num)


if __name__ == "__main__":
    main()
