"""
감사 로그(audit trail) 기록.
근거 page·규칙 ID·LLM 추론·검수자 ID를 전건 JSONL로 저장한다.
plan.md [7] 참조.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from app.graph.state import AgentState

_LOG_PATH = os.getenv("AUDIT_LOG_PATH", "./audit.jsonl")


def write_audit(state: AgentState) -> AgentState:
    """LangGraph 노드: 최종 상태를 감사 로그에 추가한다."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "msg_type": state.get("msg_type"),
        "verdict": (state.get("validation_result") or {}).get("verdict"),
        "needs_hitl": state.get("needs_hitl"),
        "hitl_decision": state.get("hitl_decision"),
        "hitl_comment": state.get("hitl_comment"),
        "guidebook_basis": (state.get("validation_result") or {}).get("guidebook_basis"),
    }
    with open(_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return state
