import argparse
import requests
import time
import os
import json
import zipfile
import glob
from pathlib import Path

# ==========================================
# 1. 설정 영역 (환경에 맞게 수정)
# ==========================================

# messages JSON 파일 기본 디렉토리
MESSAGES_DIR = Path(__file__).parent / "messages"

# 다운로드 저장 루트 폴더
MX_DIR = Path(__file__).parent / "MX"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="MX PDF 다운로드 - 카테고리명 또는 JSON 파일 경로 지정",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python mx_download.py admi               # data/messages/admi_messages.json
  python mx_download.py camt               # data/messages/camt_messages.json
  python mx_download.py acmt               # data/messages/acmt_messages.json (기본)
  python mx_download.py data/messages/pacs_messages.json  # 경로 직접 지정
""",
    )
    p.add_argument(
        "target",
        nargs="?",
        default="acmt",
        help="카테고리명(예: admi) 또는 JSON 파일 경로 (기본: acmt)",
    )
    return p.parse_args()


def load_message_list(json_path: Path) -> list[str]:
    """JSON 파일에서 메시지 ID 목록을 로드합니다."""
    if not json_path.exists():
        print(f"[경고] {json_path} 파일이 없습니다.")
        return []
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    ids = [msg["id"] for msg in data.get("messages", [])]
    print(f"[설정] {json_path.name} 에서 {len(ids)}개 메시지 ID 로드됨")
    return ids


def resolve_target(target: str) -> tuple[Path, Path]:
    """
    target(카테고리명 or 파일경로)을 (json_path, download_dir)로 변환.
    """
    t = Path(target)
    # 파일 경로가 직접 주어진 경우 (.json 확장자 또는 경로 구분자 포함)
    if t.suffix == ".json" or t.parent != Path("."):
        json_path = t if t.is_absolute() else Path.cwd() / t
        # 카테고리 추출: admi_messages.json → admi
        category = json_path.stem.replace("_messages", "")
    else:
        # 카테고리명만 주어진 경우
        category = target
        json_path = MESSAGES_DIR / f"{category}_messages.json"

    download_dir = MX_DIR / category
    return json_path, download_dir

# API URLs
EXPORT_TRIGGER_URL_TEMPLATE = "https://www2.swift.com/mystandards/api/public/export/get?exportType=srv%2Fcom.swift.mystandards.core.export.pdf&subject=urn:swift:xsd:{}"
MY_DOWNLOADS_URL = "https://www2.swift.com/mystandards/api/public/myDownloads"
DOWNLOAD_BASE_URL = "https://www2.swift.com/mystandards/api/public/export/download"

# 헤더 정보
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www2.swift.com/mystandards/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": "\"Chromium\";v=\"148\", \"Google Chrome\";v=\"148\", \"Not/A)Brand\";v=\"99\"",
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": "\"Windows\""
}

# 쿠키 정보 (세션 유지)
COOKIES = {
   "mystandards-XSRF-TOKEN": "aiUvOTp6uT1c3brKzKBn0gAAAA4",
    "afiliation": "grp%2F9923",
    "_evga_2f78": "{%22uuid%22:%22bc3a31844a924174%22}",
    "_gcl_au": "1.1.1086207842.1774277239",
    "_fbp": "fb.1.1774277239140.955094552813949220",
    "_mkto_trk": "id:327-OJP-531&token:_mch-swift.com-ec89f762e1991e1270b16db55b5bb8fd",
    "_twpid": "tw.1774277239195.131939652471084326",
    "_sfid_51c1": "{%22anonymousId%22:%22bc3a31844a924174%22%2C%22consents%22:[{%22consent%22:{%22provider%22:%22Consent%20Provider%22%2C%22purpose%22:%22Personalization%22%2C%22status%22:%22Opt%20In%22}%2C%22lastUpdateTime%22:%222026-03-23T14:47:19.131Z%22%2C%22lastSentTime%22:%222026-03-23T14:47:19.478Z%22}]}",
    "_hjSessionUser_1339453": "eyJpZCI6IjBlZWRiYjMyLTk2NmQtNTI1OC1hZjBiLWMyOTE2OGY1Yjc3ZSIsImNyZWF0ZWQiOjE3NzQyNzcyMzkzODEsImV4aXN0aW5nIjp0cnVlfQ==",
    "SdCIsRegisteredC": "001708482",
    "coveo_visitorId": "217e72dd-28c4-42b1-bfe3-267474e90fd2",
    "_pk_id.1.f951": "ac0054ea22cff6a4.1776775162.",
    "_pk_ref.1.f951": "%5B%22%22%2C%22%22%2C1777711806%2C%22https%3A%2F%2Fwww2.swift.com%2F%22%5D",
    "_ga_JXL3MCVVWL": "GS2.1.s1780321095$o6$g1$t1780322326$j55$l0$h0", 
    "_ga": "GA1.2.1057366916.1774277239",
    "SDC_COOKIE": "!PlSvnp3nahUrhK4QiXZqesKIJhsj/6TIAfzBN92eEkM2/YDchulkWUqfDl6P+7s82x0Ytv92A/zElQ==",
    "CK_SDC_REG_INFO": "",
    "OAMAuthnCookie": "6a3df367ca303ef9d504b40c80b476c5ce02ea13%7E2IeW91WkP7nzUHxTXu6bAJ0rq693TwvafyxhLC85kWLcoZuklfUJXjY6hDRB%2FXeQjBnd%2FUaeAwK7Vsge8iNJwPu8H%2BtlWVsN0nUGowkK8txvRZL%2FcrbkcVsXJA5nvrpLXFAPIM3H58Vra69hbQ37jBLsmlknyVDGr3bkmj9u2pzHLPBxxPN3rxvqJTeMVXbc9vJL1niqbOpMDXbsgjo7wertzsyJBAcX0c4WBW5ZHZZYMfpZIlnZE72kqnrPdYdE0brJqaHv6We0t8eHRby8YkHgwCXLfEKVdMe%2BadgKyYeh1jlNsF48hD%2BbhhEy7%2FYNBe0GidNH7svVtH%2B6QmcgF2Ar2HIUaB08qYRl2%2FW1Ox2vI72h6qHDIh36A1kDWLBZR57M%2BWASbSqcDsj8JdqS%2Bwb0Jud03TTNFP3kUqPuRP7%2F35oPrvlq1IHzg37wG3qq2dAJQF%2B6zWkWQTgXkdpVBIjsrZ6cBQ1TPly0D5VFyyqdjwmM78wBQ%2BHci%2FuL1YBwDajeYke3YlENFcai4vXBU5HGGuUuBq4hPBGl30Wi759575kwO1%2FWEcxjxAVJMfyaK9eeWiYPE1ghIM8c0iwew4CDRysqDCPMvRgMNZCQM6NBuVtZjBudJ0zSDw%2BKPiYhm21khYtWESWKejDR4DJSCRxZFA9g%2BuRnDZGCgTBtdaDr5nq%2Ffse9PGg%2Bfy8OPO85",
    "XSRF-TOKEN-SDC-GLOBAL": "0aa9e953-7fd4-44b1-9144-ab75e8e581c7",
    "_gid": "GA1.2.718513119.1780821811",
    "bm_mi": "2013F4E59964AF43D64664C5A6B723F0~YAAQBur7yzTzgIieAQAAcB1BogAhcidtQqrC3O+fopRvF6m6/ZSOVZud78Jq7COPh1e/J1nuGp9J+DGlRJm77eZv/GmwvIQGLp4YEp6GuhGv0E+9ECPwLZhlfIja8/U0f3TibNptrC73B7KDQ37cvTdN5qhEOiWAKR/kFT6VT9V6uQ0brEej1Nygttmd7A3jpfdCxU4lVr+/3lYKDPQSoCsGysw8TisfX5uiRFrkjg/qv57wx1PQZVx4oUR+oBQ5MMGC5dQ10D2FyfjtH1r76NcHVSkaSbiY2cU1Ak/FB4eiECQCE0frBmG/Gyo2~1",
    "ak_bmsc": "81EBC03ACFE1C9CD8672DA5492BCA454~000000000000000000000000000000~YAAQZOn7yycvQ6KeAQAA2jxBogAGHQ8nrUc9D1JLCdOSlal53hdW2PSJxJNlMi1hCEhN7nMSuMeptjLoujiDoIShfkM5Nvt6M0KoE11xHZE0jExBKR1Q9eWIayVY3UIgLNP/TJkNc0wY7btokRQYrd9J4rmBGpPTy6FsWxgxo2jBH9T8TVL71xliK6X556PZwLkxbadHFrwvw83hbMt431rjaI0MDxWWsPcord+H9/wtGE2xAw0OuwwUbGmgmpE7lQpJCRh2N14vHvnPsGL+jzLkMDROO/APPY8zitYZdMXn2Ln3eEuZ/qQ7h5oXmrlBzbky+Vk0lPehmS2lxFYAJSQ1ZYM4luxCkyJVoxSdVhyNZPIUGZu7x7I95mrWtTSenwv6ER51g4En/KWvTTuHkPwAzGr+wNFt7cKVlRw8VQ9F5h9AgyPpMyFxAaOwVARH7qWUXWZZ95q36GWvtOhhNKWUCFd3fPg=",
    "_abck": "9E97D9ACD58CAED3D04D0C523C664D23~0~YAAQXOn7y3O86KCeAQAATZpBohBz2kC+UTLJKoH8rm078q3XVolXXMbJLUjby3oePQLlss/w4zI/esP20edEEPPRqrmvV2z0DVioGRurEBIpen2a2/9DHjXXkLDg8fE9sSyHFkT3KT2drDS/QoN/9ZIEHBQN3uDA5gA6rpyqUi5gg42AbmbLXSniRrARHNGAATKpexDj1cBZR0tzj/kfg59DibSX6Rn8E11GA4Vn20hiTvsMeIzaeAVNDWhd0A1aVYHIOUKsxMV9MWV2Iz6U1y3lrw9biBA/psSNN4A0DgL+I7CpKy7H16CMb02YS1+MJstMCkL3MSXg9JsB5T6jtmpkyQWoLEdwmpRmH1vjCVWUhjOVLnyzFf+014zoTwhHyytpf0Qpoz0+1AHQu4HiERLp2UVk5tJ7wymlqRB46UsytdBVXNCuZCMoBDRqjvzK1HoXSqzHEYzWzxmJl9LAKThuFE9wdQ1/mfmKzNhVrcM+I1azkcoeIQvNejL8eIGY/Xn2elpGhlHwGp/Hce2J0956kfGLSzWL+HcxKvdDN4dZffDgYT87E04xAdNrDUlNZudQLAa16qJc8Or7EMwpP3rWJX20gWerBf4lAdyEEsmEC2llEZdu3pEV5TzXMxtABzT5mFSChn6gTZ70y68GhPBfrbdqG/gMRHDzx1u1JXO9rfBGSpSIEDNVXLAg7IW6aPb52jOC/GZcGVeGJC9ERZmQ2WqK2jafUP2G/WDiSEn5CwUw9ouyjKxXKL7SnJoWYF5t1NH9jvJW7bB2h/UDJMOR5aa31zWF5GmshknX1AgsWAYI8WT9Zt/te1/4nrwl158ZSpY=~-1~-1~-1~AAQAAAAF%2f%2f%2f%2f%2f9xRRNGE8iVTF4Cvif3HIqbX+IljeKRgJWVzzI5IDfkXJIyTe37LGCtztY0%2fW1qCFTAwLUriALe3p%2fpf6TbEm1zJ1Li0%2f9iJxZtX~-1",
    "OAMAuthnHintCookie": "1",
    "BCSI-AC-5cb7874debea5fd7": "39053D6E00000004UK+RL1peCJtS/l4uZWrROTSjB0r0AAAABAAAAIwPOQBAOAAAAAAAAEGsAgAHAAAAZGVmYXVsdA==",
    "TS01cfc854": "010d79338303c261061a5e5b7921125b4beb36cb7c518bc16edbe95cdc9307ff4a6fa7ea2154b3d74954b6ca408a830338640fc217",
    "TS0130a637": "010d793383157240b337dc0e7c1451bae48eb16a07518bc16edbe95cdc9307ff4a6fa7ea21822d19ed3896f8963d8d6bece87c7338919e0abb2600a690b761d5e47dcd63e3",
    "bm_sz": "658993D9BFC39D83D0CEEF6017F9AB8B~YAAQBur7y+9ShoieAQAA4LtkogA+rbazsXKdfA2ViKzxDV0ZI19R2wTzm1EGCF0dDAG2Gn0ArQs+4NZLpsUWLaai1j9L2UKxeu5COVyjklOLnR1/ZM+za5cKfsChtuTmpBC3XjtCuaBhATszfd0ANcDGuXmXMs3ZNybHuxubAhcV0PRv0GOEW2MnuMrFSzKI26pZ/fhaXDqHLAX9rk2g7siAX8/MuVzt8LdSczJH+gX2wjrA/9pC6Df3kL2Aqk1AgdicpGWnWBMBrNUagx5oQlldhZFyXRC7bBqgXiYmpCsizk3672bktuxJzUBQ6o9GzW+0jO25QjGx0JjmfyJamS8glaRgfFJO3RAsotGRJL3SxIxNWwIkv7TRS0FrMywGvUNGKya0ncHXmQmRmkd/r5x+BJY8oNwjSZoO1Mk350I99ZlsVhY3LsQ40OUIo/xDUx4flCWxP7JKOAANzjS/LarKHRsW7BCaemjJIFVA9gV3XshLzM1CrPkK3HKWgXlpjWXsRYufONna8FMINFBqnsukP4XP~4539440~3551544",
    "bm_sv": "1B19B73B01D44E31DA73D561ABD17B9D~YAAQw+n7y1eWrYmeAQAAqvVrogBTAjvf2YEpeciqL+GGNLrBjYZEk45gGNO/GGbSnnGwKtrgnP70dRPjeDGPJJ/2eC5lePZHo+wivyc3r4XkboDj9F8a4YV6r8Az4lF5iN8CCuFuv7Ea1FthYj/fCUKq+5TUfq+gSasfckUcKivhIimOC4jMWG4RNnIrXx6gx6ejoY115CoR8pg9w1dJArnmq8Ec27V7KdgsHbU+fv22ucztdHNfrgL2puE1cA6tbzxNQI6kogc=~1"
}


# ==========================================
# 2. 핵심 함수 정의
# ==========================================

def check_downloads(session):
    """현재 다운로드 큐의 상태 목록을 가져옵니다."""
    try:
        response = session.get(MY_DOWNLOADS_URL)
        if response.ok:
            return response.json()
        return None
    except Exception as e:
        print(f"다운로드 목록 조회 중 오류 발생: {e}")
        return None

def trigger_export(session, message_id):
    """서버에 파일 생성을 요청하되, 에러 발생 시 스크립트가 멈추지 않도록 예외 처리합니다."""
    url = EXPORT_TRIGGER_URL_TEMPLATE.format(message_id)
    try:
        response = session.get(url)
        if response.ok:
            print(f"[OK] [{message_id}] 파일 생성 요청 완료")
        else:
            print(f"[WARN] [{message_id}] 서버 응답 이상 (상태 코드: {response.status_code})")
    except requests.exceptions.RequestException as e:
        print(f"[ERR] [{message_id}] 네트워크 통신 에러 발생: {e}")

def download_file(session, filename, uuid, download_dir: Path):
    """주어진 filename과 uuid를 이용해 실제 파일을 download_dir에 저장합니다."""
    download_dir.mkdir(parents=True, exist_ok=True)
    url      = f"{DOWNLOAD_BASE_URL}/{filename}"
    params   = {"uuid": uuid}
    filepath = download_dir / filename

    print(f"\n[다운로드 시작] {filename}")
    try:
        response = session.get(url, params=params, stream=True)
        response.raise_for_status()

        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        print(f"[OK] 다운로드 성공: {filepath}")
    except Exception as e:
        print(f"[ERR] 다운로드 실패: {filename}: {e}")

def compress_downloaded_files(download_dir: Path, zip_filename: str = None):
    """download_dir 안의 PDF 파일들을 하나의 ZIP 파일로 압축합니다."""
    if zip_filename is None:
        zip_filename = f"ISO20022_{download_dir.name}.zip"

    pdf_files = list(download_dir.glob("*.pdf"))

    if not pdf_files:
        print("\n[WARN] 압축할 PDF 파일이 없습니다.")
        return

    zip_path = download_dir / zip_filename
    print(f"\n[ZIP] 총 {len(pdf_files)}개의 파일을 '{zip_path}'로 압축 시작...")

    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file in pdf_files:
                zipf.write(file, file.name)
                print(f"  └ 압축됨: {file.name}")

        print(f"\n[OK] 압축 완료! ({zip_path})")
    except Exception as e:
        print(f"압축 중 오류 발생: {e}")


# ==========================================
# 3. 메인 실행 흐름
# ==========================================

def get_uuid_map(session, category: str = None):
    """myDownloads API에서 MX_*.pdf 파일의 UUID 맵을 가져옵니다."""
    uuid_map = {}

    # 캐시 파일 먼저 시도 (data/uuid_cache.json)
    cache_path = Path(__file__).parent / "uuid_cache.json"
    if cache_path.exists():
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
            prefix = f"MX_{category}_" if category else "MX_"
            for fname, uuid in cached.items():
                if fname.startswith(prefix) and fname.endswith(".pdf"):
                    uuid_map[fname] = uuid
            if uuid_map:
                print(f"[캐시] uuid_cache.json에서 {len(uuid_map)}개 UUID 로드")
                return uuid_map
        except Exception as e:
            print(f"[WARN] 캐시 로드 실패: {e}")

    try:
        r = session.get(MY_DOWNLOADS_URL, timeout=10)
        if not r.ok:
            print(f"[오류] myDownloads 응답 실패: {r.status_code}")
            return uuid_map
        data = r.json()
        if not isinstance(data, list):
            print(f"[오류] myDownloads 응답 형식 오류: {type(data)}")
            return uuid_map
        prefix = f"MX_{category}_" if category else "MX_"
        for d in data:
            filename = d.get("filename", "")
            uuid = d.get("uuid", "")
            status = d.get("status", "")
            if filename.startswith(prefix) and filename.endswith(".pdf") and status == "COMPLETED":
                uuid_map[filename] = uuid
        # 성공 시 캐시 저장 (기존 캐시와 병합)
        if uuid_map:
            try:
                existing = {}
                if cache_path.exists():
                    with open(cache_path, encoding="utf-8") as f:
                        existing = json.load(f)
                existing.update(uuid_map)
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(existing, f, indent=2)
            except Exception:
                pass
    except Exception as e:
        print(f"[오류] myDownloads 조회 실패: {e}")
    return uuid_map


def msg_id_to_prefix(msg_id: str) -> str:
    """acmt.034.001.06 → MX_acmt_034_001_ (버전 앞부분 prefix)"""
    parts = msg_id.replace(".", "_").rsplit("_", 1)[0]  # 마지막 버전 번호 제거
    return f"MX_{parts}_"


def msg_id_to_filename(msg_id: str) -> str:
    """acmt.001.001.08 → MX_acmt_001_001_08.pdf"""
    return "MX_" + msg_id.replace(".", "_") + ".pdf"


def trigger_export_browser_js(msg_id: str) -> str:
    """브라우저 JS fetch로 export trigger할 URL 반환"""
    return (f"/mystandards/api/public/export/get"
            f"?exportType=srv%2Fcom.swift.mystandards.service.generate.Generator%2FexportResultingMP"
            f"&objectUri=mx%2F{msg_id}")


def main():
    args = _parse_args()
    json_path, DOWNLOAD_DIR = resolve_target(args.target)

    MESSAGE_LIST = load_message_list(json_path)
    if not MESSAGE_LIST:
        print(f"[오류] 메시지 목록이 비어있습니다: {json_path}")
        return

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[설정] 저장 폴더: {DOWNLOAD_DIR.absolute()}")
    print(f"[설정] 다운로드 대상: {len(MESSAGE_LIST)}개 메시지 (모든 버전 포함)\n")

    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.update(COOKIES)

    # json_path에서 카테고리 추출
    category = json_path.stem.replace("_messages", "")
    print("=== 1단계: myDownloads에서 UUID 맵 조회 ===")
    uuid_map = get_uuid_map(session, category)
    print(f"큐에서 MX_{category}_*.pdf COMPLETED 파일 {len(uuid_map)}개 발견\n")

    print("=== 2단계: 모든 버전 파일 다운로드 ===")
    downloaded, skipped, missing = 0, 0, []

    # MESSAGE_LIST 기준으로 순차 처리 (이미 있으면 skip, uuid_map에 있으면 download)
    for msg_id in MESSAGE_LIST:
        filename = msg_id_to_filename(msg_id)
        filepath = DOWNLOAD_DIR / filename

        if filepath.exists():
            # 이미 디스크에 있으면 건너뜀
            print(f"[SKIP] 이미 존재: {filename}")
            skipped += 1
        elif filename in uuid_map:
            # 큐에 UUID가 있으면 다운로드
            download_file(session, filename, uuid_map[filename], DOWNLOAD_DIR)
            downloaded += 1
        else:
            # 디스크에도 없고 큐에도 없음
            missing.append(msg_id)

    print(f"\n[결과] 다운로드: {downloaded}개 | 기존 파일 스킵: {skipped}개 | 큐 없음: {len(missing)}개")

    if missing:
        print("\n[큐에 없는 메시지 - 브라우저에서 Export 필요]")
        for mid in missing:
            print(f"  - {mid}  ->  {msg_id_to_filename(mid)}")
        print(f"\n브라우저 콘솔(F12)에서 아래 JS를 실행하면 자동 Export 요청됩니다:")
        print("(async () => {")
        for mid in missing:
            url = trigger_export_browser_js(mid)
            print(f"  await fetch('{url}', {{credentials:'include'}});")
        print("})();")

    print("\n=== 3단계: 다운로드 파일 압축 ===")
    compress_downloaded_files(DOWNLOAD_DIR)
    print("\n작업 완료.")

if __name__ == "__main__":
    main()