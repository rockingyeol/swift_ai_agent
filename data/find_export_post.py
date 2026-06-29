"""export POST 엔드포인트 상세 탐색"""
import requests, re
from mt_download import parse_cookie_string, COOKIE_STRING, BASE_URL

cookies = parse_cookie_string(COOKIE_STRING)
h = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Referer": "https://www2.swift.com/mystandards/",
}
session = requests.Session()
session.cookies.update(cookies)

r = session.get("https://www2.swift.com/mystandards/scripts/bundle-3cde574a451734b639bb.js", headers=h, timeout=30)
text = r.text

# directDownloadLink 주변 전체 컨텍스트 (500자)
idx = text.find("directDownloadLink")
if idx >= 0:
    print("=== directDownloadLink context ===")
    print(text[max(0, idx-200):idx+500])

# PENDING/COMPLETED 상태 처리 전체 컨텍스트
idx2 = text.find('"COMPLETED"')
while idx2 >= 0:
    ctx = text[max(0,idx2-300):idx2+300]
    if "export" in ctx.lower() or "download" in ctx.lower() or "uuid" in ctx.lower():
        print("\n=== COMPLETED context ===")
        print(ctx)
        break
    idx2 = text.find('"COMPLETED"', idx2+1)

# http_post + export 조합
post_hits = re.findall(r'.{0,200}http_post.{0,200}export.{0,200}', text)
if not post_hits:
    post_hits = re.findall(r'.{0,200}export.{0,200}http_post.{0,200}', text)
print(f"\n=== http_post + export ({len(post_hits)} hits) ===")
for h2 in post_hits[:3]:
    print(" ", h2[:300])

# export 관련 함수명 전체 수집
export_funcs = re.findall(r'[a-zA-Z_$][a-zA-Z0-9_$]*Export[a-zA-Z0-9_$]*\s*=\s*function', text)
export_funcs += re.findall(r'function\s+[a-zA-Z_$]*[Ee]xport[a-zA-Z_$]*\s*\(', text)
print(f"\n=== export 관련 함수들 ===")
for f in list(set(export_funcs))[:20]:
    print(" ", f[:80])
