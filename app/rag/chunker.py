"""
SWIFT 가이드북 → 청크 파이프라인.

▶ MT 전문(ISO 15022) 가이드북용 (신규)
    - MTFieldChunk: 필드 태그 단위 청크 모델
    - chunk_mt_guidebook(): PDF → MTFieldChunk 목록
      · 글자 수 분할 금지 — 필드 태그 섹션 단위로만 분할
      · 정규식으로 "N. Field XX: ..." 패턴 감지
      · 각 청크에 msg_type / field_tag / doc_type / page_label 메타데이터 부착

▶ 레거시 텍스트 기반 청킹 (기존 테스트 호환 유지)
    - SwiftChunk: 기존 hierarchical chunk 모델
    - chunk_text() / chunk_guidebook(): 기존 API 유지

의존성: pymupdf(fitz), pydantic
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import fitz  # PyMuPDF
import structlog
from pydantic import BaseModel

log = structlog.get_logger(__name__)


# ===========================================================================
# ── 1. MT 전문 전용 (신규) ──────────────────────────────────────────────────
# ===========================================================================

class MTFieldChunk(BaseModel):
    """
    MT 전문 가이드북 필드 태그 단위 청크.

    Qdrant 페이로드에 저장되는 메타데이터:
      - msg_type  : MT 타입 (예: "MT101", "MT103")
      - field_tag : 필드 태그 번호 (예: "20", "32B", "50a");
                   필드 무관 섹션은 "SYSTEM"
      - doc_type  : 항상 "guidebook"
      - page_label: 섹션 시작 페이지 번호
      - category  : PDF가 속한 카테고리 폴더명 (예: "Category1", "Category2")
    """

    # ── Qdrant 페이로드 필수 메타데이터 ─────────────────────────────────────
    chunk_id:     str
    msg_type:     str  = ""          # 실제값은 chunk_mt_guidebook()에서 주입 (예: "MT103")
    field_tag:    str  = "SYSTEM"    # 필드 무관 섹션 기본값
    doc_type:     str  = "guidebook"
    page_label:   int  = 0
    category:     str  = ""          # 카테고리 폴더명 (data/MT/<category>/)

    # ── 보조 메타데이터 ──────────────────────────────────────────────────────
    section_title: str           = ""
    section_type:  str           = "field_spec"
    # "field_spec" | "message_rule" | "usage_rule" | "example" | "other"
    rule_id:       Optional[str] = None  # 메시지 레벨 C-rule (C1, C2…)

    # ── 본문 ─────────────────────────────────────────────────────────────────
    text: str = ""

    # ── 하위 호환 속성 (app/llm.py, reconciler.py 대응) ─────────────────────
    @property
    def page(self) -> int:
        """Legacy alias for page_label."""
        return self.page_label

    @property
    def message_type(self) -> str:
        """Legacy alias for msg_type."""
        return self.msg_type

    # ── 임베딩 텍스트 ─────────────────────────────────────────────────────────
    def embedding_text(self) -> str:
        """임베딩 품질 향상을 위한 컨텍스트 보강 텍스트."""
        parts: list[str] = [f"[{self.msg_type}]"]
        if self.field_tag != "SYSTEM":
            parts.append(f"Field :{self.field_tag}:")
        if self.section_title:
            parts.append(self.section_title)
        prefix = " ".join(parts)
        return f"{prefix}\n{self.text}"


# ---------------------------------------------------------------------------
# MT 가이드북 PDF 파싱 상수
# ---------------------------------------------------------------------------

# "N. Field XX: Description" — 번호 붙은 필드 섹션 헤더
# 태그: 1~2자리 숫자 + 선택적 문자 (20, 21R, 32B, 50a, 52a, 51A 등)
_RE_FIELD_HEADER = re.compile(
    r"^(\d+)\.\s+(Field\s+(\d{1,2}[A-Za-z]?)\s*:[^\n]*)",
    re.MULTILINE,
)

# 최상위 메시지 섹션 헤더
_RE_MSG_SECTION = re.compile(
    r"^(MT\s*\d{3}\s+"
    r"(?:Scope"
    r"|Format\s+Specifications"
    r"|Network\s+Validated\s+Rules"
    r"|Usage\s+Rules"
    r"|Field\s+Specifications"
    r"|Mapping"
    r"|Examples?"
    r"|Operating\s+Procedures"
    r"|Operational\s+Rules(?:\s+and\s+Checklist)?"
    r"))\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# 실제 필드 스펙 섹션임을 판별하는 서브섹션 키워드
_FIELD_SUBSECTIONS: frozenset[str] = frozenset([
    "FORMAT", "PRESENCE", "DEFINITION", "CODES",
    "NETWORK VALIDATED RULES", "USAGE RULES", "EXAMPLES",
])

# PDF 반복 헤더·푸터 패턴 제거용
_RE_PDF_ARTIFACT = re.compile(
    r"^(?:Standards\s+MT\s+\w+\s+\d{4}"
    r"|\d+\s+Message\s+Reference\s+Guide[^\n]*"
    r"|Message\s+Reference\s+Guide\s*-\s*MT\s*\d{3}[^\n]*"
    r")\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# TOC 항목 판별 (줄 끝이 "……12" 형태)
_RE_TOC_ENTRY = re.compile(r"\.{3,}\s*\d+\s*$")


# ---------------------------------------------------------------------------
# PDF 텍스트 추출 헬퍼
# ---------------------------------------------------------------------------

def _extract_pages(pdf_path: str) -> list[tuple[int, str]]:
    """PDF 각 페이지의 텍스트를 (페이지번호, 텍스트) 리스트로 반환한다."""
    doc = fitz.open(pdf_path)
    result: list[tuple[int, str]] = []
    for i in range(len(doc)):
        text = doc[i].get_text("text")
        if text.strip():
            result.append((i + 1, text))
    return result


def _build_corpus(
    pages: list[tuple[int, str]],
) -> tuple[str, list[tuple[int, int]]]:
    """
    페이지 텍스트를 하나의 코퍼스로 합친다.

    Returns:
        full_text  : 전체 텍스트
        page_map   : [(페이지번호, 시작_오프셋), …]
    """
    parts: list[str] = []
    page_map: list[tuple[int, int]] = []
    offset = 0
    for page_num, text in pages:
        page_map.append((page_num, offset))
        parts.append(text)
        offset += len(text)
    return "".join(parts), page_map


def _page_at(offset: int, page_map: list[tuple[int, int]]) -> int:
    """문자 오프셋에 대응하는 페이지 번호를 반환한다."""
    current = page_map[0][0] if page_map else 1
    for pn, start in page_map:
        if start > offset:
            break
        current = pn
    return current


def _clean_text(text: str) -> str:
    """PDF 반복 헤더·푸터 제거 및 과도한 공백 정규화."""
    text = _RE_PDF_ARTIFACT.sub("", text)
    # 혼자 있는 페이지 번호 줄 제거 (최대 3자리 숫자)
    text = re.sub(r"^\s*\d{1,3}\s*$", "", text, flags=re.MULTILINE)
    return text


def _is_real_field_section(full_text: str, match_end: int) -> bool:
    """
    필드 섹션 헤더 매치가 TOC 항목이 아닌 실제 스펙 섹션인지 확인한다.
    헤더 이후 400자 내에 서브섹션 키워드가 있으면 True.
    """
    lookahead = full_text[match_end: match_end + 400]
    return any(sub in lookahead for sub in _FIELD_SUBSECTIONS)


def _classify_msg_section(title: str) -> str:
    """최상위 메시지 섹션 제목으로 section_type을 분류한다."""
    t = title.lower()
    if "network validated rules" in t:
        return "message_rule"
    if "usage rules" in t:
        return "usage_rule"
    if "example" in t:
        return "example"
    return "other"


# ---------------------------------------------------------------------------
# 섹션 분할
# ---------------------------------------------------------------------------

def _split_into_sections(
    full_text: str,
    page_map: list[tuple[int, int]],
    msg_type: str,
) -> list[dict[str, Any]]:
    """
    전체 텍스트를 MT 전문 구조에 따른 섹션 목록으로 분할한다.

    분할 기준:
      1. 번호가 붙은 필드 섹션   ("N. Field XX: …")
      2. 최상위 메시지 섹션      ("MT 101 Scope", "MT 101 Network Validated Rules" 등)

    Returns:
        list of dicts with keys: title, field_tag, start, end, page, section_type
    """
    boundaries: list[tuple[int, str, str, str]] = []
    # (오프셋, 제목, field_tag, section_type)

    # ── 1) 번호 붙은 필드 섹션 ─────────────────────────────────────────────
    for m in _RE_FIELD_HEADER.finditer(full_text):
        line = m.group(0).rstrip()
        # TOC 항목 제외
        if _RE_TOC_ENTRY.search(line):
            continue
        # FORMAT/PRESENCE 등 서브섹션이 이후에 등장하는 경우만 유효
        if not _is_real_field_section(full_text, m.end()):
            continue
        section_title = m.group(2).strip()
        tag = m.group(3).strip()
        boundaries.append((m.start(), section_title, tag, "field_spec"))

    # ── 2) 최상위 메시지 섹션 ──────────────────────────────────────────────
    field_offsets = {b[0] for b in boundaries}
    for m in _RE_MSG_SECTION.finditer(full_text):
        if m.start() in field_offsets:
            continue
        title = m.group(1).strip()
        boundaries.append((m.start(), title, "SYSTEM", _classify_msg_section(title)))

    boundaries.sort(key=lambda x: x[0])

    if not boundaries:
        return [{
            "title": f"{msg_type} Full Document",
            "field_tag": "SYSTEM",
            "start": 0,
            "end": len(full_text),
            "page": _page_at(0, page_map),
            "section_type": "other",
        }]

    sections: list[dict[str, Any]] = []
    for i, (start, title, tag, stype) in enumerate(boundaries):
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(full_text)
        page = _page_at(start, page_map)
        sections.append({
            "title": title,
            "field_tag": tag,
            "start": start,
            "end": end,
            "page": page,
            "section_type": stype,
        })

    return sections


# ---------------------------------------------------------------------------
# 공개 API — MT 가이드북 청킹 (신규)
# ---------------------------------------------------------------------------

def chunk_mt_guidebook(
    pdf_path: str,
    msg_type: Optional[str] = None,
    category: Optional[str] = None,
) -> list[MTFieldChunk]:
    """
    MT 전문 가이드북 PDF를 필드 태그 단위 청크 목록으로 변환한다.

    청킹 규칙:
      - 글자 수 기준 분할 금지
      - 정규식으로 "N. Field XX: …" 헤더 감지 → 섹션 단위 분할
      - 각 섹션 내의 FORMAT / PRESENCE / DEFINITION /
        CODES / NETWORK VALIDATED RULES / USAGE RULES 가 하나의 청크에 묶임
      - 비필드 섹션(Scope, Network Validated Rules 등) → field_tag="SYSTEM"

    Args:
        pdf_path : SWIFT MT 가이드북 PDF 경로
        msg_type : MT 타입 (기본: 파일명에서 자동 추론, 예 "MT101")
        category : 카테고리 폴더명 (기본: PDF 부모 폴더명 자동 추출,
                   예 "data/MT/Category1/foo.pdf" → "Category1")

    Returns:
        MTFieldChunk 목록 (각 청크에 category 메타데이터 포함)
    """
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF를 찾을 수 없습니다: {pdf_path}")

    # msg_type 자동 추론
    if msg_type is None:
        stem = path.stem.upper()
        m = re.search(r"MT\s*(\d{3})", stem)
        msg_type = f"MT{m.group(1)}" if m else "MT101"

    # category 자동 추론: PDF 직계 부모 폴더명을 사용
    # data/MT/Category1/SR_2025_MT101.pdf → "Category1"
    if category is None:
        category = path.parent.name
        # 2단계 이상 중첩 경로면 category 추출이 부정확할 수 있으므로 경고
        grandparent = path.parents[1].name if len(path.parents) > 1 else ""
        if grandparent.upper() not in ("MT", "MX", "DATA", ""):
            log.warning("deep_pdf_nesting_category_inferred",
                        pdf_path=str(path), category=category,
                        hint="Pass category= explicitly if wrong")

    # ── 텍스트 추출 ──────────────────────────────────────────────────────────
    pages = _extract_pages(pdf_path)
    if not pages:
        return []

    full_text, page_map = _build_corpus(pages)
    full_text = _clean_text(full_text)

    # ── 섹션 분할 ────────────────────────────────────────────────────────────
    sections = _split_into_sections(full_text, page_map, msg_type)

    # ── 청크 생성 ────────────────────────────────────────────────────────────
    chunks: list[MTFieldChunk] = []
    seen_ids: set[str] = set()

    for sec in sections:
        text = full_text[sec["start"]: sec["end"]].strip()
        # 연속된 빈 줄 정규화
        text = re.sub(r"\n{3,}", "\n\n", text)

        if not text or len(text) < 30:
            continue

        cid = make_chunk_id(
            msg_type,
            sec["field_tag"],
            sec["title"][:40],
            str(sec["page"]),
        )
        if cid in seen_ids:
            continue
        seen_ids.add(cid)

        chunks.append(MTFieldChunk(
            chunk_id=cid,
            msg_type=msg_type,
            field_tag=sec["field_tag"],
            doc_type="guidebook",
            page_label=sec["page"],
            category=category,
            section_title=sec["title"],
            section_type=sec["section_type"],
            text=text,
        ))

    return chunks


# ===========================================================================
# ── 2-b. MTEnrichedChunk — 고도화 인제스트 전용 (ingest_mt_all.py) ─────────────
# ===========================================================================

class MTEnrichedChunk(MTFieldChunk):
    """
    MTFieldChunk 확장 모델.

    ingest_mt_all.py 의 MTParser 가 생성하며,
    Qdrant 페이로드에 추가 메타데이터가 저장된다.

    추가 필드:
      doc_category : 항상 "MT"          (MT vs MX 필터링용)
      sequence     : "A" | "B" | "none" (Sequence A/B 구분)
      source_file  : PDF 파일명 (확장자 제외, 예: "SR_2025_MT101")
                     중복 적재 방지 삭제 필터의 기준 키
    """
    doc_category: str = "MT"
    sequence:     str = "none"
    source_file:  str = ""   # PDF stem — 재적재 시 dedup 필터 키

    def embedding_text(self) -> str:
        """컨텍스트 보완 헤더 + 청크 본문 (LLM 프롬프트 상위 문맥 보존)."""
        section = self.section_title or "Unknown"
        field   = f" | Field: :{self.field_tag}:" if self.field_tag not in ("SYSTEM", "none", "") else ""
        rule    = f" | Rule: {self.rule_id}"       if self.rule_id and self.rule_id != "none" else ""
        header  = (
            f"[Document: {self.msg_type}]\n"
            f"[Category: {self.category}]\n"
            f"[Section: {section}]\n"
            f"[Sequence: {self.sequence}]{field}{rule}\n"
            f"{'─' * 50}\n"
        )
        return header + self.text


# ===========================================================================
# ── 3. MX (ISO 20022) 가이드북 및 CBPR+ SR2026 CSV 청킹 (신규) ─────────────────
# ===========================================================================

# ---------------------------------------------------------------------------
# MX 필드 청크 모델
# ---------------------------------------------------------------------------

class MXFieldChunk(BaseModel):
    """
    MX (ISO 20022) 가이드북 필드 단위 청크.

    Qdrant 페이로드 메타데이터:
      - msg_type   : "pacs.008" 등 MX 메시지 타입
      - field_path : 부모→자식 XML 태그 경로 (예: "CdtTrfTxInf/PmtId")
      - xml_tag    : 현재 필드 XML 태그 (예: "MsgId")
      - doc_type   : 항상 "mx_guide"
      - page_label : 섹션 시작 페이지 번호
      - category   : PDF가 속한 카테고리 폴더명 (예: "pacs", "camt")
    """

    chunk_id:      str
    msg_type:      str       = ""          # 실제값은 chunk_mx_guidebook()에서 주입 (예: "pacs.008")
    field_path:    str       = ""          # 예: "GrpHdr/MsgId"
    xml_tag:       str       = ""          # 예: "MsgId"
    doc_type:      str       = "mx_guide"
    page_label:    int       = 0
    category:      str       = ""         # 카테고리 폴더명 (data/MX/<category>/)
    section_title: str       = ""
    section_num:   str       = ""          # 예: "1.4.1.1"
    multiplicity:  str       = ""          # 예: "[1..1]"
    datatype:      str       = ""          # 예: "Max35Text"
    constraints:   list[str] = []          # 예: ["C1", "C11"]
    text:          str       = ""

    def embedding_text(self) -> str:
        parts: list[str] = [f"[{self.msg_type}]"]
        if self.field_path:
            parts.append(f"Path:{self.field_path}")
        if self.xml_tag:
            parts.append(f"<{self.xml_tag}>")
        if self.section_title:
            parts.append(self.section_title)
        return " ".join(parts) + f"\n{self.text}"

    @property
    def page(self) -> int:
        return self.page_label

    @property
    def message_type(self) -> str:
        return self.msg_type


class MXEnrichedChunk(MXFieldChunk):
    """
    MXFieldChunk 확장 모델.

    ingest_mx_all.py 의 MXParser 가 생성하며,
    Qdrant 페이로드에 추가 메타데이터가 저장된다.

    추가 필드:
      doc_category : 항상 "MX"
      doc_subtype  : "cbpr_plus" | "standard"  — CBPRPlus 우선순위 구분
      section      : 대분류 섹션명  (예: "Element_Specifications", "Business_Rules")
      xml_path     : 전체 XPath    (예: "/Document/FIToFICstmrCdtTrf/GrpHdr/MsgId")
      element_name : XML 태그명    (예: "MsgId"), 없으면 "none"
      mult_norm    : 정규화 다중성  (예: "1..1", "0..1", "1..*"), 없으면 "none"
      source_file  : PDF 파일명 (확장자 제외, 예: "MX_pacs_008_001_14")
                     중복 적재 방지 삭제 필터의 기준 키
    """
    doc_category: str = "MX"
    doc_subtype:  str = "standard"  # "cbpr_plus" | "standard"
    variant:      str = ""          # CBPRPlus 변형: "STP" | "ADV" | "COV" | "MultipleCharges" | ""
    section:      str = "none"
    xml_path:     str = "none"
    element_name: str = "none"
    mult_norm:    str = "none"
    source_file:  str = ""   # PDF stem — 재적재 시 dedup 필터 키

    def embedding_text(self) -> str:
        """컨텍스트 보완 헤더 + 청크 본문 (XML 계층 문맥 보존)."""
        mult_disp = self.mult_norm if self.mult_norm != "none" else self.multiplicity or "none"
        elem_disp = (
            f"{self.element_name} ({mult_disp})"
            if self.element_name != "none" else "none"
        )
        header = (
            f"[Document: {self.msg_type}]\n"
            f"[Category: {self.category}]\n"
            f"[Section: {self.section}]\n"
            f"[XML Path: {self.xml_path}]\n"
            f"[Element: {elem_disp}]\n"
            f"{'─' * 50}\n"
        )
        return header + self.text


class CbprSr2026Chunk(BaseModel):
    """
    CBPR+ SR2026 XPath 변경 사항(CR) 청크.

    Qdrant 페이로드 메타데이터:
      - msg_type       : "pacs.008" 등 (usage_guideline에서 추출)
      - doc_type       : 항상 "cbpr_sr2026_cr"
      - cr_id          : "CR 2006" 등
      - cr_title       : CR 제목 전체
      - usage_guideline: "CBPRPlus-pacs.008.001.08_..." 등
      - field_path     : "/Document/FIToFICstmrCdtTrf/..." XPath 값
    """

    chunk_id:        str
    msg_type:        str  = "pacs.008"   # usage_guideline에서 추출; 파싱 실패 시 기본값
    doc_type:        str  = "cbpr_sr2026_cr"
    cr_id:           str  = ""
    cr_title:        str  = ""
    usage_guideline: str  = ""
    field_path:      str  = ""          # xpath Impacted 값
    text:            str  = ""

    def embedding_text(self) -> str:
        parts = [
            f"[{self.msg_type}]",
            f"CR:{self.cr_id}",
        ]
        if self.cr_title:
            parts.append(self.cr_title)
        if self.field_path:
            parts.append(f"XPath:{self.field_path}")
        if self.usage_guideline:
            parts.append(self.usage_guideline)
        return " ".join(parts) + f"\n{self.text}"

    @property
    def page(self) -> int:
        return 0

    @property
    def message_type(self) -> str:
        return self.msg_type


# ---------------------------------------------------------------------------
# MX PDF 파싱 상수
# ---------------------------------------------------------------------------

# "1.4.2.3 InterbankSettlementAmount <IntrBkSttlmAmt>" 형식 섹션 헤더
_RE_MX_SECTION = re.compile(
    r"^(\d+\.\d+(?:\.\d+){0,4})\s+([\w][^<\n]{0,80}?)\s*(<[A-Za-z][A-Za-z0-9]*>)",
    re.MULTILINE,
)

# "C1  ActiveCurrency ✓" 형식 제약 조건 헤더
_RE_MX_CONSTR = re.compile(
    r"^(C\d+)\s+([\w][\w\s/]+?)(?:\s*✓)?\s*$",
    re.MULTILINE,
)

# PDF 반복 헤더·푸터 제거 패턴
_RE_MX_ARTIFACT = re.compile(
    r"^(?:Standards\s+MX"
    r"|\d+\s+Message\s+Reference\s+Guide[^\n]*"
    r"|Message\s+Reference\s+Guide\s*-[^\n]*"
    r"|MX\s+pacs\.\d+\.\d+\.\d+[^\n]*"
    r")\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# Presence / Datatype 추출
_RE_MX_PRESENCE = re.compile(r"Presence:\s*(\[[\d.*n]+\])", re.MULTILINE)
_RE_MX_DATATYPE = re.compile(r"Datatype:\s*([^\n]+)", re.MULTILINE)

# "Impacted by: ✓C1 ActiveCurrency ✓, ✓C11..." 에서 제약 조건 ID 목록
_RE_CONSTR_IDS = re.compile(r"\bC\d+\b")

# 파일명에서 MX 타입 추출: "pacs_008" → "pacs.008"
_RE_MX_TYPE_FROM_FILE = re.compile(r"([a-z]+)[._](\d{3})", re.IGNORECASE)

# usage_guideline에서 MX 타입 추출: "CBPRPlus-pacs.008.001.08_..." → "pacs.008"
_RE_MSG_TYPE_UG = re.compile(r"CBPRPlus-([a-z]+)\.(\d{3})\.", re.IGNORECASE)


# ---------------------------------------------------------------------------
# MX PDF 파싱 헬퍼
# ---------------------------------------------------------------------------

def _clean_mx_text(text: str) -> str:
    """MX PDF 반복 헤더·푸터를 제거하고 페이지 번호 줄을 제거한다."""
    text = _RE_MX_ARTIFACT.sub("", text)
    text = re.sub(r"^\s*\d{1,3}\s*$", "", text, flags=re.MULTILINE)
    return text


def _build_mx_field_path(
    section_num: str,
    xml_tag: str,
    section_tag_map: dict[str, str],
    max_depth: int = 4,
) -> str:
    """
    섹션 번호와 태그 맵에서 부모→자식 필드 경로를 구성한다.

    예:
        section_num="1.4.2.3", xml_tag="IntrBkSttlmAmt"
        section_tag_map={"1.4.2": "CdtTrfTxInf", ...}
        → "CdtTrfTxInf/IntrBkSttlmAmt"

    깊이 규칙:
        "1.4" 처럼 점(.)이 2개 이하인 최상위 섹션(컨테이너 레이블)은
        실제 XML 필드가 아니므로 경로에 포함하지 않는다.
    """
    parts = section_num.split(".")
    ancestors: list[str] = []

    # 부모 → 조부모 순으로 탐색 (역순 후 뒤집기)
    for depth in range(len(parts) - 1, 0, -1):
        ancestor_num = ".".join(parts[:depth])

        # "1.4" (점 2개 이하) 는 최상위 컨테이너 섹션 → 여기서 중단
        if len(ancestor_num.split(".")) < 3:
            break

        ancestor_tag = section_tag_map.get(ancestor_num)
        if ancestor_tag and ancestor_tag != xml_tag:
            ancestors.insert(0, ancestor_tag)

        if len(ancestors) >= max_depth - 1:
            break

    return "/".join(ancestors + [xml_tag]) if ancestors else xml_tag


# ---------------------------------------------------------------------------
# 공개 API — MX 가이드북 청킹 (신규)
# ---------------------------------------------------------------------------

def chunk_mx_guidebook(
    pdf_path: str,
    msg_type: Optional[str] = None,
    category: Optional[str] = None,
) -> list[MXFieldChunk]:
    """
    MX (ISO 20022) 가이드북 PDF를 필드 단위 청크 목록으로 변환한다.

    청킹 규칙:
      - PyMuPDF로 페이지 텍스트를 추출하고 전체 코퍼스로 합친다.
      - 정규식으로 "N.N.N FieldName <XmlTag>" 헤더를 감지하여 섹션 단위 분할.
      - 각 섹션에서 Presence / Datatype / Constraints 를 구조적으로 추출.
      - 부모 섹션 태그를 추적하여 field_path 자동 구성.

    Args:
        pdf_path : MX 가이드북 PDF 경로 (예: MX_pacs_008_001_14.pdf)
        msg_type : MX 타입 (기본: 파일명 자동 추론, 예 "pacs.008")
        category : 카테고리 폴더명 (기본: PDF 부모 폴더명 자동 추출,
                   예 "data/MX/pacs/foo.pdf" → "pacs")

    Returns:
        MXFieldChunk 목록 (각 청크에 category 메타데이터 포함)
    """
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF를 찾을 수 없습니다: {pdf_path}")

    # msg_type 자동 추론 ("MX_pacs_008_001_14" → "pacs.008")
    if msg_type is None:
        m = _RE_MX_TYPE_FROM_FILE.search(path.stem)
        msg_type = f"{m.group(1).lower()}.{m.group(2)}" if m else "pacs.008"

    # category 자동 추론: PDF 직계 부모 폴더명을 사용
    # data/MX/pacs/MX_pacs_008_001_14.pdf → "pacs"
    if category is None:
        category = path.parent.name
        grandparent = path.parents[1].name if len(path.parents) > 1 else ""
        if grandparent.upper() not in ("MT", "MX", "DATA", ""):
            log.warning("deep_pdf_nesting_category_inferred",
                        pdf_path=str(path), category=category,
                        hint="Pass category= explicitly if wrong")

    # 텍스트 추출
    pages = _extract_pages(pdf_path)
    if not pages:
        return []

    full_text, page_map = _build_corpus(pages)
    full_text = _clean_mx_text(full_text)

    # ── 섹션 헤더 파싱 ──────────────────────────────────────────────────────
    boundaries: list[tuple[int, str, str, str]] = []
    # (오프셋, 섹션번호, 필드명, xml_tag)
    seen_nums: set[str] = set()

    for m in _RE_MX_SECTION.finditer(full_text):
        sec_num   = m.group(1).strip()
        field_name = m.group(2).strip()
        xml_tag   = m.group(3).strip("<>")

        # 중복 섹션 번호는 TOC 항목 → 첫 번째 등장만 사용
        if sec_num in seen_nums:
            continue
        # "1." 으로 시작하는 섹션만 처리
        if not sec_num.startswith("1."):
            continue

        seen_nums.add(sec_num)
        boundaries.append((m.start(), sec_num, field_name, xml_tag))

    if not boundaries:
        # 섹션 감지 실패 시 전체 텍스트를 단일 청크로 반환
        return [MXFieldChunk(
            chunk_id=make_chunk_id(msg_type, "full", "doc"),
            msg_type=msg_type,
            doc_type="mx_guide",
            category=category,
            section_title="Full Document",
            text=full_text[:8000],
        )]

    # 섹션 번호 → XML 태그 매핑 (필드 경로 구성용)
    section_tag_map: dict[str, str] = {b[1]: b[3] for b in boundaries}

    # ── 청크 생성 ────────────────────────────────────────────────────────────
    chunks: list[MXFieldChunk] = []
    seen_ids: set[str] = set()

    for i, (start, sec_num, field_name, xml_tag) in enumerate(boundaries):
        end  = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(full_text)
        text = full_text[start:end].strip()
        text = re.sub(r"\n{3,}", "\n\n", text)

        if not text or len(text) < 20:
            continue

        # 구조화 필드 추출
        presence_m = _RE_MX_PRESENCE.search(text)
        datatype_m = _RE_MX_DATATYPE.search(text)
        impacted_m = re.search(r"Impacted by:\s*([^\n]+)", text)

        multiplicity = presence_m.group(1) if presence_m else ""
        datatype     = datatype_m.group(1).strip() if datatype_m else ""
        datatype     = re.sub(r"\s+on\s+page\s+\d+", "", datatype).strip()

        constraints: list[str] = []
        if impacted_m:
            constraints = list(set(_RE_CONSTR_IDS.findall(impacted_m.group(1))))

        field_path = _build_mx_field_path(sec_num, xml_tag, section_tag_map)
        page       = _page_at(start, page_map)
        cid        = make_chunk_id(msg_type, sec_num, xml_tag, str(page))

        if cid in seen_ids:
            continue
        seen_ids.add(cid)

        chunks.append(MXFieldChunk(
            chunk_id=cid,
            msg_type=msg_type,
            field_path=field_path,
            xml_tag=xml_tag,
            doc_type="mx_guide",
            page_label=page,
            category=category,
            section_title=f"{field_name} <{xml_tag}>",
            section_num=sec_num,
            multiplicity=multiplicity,
            datatype=datatype,
            constraints=constraints,
            text=text,
        ))

    return chunks


# ---------------------------------------------------------------------------
# 공개 API — CBPR+ SR2026 CSV 청킹 (신규)
# ---------------------------------------------------------------------------

def chunk_cbpr_sr2026_csv(csv_path: str) -> list[CbprSr2026Chunk]:
    """
    CBPR+ SR2026 XPath 변경 사항 CSV를 청크 목록으로 변환한다.

    CSV 구조 (행 유형):
      - CR 헤더행 : col0="CR 2006", col1="<CR 제목>"
      - 구분행    : col0="" (빈 행)
      - 데이터 헤더: col0="Usage Guideline", col1="xpath Impacted"
      - 데이터행  : col0="CBPRPlus-pacs.008...", col1="/Document/..."

    각 데이터행 → CbprSr2026Chunk 1개.

    Args:
        csv_path : CBPR+_SR2026_Impacted_Xpaths_v2.0.csv 경로

    Returns:
        CbprSr2026Chunk 목록
    """
    try:
        import pandas as pd
    except ImportError as exc:
        raise ImportError(
            "pandas 미설치. pip install pandas 실행 후 재시도."
        ) from exc

    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV를 찾을 수 없습니다: {csv_path}")

    df = pd.read_csv(csv_path, header=None, dtype=str, keep_default_na=False)

    chunks: list[CbprSr2026Chunk] = []
    seen_ids: set[str] = set()

    current_cr_id    = ""
    current_cr_title = ""

    for _, row in df.iterrows():
        # 선행 탭/공백 제거 (일부 행에 탭 접두어 존재)
        col0 = str(row.iloc[0]).strip().lstrip("\t ")
        col1 = str(row.iloc[1]).strip() if len(row) > 1 else ""

        # CR 섹션 헤더 감지 — "CR 2006" 등
        cr_m = re.match(r"^(CR\s+\d+)$", col0, re.IGNORECASE)
        if cr_m:
            current_cr_id    = cr_m.group(1).strip()
            current_cr_title = col1
            continue

        # 빈 행 또는 열 헤더 행 건너뜀
        if not col0 or col0 in ("Usage Guideline",) or not col1.startswith("/Document/"):
            continue

        usage_guideline = col0
        field_path      = col1

        # msg_type 추출: "CBPRPlus-pacs.008.001.08_..." → "pacs.008"
        ug_m     = _RE_MSG_TYPE_UG.search(usage_guideline)
        msg_type = (
            f"{ug_m.group(1).lower()}.{ug_m.group(2)}" if ug_m else "unknown"
        )

        text = (
            f"CR: {current_cr_id} - {current_cr_title}\n"
            f"Usage Guideline: {usage_guideline}\n"
            f"Impacted XPath: {field_path}"
        )

        cid = make_chunk_id(
            "cbpr_sr2026",
            current_cr_id,
            usage_guideline[:40],
            field_path[-60:],
        )
        if cid in seen_ids:
            continue
        seen_ids.add(cid)

        chunks.append(CbprSr2026Chunk(
            chunk_id=cid,
            msg_type=msg_type,
            doc_type="cbpr_sr2026_cr",
            cr_id=current_cr_id,
            cr_title=current_cr_title,
            usage_guideline=usage_guideline,
            field_path=field_path,
            text=text,
        ))

    return chunks


# ===========================================================================
# ── 2. 레거시 텍스트 기반 청킹 (기존 테스트 호환 유지) ────────────────────────
# ===========================================================================

class DocType(str, Enum):
    MT      = "mt"
    MX      = "mx"
    UNKNOWN = "unknown"


class RuleType(str, Enum):
    PRESENCE = "presence"
    FORMAT   = "format"
    NETWORK  = "network"
    BUSINESS = "business"
    USAGE    = "usage"


class SwiftChunk(BaseModel):
    chunk_id:      str
    source_type:   str           = "mt"
    level:         str           = "rule"
    message_type:  str
    field_tag:     Optional[str] = None
    element_path:  Optional[str] = None
    multiplicity:  Optional[str] = None
    rule_id:       Optional[str] = None
    rule_type:     Optional[RuleType] = None
    page:          int
    parent_id:     Optional[str] = None
    text:          str

    def embedding_text(self) -> str:
        parts: list[str] = []
        if self.message_type:
            parts.append(f"[{self.message_type}]")
        if self.field_tag:
            parts.append(f"Field {self.field_tag}:")
        if self.element_path:
            parts.append(f"Element {self.element_path}:")
        if self.rule_id:
            parts.append(f"Rule {self.rule_id}:")
        parts.append(self.text)
        return " ".join(parts)


# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------

def make_chunk_id(*parts: str) -> str:
    """결정론적 UUID v5 청크 ID 생성."""
    raw = "::".join(parts)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"swift:{raw}"))


def chunk_id_to_point_id(chunk_id: str) -> str:
    """Qdrant UUID 포인트 ID (chunk_id 자체가 UUID 문자열)."""
    return chunk_id


# ---------------------------------------------------------------------------
# 정규식 패턴 (레거시)
# ---------------------------------------------------------------------------

_RE_MT_TYPE  = re.compile(r"\b(MT\s?\d{3})\b", re.I)
_RE_MX_TYPE  = re.compile(r"\b([a-z]{4}\.\d{3}\.\d{3}\.\d{2})\b", re.I)
_RE_MT_FIELD = re.compile(r"^\s*(?:Field\s+)?(\d{1,2}[A-Z]?)\b[:\s]", re.M)
_RE_MX_ELEM  = re.compile(r"\b([A-Z][a-z][a-zA-Z]{2,})\b")
_RE_XPATH    = re.compile(r"\b([A-Z][a-zA-Z]{2,}(?:/[A-Z][a-zA-Z]{2,})+)\b")
_RE_MULT     = re.compile(r"\[(\d+)\.\.(\d+|n)\]")
_RE_MT_RULE  = re.compile(r"\b([CDT]\d{1,3})\b")
_RE_BIZ_RULE = re.compile(r"\b(BR-\d{1,4}[a-z]?)\b", re.I)
_RE_RULE_ANY = re.compile(r"\b([CDT]\d{1,3}|BR-\d{1,4}[a-z]?)\b", re.I)
_RE_FORMAT   = re.compile(r"\b\d+[!*]?[anxcde]\b")

_TABLE_MT_KEYWORDS   = {"status", "mandatory", "optional", "presence", "sequence"}
_TABLE_MX_KEYWORDS   = {"mult", "multiplicity", "type", "definition", "iso"}
_TABLE_RULE_KEYWORDS = {"rule", "c1", "c2", "d49", "t26", "br-"}


@dataclass
class _ChunkingContext:
    doc_type:       DocType      = DocType.MT
    current_mt:     Optional[str] = None
    current_field:  Optional[str] = None
    field_chunk_id: Optional[str] = None
    page_no:        int           = 0

    def reset_field(self) -> None:
        self.current_field = None
        self.field_chunk_id = None


def classify_rule(text: str) -> RuleType:
    if _RE_BIZ_RULE.search(text):
        return RuleType.BUSINESS
    if _RE_MT_RULE.search(text):
        if re.search(r"\bT\d{1,3}\b", text):
            return RuleType.NETWORK
        if re.search(r"\bD\d{1,3}\b", text):
            return RuleType.NETWORK
        return RuleType.PRESENCE
    if _RE_FORMAT.search(text) or re.search(r"\bformat\b", text.lower()):
        return RuleType.FORMAT
    return RuleType.USAGE


def _table_header_type(headers: list[Any]) -> str:
    flat = " ".join(str(h or "").lower() for h in headers)
    if any(kw in flat for kw in _TABLE_MX_KEYWORDS):
        return "mx_element"
    if any(kw in flat for kw in _TABLE_MT_KEYWORDS):
        return "mt_presence"
    if any(kw in flat for kw in _TABLE_RULE_KEYWORDS) or _RE_MT_RULE.search(flat):
        return "rule_table"
    return "unknown"


def _parse_mx_element_table(
    rows: list[list[Any]],
    ctx: _ChunkingContext,
    page: int,
) -> list[SwiftChunk]:
    chunks: list[SwiftChunk] = []
    if not ctx.current_mt:
        return chunks
    for row in rows:
        cells = [str(c or "").strip() for c in row]
        if not any(cells):
            continue
        name = next((c for c in cells if c and _RE_MX_ELEM.match(c)), None)
        if not name:
            continue
        mult       = next((c for c in cells if _RE_MULT.search(c)), None)
        definition = max(cells, key=len) if cells else ""
        xpath      = f"{ctx.current_field}/{name}" if ctx.current_field else name
        text       = f"{name} {mult or ''}: {definition}".strip()
        cid        = make_chunk_id(ctx.current_mt, "element", xpath, f"p{page}")
        chunks.append(SwiftChunk(
            chunk_id=cid, source_type="mx", level="element",
            message_type=ctx.current_mt, field_tag=name, element_path=xpath,
            multiplicity=mult, rule_type=RuleType.FORMAT,
            page=page, parent_id=ctx.field_chunk_id, text=text,
        ))
    return chunks


def _parse_mt_presence_table(
    rows: list[list[Any]],
    ctx: _ChunkingContext,
    page: int,
) -> list[SwiftChunk]:
    chunks: list[SwiftChunk] = []
    if not ctx.current_mt:
        return chunks
    for row in rows:
        cells = [str(c or "").strip() for c in row]
        if not any(cells):
            continue
        tag = next((c for c in cells if re.match(r"^\d{1,2}[A-Z]?$", c)), None)
        if not tag:
            continue
        status     = next((c for c in cells if c.upper() in
                           ("M", "O", "C", "MANDATORY", "OPTIONAL", "CONDITIONAL")), None)
        definition = max(cells, key=len) if cells else ""
        text       = f"Field {tag} [{status or '?'}]: {definition}".strip()
        cid        = make_chunk_id(ctx.current_mt, "field", tag, f"p{page}")
        chunks.append(SwiftChunk(
            chunk_id=cid, source_type="mt", level="field",
            message_type=ctx.current_mt, field_tag=tag,
            rule_type=RuleType.PRESENCE, page=page, text=text,
        ))
    return chunks


def _parse_rule_table(
    rows: list[list[Any]],
    ctx: _ChunkingContext,
    page: int,
) -> list[SwiftChunk]:
    chunks: list[SwiftChunk] = []
    if not ctx.current_mt:
        return chunks
    for row in rows:
        cells     = [str(c or "").strip() for c in row]
        full_text = " ".join(cells)
        rule_ids  = _RE_RULE_ANY.findall(full_text)
        if not rule_ids:
            continue
        definition = max(cells, key=len)
        for rid in set(rule_ids):
            cid = make_chunk_id(ctx.current_mt, ctx.current_field or "msg", rid, f"p{page}")
            chunks.append(SwiftChunk(
                chunk_id=cid, source_type=ctx.doc_type.value, level="rule",
                message_type=ctx.current_mt, field_tag=ctx.current_field,
                rule_id=rid.upper() if rid.upper().startswith(("C", "D", "T")) else rid,
                rule_type=classify_rule(full_text),
                page=page, parent_id=ctx.field_chunk_id,
                text=definition or full_text,
            ))
    return chunks


def _process_text_block(
    text: str,
    page: int,
    ctx: _ChunkingContext,
    blk_index: int = 0,
) -> list[SwiftChunk]:
    chunks: list[SwiftChunk] = []
    text = text.strip()
    if not text:
        return chunks

    mt_m = _RE_MT_TYPE.search(text)
    mx_m = _RE_MX_TYPE.search(text)
    if (mt_m or mx_m) and len(text) < 150:
        if mx_m:
            raw_type   = mx_m.group(1).lower()
            ctx.doc_type = DocType.MX
        else:
            raw_type   = mt_m.group(1).replace(" ", "").upper()  # type: ignore[union-attr]
            ctx.doc_type = DocType.MT
        ctx.current_mt = raw_type.upper() if ctx.doc_type == DocType.MT else raw_type
        ctx.reset_field()
        cid = make_chunk_id(ctx.current_mt, "msg", f"p{page}")
        chunks.append(SwiftChunk(
            chunk_id=cid, source_type=ctx.doc_type.value, level="message",
            message_type=ctx.current_mt, page=page, text=text,
        ))
        return chunks

    if ctx.current_mt is None:
        return chunks

    if ctx.doc_type == DocType.MT:
        fm = _RE_MT_FIELD.match(text)
        if fm:
            tag = fm.group(1)
            ctx.current_field = tag
            cid = make_chunk_id(ctx.current_mt, "field", tag, f"p{page}")
            ctx.field_chunk_id = cid
            chunks.append(SwiftChunk(
                chunk_id=cid, source_type="mt", level="field",
                message_type=ctx.current_mt, field_tag=tag,
                rule_type=RuleType.FORMAT, page=page, text=text,
            ))
            return chunks

    if ctx.doc_type == DocType.MX:
        xpath_m = _RE_XPATH.search(text)
        if xpath_m and len(text) < 300:
            xpath = xpath_m.group(1)
            top   = xpath.split("/")[0]
            ctx.current_field = top
            mult      = _RE_MULT.search(text)
            mult_str  = mult.group(0) if mult else None
            cid = make_chunk_id(ctx.current_mt, "field", top, f"p{page}")
            ctx.field_chunk_id = cid
            chunks.append(SwiftChunk(
                chunk_id=cid, source_type="mx", level="field",
                message_type=ctx.current_mt, field_tag=top, element_path=xpath,
                multiplicity=mult_str, rule_type=RuleType.FORMAT,
                page=page, text=text,
            ))
            return chunks

    rule_ids = _RE_RULE_ANY.findall(text)
    if rule_ids:
        for rid in set(rule_ids):
            sentences = [s for s in re.split(r"(?<=[.;\n])", text) if rid in s]
            rule_text = " ".join(sentences).strip() or text
            cid = make_chunk_id(
                ctx.current_mt, ctx.current_field or "msg", rid, f"p{page}b{blk_index}",
            )
            chunks.append(SwiftChunk(
                chunk_id=cid, source_type=ctx.doc_type.value, level="rule",
                message_type=ctx.current_mt, field_tag=ctx.current_field,
                rule_id=rid, rule_type=classify_rule(rule_text),
                page=page, parent_id=ctx.field_chunk_id, text=rule_text,
            ))
        return chunks

    if len(text) > 20:
        cid = make_chunk_id(
            ctx.current_mt, ctx.current_field or "msg", "usage", f"p{page}b{blk_index}",
        )
        chunks.append(SwiftChunk(
            chunk_id=cid, source_type=ctx.doc_type.value, level="rule",
            message_type=ctx.current_mt, field_tag=ctx.current_field,
            rule_type=RuleType.USAGE, page=page, parent_id=ctx.field_chunk_id, text=text,
        ))
    return chunks


def chunk_text(
    pages: list[str],
    doc_type: DocType = DocType.MT,
    start_page: int = 1,
) -> list[SwiftChunk]:
    """텍스트 기반 레거시 청킹 (테스트 및 비-PDF 입력용)."""
    ctx    = _ChunkingContext(doc_type=doc_type)
    chunks: list[SwiftChunk] = []
    seen:   set[str] = set()

    for offset, text in enumerate(pages):
        page = start_page + offset
        for blk_i, raw_blk in enumerate(text.split("\n\n")):
            for chunk in _process_text_block(raw_blk, page, ctx, blk_i):
                if chunk.chunk_id not in seen:
                    seen.add(chunk.chunk_id)
                    chunks.append(chunk)
    return chunks


def chunk_guidebook(pdf_path: str) -> list[SwiftChunk]:
    """
    레거시 PDF 청킹 (표 기반 hierarchical 구조).
    새 코드는 chunk_mt_guidebook() 를 사용하세요.
    """
    doc  = fitz.open(pdf_path)
    ctx  = _ChunkingContext()
    chunks: list[SwiftChunk] = []
    seen: set[str] = set()

    def _add(c: SwiftChunk) -> None:
        if c.chunk_id not in seen:
            seen.add(c.chunk_id)
            chunks.append(c)

    for page_no in range(len(doc)):
        page     = doc[page_no]
        page_num = page_no + 1
        ctx.page_no = page_no

        table_rects: list[fitz.Rect] = []
        try:
            finder = page.find_tables()
            for tbl in finder.tables:
                data = tbl.extract()
                if not data or len(data) < 2:
                    continue
                table_rects.append(tbl.bbox)
                headers = data[0]
                rows    = data[1:]
                ttype   = _table_header_type(headers)
                if ttype == "mx_element":
                    for c in _parse_mx_element_table(rows, ctx, page_num):
                        _add(c)
                elif ttype == "mt_presence":
                    for c in _parse_mt_presence_table(rows, ctx, page_num):
                        _add(c)
                elif ttype == "rule_table":
                    for c in _parse_rule_table(rows, ctx, page_num):
                        _add(c)
        except Exception:
            pass

        blocks = page.get_text("blocks")
        for blk in sorted(blocks, key=lambda b: (b[1], b[0])):
            blk_rect = fitz.Rect(blk[0], blk[1], blk[2], blk[3])
            if any(blk_rect.intersects(tr) for tr in table_rects):
                continue
            text = blk[4].strip()
            if not text:
                continue
            for c in _process_text_block(text, page_num, ctx, int(blk[5])):
                _add(c)

    return chunks
