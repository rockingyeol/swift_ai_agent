"""
Generator Agent — 프로덕션 등급 리팩토링.

주요 개선 사항:
  1. LLM 직접 XML 생성 (강화된 프롬프트 + 환각 방지)
  2. xml.etree.ElementTree XML 문법 검증
  3. Jinja2 pacs_008 템플릿 폴백 (XML 파손 시)
  4. Mapper 매핑 명세 → 프롬프트 주입
  5. 전 단계 예외 처리

## XML 구조 검증 아키텍처 결정 사항
Generator는 XML 문법(well-formed) 검사만 수행하고 XSD 스키마 검증은 하지 않는다.
XSD 구조 검증은 Prowide(오픈소스 Java 라이브러리)가 전담한다 — 이유:
  - Prowide가 ISO 20022 XSD를 내장하여 결정론적으로 구조를 검증한다.
  - 생성된 전문을 재검증하려면 /convert → analyzer 흐름을 사용한다.
  - Python에서 XSD 검증을 별도 구현하면 Prowide와 결과 불일치 위험이 생긴다.
"""
from __future__ import annotations

import re
import threading
import xml.etree.ElementTree as ET
from pathlib import Path as FilePath
from typing import Any, Dict, Optional

import structlog as logging

from langchain_core.prompts import ChatPromptTemplate

from app.graph.state import AgentState
from app.llm import format_rag_context, get_chat_llm
from app.prompts.generator_prompts import GENERATOR_SYSTEM, GENERATOR_USER
from app.rag.retriever import SwiftRetriever

log = logging.get_logger(__name__)

_retriever: SwiftRetriever | None = None
_retriever_lock = threading.Lock()
_TEMPLATE_DIR = FilePath(__file__).parent.parent / "templates"


def _get_retriever() -> SwiftRetriever:
    global _retriever
    if _retriever is not None:
        return _retriever
    with _retriever_lock:
        if _retriever is None:
            _retriever = SwiftRetriever()
    return _retriever


# ===========================================================================
# XML 검증
# ===========================================================================

def _validate_xml(text: str) -> tuple[bool, str]:
    """XML 문법 유효성 검사. (is_valid, error_message) 반환."""
    # XML 선언문이 없으면 추가하여 파싱 시도
    candidate = text.strip()
    if not candidate.startswith("<?xml"):
        candidate = '<?xml version="1.0" encoding="UTF-8"?>\n' + candidate
    try:
        ET.fromstring(candidate)
        return True, ""
    except ET.ParseError as e:
        return False, str(e)


def _extract_xml_block(text: str) -> str:
    """LLM 출력에서 XML 블록만 추출한다."""
    # ```xml ... ``` 펜스 제거
    m = re.search(r"```(?:xml)?\s*([\s\S]+?)\s*```", text)
    if m:
        return m.group(1).strip()
    # <?xml ... 시작점 직접 추출
    idx = text.find("<?xml")
    if idx >= 0:
        return text[idx:].strip()
    # <Document 시작점
    idx = text.find("<Document")
    if idx >= 0:
        return text[idx:].strip()
    return text.strip()


# ===========================================================================
# Jinja2 폴백 — pacs.008 템플릿
# ===========================================================================

def _render_pacs008_template(mapper_output: dict[str, Any] | None,
                              masked_message: str) -> str:
    """Mapper 매핑 명세로 Jinja2 pacs.008 템플릿을 렌더링한다."""
    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
    except ImportError as e:
        log.error("jinja2_not_installed", error=str(e))
        return ""

    template_path = _TEMPLATE_DIR / "pacs_008.xml.j2"
    if not template_path.exists():
        log.error("pacs008_template_not_found", path=str(template_path))
        return ""

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["xml"]),
    )

    # Mapper 매핑에서 키 값 추출
    ctx = _extract_template_context(mapper_output, masked_message)

    try:
        template = env.get_template("pacs_008.xml.j2")
        return template.render(**ctx)
    except Exception as e:
        log.error("jinja2_render_failed", error=str(e))
        return ""


