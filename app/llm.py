"""LLM 클라이언트 싱글턴 및 에이전트 공유 헬퍼."""
from __future__ import annotations

import json
import os
import threading
from typing import Any

from openai import OpenAI

from app.rag.chunker import SwiftChunk

VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://llm-svc.internal:8000/v1")
VLLM_API_KEY  = os.getenv("VLLM_API_KEY", "dummy")
VLLM_MODEL    = os.getenv("VLLM_MODEL", "meta-llama/Meta-Llama-3.1-70B-Instruct")

_client: OpenAI | None = None
_lock = threading.Lock()


def get_llm() -> OpenAI:
    """vLLM 호환 OpenAI 클라이언트 싱글턴."""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            _client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)
    return _client


def format_rule_chunks(chunks: list[SwiftChunk]) -> str:
    """SwiftChunk 리스트를 프롬프트용 규칙 텍스트로 변환한다."""
    if not chunks:
        return "관련 규칙을 찾을 수 없습니다."
    lines: list[str] = []
    for c in chunks:
        parts: list[str] = [f"p.{c.page}"]
        if c.message_type:
            parts.append(c.message_type)
        if c.field_tag:
            parts.append(f"field {c.field_tag}")
        if c.rule_id:
            parts.append(f"rule {c.rule_id}")
        lines.append(f"- ({', '.join(parts)}) {c.text}")
    return "\n".join(lines)


def parse_llm_json(text: str) -> dict[str, Any]:
    """LLM 응답에서 JSON을 추출해 파싱한다. 실패 시 ERROR verdict 반환."""
    text = text.strip()
    # ```json ... ``` 펜스 제거
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.split("\n") if not line.startswith("```")
        ).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
    return {"verdict": "ERROR", "violations": [], "warnings": [], "_parse_error": True}
