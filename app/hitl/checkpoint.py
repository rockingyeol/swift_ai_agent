"""
HITL(Human-in-the-Loop) 체크포인트.
LangGraph interrupt()를 사용해 고위험 건을 사람 검수로 중단·재개한다.
plan.md [6] 참조.
"""
from __future__ import annotations

from langgraph.types import interrupt

from app.graph.state import AgentState


def hitl_checkpoint(state: AgentState) -> AgentState:
    """needs_hitl=True 이면 그래프를 중단하고 검수자 입력을 기다린다."""
    if not state.get("needs_hitl", False):
        return state

    # interrupt()는 LangGraph가 상태를 체크포인트에 저장한 뒤 외부 재개를 기다린다.
    decision = interrupt({
        "reason": "needs_human_review",
        "validation_result": state.get("validation_result"),
    })

    return {
        **state,
        "hitl_decision": decision.get("action"),
        "hitl_comment": decision.get("comment"),
    }
