"""
Analyzer Agent — 프로덕션 등급 리팩토링.

주요 개선 사항:
  1. Pydantic 구조화 출력 (with_structured_output)
  2. RAG 컨텍스트를 [Category|Msg|Field|p.N] 단위로 구조화 주입
  3. LangChain LCEL 체인 (ChatPromptTemplate | structured_llm)
  4. structured_output 실패 시 JSON 파싱 폴백
  5. 전 단계 예외 처리 + degraded 모드 보장
"""
from __future__ import annotations

import re
import threading
import structlog
from typing import Any, Dict, List, Optional

# MT 전문에서 사용된 필드 태그 추출용
_MT_FIELD_TAG_RE = re.compile(r":([0-9]{2}[A-Z]?):")

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.graph.state import AgentState
from app.llm import (
    format_rag_context,
    format_rule_chunks,
    get_chat_llm,
    parse_llm_json,
)
from app.prompts.analyzer_prompts import ANALYZER_SYSTEM, ANALYZER_USER, FEWSHOT
from app.rag.retriever import SwiftRetriever
from app.validation.prowide_client import prowide_syntax_verify
from app.validation.reconciler import reconcile

log = structlog.get_logger(__name__)

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


# ===========================================================================
# Pydantic 출력 스키마
# ===========================================================================

class ViolationItem(BaseModel):
    field: str = Field(description="위반된 MT 필드 태그")
    issue: str = Field(description="위반 내용")
    rule_id: Optional[str] = Field(None, description="규칙 ID (예: C1)")
    page: Optional[int] = Field(None, description="가이드북 페이지 — 규칙 조각에 있는 값만")


class WarningItem(BaseModel):
    field: str = Field(description="경고 대상 필드 태그")
    issue: str = Field(description="경고 내용")
    rule_id: Optional[str] = Field(None)
    page: Optional[int] = Field(None, description="가이드북 페이지 — 규칙 조각에 있는 값만")
    reasoning: Optional[str] = Field(None)


class ConditionalRule(BaseModel):
    rule_id: str = Field(description="조건부 규칙 ID (예: C1)")
    page: Optional[int] = Field(None, description="가이드북 페이지 — 규칙 조각에 있는 값만")
    triggered: bool = Field(description="조건 발동 여부")
    why: str = Field(description="발동/미발동 이유")


class FieldInterpretation(BaseModel):
    tag:         str = Field(description="MT 필드 태그 (예: :32A:, :50K:)")
    value:       str = Field(description="전문에서 추출한 실제 값")
    description: str = Field(description="필드 역할과 값의 의미 설명 (1~2문장)")
    sequence:    Optional[str] = Field(None, description="시퀀스 구분 (예: A, B, C). 시퀀스 구조가 없는 전문은 null.")


class AnalyzerOutput(BaseModel):
    """Analyzer Agent 구조화 출력 스키마."""

    source_msg_type: str = Field(
        description="원본 메시지 유형 (예: MT103, MT101)"
    )
    target_msg_type: str = Field(
        default="",
        description="CBPR+ 변환 대상 유형 (예: pacs.008.001.08). 분석 전용이면 빈 문자열."
    )
    transaction_count: int = Field(
        default=1,
        description="전문 내 거래 건수"
    )
    currency: Optional[str] = Field(
        None,
        description="주요 통화 코드 (예: EUR, USD)"
    )
    missing_fields: List[str] = Field(
        default_factory=list,
        description="누락된 필수 필드 태그 목록"
    )
    verdict: str = Field(
        description="최종 판정: PASS | WARNING | REJECT | ERROR"
    )
    violations: List[ViolationItem] = Field(default_factory=list)
    warnings: List[WarningItem] = Field(default_factory=list)
    applied_conditional_rules: List[ConditionalRule] = Field(default_factory=list)
    field_analysis: List[FieldInterpretation] = Field(
        default_factory=list,
        description="전문에 포함된 각 필드의 값과 의미 해석 목록"
    )


# ===========================================================================
# 내부 헬퍼
# ===========================================================================

def _detect_doc_category(msg_type: str, message: str) -> str:
    """msg_type 또는 메시지 본문으로 MT/MX 판별 후 doc_category 반환."""
    if msg_type:
        mt = msg_type.upper()
        if mt.startswith("MT"):
            return "MT"
        if "." in msg_type:
            return "MX"
    # msg_type 없을 때 본문으로 판별
    if "<Document" in message or message.strip().startswith("<"):
        return "MX"
    return "MT"


