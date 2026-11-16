"""One GitHub issue for a blocked official Taiwan Sports Lottery refresh."""
import json
import os
import subprocess
from pathlib import Path

TITLE = "[台灣運彩] 官網賠率更新失敗"


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True,
                                   stderr=subprocess.STDOUT).strip()


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    status_path = Path(os.environ["TW_STATUS_PATH"])
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    failure = (os.getenv("FETCH_OUTCOME") != "success"
               or os.getenv("PUSH_OUTCOME") != "success")
    items = json.loads(gh("issue", "list", "-R", repo, "--state", "open",
                          "--limit", "100", "--json", "number,title"))
    matching = [str(x["number"]) for x in items if x.get("title") == TITLE]
    if failure:
        link = ("https://github.com/" + repo + "/actions/runs/" +
                os.environ["GITHUB_RUN_ID"])
        note = ("台彩賠率本次未能自動核實與同步。\n"
                "日期: " + str(status.get("day", "?")) + "\n"
                "檢查: " + str(status.get("checked_at", "?")) + "\n"
                "原因: " + str(status.get("error", status.get("ok", "?"))) + "\n"
                "提交: " + str(os.getenv("PUSH_OUTCOME")) + "\n"
                "執行紀錄: " + link +
                "\n\n原快照未覆蓋，不會顯示假台彩賠率。\n")
        path = Path(os.environ["RUNNER_TEMP"]) / "taiwan-odds-incident.md"
        path.write_text(note, encoding="utf-8")
        if matching:
            gh("issue", "edit", matching[0], "-R", repo, "--body-file", str(path))
        else:
            print(gh("issue", "create", "-R", repo, "--title", TITLE,
                     "--body-file", str(path)))
    else:
        for number in matching:
            gh("issue", "close", number, "-R", repo,
               "--comment", "官網賠率已重新核實並同步。")


if __name__ == "__main__":
    main()
