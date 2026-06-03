"""
Qdrant 컬렉션 설정 및 MT 가이드북 PDF 인제스트 스크립트.

환경 변수 (.env 또는 시스템 환경):
  QDRANT_URL        Qdrant 서버 주소     (기본: http://localhost:6333)
  QDRANT_API_KEY    Qdrant API 키        (선택)
  QDRANT_COLLECTION 컬렉션 이름          (기본: swift_guidebook)
  GUIDEBOOK_PDF     가이드북 PDF 경로    (--ingest 시 필수)
  EMBED_MODEL       BGE-M3 모델 경로    (기본: BAAI/bge-m3)

사용법:
  # 1. Qdrant 연결 확인만
  python scripts/setup_qdrant.py --check

  # 2. 컬렉션 생성만 (Dense+Sparse 스키마)
  python scripts/setup_qdrant.py

  # 3. MT101 PDF 청킹 결과 미리보기 (Qdrant 적재 안 함)
  python scripts/setup_qdrant.py --ingest --dry-run --show 5

  # 4. PDF 파싱 + Qdrant 적재 (전체 파이프라인)
  python scripts/setup_qdrant.py --ingest

  # 5. 기존 컬렉션 삭제 후 재생성 + 재적재
  python scripts/setup_qdrant.py --ingest --recreate

  # 6. 컬렉션 상태 조회
  python scripts/setup_qdrant.py --info
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from pathlib import Path

# ── 프로젝트 루트를 sys.path에 추가 ─────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── .env 로드 (python-dotenv 있을 때만) ─────────────────────────────────────
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
        print(f"[설정] .env 로드: {_env_path}")
except ImportError:
    pass  # python-dotenv 없어도 시스템 환경변수로 동작


# ---------------------------------------------------------------------------
# CLI 인수 파싱
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Qdrant SWIFT 가이드북 컬렉션 설정 및 인제스트 (MT + MX + CBPR+ SR2026)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # ── 공통 ─────────────────────────────────────────────────────────────────
    p.add_argument("--url",        default=None,
                   help="Qdrant URL (기본: QDRANT_URL 환경변수)")
    p.add_argument("--api-key",    default=None,
                   help="Qdrant API Key (기본: QDRANT_API_KEY 환경변수)")
    p.add_argument("--recreate",   action="store_true",
                   help="기존 컬렉션 삭제 후 재생성")
    p.add_argument("--dry-run",    action="store_true",
                   help="청킹 결과만 미리보기 (Qdrant 적재 안 함)")
    p.add_argument("--batch",      type=int, default=16,
                   help="인덱싱 배치 크기 (기본: 16; CPU 환경에서는 8 권장)")
    p.add_argument("--check",      action="store_true",
                   help="Qdrant 연결 확인만 수행")
    p.add_argument("--info",       action="store_true",
                   help="컬렉션 상태 출력")
    p.add_argument("--show",       type=int, default=0,
                   help="청킹 결과 N개 미리보기 (--ingest 또는 --dry-run 시 유효)")

    # ── MT 가이드북 (기존) ───────────────────────────────────────────────────
    p.add_argument("--ingest",     action="store_true",
                   help="MT 가이드북 PDF 파싱 및 Qdrant 적재 실행")
    p.add_argument("--pdf",        default=None,
                   help="MT 가이드북 PDF 경로 (기본: GUIDEBOOK_PDF 환경변수)")
    p.add_argument("--msg-type",   default=None,
                   help="MT 타입 강제 지정 (예: MT101; 기본: 파일명 자동 추론)")

    # ── MX 가이드북 (신규) ───────────────────────────────────────────────────
    p.add_argument("--ingest-mx",  action="store_true",
                   help="MX (ISO 20022) 가이드북 PDF 파싱 및 Qdrant 적재 실행")
    p.add_argument("--mx-pdf",     default=None,
                   help="MX 가이드북 PDF 경로 (예: MX_pacs_008_001_14.pdf; "
                        "기본: MX_GUIDEBOOK_PDF 환경변수)")
    p.add_argument("--mx-msg-type", default=None,
                   help="MX 타입 강제 지정 (예: pacs.008; 기본: 파일명 자동 추론)")

    # ── CBPR+ SR2026 CSV (신규) ──────────────────────────────────────────────
    p.add_argument("--ingest-sr2026", action="store_true",
                   help="CBPR+ SR2026 XPath 변경 사항 CSV 파싱 및 Qdrant 적재 실행")
    p.add_argument("--sr2026-csv",  default=None,
                   help="CBPR+ SR2026 CSV 경로 (예: CBPR+_SR2026_Impacted_Xpaths_v2.0.csv; "
                        "기본: SR2026_CSV 환경변수)")

    return p.parse_args()


# ---------------------------------------------------------------------------
# 유틸: 섹션 구분선 출력
# ---------------------------------------------------------------------------

def _banner(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()

    # ── 환경변수 override ────────────────────────────────────────────────────
    if args.url:
        os.environ["QDRANT_URL"] = args.url
    if args.api_key:
        os.environ["QDRANT_API_KEY"] = args.api_key

    # ── 모듈 임포트 (환경변수 override 이후) ────────────────────────────────
    from app.rag.indexer import (
        COLLECTION,
        QDRANT_URL,
        check_connection,
        create_collection,
        get_client,
        get_collection_info,
    )

    _banner("Qdrant SWIFT 가이드북 컬렉션 설정 (MT + MX + CBPR+ SR2026)")
    print(f"  Qdrant URL : {QDRANT_URL}")
    print(f"  컬렉션명   : {COLLECTION}")
    if os.getenv("QDRANT_API_KEY"):
        print(f"  API Key    : ***")

    # ── 1. Qdrant 연결 확인 (dry-run 시 건너뜀) ─────────────────────────────
    _banner("1. Qdrant 연결 확인")
    client = get_client()
    if args.dry_run:
        print("  [dry-run] 연결 확인 건너뜀.")
    else:
        print("  연결 중…", end=" ", flush=True)
        if not check_connection(client):
            print("실패 ✗")
            print(f"\n  Qdrant가 {QDRANT_URL} 에서 실행 중인지 확인하세요.")
            print("  docker compose up qdrant  로 시작할 수 있습니다.")
            sys.exit(1)
        print("OK ✓")

    if args.check:
        print("\n  --check 완료. 종료.")
        sys.exit(0)

    # ── 2. 컬렉션 상태 조회 ─────────────────────────────────────────────────
    if args.info:
        _banner("컬렉션 상태")
        info = get_collection_info(client)
        if "error" in info:
            print(f"  오류: {info['error']}")
        else:
            for k, v in info.items():
                print(f"  {k:<20}: {v}")
        sys.exit(0)

    # ── 3. 컬렉션 생성 (dry-run 시 건너뜀) ─────────────────────────────────
    _banner("2. 컬렉션 생성 (Dense=1024/Cosine + Sparse=BGE-M3)")
    if args.dry_run:
        print("  [dry-run] 컬렉션 생성 건너뜀.")
    else:
        create_collection(client, recreate=args.recreate)

    # 적재 작업이 전혀 없으면 여기서 종료
    any_ingest = args.ingest or args.ingest_mx or args.ingest_sr2026 or args.dry_run
    if not any_ingest:
        info = get_collection_info(client)
        if "error" not in info:
            print(f"\n  포인트 수 : {info.get('points', 0)}")
            print(f"  상태      : {info.get('status', '?')}")
        print(
            "\n  컬렉션 설정 완료.\n"
            "  MT 가이드북    : --ingest --pdf <경로>\n"
            "  MX 가이드북    : --ingest-mx --mx-pdf <경로>\n"
            "  SR2026 CSV     : --ingest-sr2026 --sr2026-csv <경로>\n"
        )
        sys.exit(0)

    # ── 4. MT 가이드북 인제스트 (--ingest) ──────────────────────────────────
    if args.ingest or (args.dry_run and args.pdf):
        _run_mt_ingest(args, client)

    # ── 5. MX 가이드북 인제스트 (--ingest-mx) ───────────────────────────────
    if args.ingest_mx or (args.dry_run and args.mx_pdf):
        _run_mx_ingest(args, client)

    # ── 6. CBPR+ SR2026 CSV 인제스트 (--ingest-sr2026) ──────────────────────
    if args.ingest_sr2026 or (args.dry_run and args.sr2026_csv):
        _run_sr2026_ingest(args, client)

    # dry-run 이면서 아무 소스도 없는 경우
    if args.dry_run:
        _banner("dry-run 완료")
        print("  청킹 미리보기가 완료되었습니다. Qdrant에는 아무것도 적재되지 않았습니다.")


# ---------------------------------------------------------------------------
# MT 가이드북 인제스트 (main 에서 분리)
# ---------------------------------------------------------------------------

def _run_mt_ingest(args: argparse.Namespace, client: object) -> None:
    """MT (ISO 15022) 가이드북 PDF → Qdrant 적재."""
    from app.rag.chunker import chunk_mt_guidebook
    from app.rag.indexer import (
        COLLECTION,
        collection_exists,
        create_collection,
        get_collection_info,
        index_chunks,
    )

    _banner("MT 가이드북 PDF 파싱 및 청킹")

    pdf_path = args.pdf or os.getenv("GUIDEBOOK_PDF")
    if not pdf_path:
        print(
            "\n오류: MT PDF 경로가 지정되지 않았습니다.\n"
            "  --pdf <경로> 또는 GUIDEBOOK_PDF 환경변수를 설정하세요."
        )
        sys.exit(1)

    pdf_path = str(Path(pdf_path).resolve())
    if not Path(pdf_path).exists():
        print(f"\n오류: MT PDF 파일을 찾을 수 없습니다.\n  경로: {pdf_path}")
        sys.exit(1)

    print(f"  파일: {pdf_path}")
    if args.msg_type:
        print(f"  MT 타입 (강제): {args.msg_type}")

    t0 = time.perf_counter()
    try:
        chunks = chunk_mt_guidebook(pdf_path, msg_type=args.msg_type)
    except FileNotFoundError as e:
        print(f"\n오류: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n오류: MT PDF 청킹 실패 — {type(e).__name__}: {e}")
        raise
    elapsed = time.perf_counter() - t0

    print(f"\n  청크 {len(chunks)}개 생성 완료 ({elapsed:.2f}s)")

    tag_dist  = Counter(c.field_tag    for c in chunks)
    type_dist = Counter(c.section_type for c in chunks)
    print(f"  섹션 유형: {dict(type_dist)}")
    print(f"  필드 태그 (상위 15개): {dict(tag_dist.most_common(15))}")

    if args.show > 0:
        _banner(f"MT 청크 미리보기 (처음 {args.show}개)")
        for i, c in enumerate(chunks[: args.show]):
            print(f"\n  [{i+1}] {c.msg_type} | :{c.field_tag}: | {c.section_type} | p{c.page_label}")
            print(f"       제목: {c.section_title}")
            preview = c.text[:200].replace("\n", " ")
            print(f"       텍스트: {preview}…")

    if args.dry_run:
        print("\n  [dry-run] MT Qdrant 적재 건너뜀.")
        return

    if not chunks:
        print(
            "\n오류: MT 청크가 0개입니다.\n"
            "  PDF 구조를 확인하세요. (--dry-run --show 3 으로 미리보기 가능)"
        )
        sys.exit(1)

    _banner("MT 컬렉션 준비")
    if not collection_exists(client, COLLECTION) or args.recreate:
        create_collection(client, recreate=args.recreate)
    else:
        print(f"  컬렉션 '{COLLECTION}' 확인됨.")

    _banner(f"MT Qdrant 인덱싱 (배치={args.batch})")
    print("  BGE-M3 임베딩 로드 중… (최초 실행 시 모델 다운로드 발생)")

    t1 = time.perf_counter()
    try:
        index_chunks(chunks, batch_size=args.batch, client=client)
    except RuntimeError as e:
        print(f"\n오류: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n예상치 못한 오류: {type(e).__name__}: {e}")
        raise
    elapsed2 = time.perf_counter() - t1

    _banner("MT 인덱싱 완료")
    print(f"  인덱싱 시간: {elapsed2:.2f}s")
    info = get_collection_info(client)
    if "error" not in info:
        print(f"  누적 포인트: {info.get('points', 0)}개")
        print(f"  컬렉션 상태: {info.get('status', '?')}")
    print(f"\n  MT 가이드북이 컬렉션 '{COLLECTION}' 에 적재되었습니다. ✓")


def _run_mx_ingest(args: argparse.Namespace, client: object) -> None:
    """MX (ISO 20022) 가이드북 PDF → Qdrant 적재."""
    from app.rag.chunker import chunk_mx_guidebook
    from app.rag.indexer import (
        COLLECTION,
        collection_exists,
        create_collection,
        get_collection_info,
        index_chunks,
    )

    _banner("MX 가이드북 PDF 파싱 및 청킹")

    mx_pdf = args.mx_pdf or os.getenv("MX_GUIDEBOOK_PDF")
    if not mx_pdf:
        print(
            "\n오류: MX PDF 경로가 지정되지 않았습니다.\n"
            "  --mx-pdf <경로> 또는 MX_GUIDEBOOK_PDF 환경변수를 설정하세요."
        )
        sys.exit(1)

    mx_pdf = str(Path(mx_pdf).resolve())
    if not Path(mx_pdf).exists():
        print(f"\n오류: MX PDF 파일을 찾을 수 없습니다.\n  경로: {mx_pdf}")
        sys.exit(1)

    print(f"  파일: {mx_pdf}")
    if args.mx_msg_type:
        print(f"  MX 타입 (강제): {args.mx_msg_type}")

    t0 = time.perf_counter()
    try:
        mx_chunks = chunk_mx_guidebook(mx_pdf, msg_type=args.mx_msg_type)
    except Exception as e:
        print(f"\n오류: MX PDF 청킹 실패 — {type(e).__name__}: {e}")
        raise
    elapsed = time.perf_counter() - t0

    print(f"\n  청크 {len(mx_chunks)}개 생성 완료 ({elapsed:.2f}s)")

    if mx_chunks:
        tag_dist  = Counter(c.xml_tag    for c in mx_chunks)
        path_dist = Counter(c.field_path for c in mx_chunks)
        print(f"  XML 태그 (상위 10개): {dict(tag_dist.most_common(10))}")
        print(f"  경로 샘플 (상위 5개): {list(path_dist.keys())[:5]}")

    if args.show > 0:
        _banner(f"MX 청크 미리보기 (처음 {args.show}개)")
        for i, c in enumerate(mx_chunks[: args.show]):
            print(f"\n  [{i+1}] {c.msg_type} | <{c.xml_tag}> | {c.multiplicity} | p{c.page_label}")
            print(f"       경로: {c.field_path}")
            print(f"       제약: {c.constraints}")
            preview = c.text[:200].replace("\n", " ")
            print(f"       텍스트: {preview}…")

    if args.dry_run:
        print("\n  [dry-run] MX Qdrant 적재 건너뜀.")
        return

    if not mx_chunks:
        print("\n경고: MX 청크가 0개입니다. PDF 구조를 확인하세요.")
        return

    # 컬렉션 준비
    _banner("MX 컬렉션 준비")
    if not collection_exists(client, COLLECTION) or args.recreate:
        create_collection(client, recreate=args.recreate)
    else:
        print(f"  컬렉션 '{COLLECTION}' 확인됨.")

    _banner(f"MX Qdrant 인덱싱 (배치={args.batch})")
    t1 = time.perf_counter()
    try:
        index_chunks(mx_chunks, batch_size=args.batch, client=client)
    except RuntimeError as e:
        print(f"\n오류: {e}")
        sys.exit(1)
    elapsed2 = time.perf_counter() - t1

    _banner("MX 인덱싱 완료")
    print(f"  인덱싱 시간: {elapsed2:.2f}s")
    info = get_collection_info(client)
    if "error" not in info:
        print(f"  누적 포인트: {info.get('points', 0)}개")
    print(f"\n  MX 가이드북이 컬렉션 '{COLLECTION}' 에 적재되었습니다. ✓")


def _run_sr2026_ingest(args: argparse.Namespace, client: object) -> None:
    """CBPR+ SR2026 XPath CSV → Qdrant 적재."""
    from app.rag.chunker import chunk_cbpr_sr2026_csv
    from app.rag.indexer import (
        COLLECTION,
        collection_exists,
        create_collection,
        get_collection_info,
        index_chunks,
    )

    _banner("CBPR+ SR2026 XPath CSV 파싱 및 청킹")

    csv_path = args.sr2026_csv or os.getenv("SR2026_CSV")
    if not csv_path:
        print(
            "\n오류: SR2026 CSV 경로가 지정되지 않았습니다.\n"
            "  --sr2026-csv <경로> 또는 SR2026_CSV 환경변수를 설정하세요."
        )
        sys.exit(1)

    csv_path = str(Path(csv_path).resolve())
    if not Path(csv_path).exists():
        print(f"\n오류: CSV 파일을 찾을 수 없습니다.\n  경로: {csv_path}")
        sys.exit(1)

    print(f"  파일: {csv_path}")

    t0 = time.perf_counter()
    try:
        sr_chunks = chunk_cbpr_sr2026_csv(csv_path)
    except ImportError as e:
        print(f"\n오류: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n오류: CSV 청킹 실패 — {type(e).__name__}: {e}")
        raise
    elapsed = time.perf_counter() - t0

    print(f"\n  청크 {len(sr_chunks)}개 생성 완료 ({elapsed:.2f}s)")

    if sr_chunks:
        cr_dist  = Counter(c.cr_id    for c in sr_chunks)
        msg_dist = Counter(c.msg_type for c in sr_chunks)
        print(f"  CR 분포        : {dict(cr_dist.most_common(10))}")
        print(f"  메시지 타입 분포: {dict(msg_dist.most_common(15))}")

    if args.show > 0:
        _banner(f"SR2026 청크 미리보기 (처음 {args.show}개)")
        for i, c in enumerate(sr_chunks[: args.show]):
            print(f"\n  [{i+1}] {c.cr_id} | {c.msg_type} | {c.usage_guideline[:60]}")
            print(f"       XPath: {c.field_path}")
            print(f"       제목:  {c.cr_title}")

    if args.dry_run:
        print("\n  [dry-run] SR2026 Qdrant 적재 건너뜀.")
        return

    if not sr_chunks:
        print("\n경고: SR2026 청크가 0개입니다. CSV 구조를 확인하세요.")
        return

    # 컬렉션 준비
    _banner("SR2026 컬렉션 준비")
    if not collection_exists(client, COLLECTION) or args.recreate:
        create_collection(client, recreate=args.recreate)
    else:
        print(f"  컬렉션 '{COLLECTION}' 확인됨.")

    _banner(f"SR2026 Qdrant 인덱싱 (배치={args.batch})")
    t1 = time.perf_counter()
    try:
        index_chunks(sr_chunks, batch_size=args.batch, client=client)
    except RuntimeError as e:
        print(f"\n오류: {e}")
        sys.exit(1)
    elapsed2 = time.perf_counter() - t1

    _banner("SR2026 인덱싱 완료")
    print(f"  인덱싱 시간: {elapsed2:.2f}s")
    info = get_collection_info(client)
    if "error" not in info:
        print(f"  누적 포인트: {info.get('points', 0)}개")
    print(f"\n  SR2026 XPath 변경 사항이 컬렉션 '{COLLECTION}' 에 적재되었습니다. ✓")


if __name__ == "__main__":
    main()
