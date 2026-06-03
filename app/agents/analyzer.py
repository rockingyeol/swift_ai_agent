"""
Analyzer Agent.
Prowide 구문 검증 + RAG 검색 + LLM 의미 분석을 결합한 하이브리드 검증을 수행한다.
plan.md §3 참조.
"""
from __future__ import annotations

from app.graph.state import AgentState
from app.llm import VLLM_MODEL, format_rule_chunks, get_llm, parse_llm_json
from app.prompts.analyzer_prompts import ANALYZER_SYSTEM, ANALYZER_USER, FEWSHOT
from app.rag.retriever import SwiftRetriever
from app.validation.prowide_client import prowide_syntax_verify
from app.validation.reconciler import reconcile

_retriever: SwiftRetriever | None = None


def _get_retriever() -> SwiftRetriever:
    global _retriever
    if _retriever is None:
        _retriever = SwiftRetriever()
    return _retriever


def run_analyzer(state: AgentState) -> AgentState:
    raw_message    = state.get("raw_message", "")
    masked_message = state.get("masked_message", "")
    msg_type       = state.get("msg_type", "")

    # 1. Prowide 구문/네트워크 검증 (원본 전문 — PII LLM 미노출)
    syntax_result = prowide_syntax_verify(raw_message, msg_type)

    # 2. RAG 하이브리드 검색: 해당 전문 유형의 규칙 청크 조회
    retriever   = _get_retriever()
    query       = f"{msg_type} validation rules conditional presence format {masked_message[:300]}"
    rule_chunks = retriever.search(
        query=query,
        filters={"message_type": msg_type} if msg_type else None,
        top_k=5,
        rerank=True,
        include_parents=True,
    )

    # 3. LLM 의미/조건부 규칙 분석 (마스킹 전문만 전달)
    client      = get_llm()
    user_prompt = ANALYZER_USER.format(
        fewshot=FEWSHOT,
        masked_message=masked_message,
        retrieved_rules=format_rule_chunks(rule_chunks),
    )

    response = client.chat.completions.create(
        model=VLLM_MODEL,
        messages=[
            {"role": "system", "content": ANALYZER_SYSTEM},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    llm_result = parse_llm_json(response.choices[0].message.content or "")

    # 4. Prowide 구문 결과 + LLM 의미 결과 병합
    validation_result = reconcile(syntax_result, llm_result, rule_chunks)

    return {
        **state,
        "validation_result": validation_result,
        "needs_hitl": validation_result["needs_hitl"],
        "output": {
            "type":    "analysis",
            "verdict": validation_result["verdict"],
            "details": validation_result,
        },
    }
