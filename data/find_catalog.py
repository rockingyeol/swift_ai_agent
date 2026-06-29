"""SWIFT MyStandards 전체 MT 카탈로그 + UUID 탐색"""
import requests, json, re
from mt_download import parse_cookie_string, COOKIE_STRING, XSRF_TOKEN, BASE_URL, RELEASE

cookies = parse_cookie_string(COOKIE_STRING)
h = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "mystandards-XSRF-TOKEN": XSRF_TOKEN,
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"https://www2.swift.com/mystandards/#/mtcategories/mt/{RELEASE}/",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
}
session = requests.Session()
session.cookies.update(cookies)

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# 카탈로그/목록 API 탐색
catalog_endpoints = [
    f"/api/public/mt/{RELEASE}",
    f"/api/public/mt/list/{RELEASE}",
    f"/api/public/mt/{RELEASE}/list",
    f"/api/public/mt/catalog/{RELEASE}",
    f"/api/public/release/{RELEASE}/mt",
    f"/api/public/release/{RELEASE}",
    f"/api/public/categories/{RELEASE}",
    f"/api/public/categories/{RELEASE}/mt",
    f"/api/public/mtcategory/{RELEASE}",
    f"/api/public/mtcategory/1/{RELEASE}",  # category 1
]

print("=== 카탈로그 API 탐색 ===")
for ep in catalog_endpoints:
    r = session.get(BASE_URL + ep, headers=h, timeout=10)
    if r.status_code == 200 and r.text.strip() and r.text.strip() != "null":
        uuids = UUID_RE.findall(r.text)
        print(f"  HIT {ep}: {r.status_code} uuids={len(uuids)} => {r.text[:200]}")
    else:
        print(f"       {ep}: {r.status_code}")

# MT103 export 관련 추가 패턴 (Accept 변경)
print("\n=== export/download 직접 패턴 (UUID 없이) ===")
year = RELEASE.split(".")[0]
no_uuid_patterns = [
    f"{BASE_URL}/api/public/export/download/SR_{year}_MT103.pdf",
    f"{BASE_URL}/api/public/export/mt/{RELEASE}/103/pdf",
    f"{BASE_URL}/api/public/mt/{RELEASE}/103/download",
]
for url in no_uuid_patterns:
    r = session.get(url, headers={**h, "Accept": "*/*"}, timeout=15)
    ct = r.headers.get("content-type", "")
    print(f"  {url.split('/')[-1]}: {r.status_code} {ct[:30]} size={len(r.content)}")
    if r.status_code == 200 and len(r.content) > 1000:
        print("  => 다운로드 성공!")

# 응답이 JSON으로 UUID를 반환하는 패턴
print("\n=== MT list full JSON 확인 ===")
r = session.get(f"{BASE_URL}/api/public/mt/{RELEASE}/103", headers=h, timeout=15)
d = r.json()
# 재귀 탐색
def find_doc_uuids(obj, path=""):
    results = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            results += find_doc_uuids(v, path + "." + k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            results += find_doc_uuids(v, path + f"[{i}]")
    elif isinstance(obj, str):
        m = UUID_RE.search(obj)
        if m:
            results.append((path, m.group()))
    return results

doc_uuids = find_doc_uuids(d)
print(f"  JSON 내 UUID: {len(doc_uuids)}개")
for path, uuid in doc_uuids[:5]:
    print(f"    {path}: {uuid}")
