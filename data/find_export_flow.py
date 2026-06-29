"""Angular JS 번들에서 export 워크플로우 엔드포인트 탐색"""
import requests, re
from mt_download import parse_cookie_string, COOKIE_STRING, BASE_URL

cookies = parse_cookie_string(COOKIE_STRING)
h = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www2.swift.com/mystandards/",
}
session = requests.Session()
session.cookies.update(cookies)

r = session.get("https://www2.swift.com/mystandards/scripts/bundle-3cde574a451734b639bb.js", headers=h, timeout=30)
text = r.text
print(f"Bundle size: {len(text):,} chars")

patterns = [
    ("createExport",     r".{0,120}createExport.{0,120}"),
    ("initExport",       r".{0,120}initExport.{0,120}"),
    ("requestExport",    r".{0,120}requestExport.{0,120}"),
    ("startExport",      r".{0,100}startExport.{0,100}"),
    ("PENDING status",   r".{0,100}PENDING.{0,100}"),
    ("export/queue",     r".{0,100}export.{0,3}queue.{0,100}"),
    ("export/create",    r".{0,100}export.{0,3}create.{0,100}"),
    ("export/job",       r".{0,100}export.{0,3}job.{0,100}"),
    ("exportResultingMP",r".{0,100}exportResultingMP.{0,100}"),
    ("polling uuid",     r".{0,100}uuid.{0,10}poll.{0,100}"),
    ("post export",      r".{0,60}post.{0,10}export.{0,100}"),
    ("export api url",   r".{0,60}getPublicApiUrl.{0,60}export.{0,60}"),
    ("requestDownload",  r".{0,100}requestDownload.{0,100}"),
    ("downloadExport",   r".{0,100}downloadExport.{0,100}"),
    ("exportRequest",    r".{0,100}exportRequest.{0,100}"),
]

for label, pat in patterns:
    hits = re.findall(pat, text)
    if hits:
        print(f"\n=== {label} ({len(hits)} hits) ===")
        for hit in hits[:2]:
            print("  ", hit[:200])
