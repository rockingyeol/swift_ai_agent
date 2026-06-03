"""
Swift AI Agent — FastAPI 진입점.

엔드포인트:
  POST /process              — 새 SWIFT 메시지 처리 시작
  POST /resume/{thread_id}   — HITL 검수 결정으로 중단 스레드 재개
  GET  /status/{thread_id}   — 스레드 현재 상태 조회
  GET  /health               — 헬스체크
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException  # noqa: E402
from langgraph.types import Command  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app.graph.graph import compiled_graph  # noqa: E402

app = FastAPI(title="SWIFT AI Agent", version="0.1.0")


# ---------------------------------------------------------------------------
# Request / Response 스키마
# ---------------------------------------------------------------------------

class ProcessRequest(BaseModel):
    raw_message: str
    msg_type: str = ""
    user_intent: Optional[str] = None   # "analyze" | "generate" | "map"


class ResumeRequest(BaseModel):
    action: str                         # "approve" | "reject" | "modify"
    comment: Optional[str] = None


class ProcessResponse(BaseModel):
    thread_id: str
    status: str                         # "completed" | "pending_hitl" | "error"
    output: Optional[dict[str, Any]] = None
    validation_result: Optional[dict[str, Any]] = None
    hitl_payload: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _build_response(thread_id: str, state: dict[str, Any]) -> ProcessResponse:
    """최종 상태 딕셔너리에서 API 응답을 구성한다."""
    snapshot = compiled_graph.get_state(_config(thread_id))

    # snapshot.next 가 비어 있지 않으면 그래프가 HITL 인터럽트로 일시 정지 중
    if snapshot and snapshot.next:
        return ProcessResponse(
            thread_id=thread_id,
            status="pending_hitl",
            output=state.get("output"),
            validation_result=state.get("validation_result"),
            hitl_payload={
                "reason": "needs_human_review",
                "msg_type": state.get("msg_type"),
                "validation_result": state.get("validation_result"),
                "pending_nodes": list(snapshot.next),
            },
        )

    if state.get("error"):
        return ProcessResponse(
            thread_id=thread_id,
            status="error",
            output={"error": state["error"]},
        )

    return ProcessResponse(
        thread_id=thread_id,
        status="completed",
        output=state.get("output"),
        validation_result=state.get("validation_result"),
    )


# ---------------------------------------------------------------------------
# 엔드포인트
# ---------------------------------------------------------------------------

@app.post("/process", response_model=ProcessResponse)
def process_message(req: ProcessRequest) -> ProcessResponse:
    """
    새 SWIFT 메시지 처리 요청을 시작한다.
    HITL 체크포인트에서 멈추면 status='pending_hitl'과 thread_id를 반환한다.
    검수자는 POST /resume/{thread_id}로 결정을 전송하면 된다.
    """
    thread_id = str(uuid.uuid4())
    initial_state: dict[str, Any] = {
        "raw_message": req.raw_message,
        "msg_type": req.msg_type,
    }
    if req.user_intent:
        initial_state["user_intent"] = req.user_intent

    result: dict[str, Any] = compiled_graph.invoke(
        initial_state,
        config=_config(thread_id),
    )
    return _build_response(thread_id, result)


@app.post("/resume/{thread_id}", response_model=ProcessResponse)
def resume_thread(thread_id: str, req: ResumeRequest) -> ProcessResponse:
    """
    HITL 체크포인트에서 대기 중인 스레드를 검수자 결정으로 재개한다.
    action: 'approve' | 'reject' | 'modify'
    """
    config = _config(thread_id)
    snapshot = compiled_graph.get_state(config)
    if not snapshot or not snapshot.next:
        raise HTTPException(
            status_code=404,
            detail="Thread not found or already completed.",
        )

    # interrupt() 재개 — Command(resume=...) 값이 hitl_checkpoint 의 interrupt() 반환값이 됨
    result: dict[str, Any] = compiled_graph.invoke(
        Command(resume={"action": req.action, "comment": req.comment or ""}),
        config=config,
    )
    return _build_response(thread_id, result)


@app.get("/status/{thread_id}", response_model=ProcessResponse)
def get_status(thread_id: str) -> ProcessResponse:
    """스레드의 현재 처리 상태를 조회한다."""
    config = _config(thread_id)
    snapshot = compiled_graph.get_state(config)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Thread not found.")
    return _build_response(thread_id, snapshot.values)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# CLI 진입점
# ---------------------------------------------------------------------------

def main() -> None:
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8080, reload=False)


if __name__ == "__main__":
    main()
