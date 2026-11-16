"""One-time public-source feasibility check. Never bypass access restrictions."""
import re
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

URLS = [
    "https://www.playsport.cc/robots.txt",
    "https://www.playsport.cc/gamesData/result?allianceid=4&gametime=20261116",
]


for url in URLS:
    print("\nSOURCE", url)
    try:
        req = Request(url, headers={"User-Agent": "line-sports-ai-public-source-check/1.0"})
        with urlopen(req, timeout=16) as response:
            content = response.read(750000)
            html = content.decode("utf-8", errors="replace")
            print("HTTP", response.status, "LEN", len(content), "END", response.url)
        if "robots.txt" in url:
            print("ROBOTS", html[:3500])
        else:
            for term in ("摩爾多瓦", "客3.8", "運彩盤", "賽事資訊", "1047"):
                index = html.find(term)
                print("MATCH", term, index)
                if index >= 0:
                    print("SNIPPET", re.sub(r"\s+", " ", html[max(0,index-400):index+650])[:900])
            for piece in re.findall(r"<table\b[^>]*>", html, flags=re.I)[:12]:
                print("TABLE", piece[:350])
    except (HTTPError, URLError) as exc:
        print("FETCH_ERROR", type(exc).__name__, str(exc)[:150])
