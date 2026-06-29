"""export/get 및 myDownloads 엔드포인트 테스트"""
import requests, json, time
from mt_download import parse_cookie_string, COOKIE_STRING, XSRF_TOKEN, BASE_URL, RELEASE

cookies = parse_cookie_string(COOKIE_STRING)
h_base = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "mystandards-XSRF-TOKEN": XSRF_TOKEN,
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"https://www2.swift.com/mystandards/#/mtcategories/mt/{RELEASE}/103",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
}
session = requests.Session()
session.cookies.update(cookies)

# export/get - Accept 헤더 조합 시도
url = f"{BASE_URL}/api/public/export/get"
params = {"exportType": "exportResultingMP", "objectUri": f"mt/{RELEASE}/103"}

print("=== export/get Accept 헤더 테스트 ===")
for accept in ["application/json", "*/*", "application/json, text/plain, */*", "text/html,*/*"]:
    h = {**h_base, "Accept": accept}
    r = session.get(url, params=params, headers=h, timeout=15)
    ct = r.headers.get("content-type", "")
    print(f"  Accept={accept[:30]:30s}: {r.status_code} ct={ct[:25]} body={r.text[:80]}")

# myDownloads 확인 (기존 export 요청 목록)
print("\n=== myDownloads GET ===")
for ep in ["/api/public/myDownloads", "/api/myDownloads", "/api/public/export/myDownloads"]:
    r = session.get(BASE_URL + ep, headers={**h_base, "Accept": "application/json, */*"}, timeout=15)
    print(f"  {ep}: {r.status_code} => {r.text[:150]}")

# 기존 MT103 export 목록 확인 (getMyDownloads)
print("\n=== getMyDownloads context search - API 호출 ===")
# 캐시된 MT103 UUID로 상태 확인
known_uuid = "b906a8f0-8861-4a8d-ad63-42593801a20b"
year = RELEASE.split(".")[0]
r = session.get(
    f"{BASE_URL}/api/public/export/download/SR_{year}_MT103.pdf",
    params={"uuid": known_uuid},
    headers={**h_base, "Accept": "application/pdf, */*"},
    timeout=20
)
print(f"Known UUID download: {r.status_code} ct={r.headers.get('content-type','')} size={len(r.content)}")
if len(r.content) > 1000:
    print("  => 여전히 유효한 UUID!")
