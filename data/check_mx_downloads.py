import requests, json
from mx_download import HEADERS, COOKIES

session = requests.Session()
session.headers.update(HEADERS)
session.cookies.update(COOKIES)

r = session.get("https://www2.swift.com/mystandards/api/public/myDownloads", timeout=10)
downloads = r.json()
print(f"총 {len(downloads)}개")
for d in downloads:
    status = d.get("status", "")
    fname  = d.get("filename", "")
    uuid   = d.get("uuid", "")[:10]
    print(f"  {status:12s}  {fname}  uuid={uuid}...")
