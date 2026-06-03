"""
Generator Agent.
자연어 요청을 받아 MT/MX 전문 초안을 생성한다.
"""
from __future__ import annotations

from app.graph.state import AgentState
from app.llm import VLLM_MODEL, format_rule_chunks, get_llm
from app.prompts.generator_prompts import GENERATOR_SYSTEM, GENERATOR_USER
from app.rag.retriever import SwiftRetriever

_retriever: SwiftRetriever | None = None


def _get_retriever() -> SwiftRetriever:
    global _retriever
    if _retriever is None:
        _retriever = SwiftRetriever()
    return _retriever


def run_generator(state: AgentState) -> AgentState:
    masked_message = state.get("masked_message", "")
    msg_type       = state.get("msg_type", "")

    # 1. RAG 검색 — 생성 대상 전문 유형의 필드 구조 및 필수 규칙 조회
    retriever   = _get_retriever()
    query       = f"{msg_type} mandatory fields structure usage {masked_message[:200]}"
    rule_chunks = retriever.search(
        query=query,
        filters={"message_type": msg_type} if msg_type else None,
        top_k=6,
        rerank=True,
        include_parents=False,
    )

    # 2. LLM 전문 초안 생성
    client      = get_llm()
    user_prompt = GENERATOR_USER.format(
        user_request=masked_message,
        retrieved_rules=format_rule_chunks(rule_chunks),
    )

    response = client.chat.completions.create(
        model=VLLM_MODEL,
        messages=[
            {"role": "system", "content": GENERATOR_SYSTEM},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.2,
    )
    draft = (response.choices[0].message.content or "").strip()

    return {
        **state,
        # 생성 결과는 항상 사람 검수 필요
        "needs_hitl": True,
        "validation_result": {
            "verdict":   "PENDING_REVIEW",
            "needs_hitl": True,
        },
        "output": {
            "type":  "generated_message",
            "draft": draft,
            "guidebook_basis": [
                {"page": c.page, "rule_id": c.rule_id, "field": c.field_tag}
                for c in rule_chunks
            ],
        },
    }
