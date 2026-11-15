"""Keep a single GitHub issue open while the 10-minute bot monitor fails."""
import json
import os
import subprocess
from pathlib import Path

TITLE = "[LINE Sports AI] Health monitor incident"


def gh(*args):
    return subprocess.check_output(
        ["gh", *args], text=True, stderr=subprocess.STDOUT).strip()


def main():
    status = os.environ.get("MONITOR_RESULT", "")
    report = Path(os.environ["MONITOR_REPORT"])
    if not status:
        raise RuntimeError("Missing probe result")
    existing = json.loads(gh("issue", "list", "--state", "open",
                             "--limit", "200", "--json", "number,title"))
    numbers = [str(issue["number"]) for issue in existing if issue["title"] == TITLE]
    if status != "success":
        run_link = (f'https://github.com/{os.environ["GITHUB_REPOSITORY"]}/actions/runs/'
                    f'{os.environ["GITHUB_RUN_ID"]}')
        body = (report.read_text(encoding="utf-8") if report.exists()
                else "Monitor probe failed without a report.\n")
        body += "\nWorkflow: " + run_link + "\n"
        report.write_text(body, encoding="utf-8")
        if numbers:
            gh("issue", "edit", numbers[0], "--body-file", str(report))
            print("Existing incident updated:", numbers[0])
        else:
            print(gh("issue", "create", "--title", TITLE,
                     "--body-file", str(report)))
    else:
        for number in numbers:
            gh("issue", "close", number, "--comment",
               "The bot responded to the current health check and recovered.")
            print("Incident closed:", number)


if __name__ == "__main__":
    main()
