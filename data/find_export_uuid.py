"""SWIFT MyStandards 내보내기 UUID 발견 - POST 엔드포인트 탐색"""
import requests, json
from mt_download import parse_cookie_string, COOKIE_STRING, XSRF_TOKEN, BASE_URL, RELEASE

cookies = parse_cookie_string(COOKIE_STRING)
h = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "mystandards-XSRF-TOKEN": XSRF_TOKEN,
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www2.swift.com/mystandards/",
}
session = requests.Session()
session.cookies.update(cookies)

year = RELEASE.split(".")[0]

post_tests = [
    (f"{BASE_URL}/api/public/export/prepare",  {"msgType": "MT103", "release": RELEASE}),
    (f"{BASE_URL}/api/public/export/init",     {"msgType": "MT103", "release": RELEASE}),
    (f"{BASE_URL}/api/public/export",          {"url": f"mt/{RELEASE}/103", "exportType": "exportResultingMP"}),
    (f"{BASE_URL}/api/public/export/request",  {"url": f"mt/{RELEASE}/103"}),
    (f"{BASE_URL}/api/public/mt/{RELEASE}/103/export", {"exportType": "exportResultingMP"}),
    # Generator 서비스
    (f"{BASE_URL}/srv/com.swift.mystandards.service.generate.Generator/exportResultingMP",
     {"url": f"mt/{RELEASE}/103", "releaseIndicator": RELEASE}),
    (f"{BASE_URL}/srv/com.swift.mystandards.service.generate.Generator/exportResultingMP",
     {"url": f"mt/{RELEASE}/103"}),
]

print("=== POST 엔드포인트 탐색 ===")
for url, body in post_tests:
    try:
        r = session.post(url, json=body, headers=h, timeout=15)
        ct = r.headers.get("content-type", "")
        label = url.split("/")[-1][:30]
        print(f"  {label}: {r.status_code} {ct[:25]} => {r.text[:200]}")
    except Exception as e:
        print(f"  {url.split('/')[-1]}: ERROR {e}")

# GET 방식으로도 export 엔드포인트 탐색
print("\n=== GET 엔드포인트 추가 탐색 ===")
get_tests = [
    f"{BASE_URL}/api/public/mt/{RELEASE}/103/pdfuuid",
    f"{BASE_URL}/api/public/mt/{RELEASE}/103/exportuuid",
    f"{BASE_URL}/api/public/export/uuid/mt/{RELEASE}/103",
    f"{BASE_URL}/api/public/export/metadata/{RELEASE}/MT103",
    f"{BASE_URL}/api/public/publication/{RELEASE}/103",
    f"{BASE_URL}/api/public/publication/mt/{RELEASE}/103",
]
for url in get_tests:
    try:
        r = session.get(url, headers=h, timeout=10)
        label = "/".join(url.split("/")[-3:])[:40]
        print(f"  {label}: {r.status_code} => {r.text[:150]}")
    except Exception as e:
        print(f"  {url}: ERROR")
