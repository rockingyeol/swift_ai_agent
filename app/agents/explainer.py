"""
Explainer Agent — SWIFT 전문 유형 기본 정보 설명.

사용자가 "MT112가 뭐야?", "pacs.008 설명해줘" 처럼 질문하면
가이드북 RAG + LLM으로 전문 명칭·목적·주요 필드 등을 구조화해 반환한다.
"""
from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Literal, Optional

import structlog
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.graph.state import AgentState
from app.llm import format_rag_context, get_chat_llm, parse_llm_json
from app.prompts.explainer_prompts import (
    EXPLAINER_SYSTEM, EXPLAINER_USER, EXPLAINER_FEWSHOT,
    MAPPING_RULE_SYSTEM, MAPPING_RULE_USER, MAPPING_RULE_FEWSHOT,
    GENERAL_QA_SYSTEM, GENERAL_QA_USER,
)
from app.rag.retriever import SwiftRetriever

log = structlog.get_logger(__name__)

_FIELD_DETAIL_KEYWORDS = ["전체 필드", "모든 필드", "전체필드", "필수 필드", "필수필드",
                          "all fields", "mandatory fields", "전체 스키마", "필드 목록"]


def _detect_field_detail(message: str) -> str | None:
    """전체/필수 필드 상세 요청이면 filter_mode 반환, 아니면 None."""
    lower = message.lower()
    if any(k in lower for k in ["전체 필드", "모든 필드", "전체필드", "all fields", "전체 스키마", "필드 목록"]):
        return "all"
    if any(k in lower for k in ["필수 필드", "필수필드", "mandatory fields"]):
        return "mandatory"
    return None

_retriever: SwiftRetriever | None = None
_retriever_lock = threading.Lock()


def _get_retriever() -> SwiftRetriever:
    global _retriever
    if _retriever is not None:
        return _retriever
    with _retriever_lock:
        if _retriever is None:
            _retriever = SwiftRetriever()
    return _retriever


# ---------------------------------------------------------------------------
# RAG 청크에서 태그별 샘플값 추출
# ---------------------------------------------------------------------------

# 코드표: 줄 시작에 대문자 코드 4~10자 + 공백 2칸 이상 + 설명
_RE_CODE_TABLE = re.compile(r"^([A-Z]{4,10})\s{2,}\S", re.MULTILINE)
# 명시적 예시 표기
_RE_EXAMPLE    = re.compile(r"(?:Example|예시|예:|e\.g\.)[:\s]+([^\n]{1,80})", re.IGNORECASE)
# 리프 필드 판별: 복합 컨테이너 태그는 샘플값 불필요
_CONTAINER_SUFFIXES = ("dtls", "inf", "initn", "ownr", "agt", "acct", "prtry",
                       "sndr", "rcvr", "pty", "id", "adr", "pstladr")


def _is_container_tag(tag: str) -> bool:
    """복합 컨테이너 요소는 단순 샘플값이 없으므로 건너뜀."""
    t = re.sub(r"[<>/\s]", "", tag).lower()
    return any(t.endswith(s) for s in _CONTAINER_SUFFIXES)


def _extract_path_map(chunks: list) -> dict[str, str]:
    """RAG 청크에서 xml_tag → xml_path 매핑을 추출한다."""
    path_map: dict[str, str] = {}
    for c in chunks:
        xml_tag  = getattr(c, "xml_tag", None)
        xml_path = getattr(c, "xml_path", None)
        if xml_tag and xml_path and xml_path.lower() not in ("none", ""):
            key = re.sub(r"[<>/\s]", "", xml_tag).lower()
            if key not in path_map:
                path_map[key] = xml_path
    return path_map


def _extract_sample_map(chunks: list) -> dict[str, str]:
    """RAG 청크 텍스트에서 xml_tag → 샘플값 매핑을 추출한다.
    오탐을 줄이기 위해 명시적 코드표와 예시 표기만 사용한다."""
    sample_map: dict[str, str] = {}
    for c in chunks:
        xml_tag = getattr(c, "xml_tag", None) or getattr(c, "element_name", None)
        if not xml_tag:
            continue
        if _is_container_tag(xml_tag):
            continue
        key = re.sub(r"[<>/\s]", "", xml_tag).lower()
        if key in sample_map:
            continue

        text = getattr(c, "text", "") or ""

        # 1. 명시적 예시 문장 (Example: / 예시: 등)
        m = _RE_EXAMPLE.search(text)
        if m:
            val = m.group(1).strip()[:60]
            # 숫자만이거나 점으로 구분된 버전 번호(1.2.3)는 제외
            if not re.match(r"^[\d.]+$", val):
                sample_map[key] = val
            continue

        # 2. 코드표 첫 번째 코드값 (NEWA, PASS, SHAR, SWIFTNET 등)
        codes = _RE_CODE_TABLE.findall(text)
        if codes:
            sample_map[key] = codes[0]

    return sample_map


# ---------------------------------------------------------------------------
# Pydantic 출력 스키마
# ---------------------------------------------------------------------------

class KeyField(BaseModel):
    tag:          str            = Field(description="SWIFT 필드 태그 (예: :32A:, <IntrBkSttlmAmt>)")
    name:         str            = Field(description="필드 한국어 명칭")
    mandatory:    bool           = Field(description="필수 여부")
    description:  str            = Field(description="필드 역할 설명")
    sample_value: Optional[str]  = Field(None, description="가이드북 예시값")
    xml_path:     Optional[str]  = Field(None, description="XML 전체 경로 (RAG 청크 추출)")


class SpecialCode(BaseModel):
    code:    str = Field(description="짧은 코드값 (예: /PAID/, HOLD, SHA) — 30자 이하")
    meaning: str = Field(description="한국어 한 줄 설명")

    @classmethod
    def is_valid_code(cls, code: str) -> bool:
        """SWIFT 내러티브 문장(We hereby... 등)은 코드가 아니므로 제외."""
        if len(code) > 40:
            return False
        # 문장 형식(공백 포함 단어 4개 이상)은 내러티브 → 제외
        words = code.split()
        if len(words) >= 4:
            return False
        return True