def _extract_template_context(mapper_output: dict[str, Any] | None,
                               masked_message: str) -> dict[str, Any]:
    """Mapper 매핑 명세에서 Jinja2 컨텍스트 변수를 추출한다."""
    import uuid
    from datetime import datetime, timezone

    # 기본값
    ctx: dict[str, Any] = {
        "msg_id":    f"MSGID-{uuid.uuid4().hex[:8].upper()}",
        "created_dt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "instg_agt": None,
        "instd_agt": None,
        "transactions": [_default_transaction(masked_message)],
    }

    if not mapper_output:
        return ctx

    mappings = mapper_output.get("mappings", [])
    tag_map: dict[str, str] = {}
    for m in mappings:
        tag = m.get("mt_tag", "").strip(":")
        val = m.get("mx_value") or m.get("mt_value") or ""
        if tag and val:
            tag_map[tag] = val

    # 기본 헤더 추출
    tx: dict[str, Any] = {
        "end_to_end_id": tag_map.get("20", "NOTPROVIDED"),
        "tx_id":         tag_map.get("20", "NOTPROVIDED"),
        "currency":      None,
        "amount":        None,
        "sttlm_dt":      None,
        "dbtr_name":     tag_map.get("50K") or tag_map.get("50H") or "<<NAME_1>>",
        "dbtr_iban":     None,
        "cdtr_name":     tag_map.get("59") or "<<NAME_2>>",
        "cdtr_iban":     None,
        "dbtr_agt":      tag_map.get("52A"),
        "cdtr_agt":      tag_map.get("57A"),
        "charge_bearer": tag_map.get("71A", "SHAR"),
        "remittance_info": tag_map.get("70"),
    }

    # 32A: YYMMDD + CCY + AMT  예: 240115EUR10000,00
    raw_32a = tag_map.get("32A", "")
    if len(raw_32a) >= 9:
        yy, mm, dd = raw_32a[0:2], raw_32a[2:4], raw_32a[4:6]
        tx["sttlm_dt"] = f"20{yy}-{mm}-{dd}"
        tx["currency"] = raw_32a[6:9]
        tx["amount"]   = raw_32a[9:].replace(",", ".")
    # 32B: CCY + AMT  예: EUR10000,00
    raw_32b = tag_map.get("32B", "")
    if not tx.get("currency") and len(raw_32b) >= 4:
        tx["currency"] = raw_32b[0:3]
        tx["amount"]   = raw_32b[3:].replace(",", ".")

    ctx["transactions"] = [tx]
    return ctx


def _default_transaction(masked_message: str) -> dict[str, Any]:
    return {
        "end_to_end_id": "NOTPROVIDED",
        "tx_id":         "NOTPROVIDED",
        "currency":      "XXX",
        "amount":        "0",
        "sttlm_dt":      None,
        "dbtr_name":     "<<NAME_1>>",
        "dbtr_iban":     None,
        "cdtr_name":     "<<NAME_2>>",
        "cdtr_iban":     None,
        "dbtr_agt":      None,
        "cdtr_agt":      None,
        "charge_bearer": "SHAR",
        "remittance_info": None,
    }


# ===========================================================================
# LLM 체인 팩토리 — 테스트에서 패치 가능한 단일 지점
# ===========================================================================

def _build_generator_chain():
    """Generator LCEL 체인 반환 (prompt | llm). 테스트에서 이 함수를 패치한다."""
    prompt = ChatPromptTemplate.from_messages([
        ("system", GENERATOR_SYSTEM),
        ("human", GENERATOR_USER),
    ])
    llm = get_chat_llm(temperature=0.0)  # 재현성 보장 — 생성 결과가 매번 달라지지 않도록
    return prompt | llm


# ===========================================================================
# LangGraph 노드 진입점
# ===========================================================================

