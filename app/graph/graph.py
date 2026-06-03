"""
LangGraph 메인 그래프 조립.

노드 순서:
  pii_mask → supervisor → [analyzer | generator | mapper]
           → hitl_checkpoint ──► (needs_hitl=True)  → [interrupt] → resume
                              └─► (needs_hitl=False) ─┐
  hitl 승인/수정: ─────────────────────────────────────┴► unmask → audit → END
  hitl 거부:                                                reject → audit → END
"""
from __future__ import annotations

from typing import Literal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from app.agents.analyzer import run_analyzer
from app.agents.generator import run_generator
from app.agents.mapper import run_mapper
from app.audit.logger import write_audit
from app.graph.state import AgentState
from app.graph.supervisor import classify_intent, route
from app.hitl.checkpoint import hitl_checkpoint
from app.pii.masker import mask_pii, unmask_pii


def _handle_rejection(state: AgentState) -> AgentState:
    """HITL 거부 결정 시 output에 상태를 기록한다."""
    return {
        **state,
        "output": {
            **(state.get("output") or {}),
            "status": "rejected",
            "reason": state.get("hitl_comment") or "Human reviewer rejected this request.",
        },
    }


def _route_after_hitl(state: AgentState) -> Literal["unmask", "reject"]:
    """HITL 결과에 따라 분기한다.
    거부(reject) → reject 노드, 그 외(승인/수정/HITL 불필요) → unmask 노드.
    """
    if state.get("hitl_decision") == "reject":
        return "reject"
    return "unmask"


def build_graph() -> StateGraph:
    """StateGraph를 조립하여 반환한다 (compile 미포함)."""
    g = StateGraph(AgentState)

    g.add_node("pii_mask", mask_pii)
    g.add_node("supervisor", classify_intent)
    g.add_node("analyzer", run_analyzer)
    g.add_node("generator", run_generator)
    g.add_node("mapper", run_mapper)
    g.add_node("hitl_checkpoint", hitl_checkpoint)
    g.add_node("unmask", unmask_pii)
    g.add_node("reject", _handle_rejection)
    g.add_node("audit", write_audit)

    g.set_entry_point("pii_mask")
    g.add_edge("pii_mask", "supervisor")
    g.add_conditional_edges("supervisor", route, {
        "analyzer": "analyzer",
        "generator": "generator",
        "mapper": "mapper",
    })
    for agent in ("analyzer", "generator", "mapper"):
        g.add_edge(agent, "hitl_checkpoint")

    # HITL 이후: 거부 여부에 따라 분기
    g.add_conditional_edges("hitl_checkpoint", _route_after_hitl, {
        "unmask": "unmask",
        "reject": "reject",
    })
    g.add_edge("unmask", "audit")
    g.add_edge("reject", "audit")
    g.add_edge("audit", END)

    return g


# 개발용 인메모리 체크포인터 — interrupt() 재개에 필수
# 프로덕션 전환 시 SqliteSaver / PostgresSaver 로 교체
_checkpointer = MemorySaver()
compiled_graph = build_graph().compile(checkpointer=_checkpointer)