class RelatedMessage(BaseModel):
    msg_type:     str = Field(description="관련 전문 유형")
    relationship: str = Field(description="관계 설명")


class ExplainerOutput(BaseModel):
    """Explainer Agent 구조화 출력 스키마."""
    msg_type:           str               = Field(description="전문 유형 (예: MT112)")
    msg_type_full_name: str               = Field(description="영문 공식 명칭")
    msg_type_korean:    str               = Field(description="한국어 명칭")
    purpose:            str               = Field(description="전문 목적 설명")
    use_cases:          List[str]         = Field(default_factory=list)
    key_fields:         List[KeyField]    = Field(default_factory=list)
    special_codes:      List[SpecialCode] = Field(default_factory=list)
    related_messages:   List[RelatedMessage] = Field(default_factory=list)
    flow_description:   str               = Field(default="")


# ---------------------------------------------------------------------------
# Mapping Rule 출력 스키마
# ---------------------------------------------------------------------------

class MappingDetail(BaseModel):
    condition:      str           = Field(description="적용 조건 (예: '/ACC/ 코드워드 포함 시')")
    mx_path:        str           = Field(description="완전한 MX XML 경로")
    mx_value_hint:  Optional[str] = Field(default=None, description="값 변환 힌트")
    notes:          Optional[str] = Field(default=None, description="주의사항")


class MappingRuleOutput(BaseModel):
    """필드 매핑 규칙 질문에 대한 구조화 출력."""
    query_type:      Literal["mapping_rule"] = "mapping_rule"
    source_field:    str            = Field(description="MT 필드 태그 (예: :72:)")
    source_msg_type: str            = Field(description="원본 전문 유형 (예: MT103)")
    target_msg_type: str            = Field(description="대상 전문 유형 (예: pacs.008.001.08)")
    mapping_summary: str            = Field(description="매핑 관계 핵심 요약")
    mapping_details: List[MappingDetail] = Field(default_factory=list)
    constraints:     List[str]      = Field(default_factory=list)
    guidebook_refs:  List[str]      = Field(default_factory=list)


# ---------------------------------------------------------------------------
# msg_type 추출 헬퍼
# ---------------------------------------------------------------------------

# MT 패턴: "MT112", "MT 103", "mt103", "MT101과" (한국어 조사 붙어도 인식)
_RE_MT  = re.compile(r"\bMT\s*(\d{3})(?!\d)", re.IGNORECASE)
# MX 패턴: "pacs.008", "camt.056.001.08", "pain.001"
_RE_MX  = re.compile(r"\b([a-z]{3,4}\.\d{3}(?:\.\d{3}(?:\.\d{2,3})?)?)\b", re.IGNORECASE)


def _extract_msg_type(query: str, state_msg_type: str) -> tuple[str, str]:
    """
    쿼리·상태에서 전문 유형을 추출한다.

    Returns:
        (normalized_msg_type, doc_category)  예: ("MT112", "MT") | ("pacs.008", "MX")
    """
    # 한국어 표기 정규화 ("엠티103" → "MT103")
    query = _normalize_query(query)

    # 상태에서 이미 설정된 경우 우선 사용
    if state_msg_type:
        mt = _RE_MT.search(state_msg_type)
        mx = _RE_MX.search(state_msg_type)
        if mt:
            return f"MT{mt.group(1)}", "MT"
        if mx:
            return mx.group(1).lower(), "MX"

    # 쿼리에서 추출
    mt = _RE_MT.search(query)
    if mt:
        return f"MT{mt.group(1)}", "MT"
    mx = _RE_MX.search(query)
    if mx:
        return mx.group(1).lower(), "MX"

    return state_msg_type or "", "MT"


# ---------------------------------------------------------------------------
# 쿼리 타입 감지
# ---------------------------------------------------------------------------

# 필드 태그 패턴: :72:, :50K:, Field 72, 필드 72
_RE_FIELD_TAG   = re.compile(r":(\d{1,2}[A-Za-z]?):|(?:field|필드)\s*(\d{1,2}[A-Za-z]?)", re.IGNORECASE)
# MX 메시지 유형 간 매핑 패턴: "pacs.008", "camt.056" 등
_RE_MX_TYPE     = re.compile(r"\b[a-z]{3,4}\.\d{3}\b", re.IGNORECASE)
# CamelCase MX element 이름 패턴: OrgnlTxRef, CdtTrfTxInf, InstrForCdtrAgt 등
_RE_MX_ELEMENT  = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]*)+)\b")
# MT→MX 매핑 의도 키워드
_MAPPING_KEYWORDS = [
    "매핑", "엘리먼트", "element", "경로", "path", "xpath",
    "어느", "어디", "분기", "코드워드", "code word", "codeword",
    "변환", "대응", "maps to", "mapped to",
    "채우나요", "채워", "채울", "구조는", "어떻게 채",
]
# MT 전문 유형 키워드 (MT + 번호, 한국어 조사 허용)
_RE_MT_TYPE = re.compile(r"\bMT\s*\d{3}(?!\d)", re.IGNORECASE)
# "MX전문", "MX 전문", "mx message" 등 MX 대상을 가리키는 표현
_RE_MX_TARGET = re.compile(r"MX\s*전문|MX\s*메시지|mx\s*message|iso\s*20022", re.IGNORECASE)
# 매핑 관계를 묻는 표현 ("같은 역할", "대응", "해당하는", "어떤 전문")
_MAPPING_RELATION_KW = [
    "같은 역할", "대응되는", "대응하는", "해당하는", "해당 mx", "해당 전문",
    "어떤 전문", "어느 전문", "무슨 전문", "어떤 메시지", "어느 메시지",
    "equivalent", "corresponds", "counterpart",
]


