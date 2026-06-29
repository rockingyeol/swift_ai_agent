"""
MX (ISO 20022) 가이드북 고도화 인제스트 스크립트 (프로덕션급).

주요 기능:
  - 단건(--file) / 다건(--dir) 처리
  - TOC 기반 동적 대분류 섹션 감지 (MX 도메인 특화)
  - 섹션 유형별 2차 청킹
      · Element Specifications → XML 엘리먼트 단위 (XPath 보존)
      · Business Rules         → C/R-rule 단위
      · 기타                   → 논리 단락 단위
  - 2-Pass 전략: element spec 전역 스캔 + 섹션 기반 보완
  - 중복 방지(Dedup): 파일 처리 전 source_file 기반 기존 포인트 삭제
    · 동일 파일 재적재 → 기존 청크 삭제 후 신규 청크 upsert
    · --no-dedup 옵션으로 비활성화 가능
    · --recreate 는 컬렉션 전체 초기화 (dedup 불필요)
  - doc_category / section / xml_path / element_name / mult_norm /
    source_file 메타데이터 완전 지원

사용법:
  # 단건
  python scripts/ingest_mx_all.py --file data/MX/pacs/MX_pacs_008_001_14.pdf

  # 다건 (디렉토리 재귀)
  python scripts/ingest_mx_all.py --dir data/MX

  # 특정 카테고리만
  python scripts/ingest_mx_all.py --dir data/MX --category pacs

  # 청킹 미리보기 (적재/삭제 안 함)
  python scripts/ingest_mx_all.py --dir data/MX --dry-run --show 3

  # 컬렉션 전체 초기화 후 재적재
  python scripts/ingest_mx_all.py --dir data/MX --recreate

  # 중복 방지 없이 단순 upsert만
  python scripts/ingest_mx_all.py --dir data/MX --no-dedup
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

from app.rag.chunker import MXEnrichedChunk, make_chunk_id  # noqa: E402


# ===========================================================================
# MX TOC 섹션 패턴
# ===========================================================================

_MX_TOC_PATTERNS: dict[str, str] = {
    "Message_Overview":       r"(?:Message\s+Definition|Introduction|Overview|Business\s+Area|General\s+Description)",
    "Message_Scope":          r"Message\s+Scope",
    "Message_Structure":      r"(?:Message\s+Structure|Logical\s+Structure|Message\s+Building\s+Block)",
    "Element_Specifications": r"(?:Message\s+(?:Component\s+)?(?:Reference\s+)?(?:Guide|Specifications?)|Element\s+Specifications?|Field\s+Specifications?)",
    "Business_Rules":         r"(?:Business\s+Rules?|Validation\s+Rules?|Network\s+Validated\s+Rules?)",
    "Constraints":            r"Constraints?",
    "Data_Types":             r"(?:Data\s+Types?|Simple\s+Data\s+Types?)",
    "Appendix":               r"Appendix|Annex",
}

_RE_MX_ELEM_HDR = re.compile(
    r"^(\d+\.\d+(?:\.\d+){0,4})\s+([\w][^<\n]{0,80}?)\s*(<[A-Za-z][A-Za-z0-9]*>)",
    re.MULTILINE,
)
# CBPRPlus 문서 포맷: 태그명이 별도 줄에 "XML Tag: TagName" 형식으로 표기됨
#   5.54.1  Originator\nXML Tag: Orgtr\nPresence: [0..1]...
_RE_MX_ELEM_HDR_CBPR = re.compile(
    r"^(\d+\.\d+(?:\.\d+){0,4})\s+([\w][^\n]{0,80}?)\s*\n[^\n]*?XML\s+Tag:\s*([A-Za-z][A-Za-z0-9]*)",
    re.MULTILINE,
)
_RE_MX_RULE_HDR = re.compile(
    r"^(C\d+|R\d+|BR[-\s]?\d+|Rule\s+\d+|Constraint\s+\w+)\b",
    re.MULTILINE | re.IGNORECASE,
)
_RE_PRESENCE   = re.compile(r"Presence:\s*(\[[\d.*n]+\])", re.IGNORECASE)
_RE_DATATYPE   = re.compile(r"Datatype:\s*([^\n]+)",        re.IGNORECASE)
_RE_CONSTR_IDS = re.compile(r"\bC\d+\b")
_RE_TOC_ENTRY  = re.compile(r"\.{3,}\s*\d+\s*$")
_RE_MX_ARTIFACT = re.compile(
    r"^(?:Standards\s+MX|\d+\s+Message\s+Reference\s+Guide[^\n]*"
    r"|Message\s+Reference\s+Guide\s*-[^\n]*"
    r"|MX\s+(?:pacs|camt|pain|seev)\.\d+\.\d+\.\d+[^\n]*)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_RE_MX_TYPE_FILE = re.compile(r"([a-z]+)[._](\d{3})[._](\d{3})[._](\d{2,3})", re.IGNORECASE)
# CBPRPlus 파일명 패턴: CBPRPlus-pacs_008_001_08_... → pacs.008.001.08
_RE_CBPR_TYPE    = re.compile(r"CBPRPlus-([a-z]+)[_](\d{3})[_](\d{3})[_](\d{2,3})", re.IGNORECASE)
# CBPRPlus 변형 추출 (화이트리스트)
# · 버전 바로 뒤 단문 코드: STP / ADV / COV
# · 비즈니스명 뒤 접미사: MultipleCharges (날짜 앞)
_RE_CBPR_VARIANT = re.compile(
    r"CBPRPlus-[a-z]+_\d{3}_\d{3}_\d{2,3}_(STP|ADV|COV)_"
    r"|_(MultipleCharges)_\d{8}",
    re.IGNORECASE,
)
_RE_MULT_NORM    = re.compile(r"\[?([\d*n]+)\.\.([\d*n]+)\]?")

# CBPRPlus 디렉토리 이름
_CBPRPLUS_DIRNAME = "CBPRPlus_SR2026"


# ===========================================================================
# MX 파서
# ===========================================================================

class MXParser:
    """ISO 20022 MX 가이드북 PDF → MXEnrichedChunk 목록 변환."""

    def __init__(self, pdf_path: str,
                 msg_type: Optional[str] = None,
                 category: Optional[str] = None,
                 doc_subtype: str = "standard") -> None:
        import fitz
        self.pdf_path = Path(pdf_path)
        self._fitz = fitz

        if msg_type is None:
            # CBPRPlus 파일명 우선 시도: CBPRPlus-pacs_008_001_08_... → pacs.008.001.08
            cm = _RE_CBPR_TYPE.search(self.pdf_path.stem)
            if cm:
                msg_type = f"{cm.group(1).lower()}.{cm.group(2)}.{cm.group(3)}.{cm.group(4)}"
            else:
                m = _RE_MX_TYPE_FILE.search(self.pdf_path.stem)
                msg_type = (f"{m.group(1).lower()}.{m.group(2)}.{m.group(3)}.{m.group(4)}"
                            if m else "pacs.008")
        self.msg_type    = msg_type
        self.doc_subtype = doc_subtype

        # CBPRPlus 변형 추출: STP / ADV / COV / MultipleCharges
        vm = _RE_CBPR_VARIANT.search(self.pdf_path.stem)
        self.variant = (vm.group(1) or vm.group(2)).upper() if vm else ""

        self.category    = category or self.pdf_path.parent.name
        self.source_file = self.pdf_path.stem  # 중복 방지 키
        self._msg_root   = ""

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
        text = _RE_MX_ARTIFACT.sub("", text)
        return re.sub(r"^\s*\d{1,3}\s*$", "", text, flags=re.MULTILINE)

    def _page_at(self, offset: int) -> int:
        current = self._page_map[0][0] if self._page_map else 1
        for pn, start in self._page_map:
            if start > offset: break
            current = pn
        return current

    # ── §2 TOC 동적 섹션 감지 ──────────────────────────────────────────────

    def extract_toc_sections(self) -> list[str]:
        toc_m = re.search(r'(?:Table\s+of\s+Contents?|CONTENTS?)\s*\n',
                          self._full_text, re.IGNORECASE)
        zone = (self._full_text[toc_m.start(): toc_m.start() + 4000]
                if toc_m else self._full_text[:4000])
        found = [k for k, p in _MX_TOC_PATTERNS.items()
                 if re.search(p, zone, re.IGNORECASE)]
        return found if found else list(_MX_TOC_PATTERNS.keys())

    # ── 메시지 루트 추출 ───────────────────────────────────────────────────

    def _extract_message_root(self, boundaries: list) -> str:
        # depth==2(x.y) 인 첫 번째 섹션의 태그를 루트로 사용.
        # 섹션 번호가 1.x 로 고정되지 않는 CBPRPlus Combined 문서 지원.
        for _, sec_num, _, xml_tag in boundaries:
            if len(sec_num.split(".")) == 2:
                return xml_tag
        return ""

    # ── XPath 빌더 ────────────────────────────────────────────────────────

    def _build_full_xpath(self, sec_num: str, xml_tag: str,
                          section_tag_map: dict) -> str:
        parts     = sec_num.split(".")
        ancestors = []
        for depth in range(len(parts) - 1, 0, -1):
            anc_num = ".".join(parts[:depth])
            if len(anc_num.split(".")) < 3: break
            atag = section_tag_map.get(anc_num)
            if atag and atag != xml_tag:
                ancestors.insert(0, atag)
            if len(ancestors) >= 4: break
        relative    = "/".join(ancestors + [xml_tag]) if ancestors else xml_tag
        root_prefix = f"/Document/{self._msg_root}" if self._msg_root else "/Document"
        return f"{root_prefix}/{relative}"

    # ── Multiplicity 정규화 ───────────────────────────────────────────────

    @staticmethod
    def _normalize_mult(raw: str) -> str:
        if not raw: return "none"
        m = _RE_MULT_NORM.search(raw)
        return f"{m.group(1)}..{m.group(2)}" if m else (raw.strip("[]") or "none")

    # ── §3 1차 섹션 분할 ──────────────────────────────────────────────────

    def _split_top_sections(self, toc_sections: list[str]) -> list[dict]:
        active = [(k, _MX_TOC_PATTERNS.get(k, re.escape(k.replace("_", " "))))
                  for k in toc_sections]
        combined = (re.compile(
            "|".join(f"(?P<S{i}>^(?:\\d+(?:\\.\\d+)*\\s+)?({p})\\s*$)"
                     for i, (_, p) in enumerate(active)),
            re.MULTILINE | re.IGNORECASE,
        ) if active else None)

        boundaries = []
        if combined:
            for m in combined.finditer(self._full_text):
                title = m.group(0).strip()
                if _RE_TOC_ENTRY.search(title): continue
                for i, (key, _) in enumerate(active):
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

    # ── §3 2차 청킹: Element Specifications (★핵심) ──────────────────────

    def _chunk_element_specs(self, text: str, section_key: str,
                              base_page: int, sec_start: int) -> list[MXEnrichedChunk]:
        chunks, seen = [], set()
        boundaries, seen_nums = [], set()

        # ── 패턴 1: 인라인 형식  "1.2.3 FieldName <XmlTag>" ─────────────────
        for m in _RE_MX_ELEM_HDR.finditer(text):
            sec_num = m.group(1).strip()
            if sec_num in seen_nums: continue
            if _RE_TOC_ENTRY.search(m.group(0)): continue
            seen_nums.add(sec_num)
            boundaries.append((m.start(), sec_num, m.group(2).strip(), m.group(3).strip("<>")))

        # ── 패턴 2: CBPRPlus 포맷  "5.54.1 Originator\nXML Tag: Orgtr" ──────
        # 패턴 1이 전혀 없는 경우에만 시도 (중복 방지)
        if not boundaries:
            for m in _RE_MX_ELEM_HDR_CBPR.finditer(text):
                sec_num = m.group(1).strip()
                if sec_num in seen_nums: continue
                if _RE_TOC_ENTRY.search(m.group(0)): continue
                seen_nums.add(sec_num)
                # 헤더 전체 매치 시작점을 경계로 사용
                boundaries.append((m.start(), sec_num, m.group(2).strip(), m.group(3).strip()))

        boundaries.sort(key=lambda x: x[0])

        if not boundaries:
            body = re.sub(r"\n{3,}", "\n\n", text.strip())
            if body and len(body) >= 20:
                cid = make_chunk_id(self.msg_type, "full", section_key)
                chunks.append(self._make_generic_mx_chunk(cid, section_key, base_page, body))
            return chunks

        section_tag_map = {b[1]: b[3] for b in boundaries}
        if not self._msg_root:
            self._msg_root = self._extract_message_root(boundaries)

        for i, (start, sec_num, field_name, xml_tag) in enumerate(boundaries):
            end  = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
            body = re.sub(r"\n{3,}", "\n\n", text[start:end].strip())
            if not body or len(body) < 15: continue

            presence_m = _RE_PRESENCE.search(body)
            datatype_m = _RE_DATATYPE.search(body)
            impacted_m = re.search(r"Impacted\s+by:\s*([^\n]+)", body)

            raw_mult   = presence_m.group(1) if presence_m else ""
            mult_norm  = self._normalize_mult(raw_mult)
            datatype   = re.sub(r"\s+on\s+page\s+\d+", "",
                                datatype_m.group(1).strip()).strip() if datatype_m else ""
            constraints = list(set(_RE_CONSTR_IDS.findall(impacted_m.group(1)))) if impacted_m else []

            # 상대 field_path
            parts_p    = sec_num.split(".")
            ancs       = []
            for depth in range(len(parts_p) - 1, 0, -1):
                anc = ".".join(parts_p[:depth])
                if len(anc.split(".")) < 3: break
                atag = section_tag_map.get(anc)
                if atag and atag != xml_tag: ancs.insert(0, atag)
                if len(ancs) >= 3: break
            field_path = "/".join(ancs + [xml_tag])
            full_xpath = self._build_full_xpath(sec_num, xml_tag, section_tag_map)

            page = self._page_at(sec_start + start)
            cid  = make_chunk_id(self.msg_type, sec_num, xml_tag, str(page))
            if cid in seen: continue
            seen.add(cid)

            chunks.append(MXEnrichedChunk(
                chunk_id=cid, msg_type=self.msg_type,
                field_path=field_path, xml_tag=xml_tag,
                doc_type="mx_guide", doc_category="MX",
                doc_subtype=self.doc_subtype, variant=self.variant,
                page_label=page, category=self.category,
                section=section_key,
                section_title=f"{field_name} <{xml_tag}>",
                section_num=sec_num,
                multiplicity=raw_mult, mult_norm=mult_norm,
                datatype=datatype, constraints=constraints,
                xml_path=full_xpath, element_name=xml_tag,
                source_file=self.source_file, text=body,
            ))
        return chunks

    # ── §3 2차 청킹: Business Rules ──────────────────────────────────────

    def _chunk_business_rules(self, text: str, section_key: str,
                               base_page: int, sec_start: int) -> list[MXEnrichedChunk]:
        chunks, seen = [], set()
        boundaries = [(m.start(), m.group(1)) for m in _RE_MX_RULE_HDR.finditer(text)]

        if not boundaries:
            body = re.sub(r"\n{3,}", "\n\n", text.strip())
            if body and len(body) >= 20:
                cid = make_chunk_id(self.msg_type, "rules", section_key, str(base_page))
                chunks.append(self._make_generic_mx_chunk(cid, section_key, base_page, body))
            return chunks

        for i, (start, rule_id) in enumerate(boundaries):
            end  = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
            body = re.sub(r"\n{3,}", "\n\n", text[start:end].strip())
            if not body or len(body) < 10: continue
            page = self._page_at(sec_start + start)
            cid  = make_chunk_id(self.msg_type, rule_id, section_key, str(page))
            if cid in seen: continue
            seen.add(cid)
            chunks.append(MXEnrichedChunk(
                chunk_id=cid, msg_type=self.msg_type,
                doc_type="mx_guide", doc_category="MX",
                doc_subtype=self.doc_subtype, variant=self.variant,
                page_label=page, category=self.category,
                section=section_key, section_title=f"Rule {rule_id}",
                constraints=[rule_id],
                xml_path="none", element_name="none", mult_norm="none",
                source_file=self.source_file, text=body,
            ))
        return chunks

    # ── §3 2차 청킹: 기타 ────────────────────────────────────────────────

    def _chunk_generic_section(self, text: str, section_key: str,
                                base_page: int, sec_start: int) -> list[MXEnrichedChunk]:
        MAX_CHUNK = 1200
        chunks, seen = [], set()
        current_parts, current_len, offset_cursor = [], 0, 0

        def _flush(parts, off):
            body = re.sub(r"\n{3,}", "\n\n", "\n\n".join(parts).strip())
            if not body or len(body) < 20: return
            page = self._page_at(sec_start + off)
            cid  = make_chunk_id(self.msg_type, "generic",
                                  body[:40].replace("\n", " "), str(page))
            if cid in seen: return
            seen.add(cid)
            chunks.append(self._make_generic_mx_chunk(cid, section_key, page, body))

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

    def _make_generic_mx_chunk(self, cid: str, section_key: str,
                                page: int, body: str) -> MXEnrichedChunk:
        return MXEnrichedChunk(
            chunk_id=cid, msg_type=self.msg_type,
            doc_type="mx_guide", doc_category="MX",
            doc_subtype=self.doc_subtype, variant=self.variant,
            page_label=page, category=self.category,
            section=section_key, section_title=section_key,
            xml_path="none", element_name="none", mult_norm="none",
            source_file=self.source_file, text=body,
        )

    # ── element 헤더 유무 감지 ───────────────────────────────────────────

    @staticmethod
    def _has_element_headers(text: str) -> bool:
        count = 0
        for pattern in (_RE_MX_ELEM_HDR, _RE_MX_ELEM_HDR_CBPR):
            for m in pattern.finditer(text):
                if not _RE_TOC_ENTRY.search(m.group(0)):
                    count += 1
                    if count >= 2: return True
        return False

    # ── 섹션 유형 분류 ──────────────────────────────────────────────────────

    @staticmethod
    def _section_type(key: str) -> str:
        k = key.lower()
        if "element" in k or "spec" in k: return "element_spec"
        if "business" in k or "constraint" in k or "valid" in k: return "business_rule"
        return "generic"

    # ── 공개 API (2-Pass) ─────────────────────────────────────────────────

    def parse(self) -> list[MXEnrichedChunk]:
        """
        2-Pass 전략:
          Pass 1: 전역 element spec 스캔 (섹션 경계 무관)
          Pass 2: 섹션 기반 business rule / generic 청킹
        """
        if not self._pages:
            return []

        # Pass 1 — 전역 element spec
        element_chunks = self._chunk_element_specs(
            self._full_text, "Element_Specifications", self._page_at(0), 0
        )

        # Pass 2 — 섹션 기반
        toc      = self.extract_toc_sections()
        sections = self._split_top_sections(toc)
        other_chunks: list[MXEnrichedChunk] = []

        for sec in sections:
            key   = sec["key"]
            body  = self._full_text[sec["start"]: sec["end"]]
            pg    = sec["page"]
            start = sec["start"]
            stype = self._section_type(key)

            if stype == "element_spec":
                continue  # Pass 1 처리됨
            if stype == "business_rule":
                other_chunks.extend(self._chunk_business_rules(body, key, pg, start))
            else:
                if not self._has_element_headers(body):
                    other_chunks.extend(self._chunk_generic_section(body, key, pg, start))

        return element_chunks + other_chunks


# ===========================================================================
# Qdrant Dedup 헬퍼
# ===========================================================================

def _count_existing(client, collection: str,
                    source_file: str, category: str) -> int:
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
        description="MX (ISO 20022) 가이드북 PDF 청킹 및 Qdrant 적재 (단건 / 다건, Dedup 지원)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--file", default=None, metavar="PATH",
                     help="단건: PDF 파일 경로")
    src.add_argument("--dir",  default=None, metavar="DIR",
                     help="다건: MX PDF 루트 디렉토리 (기본: MX_DIR 환경변수 → data/MX)")
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

def _find_pdfs(
    root_dir: Path,
    only_category: str | None,
) -> list[tuple[Path, str, str]]:
    """
    PDF 목록 반환. (path, category, doc_subtype) 튜플.

    우선순위:
      1. CBPRPlus_SR2026 디렉토리 → doc_subtype="cbpr_plus"
      2. 나머지 카테고리 디렉토리 → doc_subtype="standard"
         단, CBPRPlus가 이미 커버하는 base msg_type(예: pacs.008)은 제외.
    """
    if not root_dir.exists():
        return []

    result: list[tuple[Path, str, str]] = []

    # ── 1. CBPRPlus_SR2026 디렉토리 우선 처리 ────────────────────────────
    cbpr_dir = root_dir / _CBPRPLUS_DIRNAME
    cbpr_covered: set[str] = set()   # base msg_type (예: "pacs.008")

    if cbpr_dir.is_dir() and (only_category is None or only_category == _CBPRPLUS_DIRNAME):
        for pdf in sorted(cbpr_dir.rglob("*.pdf")):
            result.append((pdf, _CBPRPLUS_DIRNAME, "cbpr_plus"))
            # 커버 base type 수집 (pacs.008 수준으로 — standard 제외 판단 기준)
            cm = _RE_CBPR_TYPE.search(pdf.stem)
            if cm:
                cbpr_covered.add(f"{cm.group(1).lower()}.{cm.group(2)}")
            else:
                m = _RE_MX_TYPE_FILE.search(pdf.stem)
                if m:
                    cbpr_covered.add(f"{m.group(1).lower()}.{m.group(2)}")

    # ── 2. 나머지 카테고리 디렉토리 (standard) ──────────────────────────
    for cat_dir in sorted(root_dir.iterdir()):
        if not cat_dir.is_dir(): continue
        if cat_dir.name == _CBPRPLUS_DIRNAME: continue  # 이미 처리됨
        if only_category and cat_dir.name != only_category: continue

        for pdf in sorted(cat_dir.rglob("*.pdf")):
            # 이 파일의 base msg_type이 CBPRPlus로 커버되면 제외
            m = _RE_MX_TYPE_FILE.search(pdf.stem)
            if m:
                base_type = f"{m.group(1).lower()}.{m.group(2)}"
                if base_type in cbpr_covered:
                    continue
            result.append((pdf, cat_dir.name, "standard"))

    return result


# ===========================================================================
# 배너 / 미리보기
# ===========================================================================

def _banner(title: str) -> None:
    print(f"\n{'─' * 64}")
    print(f"  {title}")
    print(f"{'─' * 64}")


def _print_preview(chunks: list[MXEnrichedChunk], n: int) -> None:
    for i, c in enumerate(chunks[:n]):
        print(f"\n    [{i+1}] {c.msg_type} | section={c.section}"
              f" | elem={c.element_name} | mult={c.mult_norm} | p{c.page_label}")
        print(f"         xpath:       {c.xml_path}")
        print(f"         source_file: {c.source_file}")
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
    doc_subtype: str = "standard",
) -> tuple[int, int, float]:
    """
    단일 PDF 처리.

    Returns:
        (deleted_count, chunk_count, ingest_elapsed_sec)
    """
    from app.rag.indexer import index_chunks

    print(f"\n  파일: {pdf_path.name}  (category={category}, subtype={doc_subtype})")

    # ── 청킹 ─────────────────────────────────────────────────────────────────
    t0 = time.perf_counter()
    parser = MXParser(str(pdf_path), category=category, doc_subtype=doc_subtype)
    chunks = parser.parse()
    parse_elapsed = time.perf_counter() - t0

    toc = parser.extract_toc_sections()
    print(f"    TOC 섹션:    {toc}")
    print(f"    메시지 루트: {parser._msg_root or '(미감지)'}")
    print(f"    청크 {len(chunks)}개  ({parse_elapsed:.2f}s)")

    if chunks:
        sec_dist  = Counter(c.section      for c in chunks)
        elem_dist = Counter(c.element_name for c in chunks if c.element_name != "none")
        mult_dist = Counter(c.mult_norm    for c in chunks)
        print(f"    섹션 분포:    {dict(sec_dist)}")
        print(f"    element (상위 10): {dict(Counter(elem_dist).most_common(10))}")
        print(f"    multiplicity: {dict(mult_dist)}")
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
        and not (args.recreate and is_first)
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

    _banner("MX 가이드북 인제스트 (동적 TOC 파싱 + Dedup)")
    print(f"  Qdrant URL : {QDRANT_URL}")
    print(f"  컬렉션명   : {COLLECTION}")
    print(f"  Dedup 모드 : {'비활성화 (--no-dedup)' if args.no_dedup else '활성화 (source_file 기반 삭제)'}")

    # ── 처리 대상 ─────────────────────────────────────────────────────────────
    if args.file:
        pdf_path = Path(args.file).resolve()
        if not pdf_path.exists():
            print(f"\n  오류: 파일 없음 — {pdf_path}")
            sys.exit(1)
        # 단건: CBPRPlus 파일이면 cbpr_plus, 아니면 standard
        is_cbpr = bool(_RE_CBPR_TYPE.search(pdf_path.stem))
        pdf_list = [(pdf_path, pdf_path.parent.name, "cbpr_plus" if is_cbpr else "standard")]
        print(f"  모드       : 단건 처리  →  {pdf_path.name}")
    else:
        mx_dir = Path(args.dir or os.getenv("MX_DIR") or "data/MX").resolve()
        print(f"  모드       : 다건 처리  →  {mx_dir}")

        _banner("1. PDF 파일 탐색")
        pdf_list = _find_pdfs(mx_dir, args.category)
        if not pdf_list:
            print(f"\n  오류: PDF를 찾을 수 없습니다.")
            sys.exit(1)

        cat_stats = Counter(cat for _, cat, _ in pdf_list)
        print(f"\n  총 {len(pdf_list)}개 PDF 발견")
        for cat, cnt in sorted(cat_stats.items()):
            print(f"    {cat}: {cnt}개")
        for pdf, cat, subtype in pdf_list:
            print(f"    [{cat}|{subtype}] {pdf.name}")

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

    for idx, (pdf_path, category, doc_subtype) in enumerate(pdf_list, 1):
        print(f"\n  [{idx}/{len(pdf_list)}]", end="")
        try:
            deleted, n, t = _process_file(
                pdf_path, category, args, client, COLLECTION,
                is_first=(idx == 1), doc_subtype=doc_subtype,
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
        print(f"\n  모든 MX 가이드북이 컬렉션 '{COLLECTION}' 에 적재되었습니다. ✓")


if __name__ == "__main__":
    main()
