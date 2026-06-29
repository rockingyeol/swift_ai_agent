"""
HITL(Human-in-the-Loop) 체크포인트.
LangGraph interrupt()를 사용해 고위험 건을 사람 검수로 중단·재개한다.
"""
from __future__ import annotations

import structlog
from langgraph.types import interrupt

from app.graph.state import AgentState

log = structlog.get_logger(__name__)

_VALID_ACTIONS = {"approve", "reject", "modify"}


def _validate_decision(decision: object) -> tuple[str, str]:
    """
    interrupt() 반환값 검증.

    반환: (action, comment) 튜플
    잘못된 구조나 action 값이면 ValueError 발생.
    """
    if not isinstance(decision, dict):
        raise ValueError(
            f"HITL decision must be a dict, got {type(decision).__name__}"
        )

    action = decision.get("action")
    if not action:
        raise ValueError("HITL decision missing required field: 'action'")

    if action not in _VALID_ACTIONS:
        raise ValueError(
            f"Invalid HITL action '{action}'. Must be one of: {sorted(_VALID_ACTIONS)}"
        )

    comment = decision.get("comment", "")
    if not isinstance(comment, str):
        comment = str(comment)

    return action, comment


def hitl_checkpoint(state: AgentState) -> AgentState:
    """needs_hitl=True 이면 그래프를 중단하고 검수자 입력을 기다린다."""
    if not state.get("needs_hitl", False):
        return state

    # interrupt()는 LangGraph가 상태를 체크포인트에 저장한 뒤 외부 재개를 기다린다.
    decision = interrupt({
        "reason": "needs_human_review",
        "validation_result": state.get("validation_result"),
    })

    try:
        action, comment = _validate_decision(decision)
    except ValueError as exc:
        log.error("hitl_invalid_decision", error=str(exc), decision=str(decision))
        # needs_hitl=False + error 설정 → graph가 FAILED 상태로 종료.
        # needs_hitl=True 유지 시 재개 시도 → 무한 루프 가능성 있음.
        return {
            **state,
            "needs_hitl": False,
            "error": f"HITL 결정 오류: {exc}",
            "hitl_decision": "error",
            "hitl_comment": None,
        }

    log.info("hitl_decision_received", action=action, has_comment=bool(comment))
    return {
        **state,
        "hitl_decision": action,
        "hitl_comment":  comment,
    }