def _extract_field_tags(message: str) -> list[str]:
    """MT 전문에서 실제 사용된 필드 태그 목록 반환 (중복 제거, 순서 유지)."""
    seen: set[str] = set()
    tags: list[str] = []
    for tag in _MT_FIELD_TAG_RE.findall(message):
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def _build_llm_chain():
    """ChatPromptTemplate | structured_llm LCEL 체인 반환."""
    prompt = ChatPromptTemplate.from_messages([
        ("system", ANALYZER_SYSTEM),
        ("human", ANALYZER_USER),
    ])
    llm = get_chat_llm(temperature=0.0)
    structured_llm = llm.with_structured_output(AnalyzerOutput, method="json_mode")
    return prompt | structured_llm


def _remove_false_missing(output: AnalyzerOutput, present_tags: list[str]) -> AnalyzerOutput:
    """전문에 실제 존재하는 태그를 missing_fields / violations에서 제거한다.

    LLM이 마스킹 플레이스홀더(<<BIC_N>> 등)를 값 없음으로 오해하여
    존재하는 필드를 누락으로 판정하는 false positive를 코드 레벨에서 제거한다.
    """
    if not present_tags:
        return output

    # 비교를 위해 숫자+옵션 형태로 정규화 (":57A:" → "57A", "57a" → "57A")
    present_norm: set[str] = set()
    for t in present_tags:
        present_norm.add(t.upper().strip(":"))

    def _tag_present(tag: str) -> bool:
        return tag.upper().strip(":") in present_norm

    removed_missing: list[str] = []
    clean_missing: list[str] = []
    for f in output.missing_fields:
        if _tag_present(f):
            removed_missing.append(f)
        else:
            clean_missing.append(f)

    removed_violations: list[str] = []
    clean_violations: list[ViolationItem] = []
    for v in output.violations:
        # "필드 누락" 키워드가 포함되고 해당 태그가 실제로 존재하면 제거
        is_absence_claim = any(kw in v.issue for kw in ["누락", "부재", "absent", "missing"])
        if is_absence_claim and _tag_present(v.field):
            removed_violations.append(v.field)
        else:
            clean_violations.append(v)

    if removed_missing or removed_violations:
        log.warning(
            "analyzer_false_positive_removed",
            removed_missing=removed_missing,
            removed_violations=removed_violations,
        )

    return AnalyzerOutput(
        source_msg_type=output.source_msg_type,
        target_msg_type=output.target_msg_type,
        transaction_count=output.transaction_count,
        currency=output.currency,
        missing_fields=clean_missing,
        verdict=output.verdict if (clean_missing or clean_violations) else (
            "PASS" if output.verdict == "REJECT" and not clean_missing and not clean_violations
            else output.verdict
        ),
        violations=clean_violations,
        warnings=output.warnings,
        applied_conditional_rules=output.applied_conditional_rules,
        field_analysis=output.field_analysis,
    )


def _parse_analyzer_fallback(content: str) -> AnalyzerOutput:
    """structured_output 실패 시 JSON 파싱 → Pydantic 수동 변환."""
    raw = parse_llm_json(content)
    try:
        violations = [
            ViolationItem(**v) for v in raw.get("violations", [])
            if isinstance(v, dict)
        ]
        warnings = [
            WarningItem(**w) for w in raw.get("warnings", [])
            if isinstance(w, dict)
        ]
        conds = [
            ConditionalRule(**c) for c in raw.get("applied_conditional_rules", [])
            if isinstance(c, dict)
        ]
        field_analysis = [
            FieldInterpretation(**f) for f in raw.get("field_analysis", [])
            if isinstance(f, dict)
        ]
        try:
            tx_count = int(raw.get("transaction_count", 1))
        except (TypeError, ValueError):
            tx_count = 1
        return AnalyzerOutput(
            source_msg_type=raw.get("source_msg_type", ""),
            target_msg_type=raw.get("target_msg_type", ""),
            transaction_count=tx_count,
            currency=raw.get("currency"),
            missing_fields=raw.get("missing_fields", []),
            verdict=raw.get("verdict", "ERROR"),
            violations=violations,
            warnings=warnings,
            applied_conditional_rules=conds,
            field_analysis=field_analysis,
        )
    except Exception as e:
        log.warning("analyzer_fallback_parse_failed", error=str(e),
                    raw_keys=list(raw.keys()) if isinstance(raw, dict) else None)
        return AnalyzerOutput(
            source_msg_type="",
            target_msg_type="",
            verdict="ERROR",
        )


