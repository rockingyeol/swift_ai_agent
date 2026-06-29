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
from app.prompts.generator_prompts import (
    MT_GENERATOR_SYSTEM, MT_GENERATOR_USER,
    MX_GENERATOR_SYSTEM, MX_GENERATOR_USER,
)
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

def _build_xsd_skeleton(msg_type: str, max_depth: int = 3) -> str:
    """
    XSD 파일에서 실제 XML 태그 구조를 추출해 스켈레톤 텍스트로 반환한다.
    LLM이 잘못된 풀네임 태그 대신 XSD에 정의된 축약형 태그를 사용하도록 강제한다.
    XSD가 없으면 빈 문자열 반환.
    """
    try:
        from app.rag.xsd_parser import parse_xsd
        sections = parse_xsd(msg_type)
        if not sections:
            return ""

        lines = ["[XSD 스키마 태그 구조 — 아래 태그명을 그대로 사용할 것]"]
        lines.append(f"<Document xmlns=\"...\">  <!-- 실제 네임스페이스는 가이드라인 문서에서 확인 -->")

        def _render(fields: list, indent: int, depth: int) -> None:
            if depth > max_depth:
                return
            for f in fields:
                tag  = f.get("xml_tag", "")
                m_o  = f.get("mandatory", "O")
                mult = f.get("multiplicity", "[1..1]")
                children = f.get("children", [])
                pad = "  " * indent
                label = f"<!-- {m_o} {mult} -->"
                if children:
                    lines.append(f"{pad}<{tag}> {label}")
                    _render(children, indent + 1, depth + 1)
                    lines.append(f"{pad}</{tag}>")
                else:
                    lines.append(f"{pad}<{tag}>...</{tag}> {label}")

        for sec in sections:
            tag  = sec.get("xml_tag", "")
            m_o  = sec.get("mandatory", "O")
            mult = sec.get("multiplicity", "[1..1]")
            fields = sec.get("fields", [])
            lines.append(f"  <{tag}> <!-- {m_o} {mult} -->")
            _render(fields, indent=2, depth=1)
            lines.append(f"  </{tag}>")

        lines.append("</Document>")
        return "\n".join(lines)
    except Exception as e:
        log.warning("xsd_skeleton_failed", msg_type=msg_type, error=str(e))
        return ""


def _is_mx(msg_type: str) -> bool:
    """MX 전문 여부 판단. pacs/camt/pain 등 ISO 20022 접두어로 확인."""
    if not msg_type:
        return False
    return bool(re.match(r"^(pacs|camt|pain|acmt|auth|reda|remt|sese|seev|semt)\.", msg_type, re.I))


def _build_generator_chain(mx: bool = False):
    """Generator LCEL 체인 반환. mx=True 면 MX CoT 프롬프트 사용."""
    system_tpl = MX_GENERATOR_SYSTEM if mx else MT_GENERATOR_SYSTEM
    user_tpl   = MX_GENERATOR_USER   if mx else MT_GENERATOR_USER
    prompt = ChatPromptTemplate.from_messages([
        ("system", system_tpl),
        ("human",  user_tpl),
    ])
    llm = get_chat_llm(temperature=0.0)
    return prompt | llm


def _build_scenario(user_request: str, msg_type: str) -> str:
    """
    사용자 요청 문자열에서 MX 거래 시나리오 항목을 추출해 구조화 텍스트로 반환.
    명시된 항목만 포함하고 임의 값을 생성하지 않는다.
    """
    lines: list[str] = [f"전문 유형: {msg_type}"]
    patterns = [
        (r"채무자[:\s]+([^\n,;]+)", "채무자(Debtor)"),
        (r"출금인[:\s]+([^\n,;]+)", "채무자(Debtor)"),
        (r"Debtor[:\s]+([^\n,;]+)", "채무자(Debtor)"),
        (r"채권자[:\s]+([^\n,;]+)", "채권자(Creditor)"),
        (r"수취인[:\s]+([^\n,;]+)", "채권자(Creditor)"),
        (r"Creditor[:\s]+([^\n,;]+)", "채권자(Creditor)"),
        (r"금액[:\s]+([^\n,;]+)", "금액(Amount)"),
        (r"Amount[:\s]+([^\n,;]+)", "금액(Amount)"),
        (r"통화[:\s]+([A-Z]{3})", "통화(Currency)"),
        (r"Currency[:\s]+([A-Z]{3})", "통화(Currency)"),
        (r"목적[:\s]+([^\n,;]+)", "목적(Purpose)"),
        (r"Purpose[:\s]+([^\n,;]+)", "목적(Purpose)"),
        (r"날짜[:\s]+([^\n,;]+)", "날짜(Date)"),
        (r"Date[:\s]+([^\n,;]+)", "날짜(Date)"),
        (r"송금인 은행[:\s]+([^\n,;]+)", "송금인 은행(Debtor Agent)"),
        (r"수취인 은행[:\s]+([^\n,;]+)", "수취인 은행(Creditor Agent)"),
    ]
    for pattern, label in patterns:
        m = re.search(pattern, user_request, re.IGNORECASE)
        if m:
            lines.append(f"{label}: {m.group(1).strip()}")

    # 별도 키워드가 없으면 사용자 요청 전체를 시나리오로 사용
    if len(lines) == 1:
        lines.append(user_request.strip())

    return "\n".join(lines)


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
        mx = _is_mx(msg_type)
        chain = _build_generator_chain(mx=mx)

        import json as _json
        mapper_out = state.get("output") or {}
        mapping_spec = ""
        if mapper_out.get("mappings"):
            mapping_spec = _json.dumps(mapper_out["mappings"], ensure_ascii=False, indent=2)

        req_fields_match = _re.search(
            r"\[필드 목록[^\]]*\](.*?)(?:\Z)", masked_message, _re.DOTALL
        )
        required_fields = req_fields_match.group(1).strip() if req_fields_match else "없음"

        if mx:
            scenario = _build_scenario(masked_message, msg_type)
            xsd_skeleton = _build_xsd_skeleton(msg_type)
            if xsd_skeleton:
                rag_context = xsd_skeleton + "\n\n" + rag_context
            resp = chain.invoke({
                "msg_type":        msg_type,
                "scenario":        scenario,
                "required_fields": required_fields,
                "rag_context":     rag_context,
                "mapping_spec":    mapping_spec or "없음",
            })
        else:
            resp = chain.invoke({
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
