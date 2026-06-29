"""
SWIFT MyStandards MT PDF 다운로드 스크립트 (myDownloads API 기반)

사용 방법:
========================================================================
[1단계] 브라우저에서 PDF Export 요청 (최초 1회 + 누락분)
  1. Chrome에서 https://www2.swift.com/mystandards 로그인
  2. MT103 페이지 접속 → "Export as PDF" → "Plain PDF" 클릭
  3. 나머지 MT들도 같은 방식으로 클릭 (화면 우측 "My Downloads" 에 쌓임)
  4. 모든 항목이 "Complete" 상태가 될 때까지 대기

[2단계] 쿠키 업데이트
  Chrome F12 → Application → Cookies → https://www2.swift.com
  아래 COOKIE_STRING 과 XSRF_TOKEN 을 최신 값으로 교체

[3단계] 스크립트 실행
  set PYTHONIOENCODING=utf-8
  python mt_download.py
========================================================================
"""

import requests
import os
import time
import json
from pathlib import Path

# ============================================================
# ★ 여기에 브라우저 쿠키 값을 붙여넣으세요 ★
# Chrome F12 → Application → Cookies → https://www2.swift.com
# ============================================================

COOKIE_STRING = """
mystandards-XSRF-TOKEN=aiUvOTp6uT1c3brKzKBn0gAAAA4; afiliation=grp%2F9923; _evga_2f78={%22uuid%22:%22bc3a31844a924174%22}; _gcl_au=1.1.1086207842.1774277239; _fbp=fb.1.1774277239140.955094552813949220; _mkto_trk=id:327-OJP-531&token:_mch-swift.com-ec89f762e1991e1270b16db55b5bb8fd; _twpid=tw.1774277239195.131939652471084326; _sfid_51c1={%22anonymousId%22:%22bc3a31844a924174%22%2C%22consents%22:[{%22consent%22:{%22provider%22:%22Consent%20Provider%22%2C%22purpose%22:%22Personalization%22%2C%22status%22:%22Opt%20In%22}%2C%22lastUpdateTime%22:%222026-03-23T14:47:19.131Z%22%2C%22lastSentTime%22:%222026-03-23T14:47:19.478Z%22}]}; _hjSessionUser_1339453=eyJpZCI6IjBlZWRiYjMyLTk2NmQtNTI1OC1hZjBiLWMyOTE2OGY1Yjc3ZSIsImNyZWF0ZWQiOjE3NzQyNzcyMzkzODEsImV4aXN0aW5nIjp0cnVlfQ==; SdCIsRegisteredC=001708482; coveo_visitorId=217e72dd-28c4-42b1-bfe3-267474e90fd2; _pk_id.1.f951=ac0054ea22cff6a4.1776775162.; _pk_ref.1.f951=%5B%22%22%2C%22%22%2C1777711806%2C%22https%3A%2F%2Fwww2.swift.com%2F%22%5D; _ga_JXL3MCVVWL=GS2.1.s1780321095$o6$g1$t1780322326$j55$l0$h0; _ga=GA1.2.1057366916.1774277239; SDC_COOKIE=!PlSvnp3nahUrhK4QiXZqesKIJhsj/6TIAfzBN92eEkM2/YDchulkWUqfDl6P+7s82x0Ytv92A/zElQ==; bm_mi=B149751D4D1A0890F8F9BCBC17EB8159~YAAQBur7y1B7NoieAQAA4aI/oQBsXJaKbwTrtMMvN1pcIaod33NxrsdHhpeqOipIMEfMIrDtiupMs1j9FchB1/y9+C+m7yoGx63YrrVUA8AYhx4NYuTUMl/2gbkfkotkGAnY1IiSew7XwarmOWrubflm4GCUMFr6zG5lawGnM17E78Nlqk2jVTy2rXvFrc6KibWAv7ZPZxDMqQqY8TLRN+E+2dzvH2A1RVcf3+KAw/MvbGYKHhS5puuHVubWt0DzyR7Sf7ZrI4H2TSmmjau/5twegiebrRUhNWCvtTzQGU8EfCPZ8tBEWQsOb4Nt~1; CK_SDC_REG_INFO=; ak_bmsc=58ABED6069433739B17D01BE821887F8~000000000000000000000000000000~YAAQLun7y5XT04aeAQAA/sQ/oQClvSUobSKcp1y5JCb6U/yjyDaL0b6B1iICDJByxJnwGfvml3OrOzYsmFeEkQdjWkrp4i8UpGx+RRJ/tuTF1wTm8ayunbeQ1uuX7q+xUcHa0ueL/N05MgzesifymiNhKz/hkWiW5qqLYMzNt3Odzx9pVru+WPK3GWqG8kEMIPZX+FQBV/qLoXJIGlUaLmyjAdCg6UHcmpcidV9ecxGPfkh5DBs+UyRY8y/7ntKrfOOFW+wAJyP1m6SLl+XWSnulTpolaehQblzqD6EbSbAFDydnleHXZCe63HVF78Decbx2uDgC0pY9lM3S3SdEQofPsEpDNnouQ9hyRePXobEZiz8z+WLZvZRHDZE3xIoHdZGYCjEmQE2KMlcOfHxnUatoCJvFIdnaP4QjOD0KxJiOIcPJGXJDrMoosMgElcUi2vvnthkG1Q5O1rsl1P9wGvcTQg2jI8k=; _abck=9E97D9ACD58CAED3D04D0C523C664D23~0~YAAQLun7y0Pj04aeAQAA9zpAoRCaO2H1xEUAaDMaRx9GXn2qN8xZP906gpfFccQVgjeuwVC4F2SqDz0QXYcyAlr6ouIvyc/BVqQrfE/Nflm5wBncJx814SkCx4cAte0Wrck505jCK/voC+GUHS8pqTWRZc8hmPu705ZirV9AszsiweSuDFzY/8FT7C1wT5Q3TBnJFmR1euIhlwB92gNbF4AIVMMELcPsF8OpW+kSYTUclkPJ0SJ7gdiG4rL+nwSFpHXq7IrzmbZttFa3tyeEZ3nyP1Mg21cEO2M1K5yA9f3UbBCa9NSQLuc+AP21TkZcQcGcY+UWXongNjTIjgl1Q+YD2BnA5wVQb6DwOMmn7sedHLxyjCKM6l/vafjjk/AmhTzd8OBTU5/Uu7i0VXoaoB0ssWMx2/qQlWWfBfVnspxIIlDxecnay0dfdXr5HsvZv9qqf4fv19OsPgf/9mLXb+9dNg0VypM0lm8drs+uW3HId8NnIWlAAqtVbPJj5QKSQ1Lp9xw/eXXz8mmLQTMJJ9OzPYa/KfUwFgA9sSLqcpYKI1kGvMmbMHqC6AAWOOLbFRJpXRq3DrDXCDkrL16nAhrAoWfvj5Fp60rLzH5l6vqlxgnouhOcw2dtdgdjlkhMCO93WJeW6DGyZ5GuIup+BVF2Q2n1WOW8i4F0fouocZVF9GZHG1jCXkwm32YwCrlaBzUMqLYItfaR6t1yVCiTsPzrisqTTgYP0+n2QNDtb83IeQ+JWZyhqbMBcR2CF1SVkaqXzweP0h87YvZTnVmzTPcty981I2UebWuL6pTL8CWN0qH2x/j+NLceHQd6eT4WEyuFcc4=~-1~-1~-1~AAQAAAAF%2f%2f%2f%2f%2f9xRRNGE8iVTF4Cvif3HIqbX+IljeKRgJWVzzI5IDfkXJIyTe37LGCtztY0%2fW1qCFTAwLUriALe3p%2fpf6TbEm1zJ1Li0%2f9iJxZtX~-1; OAMAuthnHintCookie=1; BCSI-AC-5cb7874debea5fd7=39053D6E00000004+uQa3NpHUH0J7cwT+KlH0IIMDD/zAAAABAAAAKnNOABAOAAAAAAAAEGsAgAHAAAAZGVmYXVsdA==; OAMAuthnCookie=6a3df367ca303ef9d504b40c80b476c5ce02ea13%7E2IeW91WkP7nzUHxTXu6bAJ0rq693TwvafyxhLC85kWLcoZuklfUJXjY6hDRB%2FXeQjBnd%2FUaeAwK7Vsge8iNJwPu8H%2BtlWVsN0nUGowkK8txvRZL%2FcrbkcVsXJA5nvrpLXFAPIM3H58Vra69hbQ37jBLsmlknyVDGr3bkmj9u2pzHLPBxxPN3rxvqJTeMVXbc9vJL1niqbOpMDXbsgjo7wertzsyJBAcX0c4WBW5ZHZZYMfpZIlnZE72kqnrPdYdE0brJqaHv6We0t8eHRby8YkHgwCXLfEKVdMe%2BadgKyYeh1jlNsF48hD%2BbhhEy7%2FYNBe0GidNH7svVtH%2B6QmcgF2Ar2HIUaB08qYRl2%2FW1Ox2vI72h6qHDIh36A1kDWLBZR57M%2BWASbSqcDsj8JdqS%2Bwb0Jud03TTNFP3kUqPuRP7%2F35oPrvlq1IHzg37wG3qq2dAJQF%2B6zWkWQTgXkdpVBIjsrZ6cBQ1TPly0D5VFyyqdjwmM78wBQ%2BHci%2FuL1YBwDajeYke3YlENFcai4vXBU5HGGuUuBq4hPBGl30Wi759575kwO1%2FWEcxjxAVJMfyaK9eeWiYPE1ghIM8c0iwew4CDRysqDCPMvRgMNZCQM6NBuVtZjBudJ0zSDw%2BKPiYhm21khYtWESWKejDR4DJSCRxZFA9g%2BuRnDZGCgTBtdaDr5nq%2Ffse9PGg%2Bfy8OPO85; XSRF-TOKEN-SDC-GLOBAL=0aa9e953-7fd4-44b1-9144-ab75e8e581c7; _gid=GA1.2.718513119.1780821811; bm_sz=ABC3AB406EB21E40274CFDA8CA4BF617~YAAQBur7y6rNNoieAQAAAYxCoQDs0eYLE+6bFhSTJM4voA9whLd7oE6SWieMPcp1Rph6Y/R0UghmoTYyqmsy6LBKJHP+DbDOBZ78Q9Z4WT/s62SYQSazFnjbeRKCvJZ3dftJ54EQrbdtDhj1mQqAq78PHMXQKPfut5D1m5KmNSWCHl3d2EhiML5pnVd759ib0xZwOi/jYZsKh9MJsQ7sW4fo3KXQDjEVMoxU5emcmjXKvdbgddruB+x0MSBsBT4C5ThCsM33e9kkpc9FD0YjGXVo1KjMCo7VJz8DZPyOTyrF4IxKKcct/NMcCqRMRrixX9Uny8VM1ilESOYqWIR9eZDSBFGbt/jbPSfqU61LD0OpB9wMsN3NmnMXRnAL1cMoFxALCI5ARPlUs9eQxp4rNdhOuzGWSCbYC6996l9WYsoVnIiHyMrZHTg2TGoEKpuJMQeES727luU7NFnLdYpnzZY9HlL2TjtLpGw05rW3+DU5D/ibgukM+Uh5fgfcTfk+voc8cjbjwadclpWVTCMJFV2hMCAd~3158337~3228227; bm_sv=2E9CB77F03A758C1F41DC195BCD855A6~YAAQBur7yx7aNoieAQAA6RVDoQCOIfo+rxMPnmVArN7CHfJ8KGfRZ82MZqNMxWJHxo1hHpNHP9C8PBKHrOYxXS0pY0grlGQ2roYZXF0viWEP0lvqlRYEjwQwBZvRR4PSvSlD9YBarQp//HC8G46p6Cxx8cT9uJRTMw0HeygI3L8lLIko6OBXTM9CkWj7ngn/99GAEu31gfmKENEYxSpMejywQnrd1vuIBKBg/fMDzLW5BFAM0YoLl8DFT7X0Jo0x~1
"""