def run_generator(state: AgentState) -> Dict[str, Any]:
    """
    Generator Agent LangGraph 노드.

    State 입력:  masked_message, msg_type, output(mapper_output 포함 가능)
    State 출력:  validation_result, needs_hitl, output
    """
    masked_message = state.get("masked_message", "")
    msg_type       = state.get("msg_type", "")

    # ── 1. RAG — 생성 대상 전문 구조 및 필수 규칙 검색 ────────────────────
    # 사용자 요청에서 태그 목록 추출 → 해당 필드의 XML 경로 정보도 함께 검색
    import re as _re
    requested_tags = _re.findall(r"<([A-Za-z][A-Za-z0-9]*)>", masked_message)
    tag_query = " ".join(requested_tags[:10]) if requested_tags else ""

    try:
        retriever   = _get_retriever()
        query       = f"{msg_type} {tag_query} XML path structure mandatory fields {masked_message[:200]}"
        rule_chunks = retriever.search(
            query=query,
            filters={"msg_type": msg_type} if msg_type else None,
            top_k=10,
            rerank=True,
        )
        rag_context = format_rag_context(rule_chunks)
    except Exception as e:
        log.error("rag_search_failed", error=str(e))
        rule_chunks = []
        rag_context = "RAG 검색 실패 — 가이드라인 없이 생성을 진행합니다."

    # ── 2. LLM 전문 초안 생성 ────────────────────────────────────────────
    draft: str = ""
    xml_valid  = False
    xml_error  = ""

    try:
        chain = _build_generator_chain()
        # mapper_output이 있으면 mapping_spec 주입, 없으면 빈 문자열
        mapper_out = state.get("output") or {}
        mapping_spec = ""
        if mapper_out.get("mappings"):
            import json as _json
            mapping_spec = _json.dumps(mapper_out["mappings"], ensure_ascii=False, indent=2)

        # 사용자 요청에서 [필드 목록] 섹션 추출 (없으면 빈 문자열)
        req_fields_match = _re.search(
            r"\[필드 목록[^\]]*\](.*?)(?:\Z)", masked_message, _re.DOTALL
        )
        required_fields = req_fields_match.group(1).strip() if req_fields_match else "없음"

        resp  = chain.invoke({
            "user_request":    masked_message,
            "required_fields": required_fields,
            "rag_context":     rag_context,
            "mapping_spec":    mapping_spec or "없음",
        })
        raw_draft = (resp.content or "").strip()
        draft     = _extract_xml_block(raw_draft)

        # ── 3. XML 문법 검증 ─────────────────────────────────────────────
        xml_valid, xml_error = _validate_xml(draft)
        if xml_valid:
            log.info("generator_xml_valid")
        else:
            log.warning("generator_xml_invalid", error=xml_error)

    except Exception as e:
        log.error("generator_llm_failed", error=str(e))
        xml_error = str(e)

    # ── 4. XML 파손 시 Jinja2 템플릿 폴백 ────────────────────────────────
    if not xml_valid and "pacs.008" in (msg_type or "").lower():
        log.warning("generator_jinja2_fallback_triggered",
                    xml_error=xml_error, draft_preview=draft[:200] if draft else "")
        fallback = _render_pacs008_template(None, masked_message)
        if fallback:
            is_valid, _ = _validate_xml(fallback)
            if is_valid:
                draft     = fallback
                xml_valid = True
                xml_error = ""
                log.info("generator_jinja2_fallback_ok")

    return {
        **state,
        "needs_hitl": True,   # 생성 결과는 항상 인간 검수
        "validation_result": {
            "verdict":    "PENDING_REVIEW",
            "needs_hitl": True,
            "xml_valid":  xml_valid,
            "xml_error":  xml_error if not xml_valid else None,
        },
        "output": {
            "type":          "generated_message",
            "draft":         draft,
            "xml_valid":     xml_valid,
            "xml_error":     xml_error if not xml_valid else None,
            "fallback_used": not xml_valid and bool(draft),
            "guidebook_basis": [
                {
                    "page":    getattr(c, "page_label", None) or getattr(c, "page", None),
                    "rule_id": getattr(c, "rule_id", None),
                    "field":   getattr(c, "field_tag", None) or getattr(c, "xml_tag", None),
                }
                for c in rule_chunks
            ],
        },
    }