def _detect_mapping_rule_query(query: str) -> bool:
    """매핑 규칙 질문 여부 감지.

    아래 조건 중 하나라도 만족하면 mapping_rule 모드로 처리한다:
    1. 필드 태그(:72:, Field 72) + 매핑 키워드
    2. MX element 이름(CamelCase) + 매핑 키워드
    3. MT 전문 유형 + MX 전문 유형(pacs.008 등) 모두 언급
    4. MT 전문 유형 + MX 타겟 표현("MX전문", "ISO 20022") 또는 매핑 관계 표현
    """
    lower = query.lower()
    has_mapping_kw = any(kw in lower for kw in _MAPPING_KEYWORDS)
    has_mt = bool(_RE_MT_TYPE.search(query))

    # 조건 1: 필드 태그 + 매핑 키워드
    if bool(_RE_FIELD_TAG.search(query)) and has_mapping_kw:
        return True

    # 조건 2: MX element 이름(CamelCase) + 매핑 키워드
    if bool(_RE_MX_ELEMENT.search(query)) and has_mapping_kw:
        return True

    # 조건 3: MT 유형 + MX 유형(pacs.008 형식) 모두 명시
    if has_mt and bool(_RE_MX_TYPE.search(query)):
        return True

    # 조건 4: MT 유형 + "MX전문/MX메시지/ISO 20022" 또는 매핑 관계 표현
    if has_mt and (
        bool(_RE_MX_TARGET.search(query))
        or any(kw in lower for kw in _MAPPING_RELATION_KW)
        or has_mapping_kw
    ):
        return True

    return False


def _extract_field_tag(query: str) -> str:
    """쿼리에서 첫 번째 MT 필드 태그를 추출한다."""
    m = _RE_FIELD_TAG.search(query)
    if not m:
        return ""
    tag = m.group(1) or m.group(2)
    return f":{tag}:" if tag else ""


_FIELD_INFO_KEYWORDS = [
    "상세", "상세 정보", "알려줘", "설명해줘", "뭐야", "무엇", "정보",
    "format", "presence", "definition", "codes", "포맷", "정의",
    "어떤 필드", "어떤 태그",
]

# 순수 전문 소개/설명 요청 키워드 — 이 키워드가 있어야 main explainer 경로로 처리
# 없으면 _run_general_qa로 보내 LLM이 실제 질문에 맞게 자유 답변
_BASIC_EXPLAIN_KEYWORDS = [
    "뭐야", "뭔가요", "뭔지", "뭐임", "뭐에요", "뭔가",
    "무엇인가", "무엇인지", "무엇이야", "무엇이에요",
    "설명해줘", "설명해주세요", "설명해", "설명좀",
    "소개해줘", "소개해", "소개해주세요",
    "개요", "기본 정보", "기본정보",
    "what is", "explain", "describe", "overview",
]


def _detect_basic_explain_query(query: str) -> bool:
    """순수 전문 소개/설명 요청 감지 ('MT103이 뭐야?', '설명해줘' 등).
    True일 때만 main explainer(key_fields 카드) 경로를 사용한다."""
    lower = _normalize_query(query).lower()
    return any(kw in lower for kw in _BASIC_EXPLAIN_KEYWORDS)

# 필드 태그 + 이 키워드 조합 → general_qa 예시 3 (Usage Rules 특정 조회)
_FIELD_SECTION_KEYWORDS = [
    "usage rules", "usage rule", "작성 규칙", "사용 규칙", "사용규칙", "작성규칙",
    "rules", "룰", "규칙",
]


def _detect_field_usage_rules_query(query: str) -> bool:
    """특정 필드의 Usage Rules 조회 감지 (예: 'MT103 Field 20 Usage Rules')."""
    lower = _normalize_query(query).lower()
    has_field = bool(_RE_FIELD_TAG.search(query))
    has_section_kw = any(kw in lower for kw in _FIELD_SECTION_KEYWORDS)
    return has_field and has_section_kw and not _detect_mapping_rule_query(query)


def _detect_field_info_query(query: str) -> bool:
    """특정 필드 전체 스펙 조회 감지 (매핑 질문 및 Usage Rules 특정 조회 제외)."""
    if _detect_mapping_rule_query(query):
        return False
    # 필드 + Usage Rules 조합은 general_qa(예시 3)로 처리
    if _detect_field_usage_rules_query(query):
        return False
    lower = query.lower()
    has_field = bool(_RE_FIELD_TAG.search(query))
    has_info_kw = any(kw in lower for kw in _FIELD_INFO_KEYWORDS)
    return has_field and has_info_kw


# ---------------------------------------------------------------------------
# RAG 검색
# ---------------------------------------------------------------------------

def _search_rag_mapping(field_tag: str, source_msg_type: str, target_msg_type: str, query: str) -> list:
    """매핑 규칙 질문용 RAG: MT 필드 스펙 + MX 가이드 동시 검색."""
    retriever = _get_retriever()
    tag_bare = field_tag.strip(":")

    # MT 필드 스펙 검색
    mt_chunks = retriever.search(
        query=f"{source_msg_type} field {tag_bare} mapping MX ISO20022 {query}",
        filters={"doc_category": "MT", "msg_type": source_msg_type},
        top_k=4,
        rerank=True,
    )

    # MX / CBPR+ 매핑 가이드 검색
    mx_chunks = retriever.search(
        query=f"{source_msg_type} {field_tag} to {target_msg_type} mapping xpath {query}",
        filters={"doc_type": "mx_guide"},
        top_k=4,
        rerank=True,
    )

    # etc 카테고리(매핑 가이드북) 검색
    etc_chunks = retriever.search(
        query=f"{source_msg_type} {field_tag} {target_msg_type} mapping codeword {query}",
        filters={"category": "etc"},
        top_k=4,
        rerank=True,
    )

    seen, merged = set(), []
    for c in mt_chunks + mx_chunks + etc_chunks:
        if c.chunk_id not in seen:
            seen.add(c.chunk_id)
            merged.append(c)
    log.info("explainer_mapping_rag", field=field_tag, chunks=len(merged))
    return merged


