"""export/get의 올바른 exportType 값 탐색"""
import requests, json
from mt_download import parse_cookie_string, COOKIE_STRING, XSRF_TOKEN, BASE_URL, RELEASE

cookies = parse_cookie_string(COOKIE_STRING)
h = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "mystandards-XSRF-TOKEN": XSRF_TOKEN,
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"https://www2.swift.com/mystandards/#/mtcategories/mt/{RELEASE}/103",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
}
session = requests.Session()
session.cookies.update(cookies)

url = f"{BASE_URL}/api/public/export/get"
obj_uri = f"mt/{RELEASE}/103"

# 다양한 exportType 값 시도
export_types = [
    "exportResultingMP",
    "exportNewPDF",
    "exportInternalNewPDF",
    "exportInternalExcel",
    "RESULTING_MP",
    "resulting_mp",
    "PDF",
    "pdf",
    "MT_PDF",
    "mt_pdf",
    "PLAIN_PDF",
    "plain_pdf",
    "SR",
    "sr",
    "exportMT",
    "mt",
    "MT",
]

print("=== exportType 값 탐색 ===")
for et in export_types:
    r = session.get(url, params={"exportType": et, "objectUri": obj_uri}, headers=h, timeout=10)
    ct = r.headers.get("content-type", "")
    body = r.text[:80]
    marker = "<<< HIT" if r.status_code == 200 else ""
    print(f"  {et:30s}: {r.status_code} {ct[:20]} | {body[:60]} {marker}")

# 기존에 성공한 MT102 UUID 로 export/get 상태 확인
print("\n=== MT102 UUID로 상태 조회 (reInitiateDownload 패턴) ===")
mt102_uuid = "035830ab-49d4-457d-8c96-eb4e98063d4a"
# myDownloads의 id 필드로 reinitiate
r = session.get(f"{BASE_URL}/api/public/myDownloads", headers=h, timeout=10)
downloads = r.json()
if downloads:
    d = downloads[0]
    print(f"  id={d.get('id')} status={d.get('status')} uuid={d.get('uuid','')[:20]}")
    # reinitiate 시도 (PUT)
    r2 = session.put(f"{BASE_URL}/api/public/myDownloads/requestid",
                     json=d.get("id"), headers={**h, "Content-Type": "application/json"}, timeout=10)
    print(f"  PUT myDownloads/requestid: {r2.status_code} => {r2.text[:100]}")
