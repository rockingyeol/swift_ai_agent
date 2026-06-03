"""
SWIFT MT 가이드북 PDF → Qdrant 인덱싱 스크립트 (경량 버전).

전체 파이프라인이 필요한 경우 setup_qdrant.py --ingest 를 사용하세요.

사용법:
    python scripts/ingest_guidebook.py --pdf SR_2025_MT101.pdf
    python scripts/ingest_guidebook.py --pdf SR_2025_MT101.pdf --recreate
    python scripts/ingest_guidebook.py --pdf SR_2025_MT101.pdf --batch 8
    python scripts/ingest_guidebook.py --pdf SR_2025_MT101.pdf --dry-run
    python scripts/ingest_guidebook.py --pdf SR_2025_MT101.pdf --show 5
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    _env = Path(__file__).resolve().parent.parent / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass


def main() -> None:
    parser = argparse.ArgumentParser(description="SWIFT MT 가이드북 PDF → Qdrant 인덱싱")
    parser.add_argument("--pdf",      required=True, help="MT 가이드북 PDF 경로")
    parser.add_argument("--msg-type", default=None,  help="MT 타입 강제 지정 (예: MT101)")
    parser.add_argument("--recreate", action="store_true", help="컬렉션 재생성")
    parser.add_argument("--batch",    type=int, default=16,  help="인덱싱 배치 크기 (기본: 16)")
    parser.add_argument("--dry-run",  action="store_true", help="청킹만 수행 (Qdrant 적재 안 함)")
    parser.add_argument("--show",     type=int, default=0,   help="처음 N개 청크 내용 출력")
    args = parser.parse_args()

    from app.rag.chunker import chunk_mt_guidebook
    from app.rag.indexer import (
        check_connection,
        create_collection,
        collection_exists,
        get_client,
        get_collection_info,
        index_chunks,
        COLLECTION,
    )

    # ── 청킹 ─────────────────────────────────────────────────────────────────
    print(f"청킹 시작: {args.pdf}")
    if not Path(args.pdf).exists():
        print(f"오류: 파일을 찾을 수 없습니다: {args.pdf}")
        sys.exit(1)

    t0     = time.perf_counter()
    chunks = chunk_mt_guidebook(args.pdf, msg_type=args.msg_type)
    elapsed = time.perf_counter() - t0
    print(f"  → 청크 {len(chunks)}개 생성 ({elapsed:.2f}s)")

    tag_dist  = Counter(c.field_tag    for c in chunks)
    type_dist = Counter(c.section_type for c in chunks)
    print(f"  섹션 유형: {dict(type_dist)}")
    print(f"  필드 태그(상위15): {dict(tag_dist.most_common(15))}")

    if args.show:
        print(f"\n처음 {args.show}개 청크:")
        for c in chunks[: args.show]:
            print(f"  [{c.section_type}] {c.msg_type} / :{c.field_tag}: / p{c.page_label}")
            print(f"    제목: {c.section_title}")
            print(f"    텍스트: {c.text[:100]}…\n")

    if args.dry_run:
        print("\n[dry-run] Qdrant 적재 건너뜀.")
        sys.exit(0)

    if not chunks:
        print("오류: 청크가 없습니다. PDF 구조를 확인하세요.")
        sys.exit(1)

    # ── Qdrant 연결 ───────────────────────────────────────────────────────────
    client = get_client()
    print("\nQdrant 연결 확인 중…", end=" ", flush=True)
    if not check_connection(client):
        print("실패")
        print("  docker compose up qdrant 로 Qdrant를 먼저 시작하세요.")
        sys.exit(1)
    print("OK")

    if not collection_exists(client) or args.recreate:
        create_collection(client, recreate=args.recreate)

    # ── 인덱싱 ────────────────────────────────────────────────────────────────
    print(f"\n인덱싱 시작 (배치={args.batch})…")
    t1 = time.perf_counter()
    index_chunks(chunks, batch_size=args.batch, client=client)
    elapsed2 = time.perf_counter() - t1

    print(f"\n인덱싱 완료 ({elapsed2:.2f}s, 컬렉션: {COLLECTION})")
    info = get_collection_info(client)
    if "error" not in info:
        print(f"적재 포인트: {info.get('points', 0)}개")


if __name__ == "__main__":
    main()
