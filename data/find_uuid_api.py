"""SWIFT MyStandards PDF export UUID 발견 스크립트"""
import requests, re, json
from mt_download import parse_cookie_string, COOKIE_STRING, XSRF_TOKEN, BASE_URL, RELEASE

cookies = parse_cookie_string(COOKIE_STRING)
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,*/*",
    "Referer": "https://www2.swift.com/mystandards/",
}
json_headers = {**headers, "Accept": "application/json", "mystandards-XSRF-TOKEN": XSRF_TOKEN, "X-Requested-With": "XMLHttpRequest"}

session = requests.Session()
session.cookies.update(cookies)

TARGET_UUID = "b906a8f0-8861-4a8d-ad63-42593801a20b"

# 1. SPA 메인 페이지에서 JS 번들 찾기
print("=== SPA JS 분석 ===")
r = session.get(BASE_URL + "/", headers=headers, timeout=15)
js_files = re.findall(r'src="(/mystandards/[^"]+\.js)"', r.text)
print(f"JS files: {js_files[:5]}")

for jsf in js_files[:5]:
    rj = session.get("https://www2.swift.com" + jsf, headers=headers, timeout=20)
    text = rj.text
    if "export" in text and "download" in text:
        export_hits = re.findall(r'.{0,80}export.{0,5}download.{0,80}', text)[:3]
        uuid_api_hits = re.findall(r'.{0,60}uuid.{0,60}', text)[:3]
        if export_hits:
            print(f"\n--- {jsf[-40:]} ---")
            for h in export_hits[:2]:
                print("  EXPORT:", h[:120])
        break

# 2. categories 목록 API - UUID가 있을 수 있음
print("\n=== Category API 시도 ===")
cat_endpoints = [
    f"/api/public/mtcategories",
    f"/api/public/mtcategories/{RELEASE}",
    f"/api/public/mtcategories/{RELEASE}/mt",
    f"/api/public/mtcategories/{RELEASE}/mt/103",
]
for ep in cat_endpoints:
    r = session.get(BASE_URL + ep, headers=json_headers, timeout=10)
    if r.status_code == 200 and r.text.strip():
        body = r.text[:300]
        has_uuid = TARGET_UUID in body or bool(re.search(r'[0-9a-f]{8}-[0-9a-f]{4}', body))
        print(f"  {ep}: 200 uuid={has_uuid} => {body[:150]}")
    else:
        print(f"  {ep}: {r.status_code}")

# 3. 알려진 UUID로 역방향 검색
print("\n=== UUID 역방향 탐색 ===")
uuid_endpoints = [
    f"/api/public/document/{TARGET_UUID}",
    f"/api/public/export/{TARGET_UUID}",
    f"/api/public/mt/document/{TARGET_UUID}",
]
for ep in uuid_endpoints:
    r = session.get(BASE_URL + ep, headers=json_headers, timeout=10)
    print(f"  {ep}: {r.status_code} => {r.text[:150]}")

# 4. MT103 상세 페이지 HTML에서 UUID 추출
print("\n=== MT103 HTML 페이지 UUID 탐색 ===")
r = session.get(f"{BASE_URL}/#/mtcategories/mt/{RELEASE}/103", headers=headers, timeout=15)
uuids_in_page = re.findall(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', r.text)
print(f"  HTML에서 UUID {len(uuids_in_page)}개 발견: {uuids_in_page[:5]}")
if TARGET_UUID in uuids_in_page:
    print(f"  => 타겟 UUID 발견!")
