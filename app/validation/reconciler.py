"""
Rule Engine(Prowide) + LLM 결과 병합 및 HITL 판정.
원칙:
  - 구문 위반 → Prowide 신뢰 (결정론적)
  - 의미/조건부 → LLM 신뢰
  - degraded(룰엔진 장애) 시 무인 통과 금지 → fail-safe HITL
plan.md §3 reconcile() 참조.
"""
from __future__ import annotations

from typing import Any

from app.rag.chunker import SwiftChunk


def reconcile(
    syntax: dict[str, Any],
    llm_result: dict[str, Any],
    rule_chunks: list[SwiftChunk],
) -> dict[str, Any]:
    syntax_problems = syntax.get("problems", [])
    llm_violations  = llm_result.get("violations", [])
    llm_verdict     = llm_result.get("verdict", "ERROR")

    needs_hitl = (
        bool(syntax_problems)
        or llm_verdict in ("REJECT", "WARNING", "ERROR")
        or syntax.get("degraded", False)
    )

    final_verdict = (
        "REJECT"  if (syntax_problems or llm_verdict == "REJECT")
        else "WARNING" if llm_verdict == "WARNING"
        else "PASS"
    )

    return {
        "verdict": final_verdict,
        "needs_hitl": needs_hitl,
        "rule_engine": {
            "problems": syntax_problems,
            "degraded": syntax.get("degraded"),
        },
        "semantic": {
            "violations": llm_violations,
            "warnings": llm_result.get("warnings", []),
            "conditional_rules": llm_result.get("applied_conditional_rules", []),
        },
        "guidebook_basis": [
            {"page": c.page, "rule_id": c.rule_id, "field": c.field_tag}
            for c in rule_chunks
        ],
    }
