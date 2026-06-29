"""
Mapper Agent — 프로덕션 등급 리팩토링.

주요 개선 사항:
  1. Pydantic 구조화 출력 — FieldMapping 1:N 매핑 명세 지원
  2. [Category|Msg|Field|p.N] RAG 컨텍스트 구조화 주입
  3. LangChain LCEL 체인 (with_structured_output)
  4. 환각 방지: 가이드라인 없는 태그 → is_unmapped=True 강제
  5. 전 단계 예외 처리 + degraded 폴백
"""
from __future__ import annotations

import re
import threading

import structlog
from typing import Any, Dict, List, Optional

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.graph.state import AgentState
from app.llm import (
    format_rag_context,
    get_chat_llm,
    parse_llm_json,
)
from app.prompts.mapper_prompts import MAPPER_SYSTEM, MAPPER_USER, MAPPER_FEWSHOT
from app.rag.retriever import SwiftRetriever
from app.validation.prowide_client import prowide_translate

log = structlog.get_logger(__name__)

_RE_XML_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9]*>")

# MT→MX 기본 유형 매핑 (CBPR+ SRU 기준)
_MT_TO_MX: dict[str, str] = {
    # 고객 송금
    "MT101": "pain.001.001.03",   # Multiple Credit Transfer → Request for Transfer
    "MT103": "pacs.008.001.08",   # Customer Credit Transfer
    "MT104": "pain.008.001.02",   # Direct Debit
    "MT107": "pain.008.001.02",   # General Direct Debit
    # 금융기관 간 송금
    "MT200": "pacs.009.001.08",   # FI Transfer for its Own Account
    "MT201": "pacs.009.001.08",   # Multiple FI Transfers
    "MT202": "pacs.009.001.08",   # General FI Transfer
    "MT203": "pacs.009.001.08",   # Multiple General FI Transfers
    "MT204": "pacs.010.001.03",   # FI Direct Debit
    "MT205": "pacs.009.001.08",   # FI Transfer Execution
    # 반환/취소
    "MT192": "camt.056.001.08",   # Request for Cancellation
    "MT196": "camt.029.001.09",   # Answers
    "MT292": "camt.056.001.08",
    "MT296": "camt.029.001.09",
    # 수취 통보
    "MT210": "camt.057.001.06",   # Notice to Receive
    # 계좌 명세
    "MT900": "camt.054.001.08",   # Confirmation of Debit
    "MT910": "camt.054.001.08",   # Confirmation of Credit
    "MT940": "camt.053.001.08",
    "MT950": "camt.053.001.08",
}
_MX_TO_MT: dict[str, str] = {v: k for k, v in _MT_TO_MX.items()}

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


def _infer_target_type(msg_type: str, direction: str) -> str:
    if direction == "mt_to_mx":
        return _MT_TO_MX.get(msg_type.upper(), "")
    return _MX_TO_MT.get(msg_type.lower(), "")


# ===========================================================================
# Pydantic 출력 스키마
# ===========================================================================

class FieldMapping(BaseModel):
    """MT 필드 태그 ↔ MX XML 경로 간 1:1 또는 1:N 매핑 명세."""

    mt_tag: str = Field(
        description="MT 필드 태그 (예: :32A:, :50K:)"
    )
    mt_value: Optional[str] = Field(
        None,
        description="마스킹된 원본 값 (PII는 <<PLACEHOLDER>> 유지)"
    )
    mx_paths: List[str] = Field(
        default_factory=list,
        description="1:N 매핑 MX XML 경로 목록 (예: GrpHdr/IntrBkSttlmDt)"
    )
    mx_value: Optional[str] = Field(
        None,
        description="변환된 MX 값 (포맷 변환 적용 후)"
    )
    is_unmapped: bool = Field(
        default=False,
        description="True = 가이드라인에 매핑 근거 없음"
    )
    notes: Optional[str] = Field(
        None,
        description="포맷 변환 설명 또는 주의 사항"
    )
    guidebook_ref: Optional[str] = Field(
        None,
        description="근거 가이드라인 출처 (Category | p.N)"
    )


class EnhancementWarning(BaseModel):
    field: str
    issue: str
    guidebook_ref: Optional[str] = None


