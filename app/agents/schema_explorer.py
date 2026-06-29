"""Schema Explorer Agent — ISO 20022 전문의 섹션별 스키마를 생성한다."""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict

import structlog as logging
from langchain_core.prompts import ChatPromptTemplate

from app.graph.state import AgentState
from app.llm import format_rag_context, get_chat_llm
from app.prompts.schema_explorer_prompts import SCHEMA_EXPLORER_SYSTEM, SCHEMA_EXPLORER_USER
from app.rag.retriever import SwiftRetriever

log = logging.get_logger(__name__)

_retriever: SwiftRetriever | None = None
_retriever_lock = threading.Lock()
_CACHE_DIR = Path(os.getenv("SCHEMA_CACHE_DIR", "./schema_cache"))


def _get_retriever() -> SwiftRetriever:
    global _retriever
    if _retriever is not None:
        return _retriever
    with _retriever_lock:
        if _retriever is None:
            _retriever = SwiftRetriever()
    return _retriever


# ── 캐시 ──────────────────────────────────────────────────────────────────────

def _cache_key(msg_type: str, filter_mode: str) -> str:
    key = f"{msg_type}_{filter_mode}".lower()
    return re.sub(r"[^a-zA-Z0-9._-]", "_", key)


def _load_cache(msg_type: str, filter_mode: str) -> dict | None:
    if not msg_type:
        return None
    path = _CACHE_DIR / f"{_cache_key(msg_type, filter_mode)}.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            log.info("schema_cache_hit", msg_type=msg_type, filter_mode=filter_mode)
            return data
        except Exception as e:
            log.warning("schema_cache_load_error", error=str(e))
    return None


def _save_cache(msg_type: str, filter_mode: str, payload: dict) -> None:
    if not msg_type:
        return
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _CACHE_DIR / f"{_cache_key(msg_type, filter_mode)}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("schema_cache_saved", msg_type=msg_type, filter_mode=filter_mode)
    except Exception as e:
        log.warning("schema_cache_save_error", error=str(e))


# ── 파싱 ──────────────────────────────────────────────────────────────────────

# MX: pacs.002.001.10 / MT: MT103 등 — 버전 자리수 유연 처리
_MSG_TYPE_RE = re.compile(
    r'\b(MT\d{3}|[a-z]{3,4}\.\d{3}\.\d{3}\.\d{1,3})\b',
    re.IGNORECASE,
)


def _extract_msg_type(message: str) -> str:
    """메시지 텍스트에서 전문 유형을 추출한다. 없으면 빈 문자열."""
    m = _MSG_TYPE_RE.search(message)
    return m.group(1) if m else ""


def _detect_filter_mode(message: str) -> str:
    lower = message.lower()
    if any(k in lower for k in ["전체", "모든", "all", "전부", "모두"]):
        return "all"
    return "mandatory"


def _try_recover_truncated_json(raw_json: str) -> list | None:
    """토큰 한도로 잘린 JSON 배열을 복구 시도한다.

    불완전한 마지막 객체를 제거하고 닫는 괄호를 추가하는 방식으로 복구한다.
    최소 1개 섹션이라도 살릴 수 있으면 반환한다.
    """
    # 완전한 섹션만 포함되도록 마지막 완전한 '}' 위치 이후를 잘라낸다.
    last_complete = raw_json.rfind("},")
    if last_complete == -1:
        last_complete = raw_json.rfind("}")
    if last_complete == -1:
        return None

    candidate = raw_json[: last_complete + 1].rstrip().rstrip(",") + "\n]"
    try:
        result = json.loads(candidate)
        if isinstance(result, list) and result:
            log.warning("schema_json_recovered_from_truncation",
                        original_len=len(raw_json), recovered_sections=len(result))
            return result
    except json.JSONDecodeError:
        pass
    return None


def _parse_response(raw: str) -> tuple[str, list | None, str | None]:
    """LLM 응답에서 설명과 섹션 JSON을 추출한다.

    반환: (explanation, sections, parse_error)
    """
    explanation = ""
    m = re.search(r"<text_explanation>(.*?)</text_explanation>", raw, re.DOTALL)
    if m:
        explanation = m.group(1).strip()

    sections = None
    parse_error: str | None = None

    # 닫는 태그가 없어도 여는 태그부터 끝까지 시도
    m2 = re.search(r"<schema_sections_json>(.*?)(?:</schema_sections_json>|$)", raw, re.DOTALL)
    if m2:
        raw_json = m2.group(1).strip()
        raw_json = re.sub(r"^```(?:json)?\s*", "", raw_json)
        raw_json = re.sub(r"\s*```$", "", raw_json)
        try:
            sections = json.loads(raw_json)
            if not isinstance(sections, list):
                sections = None
                parse_error = "schema_sections_json가 배열이 아님"
        except json.JSONDecodeError as e:
            log.warning("schema_sections_parse_error", error=str(e), tail=raw_json[-300:])
            # 잘린 JSON 복구 시도
            sections = _try_recover_truncated_json(raw_json)
            if sections is None:
                parse_error = f"JSON 파싱 실패 (토큰 한도 초과 가능성): {e}"
    else:
        parse_error = "schema_sections_json 태그 없음"

    return explanation, sections, parse_error




# ── LangGraph 노드 ────────────────────────────────────────────────────────────

