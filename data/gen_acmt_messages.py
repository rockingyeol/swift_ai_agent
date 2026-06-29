"""MyStandards /versions API로 전체 acmt 버전 목록을 수집해서 acmt_messages.json 생성"""
import requests
import json
from pathlib import Path
from mx_download import HEADERS, COOKIES

session = requests.Session()
session.headers.update(HEADERS)
session.cookies.update(COOKIES)

BASE = "https://www2.swift.com/mystandards/api/public"

# 1. catalog에서 acmt 최신 버전 목록 가져오기
r = session.get(f"{BASE}/mx/catalog", timeout=10)
catalog = r.json()
acmt_group = next(g for g in catalog if g["id"] == "acmt")
latest_ids = [
    v["messageIdentifier"]
    for v in acmt_group["variants"]
    if not v["messageIdentifier"].startswith("DRAFT")
]
print(f"카탈로그 acmt 메시지: {len(latest_ids)}개")

# 2. 각 메시지의 전체 버전 수집
all_messages = []
for latest_id in sorted(latest_ids):
    r = session.get(f"{BASE}/mx/{latest_id}/versions", timeout=10)
    if not r.ok:
        print(f"  오류: {latest_id} -> {r.status_code}")
        continue
    versions = r.json()
    for v in versions:
        all_messages.append({
            "id": v["messageIdentifier"],
            "name": v["name"],
            "description": ""
        })
    print(f"  {latest_id}: {len(versions)}개 버전")

all_messages.sort(key=lambda x: x["id"])
print(f"\n전체 버전 수: {len(all_messages)}개")

# 3. acmt_messages.json 저장
output = {
    "description": "SWIFT ISO 20022 acmt (Account Management) 메시지 목록 (MyStandards 카탈로그 전체 버전)",
    "messages": all_messages
}
out_path = Path(__file__).parent / "acmt_messages.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"\nacmt_messages.json 저장 완료: {out_path}")
print(f"총 {len(all_messages)}개 메시지 ID 포함")