# XSRF 토큰 (COOKIE_STRING의 mystandards-XSRF-TOKEN 값과 동일)
XSRF_TOKEN = "aiUvOTp6uT1c3brKzKBn0gAAAA4"

# ============================================================
# 설정
# ============================================================
DOWNLOAD_DIR = Path("SWIFT_MT_PDFs")
BASE_URL     = "https://www2.swift.com/mystandards"
RELEASE      = "2026.November"
DELAY        = 1   # 요청 간 딜레이(초)

MT_LIST = [
    # Category 1
    "101","102","102.STP","103","103.REMIT","103.STP",
    "104","105","107","110","111","112",
    # Category 2
    "200","201","202","202.COV","203","204",
    "205","205.COV","206","207","210",
    # Category 3
    "300","303","304","305","306","307","308",
    "320","321","330","340","341","350",
    "360","361","362","364","365",
    # Category 4
    "400","405","410","412","416","420","422","430",
    "450","455","456",
    # Category 5
    "500","501","502","503","504","505","506","507",
    "508","509","510","513","514","515","516","517",
    "518","519","524","526","527","528","529",
    "535","536","537","538","540","541","542","543",
    "544","545","546","547","548","549",
    "558","559","564","565","566","567","568","569",
    # Category 6
    "600","601","604","605","606","607","608",
    "643","644","645","646",
    # Category 7
    "700","701","705","707","708","710","711",
    "720","721","730","732","734","740","742",
    "744","747","750","752","754","756","759",
    "760","761","762","763","764","765","766",
    "767","768","769",
    # Category 8
    "800","801",
    # Category 9
    "900","910","920","935","940","941","942",
    "950","960","961","962","963","964","965",
    "966","967","970","971","972","973",
    "985","986","990","991","992","995","996","998","999",
    # Category n
    "n90","n91","n92","n93","n94","n95","n96",
]


