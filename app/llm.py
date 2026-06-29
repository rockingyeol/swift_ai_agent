"""LLM 클라이언트 팩토리 및 에이전트 공유 헬퍼.

LLM_PROVIDER 환경변수로 백엔드를 전환합니다:
  - "anthropic": Claude API 직접 호출 — 빠르고 정확
  - "gemini"   : Google Gemini API — 무료 티어 제공
  - "ollama"   : 로컬 Ollama OpenAI 호환 엔드포인트
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

from openai import OpenAI

# ── Provider 선택 ────────────────────────────────────────────────────────────
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()  # "anthropic" | "gemini" | "ollama"

# ── Anthropic (Claude) 설정 ───────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL   = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")

# ── Google Gemini 설정 ────────────────────────────────────────────────────────
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# ── Ollama / vLLM 설정 ────────────────────────────────────────────────────────
VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://llm-svc.internal:8000/v1")
VLLM_API_KEY  = os.getenv("VLLM_API_KEY", "dummy")
VLLM_MODEL    = os.getenv("VLLM_MODEL", "meta-llama/Meta-Llama-3.1-70B-Instruct")

_client: OpenAI | None = None
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Raw OpenAI 클라이언트 (레거시 호환 — Ollama 전용)
# ---------------------------------------------------------------------------

def get_llm() -> OpenAI:
    """vLLM 호환 OpenAI 클라이언트 싱글턴."""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            _client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)
    return _client


# ---------------------------------------------------------------------------
# LangChain Chat LLM (with_structured_output 전용)
# ---------------------------------------------------------------------------

def get_chat_llm(temperature: float = 0.0):
    """LLM_PROVIDER 에 따라 Claude 또는 Ollama LangChain 클라이언트 반환.

    두 클라이언트 모두 with_structured_output(PydanticModel) 을 지원하므로
    에이전트 코드 변경 없이 provider 전환이 가능합니다.
    """
    if LLM_PROVIDER == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            api_key=ANTHROPIC_API_KEY,
            model=ANTHROPIC_MODEL,
            temperature=temperature,
            max_tokens=8192,
        )

    if LLM_PROVIDER == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            google_api_key=GOOGLE_API_KEY,
            model=GEMINI_MODEL,
            temperature=temperature,
        )

    # 기본값: Ollama / vLLM (OpenAI 호환)
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        base_url=VLLM_BASE_URL,
        api_key=VLLM_API_KEY,
        model=VLLM_MODEL,
        temperature=temperature,
        model_kwargs={"seed": 42},
    )


# ---------------------------------------------------------------------------
# RAG 컨텍스트 포맷터
# ---------------------------------------------------------------------------

def format_rag_context(chunks: list[Any]) -> str:
    """청크를 [Category | Msg | Field | p.N] 헤더 단위로 구조화하여 반환.

    LLM이 출처를 명확히 인식하고 페이지/규칙 번호를 그대로 인용하도록
    각 청크 앞에 메타데이터 헤더를 붙인다.
    """
    if not chunks:
        return "관련 가이드라인 문서를 찾을 수 없습니다."

    sections: list[str] = []
    for c in chunks:
        page         = getattr(c, "page", None) or getattr(c, "page_label", None)
        msg_type     = getattr(c, "message_type", None) or getattr(c, "msg_type", None)
        category     = getattr(c, "category", "")
        section      = getattr(c, "section_title", "") or getattr(c, "xml_tag", "")
        field_tag    = getattr(c, "field_tag", None) or getattr(c, "xml_tag", None)
        rule_id      = getattr(c, "rule_id", None)
        field_path   = getattr(c, "field_path", "")
        text         = getattr(c, "text", "") or ""
        text         = text.strip() if isinstance(text, str) else ""

        meta: list[str] = []
        if category:
            meta.append(f"Category: {category}")
        if msg_type:
            meta.append(f"Msg: {msg_type}")
        if field_tag and field_tag not in ("SYSTEM", ""):
            meta.append(f"Field: :{field_tag}:")
        elif field_path:
            meta.append(f"Path: {field_path}")
        if rule_id:
            meta.append(f"Rule: {rule_id}")
        if section:
            meta.append(f'Section: "{section}"')
        if page is not None:
            meta.append(f"p.{page}")

        header = " | ".join(meta) if meta else "Unknown Source"
        sections.append(f"[{header}]\n{text}")

    return "\n\n".join(sections)


def format_rule_chunks(chunks: list[Any]) -> str:
    """레거시 인라인 포맷 — 기존 테스트 / 호환 코드에서 사용."""
    if not chunks:
        return "관련 규칙을 찾을 수 없습니다."
    lines: list[str] = []
    for c in chunks:
        page         = getattr(c, "page", None) or getattr(c, "page_label", None)
        message_type = getattr(c, "message_type", None) or getattr(c, "msg_type", None)
        field_tag    = getattr(c, "field_tag", None) or getattr(c, "xml_tag", None)
        rule_id      = getattr(c, "rule_id", None)
        section      = getattr(c, "section_title", None)
        text         = getattr(c, "text", "") or ""
        text         = text if isinstance(text, str) else ""

        parts: list[str] = [f"p.{page}" if page is not None else "p.?"]
        if message_type:
            parts.append(message_type)
        if field_tag and field_tag != "SYSTEM":
            parts.append(f"field {field_tag}")
        if rule_id:
            parts.append(f"rule {rule_id}")
        if section:
            parts.append(f'"{section}"')

        lines.append(f"- ({', '.join(parts)}) {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON 파서
# ---------------------------------------------------------------------------

def parse_llm_json(text: str) -> dict[str, Any]:
    """LLM 응답에서 JSON을 추출해 파싱한다. 실패 시 ERROR verdict 반환."""
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.split("\n") if not line.startswith("```")
        ).strip()
    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            return {"verdict": "ERROR", "violations": [], "warnings": [], "_parse_error": True}
        return parsed
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start:end])
                if not isinstance(parsed, dict):
                    return {"verdict": "ERROR", "violations": [], "warnings": [], "_parse_error": True}
                return parsed
            except json.JSONDecodeError as e:
                import structlog as _slog
                _slog.get_logger(__name__).warning(
                    "parse_llm_json_fallback_failed", error=str(e), snippet=text[start:start+80]
                )
    return {"verdict": "ERROR", "violations": [], "warnings": [], "_parse_error": True}
