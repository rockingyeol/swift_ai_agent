"""
MyStandards /versions API로 전체 ISO 20022 카테고리별 메시지 JSON 파일 생성
출력: C:\swift-ai-agent\data\messages\{category}_messages.json
"""
import requests
import json
import time
from pathlib import Path
from mx_download import HEADERS, COOKIES

OUTPUT_DIR = Path(__file__).parent / "messages"
OUTPUT_DIR.mkdir(exist_ok=True)

BASE = "https://www2.swift.com/mystandards/api/public"

# /versions API는 Accept: application/json 헤더 필요
session = requests.Session()
session.headers.update({
    **HEADERS,
    "Accept": "application/json",
    "X-Requested-With": "XMLHttpRequest",
})
session.cookies.update(COOKIES)

# 1. 전체 카탈로그 가져오기
print("카탈로그 로드 중...")
r = session.get(f"{BASE}/mx/catalog", timeout=15)
r.raise_for_status()
catalog = r.json()
print(f"카테고리 수: {len(catalog)}개\n")

# 2. 카테고리별 처리
for group in sorted(catalog, key=lambda g: g["id"]):
    gid = group["id"]
    latest_ids = [
        v["messageIdentifier"]
        for v in group["variants"]
        if not v["messageIdentifier"].startswith("DRAFT")
    ]
    if not latest_ids:
        print(f"[SKIP] {gid}: 최신 버전 없음")
        continue

    out_path = OUTPUT_DIR / f"{gid}_messages.json"

    all_messages = []
    errors = []

    for latest_id in latest_ids:
        r = session.get(f"{BASE}/mx/{latest_id}/versions", timeout=10)
        if not r.ok:
            errors.append(f"{latest_id}: {r.status_code}")
            continue
        for v in r.json():
            all_messages.append({
                "id": v["messageIdentifier"],
                "name": v["name"],
                "description": ""
            })

    all_messages.sort(key=lambda x: x["id"])

    output = {
        "description": f"SWIFT ISO 20022 {gid} 메시지 목록 (전체 버전)",
        "messages": all_messages
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    status = f"오류{len(errors)}건 " if errors else ""
    print(f"[OK] {gid}_messages.json: {len(all_messages)}개 {status}-> {out_path}")
    if errors:
        for e in errors:
            print(f"     [ERR] {e}")

    time.sleep(0.05)  # 서버 부하 방지

print("\n완료!")
