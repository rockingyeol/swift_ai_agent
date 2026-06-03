"""
Supervisor / Router 노드.
사용자 의도를 분류하여 Analyzer · Generator · Mapper 중 하나로 라우팅한다.
"""
from __future__ import annotations

import json
from typing import Literal

from app.graph.state import AgentState
from app.llm import VLLM_MODEL, get_llm

_INTENT_SYSTEM = """\
SWIFT 메시지 처리 시스템의 라우터입니다.
사용자 입력을 읽고 아래 세 가지 의도 중 하나로 분류하십시오.
- "analyze": 기존 MT/MX 전문의 유효성 검증·오류 분석
- "generate": 자연어 요청으로부터 새 MT/MX 전문 생성
- "map": MT↔MX 형식 변환 (업리프트/다운리프트)
JSON 형식으로만 응답하십시오: {"intent": "analyze"|"generate"|"map"}\
"""

_INTENT_KEYWORDS: dict[str, list[str]] = {
    "analyze":  ["검증", "분석", "오류", "위반", "validate", "check", "verify", "analyze"],
    "generate": ["생성", "작성", "만들어", "초안", "generate", "create", "draft", "write"],
    "map":      ["변환", "매핑", "uplift", "translate", "convert", "map", "MT→MX", "MX→MT"],
}

_INTENT_TO_AGENT: dict[str, Literal["analyzer", "generator", "mapper"]] = {
    "analyze":  "analyzer",
    "generate": "generator",
    "map":      "mapper",
}


def _keyword_classify(text: str) -> str | None:
    """빠른 키워드 매칭으로 의도 추론. 점수가 동점이거나 0이면 None 반환."""
    lower = text.lower()
    scores = {k: sum(1 for kw in kws if kw.lower() in lower)
              for k, kws in _INTENT_KEYWORDS.items()}
    best, best_score = max(scores.items(), key=lambda x: x[1])
    return best if best_score > 0 else None


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

    routed: Literal["analyzer", "generator", "mapper"] = (
        _INTENT_TO_AGENT.get(intent, "analyzer")
    )
    return {**state, "routed_agent": routed}


def route(state: AgentState) -> Literal["analyzer", "generator", "mapper"]:
    """LangGraph conditional_edge 용 라우팅 함수."""
    return state["routed_agent"]
