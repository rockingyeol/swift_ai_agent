"""myDownloads에서 MT PDF UUID 전체 목록 조회"""
import requests, json
from mt_download import parse_cookie_string, COOKIE_STRING, XSRF_TOKEN, BASE_URL, RELEASE

cookies = parse_cookie_string(COOKIE_STRING)
h = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept": "application/json, */*",
    "mystandards-XSRF-TOKEN": XSRF_TOKEN,
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"https://www2.swift.com/mystandards/",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
}
session = requests.Session()
session.cookies.update(cookies)

r = session.get(f"{BASE_URL}/api/public/myDownloads", headers=h, timeout=15)
downloads = r.json()
print(f"총 {len(downloads)}개 download 항목")
print()

year = RELEASE.split(".")[0]

# MT PDF만 필터링 + UUID 맵 생성
uuid_map = {}
for d in downloads:
    filename = d.get("filename", "")
    uuid = d.get("uuid", "")
    status = d.get("status", "")
    # SR_2026_MT*.pdf 패턴
    if filename.startswith(f"SR_{year}_MT") and filename.endswith(".pdf") and status == "COMPLETED":
        mt = filename[len(f"SR_{year}_MT"):-4]  # MT 번호 추출
        uuid_map[mt] = uuid
        print(f"  MT{mt}: {uuid}  ({filename})")

print(f"\n총 {len(uuid_map)}개 MT UUID 확보")

# mt_download.py의 MT_LIST와 비교
from mt_download import MT_LIST
missing = [mt for mt in MT_LIST if mt not in uuid_map]
print(f"누락된 MT: {len(missing)}개")
if missing:
    print("누락 목록:", missing[:20])
