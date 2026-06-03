"""
LangGraph 공유 상태 스키마.
모든 노드는 이 TypedDict를 읽고 부분 업데이트를 반환한다.
"""
from __future__ import annotations

from typing import Any, Literal, Optional
from typing_extensions import TypedDict


class AgentState(TypedDict, total=False):
    # --- 입력 ---
    raw_message: str                        # 원본 MT/MX 전문 (Prowide 전용)
    masked_message: str                     # PII 마스킹본 (LLM 전용)
    msg_type: str                           # "MT103", "pacs.008.001.08" 등
    user_intent: Literal["analyze", "generate", "map"]

    # --- PII 마스킹 ---
    pii_mapping: dict[str, str]             # placeholder → 원본 매핑

    # --- 라우팅 ---
    routed_agent: Literal["analyzer", "generator", "mapper"]

    # --- 검증/분석 결과 ---
    validation_result: dict[str, Any]       # reconcile() 반환값

    # --- HITL ---
    needs_hitl: bool
    hitl_decision: Optional[Literal["approve", "reject", "modify"]]
    hitl_comment: Optional[str]

    # --- 최종 산출물 ---
    output: dict[str, Any]

    # --- 감사 로그 ---
    audit_entries: list[dict[str, Any]]

    # --- 에러 ---
    error: Optional[str]