def _analyzer_output_to_llm_dict(out: AnalyzerOutput) -> dict[str, Any]:
    """AnalyzerOutput → reconcile() 호환 llm_result dict 변환."""
    return {
        "verdict": out.verdict,
        "violations": [v.model_dump() for v in out.violations],
        "warnings": [w.model_dump() for w in out.warnings],
        "applied_conditional_rules": [c.model_dump() for c in out.applied_conditional_rules],
        "source_msg_type": out.source_msg_type,
        "target_msg_type": out.target_msg_type,
        "transaction_count": out.transaction_count,
        "currency": out.currency,
        "missing_fields": out.missing_fields,
    }


# ===========================================================================
# LangGraph 노드 진입점
# ===========================================================================

def run_analyzer(state: AgentState) -> Dict[str, Any]:
    """
    Analyzer Agent LangGraph 노드.

    State 입력:  raw_message, masked_message, msg_type
    State 출력:  validation_result, needs_hitl, output
    """
    raw_message    = state.get("raw_message", "")
    masked_message = state.get("masked_message", "")
    msg_type       = state.get("msg_type", "")
    doc_category   = _detect_doc_category(msg_type, raw_message)

    # ── 1. Prowide 구문/네트워크 검증 ─────────────────────────────────────
    try:
        syntax_result = prowide_syntax_verify(raw_message, msg_type)
    except Exception as e:
        log.error("prowide_verify_failed", error=str(e))
        syntax_result = {
            "syntax_ok": False,
            "problems": [{"code": "SVC_ERR", "desc": str(e)}],
            "degraded": True,
        }

    # ── 1b. 조기 반환: 구문 파싱 실패(degraded 아님) → LLM 호출 불필요 ────
    # degraded(서비스 장애)와 달리 구문 자체가 틀린 경우는 LLM 분석이 무의미하다.
    # 오류 코드로 RAG 검색만 수행하여 가이드북 근거를 첨부하고 REJECT 반환한다.
    if not syntax_result.get("syntax_ok", False) and not syntax_result.get("degraded", False):
        try:
            retriever   = _get_retriever()
            error_codes = " ".join(
                p.get("code", "") for p in syntax_result.get("problems", [])
            )
            rag_filters: dict = {"doc_category": doc_category}
            if msg_type:
                rag_filters["msg_type"] = msg_type
            rule_chunks = retriever.search(
                query=f"{msg_type} {error_codes} syntax error format",
                filters=rag_filters,
                top_k=5,
                rerank=True,
                include_parents=True,
            )
        except Exception as e:
            log.error("rag_search_failed_early", error=str(e))
            rule_chunks = []

        log.info("analyzer_early_reject",
                 msg_type=msg_type,
                 problems=len(syntax_result.get("problems", [])))

        validation_result = reconcile(
            syntax_result,
            {"verdict": "REJECT", "violations": [], "warnings": [],
             "applied_conditional_rules": []},
            rule_chunks,
        )
        return {
            **state,
            "validation_result": validation_result,
            "needs_hitl":        True,
            "output": {
                "type":              "analysis",
                "verdict":           "REJECT",
                "source_msg_type":   msg_type,
                "target_msg_type":   "",
                "transaction_count": 1,
                "currency":          None,
                "missing_fields":    [],
                "details":           validation_result,
            },
        }

    # ── 2. RAG 2단계 검색 ────────────────────────────────────────────────────
    # 전문에서 사용된 필드 태그 추출 (포맷 규칙 검색에 활용)
    used_tags = _extract_field_tags(raw_message)
    tags_str  = " ".join(used_tags[:10])  # 상위 10개만 사용

    try:
        retriever  = _get_retriever()
        rag_filters: dict = {"doc_category": doc_category}
        if msg_type:
            rag_filters["msg_type"] = msg_type

        # ① 조건부 규칙·필수 필드 검색
        chunks_rules = retriever.search(
            query=f"{msg_type} mandatory fields conditional rules presence network validated",
            filters=rag_filters,
            top_k=5,
            rerank=True,
            include_parents=True,
        )

        # ② 사용된 필드들의 포맷 규칙 검색 (가이드북의 Field Specifications 섹션)
        chunks_fmt = retriever.search(
            query=f"{msg_type} field format length characters specification {tags_str}",
            filters=rag_filters,
            top_k=5,
            rerank=True,
            include_parents=True,
        )

        # 중복 제거 후 병합 (page + field_tag 기준)
        seen_keys: set[str] = set()
        rule_chunks: list = []
        for c in chunks_rules + chunks_fmt:
            key = f"{getattr(c, 'page', '')}_{getattr(c, 'field_tag', '')}_{getattr(c, 'text', '')[:40]}"
            if key not in seen_keys:
                seen_keys.add(key)
                rule_chunks.append(c)

        retrieved_rules = format_rule_chunks(rule_chunks)
        log.info("analyzer_rag_chunks",
                 msg_type=msg_type,
                 rules=len(chunks_rules),
                 fmt=len(chunks_fmt),
                 total=len(rule_chunks),
                 tags=tags_str[:50])

        if len(rule_chunks) < 3:
            retrieved_rules = (
                f"[주의] {msg_type}에 대한 가이드북 규칙 조각이 {len(rule_chunks)}개만 검색되었습니다.\n"
                f"아래에 명시된 필드만 필수로 판단하고, 그 외 필드 누락은 REJECT 사유로 삼지 마십시오.\n\n"
                + retrieved_rules
            )
    except Exception as e:
        log.error("rag_search_failed", error=str(e))
        rule_chunks     = []
        retrieved_rules = "관련 규칙을 찾을 수 없습니다."

    # ── 3. LLM 의미 분석 (LCEL + structured_output) ───────────────────────
    # 파서가 확정한 존재 태그 목록을 프롬프트에 명시적으로 주입한다.
    present_tags_str = ", ".join(f":{t}:" for t in used_tags) if used_tags else "(태그 추출 불가)"
    llm_inputs = {
        "fewshot":        FEWSHOT,
        "present_tags":   present_tags_str,
        "masked_message": masked_message,
        "retrieved_rules": retrieved_rules,
    }

    analyzer_output: AnalyzerOutput
    try:
        chain = _build_llm_chain()
        analyzer_output = chain.invoke(llm_inputs)
        log.info("analyzer_structured_output_ok",
                 verdict=analyzer_output.verdict,
                 msg_type=analyzer_output.source_msg_type)
    except Exception as e:
        log.warning("analyzer_structured_output_failed", error=str(e))
        # 폴백: raw LLM → JSON 파싱
        try:
            from langchain_core.prompts import ChatPromptTemplate
            llm = get_chat_llm(temperature=0.0)
            prompt = ChatPromptTemplate.from_messages([
                ("system", ANALYZER_SYSTEM),
                ("human", ANALYZER_USER),
            ])
            raw_chain = prompt | llm
            raw_resp  = raw_chain.invoke(llm_inputs)
            analyzer_output = _parse_analyzer_fallback(raw_resp.content)
        except Exception as e2:
            log.error("analyzer_fallback_failed", error=str(e2))
            analyzer_output = AnalyzerOutput(
                source_msg_type=msg_type,
                target_msg_type="",
                verdict="ERROR",
            )

    # ── 3b. false positive 제거 — 존재하는 태그를 "누락"으로 판정한 경우 ──
    analyzer_output = _remove_false_missing(analyzer_output, used_tags)

    # ── 4. Prowide + LLM 결과 병합 ────────────────────────────────────────
    llm_dict          = _analyzer_output_to_llm_dict(analyzer_output)
    validation_result = reconcile(syntax_result, llm_dict, rule_chunks)

    return {
        **state,
        "validation_result": validation_result,
        "needs_hitl":        validation_result["needs_hitl"],
        "output": {
            "type":             "analysis",
            "verdict":          validation_result["verdict"],
            "source_msg_type":  analyzer_output.source_msg_type,
            "target_msg_type":  analyzer_output.target_msg_type,
            "transaction_count": analyzer_output.transaction_count,
            "currency":         analyzer_output.currency,
            "missing_fields":   analyzer_output.missing_fields,
            "details":          validation_result,
            "field_analysis":   [f.model_dump() for f in analyzer_output.field_analysis],
        },
    }