def parse_cookie_string(cookie_str):
    """쿠키 문자열 -> dict 변환"""
    cookies = {}
    for part in cookie_str.strip().split(";"):
        part = part.strip()
        if "=" in part:
            key, _, val = part.partition("=")
            cookies[key.strip()] = val.strip()
    return cookies


def build_session():
    cookies = parse_cookie_string(COOKIE_STRING)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/148.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        "Referer": f"{BASE_URL}/#/mtcategories/mt/{RELEASE}/",
        "mystandards-XSRF-TOKEN": XSRF_TOKEN,
        "X-Requested-With": "XMLHttpRequest",
    }
    session = requests.Session()
    session.cookies.update(cookies)
    return session, headers


def get_uuid_map(session, headers):
    """
    /api/public/myDownloads 에서 완료된 MT PDF UUID 맵 반환.
    반환값: {"103": "uuid-xxx", "102": "uuid-yyy", ...}
    """
    year = RELEASE.split(".")[0]
    r = session.get(f"{BASE_URL}/api/public/myDownloads", headers=headers, timeout=15)
    if r.status_code != 200:
        print(f"[경고] myDownloads API 실패: {r.status_code}")
        return {}

    uuid_map = {}
    for d in r.json():
        filename = d.get("filename", "")
        uuid     = d.get("uuid", "")
        status   = d.get("status", "")
        # SR_2026_MT*.pdf 파일명 패턴 + COMPLETED
        prefix = f"SR_{year}_MT"
        if filename.startswith(prefix) and filename.endswith(".pdf") and status == "COMPLETED" and uuid:
            mt = filename[len(prefix):-4]
            uuid_map[mt] = uuid
    return uuid_map


