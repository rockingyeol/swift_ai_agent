"""
MT 가이드북 고도화 인제스트 스크립트 (프로덕션급).

주요 기능:
  - 단건(--file) / 다건(--dir) 처리
  - TOC 기반 동적 대분류 섹션 감지
  - 섹션 유형별 2차 청킹 (Field Specs / Network Rules / Generic)
  - 중복 방지(Dedup): 파일 처리 전 source_file 기반 기존 포인트 삭제
    · 동일 파일 재적재 → 기존 청크 삭제 후 신규 청크 upsert
    · --no-dedup 옵션으로 비활성화 가능
    · --recreate 는 컬렉션 전체 초기화 (dedup 불필요)
  - doc_category / section / field_tag / rule_id / sequence /
    source_file 메타데이터 완전 지원
  - 컨텍스트 보완 헤더 (LLM 프롬프트 상위 문맥 보존)

사용법:
  # 단건
  python scripts/ingest_mt_all.py --file data/MT/Category1/SR_2025_MT101.pdf

  # 다건 (디렉토리 재귀)
  python scripts/ingest_mt_all.py --dir data/MT

  # 특정 카테고리만
  python scripts/ingest_mt_all.py --dir data/MT --category Category1

  # 청킹 미리보기 (Qdrant 적재/삭제 안 함)
  python scripts/ingest_mt_all.py --dir data/MT --dry-run --show 3

  # 컬렉션 전체 초기화 후 재적재
  python scripts/ingest_mt_all.py --dir data/MT --recreate

  # 중복 방지 없이 단순 upsert만
  python scripts/ingest_mt_all.py --dir data/MT --no-dedup
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf-8-sig"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    _env = Path(__file__).resolve().parent.parent / ".env"
    if _env.exists():
        load_dotenv(_env)
        print(f"[설정] .env 로드: {_env}")
except ImportError:
    pass


# ===========================================================================
# Enriched 모델 import
# ===========================================================================

from app.rag.chunker import MTEnrichedChunk, make_chunk_id  # noqa: E402


# ===========================================================================
# TOC 섹션 패턴
# ===========================================================================

_TOC_SECTION_PATTERNS: dict[str, str] = {
    "Scope":                   r"Scope",
    "Format_Specifications":   r"Format\s+Specifications",
    "Network_Validated_Rules": r"Network\s+Validated\s+Rules",
    "Usage_Rules":             r"Usage\s+Rules",
    "Field_Specifications":    r"Field\s+Specifications",
    "Mapping":                 r"Mapping",
    "Operating_Procedures":    r"Operating\s+Procedures",
    "Operational_Rules":       r"Operational\s+Rules(?:\s+and\s+Checklist)?",
    "Market_Practice_Rules":   r"Market\s+Practice(?:\s+Rules)?",
    "Examples":                r"Examples?",
}

_RE_FIELD_HDR = re.compile(
    r"^(\d+)\.\s+(Field\s+(\d{1,2}[A-Za-z]?)\s*:[^\n]*)",
    re.MULTILINE,
)
_RE_C_RULE_HDR  = re.compile(r"^(C\d+)\b", re.MULTILINE)
_RE_TOC_ENTRY   = re.compile(r"\.{3,}\s*\d+\s*$")
_FIELD_SUBSECTIONS = frozenset([
    "FORMAT", "PRESENCE", "DEFINITION", "CODES",
    "NETWORK VALIDATED RULES", "USAGE RULES", "EXAMPLES",
])
_RE_ARTIFACT = re.compile(
    r"^(?:Standards\s+MT\s+\w+\s+\d{4}"
    r"|\d+\s+Message\s+Reference\s+Guide[^\n]*"
    r"|Message\s+Reference\s+Guide\s*-\s*MT\s*\d{3}[^\n]*"
    r")\s*$",
    re.MULTILINE | re.IGNORECASE,
)


# ===========================================================================
# MT 파서
# ===========================================================================

class MTParser:
    """MT 가이드북 PDF → MTEnrichedChunk 목록 변환."""

    def __init__(self, pdf_path: str,
                 msg_type: Optional[str] = None,
                 category: Optional[str] = None) -> None:
        import fitz
        self.pdf_path = Path(pdf_path)
        self._fitz = fitz

        if msg_type is None:
            stem = self.pdf_path.stem.upper()
            m = re.search(r"MT\s*(\d{3})", stem)
            msg_type = f"MT{m.group(1)}" if m else "MT101"
        self.msg_type = msg_type
        self.category = category or self.pdf_path.parent.name
        self.source_file = self.pdf_path.stem  # 중복 방지 키

        self._pages = self._extract_pages()
        self._full_text, self._page_map = self._build_corpus()
        self._full_text = self._clean_text(self._full_text)

    def _extract_pages(self):
        doc = self._fitz.open(str(self.pdf_path))
        return [(i + 1, doc[i].get_text("text"))
                for i in range(len(doc)) if doc[i].get_text("text").strip()]

    def _build_corpus(self):
        parts, page_map, offset = [], [], 0
        for pn, text in self._pages:
            page_map.append((pn, offset))
            parts.append(text)
            offset += len(text)
        return "".join(parts), page_map

    def _clean_text(self, text: str) -> str:
        text = _RE_ARTIFACT.sub("", text)
        return re.sub(r"^\s*\d{1,3}\s*$", "", text, flags=re.MULTILINE)

    def _page_at(self, offset: int) -> int:
        current = self._page_map[0][0] if self._page_map else 1
        for pn, start in self._page_map:
            if start > offset:
                break
            current = pn
        return current

    # ── §2 TOC 동적 섹션 감지 ──────────────────────────────────────────────

    def extract_toc_sections(self) -> list[str]:
        toc_m = re.search(r'(?:Table\s+of\s+Contents?|CONTENTS?)\s*\n',
                          self._full_text, re.IGNORECASE)
        zone = (self._full_text[toc_m.start(): toc_m.start() + 3000]
                if toc_m else self._full_text[:3000])
        found = [k for k, p in _TOC_SECTION_PATTERNS.items()
                 if re.search(p, zone, re.IGNORECASE)]
        return found if found else list(_TOC_SECTION_PATTERNS.keys())

    # ── §4 sequence 감지 ──────────────────────────────────────────────────

    def _detect_sequence(self, text: str) -> str:
        upper = text.upper()
        in_a = bool(re.search(r'(?:MANDATORY|OPTIONAL)\s+IN\s+(?:MANDATORY\s+)?SEQUENCE\s+A', upper))
        in_b = bool(re.search(r'(?:MANDATORY|OPTIONAL)\s+IN\s+(?:MANDATORY\s+)?SEQUENCE\s+B', upper))
        if in_a and not in_b: return "A"
        if in_b and not in_a: return "B"
        if in_a and in_b:     return "none"
        has_a = bool(re.search(r'\bSEQUENCE\s+A\b', upper))
        has_b = bool(re.search(r'\bSEQUENCE\s+B\b', upper))
        if has_a and not has_b: return "A"
        if has_b and not has_a: return "B"
        return "none"

    # ── §3 1차 섹션 분할 ──────────────────────────────────────────────────

    def _split_top_sections(self, toc_sections: list[str]) -> list[dict]:
        patterns = [(k, _TOC_SECTION_PATTERNS.get(k, re.escape(k.replace("_", " "))))
                    for k in toc_sections]
        combined = re.compile(
            "|".join(f"(?P<S{i}>^(MT\\s*\\d{{3}}\\s*{p})\\s*$)"
                     for i, (_, p) in enumerate(patterns)),
            re.MULTILINE | re.IGNORECASE,
        ) if patterns else None

        boundaries = []
        if combined:
            for m in combined.finditer(self._full_text):
                title = m.group(0).strip()
                if _RE_TOC_ENTRY.search(title): continue
                for i, (key, _) in enumerate(patterns):
                    try:
                        if m.group(f"S{i}") is not None:
                            boundaries.append((m.start(), key, title))
                            break
                    except IndexError:
                        pass
        boundaries.sort(key=lambda x: x[0])

        if not boundaries:
            return [{"key": "Full_Document", "title": f"{self.msg_type} Full Document",
                     "start": 0, "end": len(self._full_text), "page": self._page_at(0)}]

        return [{"key": k, "title": t, "start": s,
                 "end": boundaries[i + 1][0] if i + 1 < len(boundaries) else len(self._full_text),
                 "page": self._page_at(s)}
                for i, (s, k, t) in enumerate(boundaries)]

    # ── §3 2차 청킹: Field Specifications ────────────────────────────────

    def _chunk_field_specs(self, text: str, section_key: str,
                           base_page: int, sec_start: int) -> list[MTEnrichedChunk]:
        chunks, seen = [], set()
        boundaries = []
        for m in _RE_FIELD_HDR.finditer(text):
            if _RE_TOC_ENTRY.search(m.group(0)): continue
            lookahead = text[m.end(): m.end() + 400]
            if not any(s in lookahead for s in _FIELD_SUBSECTIONS): continue
            boundaries.append((m.start(), m.group(2).strip(), m.group(3).strip()))

        for i, (start, title, tag) in enumerate(boundaries):
            end  = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
            body = re.sub(r"\n{3,}", "\n\n", text[start:end].strip())
            if not body or len(body) < 20: continue
            page = self._page_at(sec_start + start)
            seq  = self._detect_sequence(body)
            cid  = make_chunk_id(self.msg_type, tag, title[:40], str(page))
            if cid in seen: continue
            seen.add(cid)
            chunks.append(MTEnrichedChunk(
                chunk_id=cid, msg_type=self.msg_type, field_tag=tag,
                doc_type="guidebook", doc_category="MT",
                page_label=page, category=self.category,
                section_title=section_key, section_type="field_spec",
                sequence=seq, source_file=self.source_file, text=body,
            ))

        if not chunks:
            body = re.sub(r"\n{3,}", "\n\n", text.strip())
            if body and len(body) >= 20:
                cid = make_chunk_id(self.msg_type, "SYSTEM", section_key, str(base_page))
                if cid not in seen:
                    chunks.append(MTEnrichedChunk(
                        chunk_id=cid, msg_type=self.msg_type, field_tag="SYSTEM",
                        doc_type="guidebook", doc_category="MT",
                        page_label=base_page, category=self.category,
                        section_title=section_key, section_type="field_spec",
                        sequence="none", source_file=self.source_file, text=body,
                    ))
        return chunks

    # ── §3 2차 청킹: Network Validated Rules ─────────────────────────────

    def _chunk_network_rules(self, text: str, section_key: str,
                              base_page: int, sec_start: int) -> list[MTEnrichedChunk]:
        chunks, seen = [], set()
        boundaries = [(m.start(), m.group(1)) for m in _RE_C_RULE_HDR.finditer(text)]

        if not boundaries:
            body = re.sub(r"\n{3,}", "\n\n", text.strip())
            if body and len(body) >= 20:
                cid = make_chunk_id(self.msg_type, "SYSTEM", section_key, str(base_page))
                chunks.append(MTEnrichedChunk(
                    chunk_id=cid, msg_type=self.msg_type, field_tag="SYSTEM",
                    doc_type="guidebook", doc_category="MT",
                    page_label=base_page, category=self.category,
                    section_title=section_key, section_type="message_rule",
                    sequence="none", source_file=self.source_file, text=body,
                ))
            return chunks

        for i, (start, rule_id) in enumerate(boundaries):
            end  = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
            body = re.sub(r"\n{3,}", "\n\n", text[start:end].strip())
            if not body or len(body) < 10: continue
            page = self._page_at(sec_start + start)
            cid  = make_chunk_id(self.msg_type, rule_id, section_key, str(page))
            if cid in seen: continue
            seen.add(cid)
            chunks.append(MTEnrichedChunk(
                chunk_id=cid, msg_type=self.msg_type, field_tag="SYSTEM",
                rule_id=rule_id, doc_type="guidebook", doc_category="MT",
                page_label=page, category=self.category,
                section_title=section_key, section_type="message_rule",
                sequence="none", source_file=self.source_file, text=body,
            ))
        return chunks

    # ── §3 2차 청킹: 기타 ─────────────────────────────────────────────────

    def _chunk_generic(self, text: str, section_key: str, section_type: str,
                       base_page: int, sec_start: int) -> list[MTEnrichedChunk]:
        MAX_CHUNK = 1200
        chunks, seen = [], set()
        current_parts, current_len, offset_cursor = [], 0, 0

        def _flush(parts, off):
            body = re.sub(r"\n{3,}", "\n\n", "\n\n".join(parts).strip())
            if not body or len(body) < 30: return
            page = self._page_at(sec_start + off)
            seq  = self._detect_sequence(body)
            cid  = make_chunk_id(self.msg_type, "SYSTEM",
                                  body[:40].replace("\n", " "), str(page))
            if cid in seen: return
            seen.add(cid)
            chunks.append(MTEnrichedChunk(
                chunk_id=cid, msg_type=self.msg_type, field_tag="SYSTEM",
                doc_type="guidebook", doc_category="MT",
                page_label=page, category=self.category,
                section_title=section_key, section_type=section_type,
                sequence=seq, source_file=self.source_file, text=body,
            ))

        for para in re.split(r"\n{2,}", text.strip()):
            if not para.strip(): continue
            if current_len + len(para) > MAX_CHUNK and current_parts:
                _flush(current_parts, offset_cursor)
                current_parts, current_len = [], 0
            current_parts.append(para)
            current_len += len(para)
            offset_cursor += len(para) + 2
        if current_parts:
            _flush(current_parts, offset_cursor)
        return chunks

    # ── 섹션 유형 분류 ──────────────────────────────────────────────────────

    @staticmethod
    def _section_type(key: str) -> str:
        k = key.lower()
        if "field_spec"  in k: return "field_spec"
        if "network"     in k: return "message_rule"
        if "usage_rule"  in k: return "usage_rule"
        if "example"     in k: return "example"
        return "other"

    # ── 공개 API ─────────────────────────────────────────────────────────────

    def parse(self) -> list[MTEnrichedChunk]:
        if not self._pages:
            return []
        toc    = self.extract_toc_sections()
        secs   = self._split_top_sections(toc)
        all_chunks: list[MTEnrichedChunk] = []
        for sec in secs:
            key   = sec["key"]
            body  = self._full_text[sec["start"]: sec["end"]]
            pg    = sec["page"]
            start = sec["start"]
            stype = self._section_type(key)
            if stype == "field_spec":
                all_chunks.extend(self._chunk_field_specs(body, key, pg, start))
            elif stype == "message_rule":
                all_chunks.extend(self._chunk_network_rules(body, key, pg, start))
            else:
                all_chunks.extend(self._chunk_generic(body, key, stype, pg, start))
        return all_chunks


# ===========================================================================
# Qdrant Dedup 헬퍼
# ===========================================================================

def _count_existing(client, collection: str,
                    source_file: str, category: str) -> int:
    """source_file + category 로 기존 포인트 수를 반환한다."""
    try:
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        f = Filter(must=[
            FieldCondition(key="source_file", match=MatchValue(value=source_file)),
            FieldCondition(key="category",    match=MatchValue(value=category)),
        ])
        return client.count(collection_name=collection, count_filter=f, exact=True).count
    except Exception:
        return -1


def _delete_file_chunks(client, collection: str,
                        source_file: str, category: str) -> int:
    """
    source_file + category 조건의 기존 포인트를 삭제한다.

    Returns:
        삭제된 포인트 수 (count 실패 시 -1)
    """
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    existing = _count_existing(client, collection, source_file, category)
    if existing == 0:
        return 0

    delete_filter = Filter(must=[
        FieldCondition(key="source_file", match=MatchValue(value=source_file)),
        FieldCondition(key="category",    match=MatchValue(value=category)),
    ])
    try:
        client.delete(collection_name=collection, points_selector=delete_filter)
    except Exception as e:
        print(f"    ⚠ 삭제 실패: {e}")
        return -1
    return existing


# ===========================================================================
# CLI
# ===========================================================================

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="MT 가이드북 PDF 청킹 및 Qdrant 적재 (단건 / 다건, Dedup 지원)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--file", default=None, metavar="PATH",
                     help="단건: PDF 파일 경로")
    src.add_argument("--dir",  default=None, metavar="DIR",
                     help="다건: MT PDF 루트 디렉토리 (기본: MT_DIR 환경변수 → data/MT)")
    p.add_argument("--category", default=None,
                   help="--dir 사용 시 특정 카테고리만")
    p.add_argument("--recreate",  action="store_true",
                   help="컬렉션 전체 삭제 후 재생성 (dedup 불필요)")
    p.add_argument("--no-dedup",  action="store_true",
                   help="중복 방지 비활성화 — 삭제 없이 Upsert만 수행")
    p.add_argument("--dry-run",   action="store_true",
                   help="청킹 미리보기 (적재/삭제 안 함)")
    p.add_argument("--show",      type=int, default=0,
                   help="청크 미리보기 N개")
    p.add_argument("--batch",     type=int, default=16,
                   help="인덱싱 배치 크기")
    p.add_argument("--url",       default=None, help="Qdrant URL")
    p.add_argument("--api-key",   default=None, help="Qdrant API Key")
    return p.parse_args()


# ===========================================================================
# PDF 목록
# ===========================================================================

def _find_pdfs(root_dir: Path, only_category: str | None) -> list[tuple[Path, str]]:
    result = []
    if not root_dir.exists():
        return result
    for cat_dir in sorted(root_dir.iterdir()):
        if not cat_dir.is_dir(): continue
        if only_category and cat_dir.name != only_category: continue
        for pdf in sorted(cat_dir.rglob("*.pdf")):
            result.append((pdf, cat_dir.name))
    return result


# ===========================================================================
# 배너 / 미리보기
# ===========================================================================

def _banner(title: str) -> None:
    print(f"\n{'─' * 64}")
    print(f"  {title}")
    print(f"{'─' * 64}")


def _print_preview(chunks: list[MTEnrichedChunk], n: int) -> None:
    for i, c in enumerate(chunks[:n]):
        rule = f" rule={c.rule_id}" if c.rule_id and c.rule_id != "none" else ""
        print(f"\n    [{i+1}] {c.msg_type} | section={c.section_title}"
              f" | field={c.field_tag}{rule} | seq={c.sequence} | p{c.page_label}")
        print(f"         source_file={c.source_file}")
        print(f"         텍스트: {c.text[:160].replace(chr(10),' ')}…")


# ===========================================================================
# 단건 처리
# ===========================================================================

def _process_file(
    pdf_path: Path,
    category: str,
    args: argparse.Namespace,
    client: Any,
    collection: str,
    is_first: bool,
) -> tuple[int, int, float]:
    """
    단일 PDF 처리.

    Returns:
        (deleted_count, chunk_count, ingest_elapsed_sec)
    """
    from app.rag.indexer import index_chunks

    print(f"\n  파일: {pdf_path.name}  (category={category})")

    # ── 청킹 ─────────────────────────────────────────────────────────────────
    t0 = time.perf_counter()
    parser = MTParser(str(pdf_path), category=category)
    chunks = parser.parse()
    parse_elapsed = time.perf_counter() - t0

    toc = parser.extract_toc_sections()
    print(f"    TOC 섹션:  {toc}")
    print(f"    청크 {len(chunks)}개  ({parse_elapsed:.2f}s)")

    if chunks:
        sec_dist  = Counter(c.section_title for c in chunks)
        tag_dist  = Counter(c.field_tag     for c in chunks)
        seq_dist  = Counter(c.sequence      for c in chunks)
        rule_dist = Counter(c.rule_id for c in chunks if c.rule_id and c.rule_id != "none")
        print(f"    섹션 분포:    {dict(sec_dist)}")
        print(f"    필드 태그 (상위 10): {dict(tag_dist.most_common(10))}")
        print(f"    sequence:     {dict(seq_dist)}")
        if rule_dist:
            print(f"    rule_id:      {dict(rule_dist.most_common(10))}")
        print(f"    source_file:  {parser.source_file}")

    if args.show > 0:
        _print_preview(chunks, args.show)

    # ── dry-run ───────────────────────────────────────────────────────────────
    if args.dry_run:
        if client and not args.no_dedup and not (args.recreate and is_first):
            existing = _count_existing(client, collection,
                                       parser.source_file, category)
            print(f"    [dry-run] 기존 포인트 {existing}개 (삭제 예정)")
        print(f"    [dry-run] 청킹 완료, 적재/삭제 건너뜀.")
        return 0, len(chunks), 0.0

    if not chunks:
        print("    경고: 청크 0개 — 건너뜀.")
        return 0, 0, 0.0

    # ── Dedup: 기존 포인트 삭제 ───────────────────────────────────────────────
    deleted = 0
    dedup_active = (
        not args.no_dedup
        and not (args.recreate and is_first)  # recreate 시 1번째 파일은 컬렉션 자체가 비어있음
    )
    if dedup_active:
        deleted = _delete_file_chunks(client, collection,
                                      parser.source_file, category)
        if deleted > 0:
            print(f"    Dedup: 기존 포인트 {deleted}개 삭제 완료")
        elif deleted == 0:
            print(f"    Dedup: 기존 포인트 없음 (신규 적재)")
        else:
            print(f"    Dedup: 기존 포인트 수 확인 실패 — upsert로 진행")

    # ── Qdrant 적재 ──────────────────────────────────────────────────────────
    t1 = time.perf_counter()
    index_chunks(chunks, batch_size=args.batch, client=client)
    ingest_elapsed = time.perf_counter() - t1
    print(f"    적재 완료: {len(chunks)}청크 ({ingest_elapsed:.1f}s)")
    return deleted, len(chunks), ingest_elapsed


# ===========================================================================
# 메인
# ===========================================================================

def main() -> None:
    args = _parse_args()

    if args.url:     os.environ["QDRANT_URL"]     = args.url
    if args.api_key: os.environ["QDRANT_API_KEY"] = args.api_key

    from app.rag.indexer import (
        COLLECTION, QDRANT_URL,
        check_connection, collection_exists, create_collection,
        get_client, get_collection_info,
    )

    _banner("MT 가이드북 인제스트 (동적 TOC 파싱 + Dedup)")
    print(f"  Qdrant URL : {QDRANT_URL}")
    print(f"  컬렉션명   : {COLLECTION}")
    print(f"  Dedup 모드 : {'비활성화 (--no-dedup)' if args.no_dedup else '활성화 (source_file 기반 삭제)'}")

    # ── 처리 대상 ─────────────────────────────────────────────────────────────
    if args.file:
        pdf_path = Path(args.file).resolve()
        if not pdf_path.exists():
            print(f"\n  오류: 파일 없음 — {pdf_path}")
            sys.exit(1)
        pdf_list = [(pdf_path, pdf_path.parent.name)]
        print(f"  모드       : 단건 처리  →  {pdf_path.name}")
    else:
        mx_dir = Path(args.dir or os.getenv("MT_DIR") or "data/MT").resolve()
        print(f"  모드       : 다건 처리  →  {mx_dir}")

        _banner("1. PDF 파일 탐색")
        pdf_list = _find_pdfs(mx_dir, args.category)
        if not pdf_list:
            print(f"\n  오류: PDF를 찾을 수 없습니다.")
            sys.exit(1)

        cat_stats = Counter(cat for _, cat in pdf_list)
        print(f"\n  총 {len(pdf_list)}개 PDF 발견")
        for cat, cnt in sorted(cat_stats.items()):
            print(f"    {cat}: {cnt}개")
        for pdf, cat in pdf_list:
            print(f"    [{cat}] {pdf.name}")

    # ── Qdrant 연결 ───────────────────────────────────────────────────────────
    client = None
    if not args.dry_run:
        _banner("2. Qdrant 연결 확인")
        client = get_client()
        print("  연결 중…", end=" ", flush=True)
        if not check_connection(client):
            print("실패 ✗"); sys.exit(1)
        print("OK ✓")

        _banner("3. 컬렉션 준비")
        if args.recreate or not collection_exists(client, COLLECTION):
            create_collection(client, recreate=args.recreate)
        else:
            print(f"  컬렉션 '{COLLECTION}' 확인됨.")
    elif not args.no_dedup:
        # dry-run 이지만 삭제 예정 수를 보여주기 위해 읽기 전용으로 연결
        try:
            client = get_client()
            check_connection(client)
        except Exception:
            client = None

    # ── 처리 루프 ─────────────────────────────────────────────────────────────
    _banner("4. PDF 청킹 및 적재")
    total_deleted = 0
    total_chunks  = 0
    total_ingest  = 0.0
    errors:        list[str] = []

    for idx, (pdf_path, category) in enumerate(pdf_list, 1):
        print(f"\n  [{idx}/{len(pdf_list)}]", end="")
        try:
            deleted, n, t = _process_file(
                pdf_path, category, args, client, COLLECTION, is_first=(idx == 1)
            )
            total_deleted += max(deleted, 0)
            total_chunks  += n
            total_ingest  += t
        except Exception as e:
            msg = f"{pdf_path.name}: {type(e).__name__}: {e}"
            print(f"\n    오류: {msg}")
            errors.append(msg)

    # ── 요약 ─────────────────────────────────────────────────────────────────
    _banner("완료 요약")
    print(f"  처리 PDF  : {len(pdf_list) - len(errors)} / {len(pdf_list)}개")
    if not args.no_dedup and not args.recreate:
        print(f"  삭제 포인트: {total_deleted}개 (Dedup)")
    print(f"  신규 청크  : {total_chunks}개")

    if not args.dry_run and client is not None:
        print(f"  인덱싱 시간: {total_ingest:.1f}s")
        info = get_collection_info(client)
        if "error" not in info:
            print(f"  누적 포인트: {info.get('points', 0)}개")
            print(f"  컬렉션 상태: {info.get('status', '?')}")

    if errors:
        print(f"\n  실패 ({len(errors)}개):")
        for e in errors:
            print(f"    - {e}")
        sys.exit(1)

    if args.dry_run:
        print("\n  [dry-run] Qdrant에는 아무것도 변경되지 않았습니다.")
    else:
        print(f"\n  모든 MT 가이드북이 컬렉션 '{COLLECTION}' 에 적재되었습니다. ✓")


if __name__ == "__main__":
    main()
