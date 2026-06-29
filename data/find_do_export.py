"""doExport 함수 및 export 생성 엔드포인트 상세 분석"""
import requests, re
from mt_download import parse_cookie_string, COOKIE_STRING, BASE_URL

cookies = parse_cookie_string(COOKIE_STRING)
h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "*/*", "Referer": "https://www2.swift.com/mystandards/"}
session = requests.Session()
session.cookies.update(cookies)

r = session.get("https://www2.swift.com/mystandards/scripts/bundle-3cde574a451734b639bb.js", headers=h, timeout=30)
text = r.text

# doExport 함수 전체
idx = text.find("doExport=function")
if idx >= 0:
    print("=== doExport ===")
    print(text[idx:idx+800])

# export queue/request 생성 패턴 (getPublicApiUrl + export + something)
print("\n=== getPublicApiUrl + export API calls ===")
api_calls = re.findall(r'getPublicApiUrl\(\).{0,5}["\']([^"\']+export[^"\']+)["\']', text)
api_calls2 = re.findall(r'"export/([^"]+)"', text)
print("export API paths from getPublicApiUrl:")
for c in sorted(set(api_calls))[:20]:
    print(f"  {c}")
print("\nexport/* paths:")
for c in sorted(set(api_calls2))[:20]:
    print(f"  export/{c}")

# fetchMyDownloads 함수 - 다운로드 목록 API
idx2 = text.find("fetchMyDownloads")
if idx2 >= 0:
    print("\n=== fetchMyDownloads context ===")
    print(text[max(0,idx2-50):idx2+300])

# export 요청 생성 HTTP 호출 탐색
http_export = re.findall(r'http_(?:get|post|put)\([^)]{0,200}export[^)]{0,100}\)', text)
print(f"\n=== HTTP calls with 'export' ({len(http_export)} hits) ===")
for call in http_export[:10]:
    print(f"  {call[:250]}")
