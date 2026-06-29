"""
Supervisor / Router 노드.
사용자 의도를 분류하여 Analyzer · Generator · Mapper 중 하나로 라우팅한다.
"""
from __future__ import annotations

import json
import re
from typing import Literal

import structlog

from app.graph.state import AgentState
from app.llm import VLLM_MODEL, get_llm

log = structlog.get_logger(__name__)

_INTENT_SYSTEM = """\
SWIFT 메시지 처리 시스템의 라우터입니다.
사용자 입력을 읽고 아래 의도 중 하나로 분류하십시오.
- "analyze": 기존 MT/MX 전문의 유효성 검증·오류 분석
- "generate": 자연어 요청으로부터 새 MT/MX 전문 생성
- "map": MT↔MX 형식 변환 (업리프트/다운리프트)
- "schema": 전문의 전체 필드 트리 구조·스키마 탐색
JSON 형식으로만 응답하십시오: {"intent": "analyze"|"generate"|"map"|"schema"}\
"""

_INTENT_KEYWORDS: dict[str, list[str]] = {
    "analyze":  ["검증", "분석", "오류", "위반", "validate", "check", "verify", "analyze"],
    "generate": ["생성", "작성", "만들어", "초안", "generate", "create", "draft", "write"],
    "map":      ["변환", "매핑", "uplift", "translate", "convert", "map", "MT→MX", "MX→MT"],
    "schema":   [
        "전체 필드", "스키마", "트리", "schema", "tree", "전체 구조",
        "모든 필드", "필드 구조", "xml 구조", "xml 트리",
    ],
    "explain":  [
        "설명", "알려줘", "알려주세요", "뭐야", "뭔가요", "무엇", "어떤", "소개",
        "정보", "기본", "개요", "이름", "용도", "언제", "어디", "사용",
        "어떻게", "어떻게 처리", "처리 방법", "처리해", "처리하나", "처리하는",
        "방법", "방식", "어떻게 써", "어떻게 사용",
        "explain", "what is", "describe", "overview", "tell me", "info", "how to", "how does",
        # 섹션 조회 키워드 → explainer의 general_qa 모드로 처리
        "usage rules", "작성 규칙", "사용 규칙", "사용규칙", "작성규칙",
        "rules", "룰", "규칙",
        "network validated", "네트워크 검증", "guidelines", "가이드라인",
        "market practice", "마켓 프랙티스",
        "scope", "스코프",
        # 매핑 규칙 질문 키워드 → explainer의 mapping_rule 모드로 처리
        "어느 엘리먼트", "어느 필드", "어떤 엘리먼트", "어떤 필드",
        "분기", "코드워드", "codeword", "code word",
        "매핑해야", "매핑 규칙", "mapping rule", "maps to", "mapped to",
        "어느 경로", "xpath", "xml 경로",
    ],
}

_INTENT_TO_AGENT: dict[str, Literal["analyzer", "generator", "mapper", "explainer", "schema_explorer"]] = {
    "analyze":  "analyzer",
    "generate": "generator",
    "map":      "mapper",
    "explain":  "explainer",
    "schema":   "schema_explorer",
}


# 명시적 매핑/변환 분석 요청만 mapper로 라우팅
# "만들어줘" 계열은 실제 전문 결과물이 필요하므로 generator로 처리
_MAP_EXPLICIT = [
    "매핑 규칙", "필드 매핑", "mapping rule", "field mapping",
    "uplift 분석", "downlift 분석", "변환 분석", "변환 규칙",
    "mt to mx 매핑", "mx to mt 매핑",
]


_KOREAN_MT_RE = re.compile(r"엠티\s*(\d{3})", re.IGNORECASE)


def _normalize(text: str) -> str:
    """한국어 표기 등 비표준 패턴을 정규화한다."""
    # "엠티103" → "MT103"
    text = _KOREAN_MT_RE.sub(lambda m: f"MT{m.group(1)}", text)
    return text


def _keyword_classify(text: str) -> str | None:
    """빠른 키워드 매칭으로 의도 추론.

    동점 처리: map(명시적 변환) > explain > analyze > generate > map 우선순위 적용.
    """
    text = _normalize(text)
    lower = text.lower()

    # MT↔MX 명시적 변환 요청이면 map 최우선
    if any(kw.lower() in lower for kw in _MAP_EXPLICIT):
        return "map"

    scores = {k: sum(1 for kw in kws if kw.lower() in lower)
              for k, kws in _INTENT_KEYWORDS.items()}
    best, best_score = max(scores.items(), key=lambda x: x[1])
    if best_score == 0:
        return None

    # schema 점수가 있으면 schema_explorer 우선 (전문 블록 없을 때)
    has_swift_block = any(tok in text for tok in ("{1:", "{2:", "{4:", "<Document"))
    if not has_swift_block and scores.get("schema", 0) > 0:
        return "schema"

    # 전문 헤더가 없고 explain 점수가 있으면 explain 우선
    if not has_swift_block and scores.get("explain", 0) > 0:
        return "explain"

    return best


def classify_intent(state: AgentState) -> AgentState:
    """LLM 또는 키워드 기반으로 의도를 분류하고 routed_agent를 설정한다."""
    # 호출측이 user_intent를 이미 설정한 경우 그대로 사용
    if state.get("user_intent"):
        intent: str = state["user_intent"]
    else:
        text = state.get("masked_message") or state.get("raw_message") or ""

        intent = _keyword_classify(text) or ""

        if not intent:
            # 키워드 미일치 시 LLM fallback
            client = get_llm()
            resp = client.chat.completions.create(
                model=VLLM_MODEL,
                messages=[
                    {"role": "system", "content": _INTENT_SYSTEM},
                    {"role": "user",   "content": text[:1000]},
                ],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or ""
            try:
                intent = json.loads(raw).get("intent", "analyze")
            except json.JSONDecodeError:
                intent = "analyze"

    if intent not in _INTENT_TO_AGENT:
        log.warning("unknown_intent_fallback", intent=intent, fallback="analyzer")
    routed: Literal["analyzer", "generator", "mapper", "explainer", "schema_explorer"] = (
        _INTENT_TO_AGENT.get(intent, "analyzer")
    )
    return {**state, "user_intent": intent, "routed_agent": routed}


def route(state: AgentState) -> Literal["analyzer", "generator", "mapper", "explainer", "schema_explorer"]:
    """LangGraph conditional_edge 용 라우팅 함수."""
    agent = state.get("routed_agent", "analyzer")
    valid = {"analyzer", "generator", "mapper", "explainer", "schema_explorer"}
    if agent not in valid:
        log.warning("invalid_routed_agent_fallback", routed_agent=agent, fallback="analyzer")
        return "analyzer"
    return agent
