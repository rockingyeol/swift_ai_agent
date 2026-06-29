"""export/get 및 loadMyDownloads 분석"""
import requests, re
from mt_download import parse_cookie_string, COOKIE_STRING, BASE_URL

cookies = parse_cookie_string(COOKIE_STRING)
h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "*/*", "Referer": "https://www2.swift.com/mystandards/"}
session = requests.Session()
session.cookies.update(cookies)

r = session.get("https://www2.swift.com/mystandards/scripts/bundle-3cde574a451734b639bb.js", headers=h, timeout=30)
text = r.text

# export/get 컨텍스트
for term in ["export/get", "export/getCRCmts", "loadMyDownloads", "exportMt", "exportMT", "createExportRequest", "initDownload"]:
    idx = text.find(term)
    if idx >= 0:
        ctx = text[max(0,idx-200):idx+400]
        print(f"\n=== '{term}' ===")
        print(ctx[:500])

# export 생성 함수 - exportResultingMP 주변
idx = text.find("exportResultingMP")
while idx >= 0:
    ctx = text[max(0,idx-200):idx+400]
    if "http" in ctx.lower() or "post" in ctx.lower() or "request" in ctx.lower():
        print(f"\n=== exportResultingMP with http/post ===")
        print(ctx[:500])
        break
    idx = text.find("exportResultingMP", idx+1)

# onExport 함수
idx = text.find("onExport")
while idx >= 0:
    ctx = text[max(0,idx-100):idx+400]
    if "exportResultingMP" in ctx or "http" in ctx or "post" in ctx.lower():
        print(f"\n=== onExport with export ===")
        print(ctx[:500])
        break
    idx = text.find("onExport", idx+1)