def run_schema_explorer(state: AgentState) -> Dict[str, Any]:
    # raw_message를 사용 — masked_message는 "001.10" 같은 버전번호를 AMT로 마스킹함
    raw_message    = state.get("raw_message", "") or state.get("masked_message", "")
    # NLP 모드에서 msg_type이 비어있으면 메시지에서 직접 추출 (버전번호 보존)
    msg_type       = state.get("msg_type", "") or _extract_msg_type(raw_message)
    # state에 filter_mode가 이미 있으면 그대로 사용 (explainer 등 내부 호출 시)
    filter_mode    = state.get("filter_mode") or _detect_filter_mode(raw_message)
    log.info("schema_explorer_start", msg_type=msg_type, filter_mode=filter_mode)

    # ── 1. 캐시 확인 ─────────────────────────────────────────────────────────
    cached = _load_cache(msg_type, filter_mode)
    if cached:
        return {**state, "needs_hitl": False, "output": {**cached, "cached": True}}

    # ── 2. RAG 검색 ──────────────────────────────────────────────────────────
    try:
        retriever = _get_retriever()
        filters = {"msg_type": msg_type} if msg_type else None

        is_mt = msg_type and msg_type.upper().startswith("MT")

        if is_mt:
            # MT 전문: Sequence 구조 + 전체 필드 정의 검색
            structure_query = (
                f"{msg_type} Sequence A B mandatory optional Field Status Tag FORMAT PRESENCE"
            )
            field_query = f"{msg_type} Field FORMAT PRESENCE DEFINITION mandatory sequence"
            # MT 전문 후반부 필드(Remittance, Charges, Regulatory 등) 보완 검색
            tail_query = (
                f"{msg_type} Remittance Information Regulatory Reporting "
                f"Details of Charges Exchange Rate Charges Account"
            )
        else:
            # MX 전문: MessageElement XML Tag Mult 구조 테이블 우선 검색
            structure_query = (
                f"{msg_type} MessageElement XML Tag Mult mandatory optional "
                f"GroupHeader MessageBuildingBlocks contains following elements"
            )
            field_query = f"{msg_type} Presence Definition Datatype XML Tag"
            tail_query = None

        chunks_struct = retriever.search(
            query=structure_query,
            filters=filters,
            top_k=10,
            rerank=True,
        )

        chunks_field = retriever.search(
            query=field_query,
            filters=filters,
            top_k=6,
            rerank=False,
        )

        chunks_tail = []
        if tail_query:
            chunks_tail = retriever.search(
                query=tail_query,
                filters=filters,
                top_k=5,
                rerank=False,
            )

        # 중복 제거 후 합치기 (구조 청크 우선)
        seen_ids = {getattr(c, 'id', None) or getattr(c, 'chunk_id', id(c)) for c in chunks_struct}
        all_extra = chunks_field + chunks_tail
        deduped_extra = []
        for c in all_extra:
            cid = getattr(c, 'id', None) or getattr(c, 'chunk_id', id(c))
            if cid not in seen_ids:
                seen_ids.add(cid)
                deduped_extra.append(c)
        chunks = chunks_struct + deduped_extra

        rag_context = format_rag_context(chunks)
        log.info("schema_explorer_rag", struct_chunks=len(chunks_struct),
                 field_chunks=len(chunks_field), tail_chunks=len(chunks_tail),
                 total=len(chunks))
    except Exception as e:
        log.error("schema_explorer_rag_failed", error=str(e))
        chunks = []
        rag_context = "RAG 검색 실패."

    # ── 3. LLM 호출 — 설명 + 스키마 전체 생성 ───────────────────────────────
    # max_tokens를 높게 설정: 복잡한 MX 전문(acmt, pain 등)은 섹션이 수백 개
    explanation   = ""
    sections      = None
    schema_error  = None
    llm_request   = f"전문 유형: {msg_type}\n요청: {raw_message}" if msg_type else raw_message

    try:
        prompt = ChatPromptTemplate.from_messages([
            ("system", SCHEMA_EXPLORER_SYSTEM),
            ("human",  SCHEMA_EXPLORER_USER),
        ])
        chain = prompt | get_chat_llm(temperature=0.0)
        resp  = chain.invoke({"user_request": llm_request, "rag_context": rag_context})
        raw   = (resp.content or "").strip()
        log.info("schema_explorer_response", length=len(raw))
        explanation, llm_sections, parse_err = _parse_response(raw)
        if llm_sections:
            sections = llm_sections
        else:
            schema_error = parse_err or "schema_sections_json 파싱 실패"
            log.error("schema_sections_parse_failed", msg_type=msg_type,
                      error=schema_error, raw_tail=raw[-300:])
    except Exception as e:
        schema_error = str(e)
        log.error("schema_explorer_llm_failed", error=str(e))

    payload = {
        "type":        "schema_tree",
        "msg_type":    msg_type,
        "filter_mode": filter_mode,
        "explanation": explanation,
        "sections":    sections,
        "schema_source": "llm",
        "guidebook_basis": [
            {"page": getattr(c, "page_label", None) or getattr(c, "page", None),
             "field": getattr(c, "field_tag", None) or getattr(c, "xml_tag", None)}
            for c in chunks
        ],
    }
    if schema_error:
        payload["schema_parse_error"] = schema_error

    if sections:
        _save_cache(msg_type, filter_mode, payload)

    state_update: dict = {**state, "msg_type": msg_type, "needs_hitl": False, "output": payload}
    if schema_error and not sections:
        state_update["error"] = schema_error
    return state_update