def _search_rag(msg_type: str, doc_category: str, query: str, top_k: int = 8) -> list:
    """전문 유형의 개요(SYSTEM 섹션) + 필드 스펙 청크를 검색한다."""
    retriever = _get_retriever()

    search_query = (
        f"{msg_type} message overview scope purpose mandatory fields "
        f"definition format presence usage rules {query}"
    )

    filters: dict = {"doc_category": doc_category}
    if msg_type:
        filters["msg_type"] = msg_type

    chunks = retriever.search(
        query=search_query,
        filters=filters,
        top_k=top_k,
        rerank=True,
    )

    if not chunks and doc_category == "MT":
        chunks = retriever.search(
            query=search_query,
            filters={"doc_category": "MT"},
            top_k=top_k,
            rerank=True,
        )

    log.info("explainer_rag_search", msg_type=msg_type, chunks=len(chunks))
    return chunks


# ---------------------------------------------------------------------------
# LLM 체인
# ---------------------------------------------------------------------------

def _build_chain():
    prompt = ChatPromptTemplate.from_messages([
        ("system", EXPLAINER_SYSTEM),
        ("human",  EXPLAINER_USER),
    ])
    llm = get_chat_llm(temperature=0.0)
    structured = llm.with_structured_output(ExplainerOutput, method="json_schema")
    return prompt | structured


def _build_mapping_chain():
    prompt = ChatPromptTemplate.from_messages([
        ("system", MAPPING_RULE_SYSTEM),
        ("human",  MAPPING_RULE_USER),
    ])
    llm = get_chat_llm(temperature=0.0)
    structured = llm.with_structured_output(MappingRuleOutput, method="json_schema")
    return prompt | structured


def _fallback_parse_mapping(content: str, field_tag: str, source: str, target: str) -> MappingRuleOutput:
    raw = parse_llm_json(content)
    try:
        return MappingRuleOutput(
            source_field=raw.get("source_field", field_tag),
            source_msg_type=raw.get("source_msg_type", source),
            target_msg_type=raw.get("target_msg_type", target),
            mapping_summary=raw.get("mapping_summary", ""),
            mapping_details=[MappingDetail(**d) for d in raw.get("mapping_details", []) if isinstance(d, dict)],
            constraints=raw.get("constraints", []),
            guidebook_refs=raw.get("guidebook_refs", []),
        )
    except Exception as e:
        log.warning("mapping_fallback_parse_failed", error=str(e))
        return MappingRuleOutput(
            source_field=field_tag,
            source_msg_type=source,
            target_msg_type=target,
            mapping_summary="LLM 응답 파싱 실패",
        )


def _fallback_parse(content: str, msg_type: str) -> ExplainerOutput:
    """structured_output 실패 시 JSON 파싱 폴백."""
    raw = parse_llm_json(content)
    try:
        return ExplainerOutput(
            msg_type=raw.get("msg_type", msg_type),
            msg_type_full_name=raw.get("msg_type_full_name", ""),
            msg_type_korean=raw.get("msg_type_korean", ""),
            purpose=raw.get("purpose", ""),
            use_cases=raw.get("use_cases", []),
            key_fields=[KeyField(**f) for f in raw.get("key_fields", []) if isinstance(f, dict)],
            special_codes=[SpecialCode(**c) for c in raw.get("special_codes", []) if isinstance(c, dict)],
            related_messages=[RelatedMessage(**r) for r in raw.get("related_messages", []) if isinstance(r, dict)],
            flow_description=raw.get("flow_description", ""),
        )
    except Exception as e:
        log.warning("explainer_fallback_parse_failed", error=str(e))
        return ExplainerOutput(
            msg_type=msg_type,
            msg_type_full_name="(파싱 실패)",
            msg_type_korean="(파싱 실패)",
            purpose="LLM 응답 파싱에 실패했습니다.",
        )


# ---------------------------------------------------------------------------
# LangGraph 노드 진입점
# ---------------------------------------------------------------------------