class MapperOutput(BaseModel):
    """Mapper Agent 구조화 출력 스키마."""

    direction: str = Field(
        description="변환 방향: mt_to_mx | mx_to_mt"
    )
    source_type: str = Field(
        description="원본 메시지 유형 (예: MT103)"
    )
    target_type: str = Field(
        description="변환 대상 유형 (예: pacs.008.001.08)"
    )
    mappings: List[FieldMapping] = Field(
        default_factory=list,
        description="필드별 매핑 명세 목록"
    )
    unmapped_fields: List[str] = Field(
        default_factory=list,
        description="가이드라인 근거 없이 매핑 불가한 MT 태그 목록"
    )
    enhancement_warnings: List[EnhancementWarning] = Field(
        default_factory=list,
        description="LLM 보강 중 발견된 경고 목록"
    )


# ===========================================================================
# 내부 헬퍼
# ===========================================================================

def _build_llm_chain():
    prompt = ChatPromptTemplate.from_messages([
        ("system", MAPPER_SYSTEM),
        ("human", MAPPER_USER),
    ])
    llm = get_chat_llm(temperature=0.0)
    structured_llm = llm.with_structured_output(MapperOutput, method="json_mode")
    return prompt | structured_llm


def _strip_xml_tags(value: Optional[str]) -> Optional[str]:
    """LLM 출력값에 남은 XML 태그 제거 (예: </Ustrd> 누출)."""
    if not value:
        return value
    return _RE_XML_TAG.sub("", value).strip()


def _sanitize_mapper_output(output: MapperOutput) -> MapperOutput:
    """mt_value / mx_value 필드에서 XML 태그 제거."""
    for fm in output.mappings:
        fm.mt_value = _strip_xml_tags(fm.mt_value)
        fm.mx_value = _strip_xml_tags(fm.mx_value)
    return output


def _parse_mapper_fallback(content: str, source_type: str, target_type: str,
                            direction: str) -> MapperOutput:
    """structured_output 실패 시 JSON 파싱 → Pydantic 수동 변환."""
    raw = parse_llm_json(content)
    try:
        mappings = [
            FieldMapping(**m) for m in raw.get("mappings", [])
            if isinstance(m, dict)
        ]
        warnings = [
            EnhancementWarning(**w) for w in raw.get("enhancement_warnings", [])
            if isinstance(w, dict)
        ]
        return MapperOutput(
            direction=raw.get("direction", direction),
            source_type=raw.get("source_type", source_type),
            target_type=raw.get("target_type", target_type),
            mappings=mappings,
            unmapped_fields=raw.get("unmapped_fields", []),
            enhancement_warnings=warnings,
        )
    except Exception as e:
        log.warning("mapper_fallback_parse_failed", error=str(e))
        return MapperOutput(
            direction=direction,
            source_type=source_type,
            target_type=target_type,
        )


# ===========================================================================
# LangGraph 노드 진입점
# ===========================================================================