def download_pdf(session, headers, mt, uuid):
    """UUID로 PDF 다운로드. bytes 반환, 실패 시 None."""
    year = RELEASE.split(".")[0]
    url  = f"{BASE_URL}/api/public/export/download/SR_{year}_MT{mt}.pdf"
    try:
        r = session.get(
            url,
            params={"uuid": uuid},
            headers={**headers, "Accept": "application/pdf, */*"},
            timeout=60,
            allow_redirects=True,
        )
        ct = r.headers.get("content-type", "")
        if r.status_code == 200 and ("pdf" in ct or len(r.content) > 5000):
            return r.content
    except Exception as e:
        print(f"[오류] MT{mt}: {e}")
    return None


def main():
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    session, headers = build_session()
    year = RELEASE.split(".")[0]

    print(f"[1] myDownloads에서 UUID 목록 조회 중...")
    uuid_map = get_uuid_map(session, headers)
    print(f"    => 확보된 UUID: {len(uuid_map)}개  ({', '.join(sorted(uuid_map.keys())[:10])}{'...' if len(uuid_map)>10 else ''})")

    # MT_LIST에서 아직 없는 항목 찾기
    missing_mt = [mt for mt in MT_LIST if mt not in uuid_map]
    if missing_mt:
        print(f"\n[주의] 브라우저에서 아직 Export 요청을 안 한 MT: {len(missing_mt)}개")
        print(f"    대상: {', '.join('MT'+m for m in missing_mt[:15])}{'...' if len(missing_mt)>15 else ''}")
        print()
        print("    [필요 작업]")
        print("    1. https://www2.swift.com/mystandards 접속")
        print("    2. 각 MT 페이지에서 Export > Plain PDF 클릭")
        print("    3. 우측 상단 'My Downloads' 에서 모두 Complete 확인")
        print("    4. 이 스크립트를 다시 실행하세요\n")

    # 다운로드 가능한 항목 처리
    downloadable = [(mt, uuid_map[mt]) for mt in MT_LIST if mt in uuid_map]
    print(f"[2] 다운로드 가능한 MT: {len(downloadable)}개")
    print(f"    저장 폴더: {DOWNLOAD_DIR.absolute()}\n")

    success, failed, skipped = [], [], []

    for i, (mt, uuid) in enumerate(downloadable, 1):
        fname = f"SR_{year}_MT{mt}.pdf"
        fpath = DOWNLOAD_DIR / fname

        if fpath.exists() and fpath.stat().st_size > 5000:
            print(f"[{i:3d}/{len(downloadable)}] skip  MT{mt}")
            skipped.append(mt)
            success.append(mt)
            continue

        print(f"[{i:3d}/{len(downloadable)}] down  MT{mt} ...", end=" ", flush=True)
        content = download_pdf(session, headers, mt, uuid)

        if content:
            fpath.write_bytes(content)
            print(f"OK  {len(content)//1024}KB")
            success.append(mt)
        else:
            print("FAIL")
            failed.append(mt)

        time.sleep(DELAY)

    # 결과 요약
    print(f"\n{'='*55}")
    print(f"완료: {len(success)}개  실패: {len(failed)}개  "
          f"미요청(브라우저 필요): {len(missing_mt)}개")
    if failed:
        print(f"실패 MT: {', '.join('MT'+m for m in failed)}")
    if missing_mt:
        print(f"미요청 MT: {', '.join('MT'+m for m in missing_mt)}")
    print(f"저장 위치: {DOWNLOAD_DIR.absolute()}")


if __name__ == "__main__":
    main()
