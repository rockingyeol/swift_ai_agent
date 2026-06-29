"""
Swift AI Agent — FastAPI 진입점 (v2).

엔드포인트:
  POST /convert                — 새 SWIFT 메시지 변환·분석·생성 요청 시작
  POST /resume                 — HITL 대기 스레드에 검수 결정 주입 후 재개
  GET  /convert/{thread_id}    — 스레드 처리 상태 조회
  GET  /health                 — 서비스 헬스체크 (Prowide 연결 포함)
  GET  /threads                — 활성 스레드 목록 (MemorySaver 한정)

하위 호환 엔드포인트 (deprecated — 다음 메이저에서 제거 예정):
  POST /process                — /convert 와 동일
  POST /resume/{thread_id}     — /resume 와 동일
  GET  /status/{thread_id}     — /convert/{thread_id} 와 동일
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from enum import Enum
from typing import Annotated, Any, Literal, Optional

from dotenv import load_dotenv

load_dotenv()

import structlog  # noqa: E402
from fastapi import Body, Depends, FastAPI, HTTPException, Path, Query, status  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from langgraph.types import Command  # noqa: E402
from pydantic import BaseModel, Field, field_validator, model_validator  # noqa: E402

from app.graph.graph import compiled_graph  # noqa: E402
from app.validation.prowide_client import health_check as prowide_health  # noqa: E402

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# 애플리케이션 수명 주기
# ---------------------------------------------------------------------------

_REQUIRED_ENV_VARS = ["PROWIDE_URL", "QDRANT_URL"]


@asynccontextmanager
async def lifespan(application: FastAPI):
    """기동 시 필수 환경변수 검증 및 외부 서비스 연결 상태를 로그에 기록한다."""
    missing = [v for v in _REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        log.warning("missing_env_vars", vars=missing,
                    detail="필수 환경변수가 설정되지 않았습니다. degraded 모드로 실행됩니다.")

    prowide_ok = await asyncio.to_thread(prowide_health)
    log.info("startup", prowide_available=prowide_ok, missing_env=missing or None)
    if not prowide_ok:
        log.warning(
            "prowide_unavailable",
            detail="Prowide 서비스에 연결할 수 없습니다. degraded 모드로 실행됩니다.",
        )
    yield
    log.info("shutdown")


# ---------------------------------------------------------------------------
# FastAPI 앱 구성
# ---------------------------------------------------------------------------

app = FastAPI(
    title="SWIFT AI Agent",
    version="2.0.0",
    description=(
        "MT/MX SWIFT 전문의 변환·검증·생성을 위한 LangGraph 기반 AI 에이전트 API.\n\n"
        "## 기본 흐름\n"
        "1. **POST /convert** — 전문을 제출하면 파이프라인이 실행됩니다.\n"
        "   - HITL 체크포인트에서 멈추면 `status=pending_hitl`과 `thread_id`를 반환합니다.\n"
        "2. **POST /resume** — `thread_id`와 검수 결정(`approve`/`reject`/`modify`)을 "
        "   보내면 파이프라인이 재개됩니다.\n"
        "3. **GET /convert/{thread_id}** — 언제든지 현재 상태를 조회할 수 있습니다.\n\n"
        "## PII 보안 정책\n"
        "- 원본 전문(IBAN·BIC·금액 등)은 Prowide 서비스에만 전달됩니다.\n"
        "- LLM 에는 마스킹된 전문(`<<IBAN_1>>` 형태의 플레이스홀더)만 전달됩니다.\n"
        "- 최종 응답에서는 플레이스홀더가 원본값으로 완전 복원됩니다.\n"
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Test UI — http://localhost:8000/ui/
_static_dir = FilePath(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/ui", StaticFiles(directory=str(_static_dir), html=True), name="ui")


# ---------------------------------------------------------------------------
# 열거형
# ---------------------------------------------------------------------------

class ProcessingStatus(str, Enum):
    """파이프라인 처리 상태."""
    COMPLETED   = "completed"    # 정상 완료 (PII 복원 포함)
    PENDING_HITL = "pending_hitl" # HITL 체크포인트에서 대기 중
    FAILED      = "failed"       # 오류 발생


class HitlAction(str, Enum):
    """HITL 검수자 결정 코드."""
    APPROVE = "approve"  # 현재 결과 그대로 승인
    REJECT  = "reject"   # 처리 거부 (output.status='rejected' 반환)
    MODIFY  = "modify"   # 수정 의견을 첨부하여 통과


class UserIntent(str, Enum):
    """파이프라인 실행 의도."""
    ANALYZE  = "analyze"   # 기존 전문 유효성 검증·분석
    GENERATE = "generate"  # 자연어 요청으로 새 전문 초안 생성
    MAP      = "map"       # MT↔MX 형식 변환 (업리프트/다운리프트)
    EXPLAIN  = "explain"   # 전문 유형 기본 정보 설명 (명칭·목적·필드)
    SCHEMA   = "schema"    # ISO 20022 전문의 인터랙티브 스키마 트리 탐색


# ---------------------------------------------------------------------------
# 하위 모델
# ---------------------------------------------------------------------------

class RuleEngineSummary(BaseModel):
    """Prowide 구문 검증 요약."""
    problems: list[dict[str, Any]] = Field(
        default_factory=list,
        description="발견된 구문/네트워크 위반 목록. 비어 있으면 구문 정상.",
        examples=[[{"code": "T28", "desc": "Field 32A: invalid value date"}]],
    )
    degraded: Optional[bool] = Field(
        None,
        description="True 이면 Prowide 서비스가 응답하지 않아 fail-safe HITL 발동.",
    )


class SemanticSummary(BaseModel):
    """LLM 의미·조건부 규칙 분석 요약."""
    violations: list[dict[str, Any]] = Field(
        default_factory=list,
        description="LLM 이 감지한 의미적 위반 목록.",
    )
    warnings: list[dict[str, Any]] = Field(
        default_factory=list,
        description="LLM 이 감지한 경고(권장 사항) 목록.",
    )
    conditional_rules: list[dict[str, Any]] = Field(
        default_factory=list,
        description="적용된 조건부 규칙(C1, C2 …) 목록.",
    )


class GuidebookRef(BaseModel):
    """분석 근거가 된 가이드북 참조 정보."""
    page:     Optional[int]  = Field(None, description="가이드북 페이지 번호.")
    rule_id:  Optional[str]  = Field(None, description="규칙 ID (예: C1, N4).")
    field:    Optional[str]  = Field(None, description="관련 필드 태그 (예: 50K, 32A).")
    source:   Optional[str]  = Field(None, description="출처 문서명.")


class ValidationDetail(BaseModel):
    """reconcile() 결과 전체를 담는 검증 상세 정보."""
    verdict: str = Field(
        description="최종 판정. PASS | WARNING | REJECT | PENDING_REVIEW | ERROR",
        examples=["PASS"],
    )
    needs_hitl:   bool                    = Field(description="HITL 검수 필요 여부.")
    rule_engine:  Optional[RuleEngineSummary] = None
    semantic:     Optional[SemanticSummary]   = None
    guidebook_basis: list[GuidebookRef]   = Field(
        default_factory=list,
        description="분석에 사용된 가이드북 규칙 참조 목록.",
    )


class HitlPayload(BaseModel):
    """HITL 대기 상태일 때 검수자에게 전달되는 컨텍스트."""
    reason:            str  = Field(description="HITL 발동 사유.")
    msg_type:          Optional[str]              = None
    validation_result: Optional[dict[str, Any]]   = None
    pending_nodes:     list[str]                  = Field(
        default_factory=list,
        description="재개를 기다리는 LangGraph 노드 이름 목록.",
    )


class OutputDetail(BaseModel):
    """
    에이전트별 최종 산출물.

    - analyzer  → type='analysis',        verdict, details
    - generator → type='generated_message', draft
    - mapper    → type='mapped_message',   direction, enhanced, prowide_draft
    - explainer → type='explanation',      full_name, korean_name, purpose, …
    """
    type:          str = Field(description="산출물 유형.")
    # analyzer
    verdict:        Optional[str]            = Field(None, description="분석 판정 결과.")
    details:        Optional[dict[str, Any]] = Field(None, description="분석 세부 사항.")
    field_analysis: list[dict[str, Any]]     = Field(default_factory=list, description="필드별 값·의미 해석 목록.")
    # generator
    draft:         Optional[str]           = Field(None, description="생성된 전문 초안.")
    # mapper
    direction:     Optional[str]           = Field(None, description="변환 방향 (mt_to_mx / mx_to_mt).")
    prowide_draft: Optional[str]           = Field(None, description="Prowide 1차 변환 초안.")
    enhanced:      Optional[str]           = Field(None, description="LLM 보강 후 최종 전문.")
    mapper_output: Optional[dict[str, Any]] = Field(None, description="Mapper LLM 매핑 명세.")
    unmapped_fields: list[Any]             = Field(default_factory=list)
    warnings:        list[Any]             = Field(default_factory=list)
    guidebook_basis: list[dict[str, Any]]  = Field(default_factory=list)
    # explainer
    full_name:        Optional[str]           = Field(None, description="전문 영문 공식 명칭.")
    korean_name:      Optional[str]           = Field(None, description="전문 한국어 명칭.")
    purpose:          Optional[str]           = Field(None, description="전문 목적 설명.")
    use_cases:        list[str]               = Field(default_factory=list, description="사용 시나리오 목록.")
    key_fields:       list[dict[str, Any]]    = Field(default_factory=list, description="주요 필드 목록.")
    special_codes:    list[dict[str, Any]]    = Field(default_factory=list, description="특수 코드/값 목록.")
    related_messages: list[dict[str, Any]]    = Field(default_factory=list, description="관련 전문 목록.")
    flow_description: Optional[str]           = Field(None, description="은행 간 전문 흐름 설명.")
    # general_answer (explainer 일반 Q&A 모드)
    query:           Optional[str]            = Field(None, description="원본 질문.")
    answer:          Optional[str]            = Field(None, description="자유 형식 답변.")
    # mapping_rule (explainer 매핑 규칙 모드)
    source_field:    Optional[str]            = Field(None, description="MT 필드 태그 (예: :72:).")
    source_msg_type: Optional[str]            = Field(None, description="원본 전문 유형.")
    target_msg_type: Optional[str]            = Field(None, description="대상 전문 유형.")
    mapping_summary: Optional[str]            = Field(None, description="매핑 관계 핵심 요약.")
    mapping_details: list[dict[str, Any]]     = Field(default_factory=list, description="조건별 매핑 상세.")
    constraints:     list[str]               = Field(default_factory=list, description="주의사항·제약.")
    guidebook_refs:  list[str]               = Field(default_factory=list, description="가이드북 참조.")
    # explainer / schema_explorer 공통
    msg_type:      Optional[str]           = Field(None, description="전문 유형 (버전 포함, 예: pacs.002.001.10).")
    # schema_explorer
    explanation:   Optional[str]           = Field(None, description="스키마 트리 요약 설명.")
    schema_tree:   Optional[dict[str, Any]] = Field(None, description="인터랙티브 스키마 트리 JSON.")
    filter_mode:   Optional[str]           = Field(None, description="mandatory | all")
    sections:           Optional[list[Any]] = Field(None, description="섹션별 스키마 블록 배열.")
    schema_parse_error: Optional[str]      = Field(None, description="스키마 섹션 생성 실패 시 오류 원인.")
    cached:        Optional[bool]          = Field(None, description="캐시에서 반환된 경우 True.")
    # reject
    status:        Optional[str]           = Field(None, description="'rejected' 이면 HITL 거부 완료.")
    reason:        Optional[str]           = Field(None, description="거부 사유.")


# ---------------------------------------------------------------------------
# Request / Response 모델
# ---------------------------------------------------------------------------

class ConvertRequest(BaseModel):
    """
    `/convert` 엔드포인트 요청 모델.

    `user_intent` 를 생략하면 Supervisor 가 `raw_message` 의 키워드/LLM 으로 자동 분류합니다.
    """

    raw_message: Annotated[str, Field(
        min_length=1,
        max_length=10_000_000,
        description="처리할 원본 MT 또는 MX 전문. IBAN·BIC 등 PII 포함 가능.",
        examples=[
            "{1:F01BNKBKRSEAXXX0000000000}{2:I103DEUTDEDBXXXXN}"
            "{4:\n:20:REF001\n:32A:240115EUR5000,00\n-}"
        ],
    )]
    msg_type: Annotated[str, Field(
        default="",
        description=(
            "전문 유형 식별자. 예: 'MT103', 'MT202', 'pacs.008.001.08'.\n"
            "비워두면 Prowide 가 자동 감지를 시도합니다."
        ),
        examples=["MT103"],
    )]
    user_intent: Annotated[Optional[UserIntent], Field(
        default=None,
        description=(
            "처리 의도. 생략 시 Supervisor 가 자동 분류합니다.\n"
            "- analyze: 유효성 검증\n"
            "- generate: 자연어 → 전문 생성\n"
            "- map: MT↔MX 변환\n"
            "- explain: 전문 유형 기본 정보 설명"
        ),
    )]
    thread_id: Annotated[Optional[str], Field(
        default=None,
        description=(
            "클라이언트가 직접 지정하는 thread_id (멱등성 보장용).\n"
            "생략 시 서버에서 UUID v4 를 자동 생성합니다."
        ),
        examples=["550e8400-e29b-41d4-a716-446655440000"],
    )]

    @field_validator("msg_type", mode="before")
    @classmethod
    def normalise_msg_type(cls, v: Any) -> str:
        return (v or "").strip()

    @field_validator("thread_id", mode="before")
    @classmethod
    def validate_thread_id(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        return s


class ConvertResponse(BaseModel):
    """
    `/convert` 및 `/resume` 엔드포인트 공통 응답 모델.

    `status` 에 따라 후속 처리가 달라집니다:
    - **completed**: `output` 에 최종 결과물이 있습니다.
    - **pending_hitl**: `hitl_payload` 컨텍스트를 검수자에게 보여주고
      `POST /resume` 로 결정을 전송하세요.
    - **failed**: `message` 에 오류 원인이 있습니다.
    """

    thread_id: str = Field(description="이 처리 세션의 고유 식별자.")
    status: ProcessingStatus = Field(description="파이프라인 처리 상태.")
    message: str = Field(description="사람이 읽을 수 있는 상태 요약 메시지.")
    routed_agent: Optional[str] = Field(
        None,
        description="실행된 에이전트 이름. analyzer | generator | mapper",
    )
    output: Optional[OutputDetail] = Field(
        None,
        description="에이전트 최종 산출물. completed 상태에서만 완전히 채워집니다.",
    )
    validation_result: Optional[ValidationDetail] = Field(
        None,
        description="검증 상세 결과.",
    )
    hitl_payload: Optional[HitlPayload] = Field(
        None,
        description="HITL 대기 시 검수자에게 전달할 컨텍스트. pending_hitl 상태에서만 존재.",
    )
    processing_time_ms: Optional[float] = Field(
        None,
        description="파이프라인 실행 소요 시간(밀리초).",
    )
    error: Optional[str] = Field(
        None,
        description="오류 메시지. failed 상태에서만 채워집니다.",
    )


class ResumeRequest(BaseModel):
    """
    `/resume` 엔드포인트 요청 모델.

    HITL 체크포인트에서 대기 중인 `thread_id` 의 그래프를 재개합니다.
    """

    thread_id: Annotated[str, Field(
        min_length=1,
        description="재개할 스레드의 ID. `/convert` 응답의 `thread_id` 를 사용하세요.",
        examples=["550e8400-e29b-41d4-a716-446655440000"],
    )]
    action: Annotated[HitlAction, Field(
        description=(
            "검수자 결정:\n"
            "- **approve**: 현재 결과를 그대로 승인하고 완료 처리합니다.\n"
            "- **reject**: 처리를 거부합니다. 응답 `output.status='rejected'`.\n"
            "- **modify**: 수정 의견(`comment`)을 첨부하여 승인합니다."
        ),
    )]
    comment: Annotated[Optional[str], Field(
        default=None,
        max_length=2000,
        description="검수 의견 또는 수정 내용. `modify` / `reject` 시 권장.",
        examples=["Field 32A 날짜 형식을 YYMMDD 로 수정 후 재확인 필요"],
    )]


class HealthResponse(BaseModel):
    """서비스 헬스체크 응답."""
    status:             str  = Field(description="'ok' 또는 'degraded'.")
    prowide_available:  bool = Field(description="Prowide Java 서비스 연결 상태.")
    qdrant_available:   bool = Field(description="Qdrant 벡터DB 연결 상태.")
    version:            str  = Field(description="API 버전.")


# ---------------------------------------------------------------------------
# 내부 유틸리티
# ---------------------------------------------------------------------------

def _thread_config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _parse_validation(raw: dict[str, Any] | None) -> Optional[ValidationDetail]:
    if not raw:
        return None
    try:
        return ValidationDetail(
            verdict=raw.get("verdict", "ERROR"),
            needs_hitl=bool(raw.get("needs_hitl", False)),
            rule_engine=RuleEngineSummary(
                problems=raw.get("rule_engine", {}).get("problems", []),
                degraded=raw.get("rule_engine", {}).get("degraded"),
            ) if raw.get("rule_engine") else None,
            semantic=SemanticSummary(
                violations=raw.get("semantic", {}).get("violations", []),
                warnings=raw.get("semantic", {}).get("warnings", []),
                conditional_rules=raw.get("semantic", {}).get("conditional_rules", []),
            ) if raw.get("semantic") else None,
            guidebook_basis=[
                GuidebookRef(**ref)
                for ref in raw.get("guidebook_basis", [])
                if isinstance(ref, dict)
            ],
        )
    except Exception:
        return None


def _parse_output(raw: dict[str, Any] | None) -> Optional[OutputDetail]:
    if not raw:
        return None
    import structlog as _slog
    _slog.get_logger(__name__).info("_parse_output_raw_keys",
        keys=list(raw.keys()),
        schema_tree_present="schema_tree" in raw,
        schema_tree_type=type(raw.get("schema_tree")).__name__)
    try:
        return OutputDetail(
            type=raw.get("type", "unknown"),
            # analyzer
            verdict=raw.get("verdict"),
            details=raw.get("details"),
            field_analysis=raw.get("field_analysis") or [],
            # generator
            draft=raw.get("draft"),
            # mapper
            direction=raw.get("direction"),
            prowide_draft=raw.get("prowide_draft"),
            enhanced=raw.get("enhanced"),
            mapper_output=raw.get("mapper_output"),
            unmapped_fields=raw.get("unmapped_fields") or [],
            warnings=raw.get("warnings") or [],
            guidebook_basis=raw.get("guidebook_basis") or [],
            # explainer / schema_explorer 공통
            msg_type=raw.get("msg_type"),
            # explainer
            full_name=raw.get("full_name"),
            korean_name=raw.get("korean_name"),
            purpose=raw.get("purpose"),
            use_cases=raw.get("use_cases") or [],
            key_fields=raw.get("key_fields") or [],
            special_codes=raw.get("special_codes") or [],
            related_messages=raw.get("related_messages") or [],
            flow_description=raw.get("flow_description"),
            # general_answer
            query=raw.get("query"),
            answer=raw.get("answer"),
            # mapping_rule
            source_field=raw.get("source_field"),
            source_msg_type=raw.get("source_msg_type"),
            target_msg_type=raw.get("target_msg_type"),
            mapping_summary=raw.get("mapping_summary"),
            mapping_details=raw.get("mapping_details") or [],
            constraints=raw.get("constraints") or [],
            guidebook_refs=raw.get("guidebook_refs") or [],
            # schema_explorer
            explanation=raw.get("explanation"),
            schema_tree=raw.get("schema_tree"),
            filter_mode=raw.get("filter_mode"),
            sections=raw.get("sections"),
            schema_parse_error=raw.get("schema_parse_error"),
            cached=raw.get("cached"),
            # reject
            status=raw.get("status"),
            reason=raw.get("reason"),
        )
    except Exception as e:
        log.warning("parse_output_failed", error=str(e))
        return None


def _status_message(
    st: ProcessingStatus,
    state: dict[str, Any],
) -> str:
    verdict = (state.get("validation_result") or {}).get("verdict", "")
    agent   = state.get("routed_agent", "")
    if st == ProcessingStatus.COMPLETED:
        if state.get("output", {}).get("status") == "rejected":
            return "검수자가 처리를 거부하였습니다."
        agent_label = {"analyzer": "분석", "generator": "생성", "mapper": "변환"}.get(agent, "처리")
        verdict_label = {"PASS": " (PASS)", "WARNING": " (WARNING)", "REJECT": " (REJECT)"}.get(verdict, "")
        return f"파이프라인이 정상 완료되었습니다. [{agent_label}{verdict_label}]"
    if st == ProcessingStatus.PENDING_HITL:
        return (
            f"HITL 체크포인트에서 검수 대기 중입니다. "
            f"POST /resume 로 결정을 전송하세요. [verdict={verdict or 'N/A'}]"
        )
    return "파이프라인 실행 중 오류가 발생하였습니다."


def _build_response(
    thread_id: str,
    state: dict[str, Any],
    elapsed_ms: float | None = None,
) -> ConvertResponse:
    """
    LangGraph 최종 상태 딕셔너리 → ConvertResponse 변환.

    snapshot.next 가 비어 있지 않으면 HITL 대기 상태.
    """
    snapshot = compiled_graph.get_state(_thread_config(thread_id))

    if snapshot and snapshot.next:
        st = ProcessingStatus.PENDING_HITL
        hitl = HitlPayload(
            reason="needs_human_review",
            msg_type=state.get("msg_type"),
            validation_result=state.get("validation_result"),
            pending_nodes=list(snapshot.next),
        )
    elif state.get("error"):
        st = ProcessingStatus.FAILED
        hitl = None
    else:
        st = ProcessingStatus.COMPLETED
        hitl = None

    return ConvertResponse(
        thread_id=thread_id,
        status=st,
        message=_status_message(st, state),
        routed_agent=state.get("routed_agent"),
        output=_parse_output(state.get("output")),
        validation_result=_parse_validation(state.get("validation_result")),
        hitl_payload=hitl,
        processing_time_ms=elapsed_ms,
        error=state.get("error") if st == ProcessingStatus.FAILED else None,
    )


_GRAPH_TIMEOUT = float(os.getenv("GRAPH_TIMEOUT", "300"))


async def _run_graph(initial_state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """
    LangGraph ainvoke 로 그래프를 실행한다.
    interrupt() 발생 시 ainvoke 는 현재 상태 dict 를 반환하며,
    get_state().next 에 중단 노드가 기록된다.
    """
    try:
        result: dict[str, Any] | None = await asyncio.wait_for(
            compiled_graph.ainvoke(initial_state, config),
            timeout=_GRAPH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        log.error("graph_timeout", timeout=_GRAPH_TIMEOUT)
        raise HTTPException(
            status_code=status.HTTP_408_REQUEST_TIMEOUT,
            detail=f"파이프라인 실행이 {_GRAPH_TIMEOUT}초 내에 완료되지 않았습니다.",
        )
    if result is None:
        snapshot = compiled_graph.get_state(config)
        result = snapshot.values if snapshot else {}
    return result


async def _resume_graph(command: Command, config: dict[str, Any]) -> dict[str, Any]:
    """Command(resume=…) 로 HITL 중단 스레드를 재개한다."""
    try:
        result: dict[str, Any] | None = await asyncio.wait_for(
            compiled_graph.ainvoke(command, config),
            timeout=_GRAPH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        log.error("resume_graph_timeout", timeout=_GRAPH_TIMEOUT)
        raise HTTPException(
            status_code=status.HTTP_408_REQUEST_TIMEOUT,
            detail=f"파이프라인 재개가 {_GRAPH_TIMEOUT}초 내에 완료되지 않았습니다.",
        )
    if result is None:
        snapshot = compiled_graph.get_state(config)
        result = snapshot.values if snapshot else {}
    return result


# ---------------------------------------------------------------------------
# 엔드포인트 — /convert
# ---------------------------------------------------------------------------

@app.post(
    "/convert",
    response_model=ConvertResponse,
    status_code=status.HTTP_200_OK,
    summary="SWIFT 전문 변환·분석·생성 시작",
    tags=["Pipeline"],
    responses={
        200: {"description": "처리 완료 또는 HITL 대기 상태 반환."},
        422: {"description": "요청 유효성 검사 실패."},
        500: {"description": "파이프라인 내부 오류."},
    },
)
async def convert(req: ConvertRequest) -> ConvertResponse:
    """
    새 SWIFT 전문 처리 요청을 시작합니다.

    **처리 흐름:**
    1. PII 마스킹 (IBAN·BIC·금액 → 플레이스홀더)
    2. 의도 분류 (analyze / generate / map)
    3. 해당 에이전트 실행 (Prowide + RAG + LLM)
    4. HITL 체크포인트 평가
       - 위반/경고/장애 → `status=pending_hitl` 반환, `POST /resume` 대기
       - 정상 → PII 복원 → 감사 로그 → `status=completed` 반환

    **PII 보안:** `raw_message` 의 IBAN·BIC 등은 Prowide 서비스에만 전달되며,
    LLM 에는 마스킹된 전문만 전달됩니다. 응답에는 원본값이 복원됩니다.
    """
    thread_id = req.thread_id or str(uuid.uuid4())
    config    = _thread_config(thread_id)

    initial_state: dict[str, Any] = {
        "raw_message": req.raw_message,
        "msg_type":    req.msg_type,
    }
    if req.user_intent:
        initial_state["user_intent"] = req.user_intent.value

    log.info(
        "convert_start",
        thread_id=thread_id,
        msg_type=req.msg_type,
        user_intent=req.user_intent,
    )

    t0 = time.perf_counter()
    try:
        state = await _run_graph(initial_state, config)
    except Exception as exc:
        log.exception("convert_error", thread_id=thread_id, exc=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"파이프라인 실행 오류: {exc}",
        ) from exc
    elapsed_ms = (time.perf_counter() - t0) * 1000

    resp = _build_response(thread_id, state, elapsed_ms)
    log.info(
        "convert_done",
        thread_id=thread_id,
        status=resp.status,
        elapsed_ms=round(elapsed_ms, 1),
    )
    return resp


# ---------------------------------------------------------------------------
# 엔드포인트 — /resume
# ---------------------------------------------------------------------------

@app.post(
    "/resume",
    response_model=ConvertResponse,
    status_code=status.HTTP_200_OK,
    summary="HITL 대기 스레드 재개",
    tags=["Pipeline"],
    responses={
        200: {"description": "재개 후 처리 상태 반환."},
        404: {"description": "스레드를 찾을 수 없거나 이미 완료됨."},
        422: {"description": "요청 유효성 검사 실패."},
        500: {"description": "파이프라인 재개 오류."},
    },
)
async def resume(req: ResumeRequest) -> ConvertResponse:
    """
    HITL 체크포인트에서 대기 중인 스레드에 검수 결정을 주입하여 파이프라인을 재개합니다.

    **결정 코드:**
    | action  | 동작 |
    |---------|------|
    | approve | 현재 결과를 승인하고 PII 복원 → 감사 로그 → 완료 |
    | reject  | 처리 거부. `output.status='rejected'` 로 감사 로그 후 완료 |
    | modify  | 수정 의견 첨부 후 승인과 동일한 경로로 완료 |

    **선행 조건:** `/convert` 응답의 `status` 가 `pending_hitl` 이어야 합니다.
    """
    config   = _thread_config(req.thread_id)
    snapshot = compiled_graph.get_state(config)

    if not snapshot or not snapshot.next:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"thread_id='{req.thread_id}' 를 찾을 수 없거나 이미 완료된 스레드입니다. "
                "HITL 대기 상태인 스레드에만 재개 요청을 보낼 수 있습니다."
            ),
        )

    log.info(
        "resume_start",
        thread_id=req.thread_id,
        action=req.action,
        has_comment=bool(req.comment),
    )

    t0 = time.perf_counter()
    try:
        command = Command(resume={"action": req.action.value, "comment": req.comment or ""})
        state   = await _resume_graph(command, config)
    except Exception as exc:
        log.exception("resume_error", thread_id=req.thread_id, exc=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"스레드 재개 오류: {exc}",
        ) from exc
    elapsed_ms = (time.perf_counter() - t0) * 1000

    resp = _build_response(req.thread_id, state, elapsed_ms)
    log.info(
        "resume_done",
        thread_id=req.thread_id,
        action=req.action,
        status=resp.status,
        elapsed_ms=round(elapsed_ms, 1),
    )
    return resp


# ---------------------------------------------------------------------------
# 엔드포인트 — GET /convert/{thread_id}  (상태 조회)
# ---------------------------------------------------------------------------

@app.get(
    "/convert/{thread_id}",
    response_model=ConvertResponse,
    summary="스레드 처리 상태 조회",
    tags=["Pipeline"],
    responses={
        200: {"description": "현재 스레드 상태 반환."},
        404: {"description": "스레드를 찾을 수 없음."},
    },
)
async def get_convert_status(
    thread_id: Annotated[str, Path(description="조회할 스레드 ID.")],
) -> ConvertResponse:
    """
    진행 중이거나 완료된 스레드의 현재 상태를 반환합니다.

    폴링(polling) 기반으로 HITL 완료 여부를 확인하는 데 사용할 수 있습니다.
    """
    config   = _thread_config(thread_id)
    snapshot = compiled_graph.get_state(config)

    if not snapshot or not snapshot.values:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"thread_id='{thread_id}' 에 해당하는 스레드를 찾을 수 없습니다.",
        )

    return _build_response(thread_id, snapshot.values)


# ---------------------------------------------------------------------------
# 엔드포인트 — GET /health
# ---------------------------------------------------------------------------

@app.get(
    "/health",
    response_model=HealthResponse,
    summary="서비스 헬스체크",
    tags=["Infrastructure"],
)
async def health() -> HealthResponse:
    """
    FastAPI 서버와 Prowide 마이크로서비스의 연결 상태를 반환합니다.

    Kubernetes readiness probe 또는 모니터링 대시보드에서 활용할 수 있습니다.
    """
    def _qdrant_health() -> bool:
        try:
            from app.rag.indexer import get_client
            client = get_client()
            client.get_collections()
            return True
        except Exception:
            return False

    prowide_ok, qdrant_ok = await asyncio.gather(
        asyncio.to_thread(prowide_health),
        asyncio.to_thread(_qdrant_health),
    )
    all_ok = prowide_ok and qdrant_ok
    return HealthResponse(
        status="ok" if all_ok else "degraded",
        prowide_available=prowide_ok,
        qdrant_available=qdrant_ok,
        version=app.version,
    )


# ---------------------------------------------------------------------------
# 엔드포인트 — GET /threads  (활성 스레드 목록, MemorySaver 한정)
# ---------------------------------------------------------------------------

@app.get(
    "/msg-types",
    summary="인덱싱된 전문 유형 목록 조회",
    tags=["Infrastructure"],
    response_model=dict[str, Any],
)
async def list_msg_types() -> dict[str, Any]:
    """
    Qdrant 컬렉션에 인덱싱된 전문 유형 목록을 반환합니다.
    data/msg_descriptions.json 캐시가 있으면 LLM 생성 전문명을 포함합니다.
    """
    import json as _json
    from pathlib import Path as _Path

    # LLM 사전 생성 캐시 로드 (prefix → {name_en, name_ko})
    _cache_path = _Path(__file__).parent.parent / "data" / "msg_descriptions.json"
    _desc_cache: dict[str, Any] = {}
    if _cache_path.exists():
        try:
            with open(_cache_path, encoding="utf-8") as _f:
                _desc_cache = _json.load(_f)
        except Exception as _e:
            log.warning("msg_descriptions_cache_load_error", error=str(_e))

    def _get_mx_name(msg_type: str) -> dict[str, str]:
        # 1순위: full version 키 (camt.003.001.08)
        t = msg_type.lower()
        if t in _desc_cache:
            return _desc_cache[t]
        # 2순위: prefix 키 (camt.003) — 구버전 캐시 호환
        prefix = ".".join(t.split(".")[:2])
        return _desc_cache.get(prefix, {})

    try:
        from app.rag.indexer import get_client, COLLECTION, collection_exists
        client = get_client()
        if not collection_exists(client, COLLECTION):
            return {"mt": [], "mx": [], "total": 0}

        mt_types: set[str] = set()
        mx_types: set[str] = set()
        offset = None

        while True:
            result, next_offset = client.scroll(
                collection_name=COLLECTION,
                limit=1000,
                offset=offset,
                with_payload=["msg_type", "message_type"],
                with_vectors=False,
            )
            for point in result:
                p = point.payload or {}
                raw = (p.get("msg_type") or p.get("message_type") or "").strip()
                if not raw:
                    continue
                if raw.upper().startswith("MT"):
                    mt_types.add(raw.upper())
                elif "." in raw:
                    mx_types.add(raw.lower())
            if next_offset is None:
                break
            offset = next_offset

        # MX: 캐시 있으면 {type, name_en, name_ko} 객체, 없으면 문자열
        has_cache = bool(_desc_cache)
        if has_cache:
            mx_list: Any = [
                {"type": t, **_get_mx_name(t)} for t in sorted(mx_types)
            ]
        else:
            mx_list = sorted(mx_types)

        # 도메인 레이블 (_domains 키)
        domains = _desc_cache.get("_domains", {})

        return {
            "mt": sorted(mt_types),
            "mx": mx_list,
            "domains": domains,
            "total": len(mt_types) + len(mx_types),
        }
    except Exception as exc:
        log.warning("list_msg_types_error", exc=str(exc))
        return {"mt": [], "mx": [], "total": 0, "warning": str(exc)}


@app.get(
    "/threads",
    summary="활성 스레드 목록 조회",
    tags=["Infrastructure"],
    response_model=dict[str, Any],
)
async def list_threads(
    pending_only: Annotated[bool, Query(description="True 이면 HITL 대기 중인 스레드만 반환.")] = False,
) -> dict[str, Any]:
    """
    현재 MemorySaver 에 저장된 스레드 목록을 반환합니다.

    > **참고:** MemorySaver 는 프로세스 메모리에만 저장되므로 재시작 시 초기화됩니다.
    > 프로덕션 환경에서는 SqliteSaver 또는 PostgresSaver 를 사용하세요.
    """
    try:
        # MemorySaver 내부 storage 에 직접 접근 (LangGraph 내부 API 사용)
        checkpointer = compiled_graph.checkpointer
        raw_threads: list[dict[str, Any]] = []

        # MemorySaver.storage 는 dict[thread_id, dict] 구조
        storage = getattr(checkpointer, "storage", {})
        for tid in storage:
            config   = _thread_config(tid)
            snapshot = compiled_graph.get_state(config)
            if snapshot and snapshot.values:
                is_pending = bool(snapshot.next)
                if pending_only and not is_pending:
                    continue
                raw_threads.append({
                    "thread_id":    tid,
                    "status":       "pending_hitl" if is_pending else "completed",
                    "msg_type":     snapshot.values.get("msg_type"),
                    "routed_agent": snapshot.values.get("routed_agent"),
                    "verdict":      (snapshot.values.get("validation_result") or {}).get("verdict"),
                })

        return {"total": len(raw_threads), "threads": raw_threads}

    except Exception as exc:
        log.warning("list_threads_error", exc=str(exc))
        return {"total": 0, "threads": [], "warning": "스레드 목록 조회 중 오류 발생."}


# ---------------------------------------------------------------------------
# 하위 호환 엔드포인트 (deprecated)
# ---------------------------------------------------------------------------

@app.post(
    "/process",
    response_model=ConvertResponse,
    include_in_schema=False,  # Swagger UI 에서 숨김
    deprecated=True,
)
async def process_message_compat(req: ConvertRequest) -> ConvertResponse:
    """[deprecated] POST /convert 를 사용하세요."""
    return await convert(req)


@app.post(
    "/resume/{thread_id}",
    response_model=ConvertResponse,
    include_in_schema=False,
    deprecated=True,
)
async def resume_thread_compat(
    thread_id: Annotated[str, Path()],
    action: Annotated[HitlAction, Body()],
    comment: Annotated[Optional[str], Body()] = None,
) -> ConvertResponse:
    """[deprecated] POST /resume (body 에 thread_id 포함) 를 사용하세요."""
    return await resume(ResumeRequest(thread_id=thread_id, action=action, comment=comment))


@app.get(
    "/status/{thread_id}",
    response_model=ConvertResponse,
    include_in_schema=False,
    deprecated=True,
)
async def get_status_compat(thread_id: Annotated[str, Path()]) -> ConvertResponse:
    """[deprecated] GET /convert/{thread_id} 를 사용하세요."""
    return await get_convert_status(thread_id)


# ---------------------------------------------------------------------------
# 캐시 관리
# ---------------------------------------------------------------------------

@app.post("/admin/cache/clear", include_in_schema=False)
async def clear_cache() -> JSONResponse:
    """스키마 캐시 전체 삭제."""
    cache_dir = FilePath(os.getenv("SCHEMA_CACHE_DIR", "./schema_cache"))
    deleted = 0
    if cache_dir.exists():
        for f in cache_dir.glob("*.json"):
            try:
                f.unlink()
                deleted += 1
            except Exception as _e:
                log.warning("cache_file_delete_failed", file=str(f), error=str(_e))
    log.info("cache_cleared", deleted=deleted)
    return JSONResponse({"ok": True, "deleted": deleted})


# ---------------------------------------------------------------------------
# 전역 예외 핸들러
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Any, exc: Exception) -> JSONResponse:
    log.exception("unhandled_exception", path=str(request.url), exc=str(exc))
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": f"서버 내부 오류: {type(exc).__name__}: {exc}"},
    )


# ---------------------------------------------------------------------------
# CLI 진입점
# ---------------------------------------------------------------------------

def main() -> None:
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8080,
        reload=False,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()