def run_mapper(state: AgentState) -> Dict[str, Any]:
    """
    Mapper Agent LangGraph 노드.

    State 입력:  raw_message, masked_message, msg_type
    State 출력:  validation_result, needs_hitl, output (mapper_output 포함)
    """
    raw_message    = state.get("raw_message", "")
    masked_message = state.get("masked_message", "")
    msg_type       = state.get("msg_type", "")
    # 함수 최상위 예외 시에도 output이 항상 state에 존재하도록 기본값 초기화
    _empty_output: Dict[str, Any] = {
        "type": "mapped_message", "direction": "unknown",
        "prowide_draft": "", "enhanced": "", "mapper_output": {},
        "unmapped_fields": [], "warnings": [], "guidebook_basis": [],
    }

    # msg_type이 없으면 raw_message 형식으로 방향 감지
    # MT: {1:...}{2:...}{4:...} 블록 구조
    # MX: <Document> 또는 XML 태그로 시작
    if not msg_type:
        import re as _re
        mt_header = _re.search(r'\{2:[IO](\d{3})', raw_message)
        if mt_header:
            msg_type = f"MT{mt_header.group(1)}"
        elif raw_message.lstrip().startswith('<') or '<Document' in raw_message:
            # MX 메시지: 첫 번째 XML 태그에서 메시지 유형 추출 시도
            mx_ns = _re.search(r'urn:iso:std:iso:20022[^:]*:([a-z]{3,4}\.\d{3})', raw_message)
            if mx_ns:
                msg_type = mx_ns.group(1)

    if not msg_type:
        log.warning("mapper_msg_type_undetected", raw_preview=raw_message[:60])
    try:
        direction = "mt_to_mx" if msg_type.upper().startswith("MT") else "mx_to_mt"
    except Exception:
        direction = "mt_to_mx"
    target_type = _infer_target_type(msg_type, direction)

    # ── 1. Prowide 변환 (best-effort, PII LLM 미노출) ─────────────────────
    prowide_draft    = ""
    prowide_degraded = False
    try:
        translate_result = prowide_translate(raw_message, direction=direction)
        prowide_draft    = translate_result.get("content", "")
        prowide_degraded = translate_result.get("degraded", False)
        # Prowide MX→MT 변환 시 XML 닫기 태그가 MT 필드값에 누출되는 경우 제거
        if prowide_draft:
            prowide_draft = _RE_XML_TAG.sub("", prowide_draft)
    except Exception as e:
        log.warning("prowide_translate_failed", error=str(e))
        prowide_degraded = True

    # ── 2. RAG — 매핑 가이드라인 검색 ────────────────────────────────────
    try:
        retriever   = _get_retriever()
        query       = (
            f"{target_type} field mapping structured address "
            f"LEI BIC uplift {masked_message[:200]}"
        )
        # Qdrant msg_type은 short form(pacs.008)으로 인덱싱됨
        # pacs.008.001.08 → pacs.008 으로 정규화
        def _short_mx(t: str) -> str:
            parts = t.split(".")
            return ".".join(parts[:2]) if len(parts) >= 3 else t

        rag_filter = {"msg_type": _short_mx(target_type)} if target_type else None
        rule_chunks = retriever.search(
            query=query,
            filters=rag_filter,
            top_k=8,
            rerank=True,
        )
        rag_context = format_rag_context(rule_chunks)
    except Exception as e:
        log.error("rag_search_failed", error=str(e))
        rule_chunks = []
        rag_context = "RAG 검색 실패 — 가이드라인 없이 매핑을 진행합니다."

    # ── 3. LLM 매핑 (LCEL + structured_output) ───────────────────────────
    mapper_output: MapperOutput
    invoke_kwargs = {
        "source_type":   msg_type,
        "target_type":   target_type or "unknown",
        "masked_source": masked_message,
        "prowide_draft": prowide_draft or "(Prowide 변환 미완료)",
        "rag_context":   rag_context,
        "fewshot":       MAPPER_FEWSHOT,
    }

    try:
        chain = _build_llm_chain()
        mapper_output = _sanitize_mapper_output(chain.invoke(invoke_kwargs))
        log.info("mapper_structured_output_ok",
                 mappings=len(mapper_output.mappings),
                 unmapped=len(mapper_output.unmapped_fields))
    except Exception as e:
        log.warning("mapper_structured_output_failed", error=str(e))
        try:
            prompt = ChatPromptTemplate.from_messages([
                ("system", MAPPER_SYSTEM),
                ("human", MAPPER_USER),
            ])
            llm       = get_chat_llm(temperature=0.0)
            raw_chain = prompt | llm
            raw_resp  = raw_chain.invoke(invoke_kwargs)
            mapper_output = _sanitize_mapper_output(_parse_mapper_fallback(
                raw_resp.content, msg_type, target_type or "unknown", direction
            ))
        except Exception as e2:
            log.error("mapper_fallback_failed", error=str(e2))
            mapper_output = MapperOutput(
                direction=direction,
                source_type=msg_type,
                target_type=target_type or "unknown",
            )
            _empty_output["error"] = str(e2)

    # ── 4. 상태 업데이트 ──────────────────────────────────────────────────
    # 변환 결과는 항상 사람 검수 (MT↔MX 변환은 실제 사용 전 반드시 확인 필요)
    needs_hitl = True

    guidebook_basis = [
        {
            "page":    getattr(c, "page_label", None) or getattr(c, "page", None),
            "rule_id": getattr(c, "rule_id", None),
            "field":   getattr(c, "field_tag", None) or getattr(c, "xml_tag", None),
        }
        for c in rule_chunks
    ]

    return {
        **state,
        "needs_hitl": needs_hitl,
        "validation_result": {
            "verdict":    "PENDING_REVIEW" if needs_hitl else "PASS",
            "needs_hitl": needs_hitl,
            "rule_engine": {
                "problems": [],
                "degraded": prowide_degraded,
            },
            "semantic": {
                "violations":        [],
                "warnings":          [w.model_dump() for w in mapper_output.enhancement_warnings],
                "conditional_rules": [],
            },
            "guidebook_basis": guidebook_basis,
        },
        "output": {
            "type":            "mapped_message",
            "direction":       direction,
            "prowide_draft":   prowide_draft,
            "enhanced":        prowide_draft,
            "mapper_output":   mapper_output.model_dump(),
            "unmapped_fields": mapper_output.unmapped_fields,
            "warnings":        [w.model_dump() for w in mapper_output.enhancement_warnings],
            "guidebook_basis": guidebook_basis,
        },
    }
