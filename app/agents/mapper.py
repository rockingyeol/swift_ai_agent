"""
MX/MT Mapper Agent.
레거시 MT → MX 정밀 업리프트(uplift)가 핵심.
Prowide REST /translate 엔드포인트 + LLM 보강(구조화 주소 등)을 결합한다.
plan.md §1 설계 함의 참조.
"""
from __future__ import annotations

from app.graph.state import AgentState
from app.llm import VLLM_MODEL, format_rule_chunks, get_llm, parse_llm_json
from app.prompts.mapper_prompts import MAPPER_SYSTEM, MAPPER_USER
from app.rag.retriever import SwiftRetriever
from app.validation.prowide_client import prowide_translate

# MT→MX 기본 유형 매핑 (CBPR+ 최신 SRU 기준)
_MT_TO_MX: dict[str, str] = {
    "MT103": "pacs.008.001.08",
    "MT202": "pacs.009.001.08",
    "MT200": "pacs.009.001.08",
    "MT910": "camt.054.001.08",
    "MT940": "camt.053.001.08",
    "MT950": "camt.053.001.08",
}
_MX_TO_MT: dict[str, str] = {v: k for k, v in _MT_TO_MX.items()}

_retriever: SwiftRetriever | None = None


def _get_retriever() -> SwiftRetriever:
    global _retriever
    if _retriever is None:
        _retriever = SwiftRetriever()
    return _retriever


def _infer_target_type(msg_type: str, direction: str) -> str:
    upper = msg_type.upper()
    if direction == "mt_to_mx":
        return _MT_TO_MX.get(upper, "")
    return _MX_TO_MT.get(upper, "")


def run_mapper(state: AgentState) -> AgentState:
    raw_message    = state.get("raw_message", "")
    masked_message = state.get("masked_message", "")
    msg_type       = state.get("msg_type", "")

    direction    = "mt_to_mx" if msg_type.upper().startswith("MT") else "mx_to_mt"
    target_type  = _infer_target_type(msg_type, direction)

    # 1. Prowide 변환 (원본 전문 — PII LLM 미노출)
    translate_result = prowide_translate(raw_message, direction=direction)
    prowide_draft    = translate_result.get("content", "")
    prowide_degraded = translate_result.get("degraded", False)

    # 2. 목표 전문 유형의 RAG 규칙 검색
    retriever   = _get_retriever()
    query       = (
        f"{target_type} field mapping structured address LEI BIC "
        f"uplift {masked_message[:200]}"
    )
    rule_chunks = retriever.search(
        query=query,
        filters={"message_type": target_type} if target_type else None,
        top_k=5,
        rerank=True,
        include_parents=True,
    )

    # 3. LLM 보강 — 구조화 필드 보완 및 매핑 검증
    client      = get_llm()
    user_prompt = MAPPER_USER.format(
        source_type=msg_type,
        target_type=target_type or "unknown",
        prowide_draft=prowide_draft or "(Prowide 변환 미완료)",
        masked_source=masked_message,
        retrieved_rules=format_rule_chunks(rule_chunks),
    )

    response = client.chat.completions.create(
        model=VLLM_MODEL,
        messages=[
            {"role": "system", "content": MAPPER_SYSTEM},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    llm_result = parse_llm_json(response.choices[0].message.content or "")

    needs_hitl = (
        prowide_degraded
        or bool(llm_result.get("enhancement_warnings"))
        or bool(llm_result.get("unmapped_fields"))
    )

    return {
        **state,
        "needs_hitl": needs_hitl,
        "validation_result": {
            "verdict":          "PENDING_REVIEW" if needs_hitl else "PASS",
            "needs_hitl":       needs_hitl,
            "prowide_degraded": prowide_degraded,
        },
        "output": {
            "type":             "mapped_message",
            "direction":        direction,
            "prowide_draft":    prowide_draft,
            "enhanced":         llm_result.get("enhanced_message", prowide_draft),
            "unmapped_fields":  llm_result.get("unmapped_fields", []),
            "warnings":         llm_result.get("enhancement_warnings", []),
            "guidebook_basis": [
                {"page": c.page, "rule_id": c.rule_id, "field": c.field_tag}
                for c in rule_chunks
            ],
        },
    }
