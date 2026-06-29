"""Angular 앱 JS 번들에서 MT PDF UUID 맵 탐색"""
import requests, re, json
from mt_download import parse_cookie_string, COOKIE_STRING, XSRF_TOKEN, BASE_URL, RELEASE

cookies = parse_cookie_string(COOKIE_STRING)
h = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Referer": "https://www2.swift.com/",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
}
session = requests.Session()
session.cookies.update(cookies)

TARGET_UUID = "b906a8f0-8861-4a8d-ad63-42593801a20b"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# 1. index.html 소스 가져오기
print("=== index.html 분석 ===")
r = session.get(f"{BASE_URL}/", headers=h, timeout=15)
print(f"Status: {r.status_code}, Size: {len(r.text)}")
print(r.text[:1000])

# script src 태그 찾기
scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', r.text)
print(f"\nScript tags: {scripts}")
links = re.findall(r'href=["\']([^"\']+\.js)["\']', r.text)
print(f"JS links: {links}")

# 2. 상대 경로 포함해서 시도
print("\n=== JS 파일 탐색 ===")
possible_js = [
    "/mystandards/main.js",
    "/mystandards/app.js",
    "/mystandards/vendor.js",
    "/mystandards/runtime.js",
    "/mystandards/polyfills.js",
    "/mystandards/scripts.js",
    "/mystandards/chunk.js",
    "/mystandards/static/js/main.js",
]
for js_path in possible_js:
    r = session.get("https://www2.swift.com" + js_path, headers={**h, "Accept": "*/*"}, timeout=10)
    if r.status_code == 200 and len(r.content) > 1000:
        text = r.text
        has_target = TARGET_UUID in text
        uuid_count = len(UUID_RE.findall(text))
        print(f"  FOUND {js_path}: size={len(text)} target_uuid={has_target} total_uuids={uuid_count}")
        if has_target:
            idx = text.find(TARGET_UUID)
            print(f"    Context: {text[max(0,idx-100):idx+200]}")
    else:
        print(f"  {js_path}: {r.status_code}")