def run_explainer(state: AgentState) -> Dict[str, Any]:
    """
    Explainer Agent LangGraph 노드.

    State 입력: raw_message (또는 masked_message), msg_type, user_intent
    State 출력: output (type="explanation", explanation=ExplainerOutput dict)
    """
    # masked_message는 "001.10" 같은 버전 번호를 AMT로 마스킹하므로
    # msg_type 추출에는 raw_message를 우선 사용한다
    raw_query  = state.get("raw_message") or ""
    query      = state.get("masked_message") or raw_query
    state_type = state.get("msg_type", "")

    # ── 1. 전문 유형 추출 ─────────────────────────────────────────────────
    msg_type, doc_category = _extract_msg_type(raw_query or query, state_type)
    log.info("explainer_start", msg_type=msg_type, doc_category=doc_category)

    # ── 2. 쿼리 타입 감지 ────────────────────────────────────────────────
    # 감지 함수는 raw_query 기준으로 실행 — masked_message는 spaCy NER이
    # 한국어 키워드(스코프·룰 등)를 PII로 잘못 마스킹할 수 있어 오탐 방지
    detect_src = raw_query or query

    if _detect_mapping_rule_query(detect_src):
        return _run_mapping_rule(state, query, msg_type)

    # 특정 필드의 Usage Rules 조회 (예: "MT103 Field 20 Usage Rules")
    if _detect_field_usage_rules_query(detect_src) and msg_type:
        return _run_general_qa(state, query)

    # 특정 필드 전체 스펙 조회 (예: "MT103 Field 72 상세 정보 알려줘")
    if _detect_field_info_query(detect_src) and msg_type:
        return _run_field_info(state, query, msg_type)

    # Usage Rules / Network Validated Rules / Scope 등 메시지 레벨 섹션 조회
    if _detect_section_query(detect_src):
        return _run_general_qa(state, query)

    # 특정 전문 유형이 없으면 일반 Q&A 모드
    if not msg_type:
        return _run_general_qa(state, query)

    # "전체 필드" / "모든 필드" / "필수 필드" 등 필드 목록 요청
    # → schema_explorer로 직접 라우팅 (key_fields 대신 전체 필드 트리 반환)
    field_detail_mode = _detect_field_detail(detect_src)
    if field_detail_mode and msg_type:
        log.info("explainer_field_detail_redirect",
                 msg_type=msg_type, filter_mode=field_detail_mode)
        from app.agents.schema_explorer import run_schema_explorer as _run_schema
        schema_state = {
            **state,
            "msg_type":    msg_type,
            "raw_message": raw_query,
            "filter_mode": field_detail_mode,
        }
        return _run_schema(schema_state)

    # 순수 소개/설명 요청이 아닌 경우 → general_qa로 자유 답변
    # (예: "MT103 전문은 어떤 필드들이 있어?", "MT103 수수료는 어떻게 처리해?" 등)
    if not _detect_basic_explain_query(detect_src):
        log.info("explainer_general_fallback", msg_type=msg_type, query=detect_src[:60])
        return _run_general_qa(state, query)

    # ── 2-1. 캐시 및 schema 설정 ─────────────────────────────────────────
    # 키워드 구분 없이 항상 전체 필드 표시
    # MT는 key_fields 테이블, MX는 schema_explorer 섹션 블록 사용
    field_mode = "all" if msg_type else None
    sections   = None
    schema_expl = None

    # MX 전문만 schema_explorer 사용 (MT는 key_fields 테이블 사용)
    use_schema_explorer = (doc_category == "MX") and bool(msg_type)

    from app.agents.schema_explorer import (
        run_schema_explorer as _run_schema,
        _load_cache as _schema_cache,
        _CACHE_DIR,
    )

    # explainer 결과 캐시
    _expl_cache_key  = f"{msg_type.lower()}_explainer"
    _expl_cache_path = _CACHE_DIR / f"{_expl_cache_key}.json"

    def _load_explainer_cache() -> dict | None:
        try:
            if _expl_cache_path.exists():
                import json as _json
                data = _json.loads(_expl_cache_path.read_text(encoding="utf-8"))
                log.info("explainer_cache_hit", msg_type=msg_type)
                return data
        except Exception as e:
            log.warning("explainer_cache_load_error", msg_type=msg_type, error=str(e))
        return None

    def _save_explainer_cache(payload: dict) -> None:
        try:
            import json as _json
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _expl_cache_path.write_text(
                _json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            log.info("explainer_cache_saved", msg_type=msg_type)
        except Exception as _e:
            log.warning("explainer_cache_save_error", error=str(_e))

    # ── 캐시 사전 확인 ───────────────────────────────────────────────────
    expl_cached_data  = _load_explainer_cache()
    schema_pre_cached = (
        bool(_schema_cache(msg_type, field_mode)) if use_schema_explorer else True
    )

    # 케이스 A: explainer 캐시 히트
    # → MX이면 schema도 가져오기(캐시 무료 or LLM 1회), 병합 후 반환
    if expl_cached_data:
        if use_schema_explorer:
            try:
                schema_state  = {**state, "msg_type": msg_type, "raw_message": raw_query, "filter_mode": field_mode}
                schema_result = _run_schema(schema_state)
                schema_out    = schema_result.get("output", {})
                sections      = schema_out.get("sections")
                schema_expl   = schema_out.get("explanation")
            except Exception as e:
                log.warning("explainer_schema_merge_failed", error=str(e))
        expl_cached_data["sections"]           = sections
        expl_cached_data["schema_explanation"] = schema_expl
        expl_cached_data["filter_mode"]        = field_mode
        log.info("explainer_from_cache", msg_type=msg_type)
        return {**state, "msg_type": msg_type, "needs_hitl": False, "output": expl_cached_data}

    # 케이스 B: explainer 미캐시 + MX schema 미캐시
    # → 이번 턴 explainer LLM만 실행, schema는 다음 요청(케이스 A)에서 처리
    # 케이스 C: explainer 미캐시 + schema 캐시됨(또는 MT) → 함께 실행

    if use_schema_explorer and schema_pre_cached:
        try:
            schema_state  = {**state, "msg_type": msg_type, "raw_message": raw_query, "filter_mode": field_mode}
            schema_result = _run_schema(schema_state)
            schema_out    = schema_result.get("output", {})
            sections      = schema_out.get("sections")
            schema_expl   = schema_out.get("explanation")
        except Exception as e:
            log.warning("explainer_schema_cached_fetch_failed", error=str(e))
    # MX schema 미캐시: 이번 턴 schema LLM 생략 (다음 요청 케이스 A에서 처리)

    # ── 3. RAG 검색 ─────────────────────────────────────────────────────
    chunks: list = []
    rag_context = ""
    try:
        chunks      = _search_rag(msg_type, doc_category, query, top_k=8)
        rag_context = format_rag_context(chunks)
    except Exception as e:
        log.error("explainer_rag_failed", error=str(e))
        rag_context = "RAG 검색 실패 — 가이드라인 없이 일반 지식으로 설명합니다."

    # ── 4. LLM 설명 생성 ─────────────────────────────────────────────────
    result: ExplainerOutput
    try:
        chain  = _build_chain()
        result = chain.invoke({
            "rag_context": rag_context or "ISO 20022 표준 지식을 활용하여 설명하십시오.",
            "query":       query,
            "msg_type":    msg_type,
            "fewshot":     EXPLAINER_FEWSHOT,
        })
        log.info("explainer_ok", msg_type=result.msg_type)
    except Exception as e:
        log.warning("explainer_structured_failed", error=str(e))
        try:
            llm   = get_chat_llm(temperature=0.0)
            prompt = ChatPromptTemplate.from_messages([
                ("system", EXPLAINER_SYSTEM),
                ("human",  EXPLAINER_USER),
            ])
            raw_chain = prompt | llm
            raw_resp  = raw_chain.invoke({
                "rag_context": rag_context or "ISO 20022 표준 지식을 활용하여 설명하십시오.",
                "query":       query,
                "msg_type":    msg_type,
                "fewshot":     EXPLAINER_FEWSHOT,
            })
            result = _fallback_parse(raw_resp.content, msg_type)
        except Exception as e2:
            log.error("explainer_fallback_failed", error=str(e2))
            result = ExplainerOutput(
                msg_type=msg_type,
                msg_type_full_name="오류",
                msg_type_korean="오류",
                purpose=f"설명 생성 실패: {e2}",
            )

    # ── 5. 청크에서 태그별 샘플값·XML경로 추출 → key_fields에 주입 ──────
    sample_map  = _extract_sample_map(chunks)
    path_map    = _extract_path_map(chunks)
    for f in result.key_fields:
        raw_tag = re.sub(r"[<>/\s]", "", f.tag).lower()
        if not f.sample_value:
            f.sample_value = sample_map.get(raw_tag)
        if not f.xml_path:
            f.xml_path = path_map.get(raw_tag)

    # ── 6. 가이드북 참조 정보 수집 ────────────────────────────────────────
    guidebook_basis = [
        {
            "page":    getattr(c, "page_label", None) or getattr(c, "page", None),
            "rule_id": getattr(c, "rule_id", None),
            "field":   getattr(c, "field_tag", None) or getattr(c, "xml_tag", None) or None,
            "source":  getattr(c, "source_file", None) or getattr(c, "doc_type", None),
        }
        for c in chunks
    ]

    # LLM이 버전을 축약하는 경우 텍스트 후처리로 교체
    # 예: "pacs.002" → "pacs.002.001.10"  (이미 전체 버전이 포함된 경우는 건너뜀)
    def _fix_version(text: str) -> str:
        if not text or not msg_type or "." not in msg_type:
            return text
        parts = msg_type.split(".")
        if len(parts) < 2:
            return text
        short = f"{parts[0]}.{parts[1]}"
        # \b는 한국어 문자 앞에서 단어경계로 인식 안 됨 → ASCII 경계로 대체
        return re.sub(
            rf'(?<![a-zA-Z0-9.]){re.escape(short)}(?!\.[0-9])',
            msg_type,
            text,
        )

    output_payload = {
        "type":           "explanation",
        "msg_type":       msg_type or result.msg_type,
        "full_name":      result.msg_type_full_name,
        "korean_name":    result.msg_type_korean,
        "purpose":        _fix_version(result.purpose),
        "use_cases":      result.use_cases,
        "key_fields":     [f.model_dump() for f in sorted(result.key_fields, key=lambda f: (not f.mandatory, f.tag))],
        "special_codes":  [c.model_dump() for c in result.special_codes
                       if SpecialCode.is_valid_code(c.code)],
        "related_messages": [r.model_dump() for r in result.related_messages],
        "flow_description": _fix_version(result.flow_description),
        "guidebook_basis":  guidebook_basis,
        "filter_mode":    field_mode,
        "sections":       sections,
        "schema_explanation": schema_expl,
    }

    # 설명 생성 성공 시 캐시 저장 (sections 제외 — schema 캐시에 별도 관리)
    if result.purpose and "실패" not in result.purpose:
        cacheable = {k: v for k, v in output_payload.items()
                     if k not in ("sections", "schema_explanation", "filter_mode", "guidebook_basis")}
        _save_explainer_cache(cacheable)

    return {**state, "msg_type": msg_type, "needs_hitl": False, "output": output_payload}


def _run_field_info(state: AgentState, query: str, msg_type: str) -> Dict[str, Any]:
    """특정 MT 필드 상세 정보 질문 처리."""
    field_tag = _extract_field_tag(query)
    tag_bare  = field_tag.strip(":")
    log.info("explainer_field_info", field_tag=field_tag, msg_type=msg_type)

    try:
        retriever = _get_retriever()
        filters   = {"msg_type": msg_type} if msg_type else None
        # 해당 필드 정의 청크 집중 검색
        chunks = retriever.search(
            query=f"{msg_type} Field {tag_bare} FORMAT PRESENCE DEFINITION CODES USAGE RULES",
            filters=filters,
            top_k=8,
            rerank=True,
        )
        rag_context = format_rag_context(chunks)
    except Exception as e:
        log.error("explainer_field_info_rag_failed", error=str(e))
        chunks      = []
        rag_context = "RAG 검색 실패."

    field_info_system = """\
당신은 SWIFT MT 전문 필드 전문가입니다.
사용자가 요청한 MT 필드의 상세 정보를 [가이드북 조각]에서 찾아 한국어로 구조화하여 답변하십시오.

반드시 아래 항목을 포함하여 마크다운으로 출력하십시오:
- **필드 번호 및 명칭** (예: Field 72: Sender to Receiver Information)
- **FORMAT**: 포맷 형식 그대로
- **PRESENCE**: 필수/선택 여부 및 시퀀스
- **DEFINITION**: 필드 정의 (한국어)
- **CODES**: 사용 가능한 코드 목록 (있는 경우, 코드와 의미)
- **USAGE RULES**: 주요 사용 규칙 (있는 경우)
- **NETWORK VALIDATED RULES**: 네트워크 검증 규칙 (있는 경우)

가이드북에 없는 내용은 추측하지 말고 생략하십시오.

[Few-Shot 예시]

예시 1)
질문: MT103 Field 72 상세 정보 알려줘
답변:
## Field 72: Sender to Receiver Information

- **FORMAT**: 6*35x (최대 6줄 × 35자)
- **PRESENCE**: Optional (Sequence B)
- **DEFINITION**: 송신자가 수신자에게 전달하는 추가 정보. 코드워드(/ACC/, /INS/, /REC/ 등)로 시작해야 하며, 구조화된 형식만 허용된다.

**CODES**
| 코드 | 의미 |
|---|---|
| /ACC/ | 수취 은행 계좌 지시 |
| /INS/ | 지급 지시 |
| /REC/ | 다음 중개 기관 지시 |
| /BNF/ | 수익자 정보 |

**USAGE RULES**
- 구조화된 코드 정보(coded information)만 포함될 때만 사용 가능
- 자유 텍스트(free text) 단독 사용 불가

**NETWORK VALIDATED RULES**
- 첫 번째 줄은 반드시 /코드워드/ 형식으로 시작해야 함

---

예시 2)
질문: MT103 Field 32A 알려줘
답변:
## Field 32A: Value Date/Currency/Interbank Settled Amount

- **FORMAT**: 6!n3!a15d (YYMMDD + 통화코드 + 금액)
- **PRESENCE**: Mandatory (Sequence B)
- **DEFINITION**: 결제 예정일, 통화, 은행 간 결제 금액을 지정한다.

**CODES**: 해당 없음

**USAGE RULES**
- 금액의 정수 부분에는 최소 1자리 이상의 숫자가 있어야 함
- 소수점은 콤마(,)로 표기하며 최대 길이에 포함됨
- 금액이 0이면 안 됨

**NETWORK VALIDATED RULES**
- 통화 코드는 유효한 ISO 4217 코드여야 함 (오류 코드: T52)
- 금액 형식 오류 시 오류 코드 C03, T40, T43 발생\
"""

    answer = ""
    try:
        llm    = get_chat_llm(temperature=0.0)
        prompt = ChatPromptTemplate.from_messages([
            ("system", field_info_system),
            ("human",  "[가이드북 조각]\n{rag_context}\n\n[질문]\n{query}"),
        ])
        resp   = (prompt | llm).invoke({"rag_context": rag_context, "query": query})
        answer = resp.content.strip()
    except Exception as e:
        log.error("explainer_field_info_llm_failed", error=str(e))
        answer = f"답변 생성 실패: {e}"

    guidebook_basis = [
        {
            "page":   getattr(c, "page_label", None) or getattr(c, "page", None),
            "field":  field_tag or None,
            "source": getattr(c, "source_file", None) or getattr(c, "doc_type", None),
        }
        for c in chunks
    ]

    return {
        **state,
        "needs_hitl": False,
        "output": {
            "type":            "general_answer",
            "query":           query,
            "answer":          answer,
            "guidebook_basis": guidebook_basis,
        },
    }


_SECTION_KEYWORDS = {
    "usage rules":    "USAGE RULES guidelines",
    "작성 규칙":       "USAGE RULES guidelines",
    "사용 규칙":       "USAGE RULES guidelines",
    "사용규칙":        "USAGE RULES guidelines",
    "작성규칙":        "USAGE RULES guidelines",
    "rules":          "USAGE RULES guidelines",
    "룰":             "USAGE RULES guidelines",
    "규칙":           "USAGE RULES guidelines",
    "network validated": "NETWORK VALIDATED RULES",
    "네트워크 검증":   "NETWORK VALIDATED RULES error code",
    "scope":          "Scope message purpose definition",
    "스코프":          "Scope purpose definition",
    "guidelines":     "USAGE RULES guidelines",
    "가이드라인":      "USAGE RULES guidelines",
    "market practice": "Market Practice Rules",
    "마켓 프랙티스":   "Market Practice Rules",
}

# 섹션 전체 조회 쿼리 감지 (필드 특정 없이 섹션 이름을 직접 언급)
_SECTION_QUERY_KEYWORDS = [
    "usage rules", "작성 규칙", "사용 규칙", "사용규칙", "작성규칙",
    "rules", "룰", "규칙",
    "network validated", "네트워크 검증",
    "market practice", "마켓 프랙티스",
    "guidelines", "가이드라인",
    "scope", "스코프",
]

# "엠티103" → "MT103" 정규화
_KOREAN_MT_RE = re.compile(r"엠티\s*(\d{3})", re.IGNORECASE)


def _normalize_query(query: str) -> str:
    return _KOREAN_MT_RE.sub(lambda m: f"MT{m.group(1)}", query)


def _detect_section_query(query: str) -> bool:
    """메시지 유형의 특정 섹션(Usage Rules 등)을 조회하는 쿼리 감지."""
    lower = _normalize_query(query).lower()
    # 매핑 질문은 제외 ("매핑 규칙" 등)
    if _detect_mapping_rule_query(query):
        return False
    return any(kw in lower for kw in _SECTION_QUERY_KEYWORDS)


def _run_general_qa(state: AgentState, query: str) -> Dict[str, Any]:
    """특정 전문 유형 없이 SWIFT 일반 질문에 자유 형식으로 답변."""
    log.info("explainer_general_qa", query=query[:60])

    # 메시지 유형 추출 (필터용)
    msg_type, _ = _extract_msg_type(query, "")
    filters     = {"msg_type": msg_type} if msg_type else None

    # 섹션 키워드 감지 → 쿼리 보강
    lower = _normalize_query(query).lower()
    section_boost = ""
    for kw, boost in _SECTION_KEYWORDS.items():
        if kw in lower:
            section_boost = boost
            break

    # 특정 필드 Usage Rules 조회 시 필드 번호 포함 RAG 쿼리 생성
    field_tag = _extract_field_tag(query)
    tag_bare  = field_tag.strip(":") if field_tag else ""
    if tag_bare and any(kw in lower for kw in _FIELD_SECTION_KEYWORDS):
        # 예: "MT103 Field 20 USAGE RULES"
        rag_query = (
            f"{msg_type + ' ' if msg_type else ''}Field {tag_bare} USAGE RULES DEFINITION {section_boost}"
        ).strip()
    else:
        rag_query = f"{query} {section_boost}".strip() if section_boost else query

    try:
        retriever = _get_retriever()
        # Usage Rules 섹션 조회 시 추가 커버 메서드/금액 관련 청크 보강
        is_usage_rules = any(kw in lower for kw in ["usage rules", "작성 규칙", "사용 규칙", "rules", "룰", "규칙"])
        chunks = retriever.search(
            query=rag_query,
            filters=filters,
            top_k=12 if is_usage_rules else 10,
            rerank=True,
        )
        # Usage Rules 조회 시 커버 메서드·금액 관련 필드 청크 보강
        if is_usage_rules and msg_type:
            extra = retriever.search(
                query=f"{msg_type} cover method originating bank copy field 20 MT202 COV Tracker TRCKCHZZ Amount Related Fields 33B 71F 71G 32A",
                filters=filters,
                top_k=6,
                rerank=False,
            )
            seen = {getattr(c, 'chunk_id', id(c)) for c in chunks}
            for c in extra:
                if getattr(c, 'chunk_id', id(c)) not in seen:
                    chunks.append(c)
        rag_context = format_rag_context(chunks)
    except Exception as e:
        log.error("explainer_general_rag_failed", error=str(e))
        chunks = []
        rag_context = "RAG 검색 실패 — 일반 지식으로 답변합니다."

    answer = ""
    try:
        llm = get_chat_llm(temperature=0.0)
        prompt = ChatPromptTemplate.from_messages([
            ("system", GENERAL_QA_SYSTEM),
            ("human",  GENERAL_QA_USER),
        ])
        resp = (prompt | llm).invoke({"rag_context": rag_context, "query": query})
        answer = resp.content.strip()
        log.info("explainer_general_ok", answer_len=len(answer))
    except Exception as e:
        log.error("explainer_general_llm_failed", error=str(e))
        answer = f"답변 생성에 실패했습니다: {e}"

    guidebook_basis = [
        {
            "page":   getattr(c, "page_label", None) or getattr(c, "page", None),
            "field":  getattr(c, "field_tag", None) or getattr(c, "xml_tag", None) or None,
            "source": getattr(c, "source_file", None) or getattr(c, "doc_type", None),
        }
        for c in chunks
    ]

    return {
        **state,
        "needs_hitl": False,
        "output": {
            "type":            "general_answer",
            "query":           query,
            "answer":          answer,
            "guidebook_basis": guidebook_basis,
        },
    }


def _run_mapping_rule(state: AgentState, query: str, msg_type: str) -> Dict[str, Any]:
    """매핑 규칙 질문 처리 분기."""
    field_tag       = _extract_field_tag(query)
    source_msg_type = msg_type or "MT103"

    # 대상 전문 추론: mapper의 _MT_TO_MX 참조 대신 간단 매핑
    _DEFAULT_TARGET = {
        "MT103": "pacs.008.001.08",
        "MT202": "pacs.009.001.08",
        "MT200": "pacs.009.001.08",
        "MT940": "camt.053.001.08",
        "MT950": "camt.053.001.08",
        "MT910": "camt.054.001.08",
    }
    target_msg_type = _DEFAULT_TARGET.get(source_msg_type.upper(), "pacs.008.001.08")
    mx_m = _RE_MX.search(query)
    if mx_m:
        target_msg_type = mx_m.group(1).lower()

    log.info("explainer_mapping_rule", field=field_tag, source=source_msg_type, target=target_msg_type)

    # RAG 검색
    try:
        chunks      = _search_rag_mapping(field_tag, source_msg_type, target_msg_type, query)
        rag_context = format_rag_context(chunks)
    except Exception as e:
        log.error("explainer_mapping_rag_failed", error=str(e))
        chunks      = []
        rag_context = "RAG 검색 실패 — 가이드라인 없이 일반 지식으로 답변합니다."

    invoke_kwargs = {
        "rag_context":      rag_context,
        "query":            query,
        "field_tag":        field_tag,
        "source_msg_type":  source_msg_type,
        "target_msg_type":  target_msg_type,
        "fewshot":          MAPPING_RULE_FEWSHOT,
    }

    result: MappingRuleOutput
    try:
        chain  = _build_mapping_chain()
        result = chain.invoke(invoke_kwargs)
        log.info("explainer_mapping_ok", field=result.source_field)
    except Exception as e:
        log.warning("explainer_mapping_structured_failed", error=str(e))
        try:
            prompt    = ChatPromptTemplate.from_messages([
                ("system", MAPPING_RULE_SYSTEM),
                ("human",  MAPPING_RULE_USER),
            ])
            raw_chain = prompt | get_chat_llm(temperature=0.0)
            raw_resp  = raw_chain.invoke(invoke_kwargs)
            result    = _fallback_parse_mapping(raw_resp.content, field_tag, source_msg_type, target_msg_type)
        except Exception as e2:
            log.error("explainer_mapping_fallback_failed", error=str(e2))
            result = MappingRuleOutput(
                source_field=field_tag,
                source_msg_type=source_msg_type,
                target_msg_type=target_msg_type,
                mapping_summary=f"매핑 규칙 생성 실패: {e2}",
            )

    guidebook_basis = [
        {
            "page":   getattr(c, "page_label", None) or getattr(c, "page", None),
            "field":  getattr(c, "field_tag", None) or getattr(c, "xml_tag", None) or None,
            "source": getattr(c, "source_file", None) or getattr(c, "doc_type", None),
        }
        for c in chunks
    ]

    return {
        **state,
        "msg_type":   source_msg_type,
        "needs_hitl": False,
        "output": {
            "type":             "mapping_rule",
            "source_field":     result.source_field,
            "source_msg_type":  result.source_msg_type,
            "target_msg_type":  result.target_msg_type,
            "mapping_summary":  result.mapping_summary,
            "mapping_details":  [d.model_dump() for d in result.mapping_details],
            "constraints":      result.constraints,
            "guidebook_refs":   result.guidebook_refs,
            "guidebook_basis":  guidebook_basis,
        },
    }
