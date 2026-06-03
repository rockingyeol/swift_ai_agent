"""
Prowide REST 마이크로서비스 클라이언트.
원본 전문은 여기서만 처리되며 LLM에는 절대 전달하지 않는다.
plan.md §3 prowide_syntax_verify() 참조.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

PROWIDE_URL = os.getenv("PROWIDE_URL", "http://prowide-svc.internal:8080")
_TIMEOUT = 10.0


def prowide_syntax_verify(raw_message: str, msg_type: str) -> dict[str, Any]:
    """구문/네트워크 1차 검증. 결정론적이고 빠르며 PII 노출 없음."""
    endpoint = "/validate/mt" if msg_type.upper().startswith("MT") else "/validate/mx"
    try:
        resp = httpx.post(
            f"{PROWIDE_URL}{endpoint}",
            json={"content": raw_message},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "syntax_ok": data.get("parseable", False) and not data.get("problems"),
            "problems": data.get("problems", []),
            "message_type": data.get("messageType"),
            "source": "prowide",
        }
    except httpx.HTTPStatusError as e:
        return {
            "syntax_ok": False,
            "problems": [{"code": "HTTP_ERR", "desc": f"{e.response.status_code} {e.response.text[:200]}"}],
            "source": "prowide",
            "degraded": True,
        }
    except httpx.HTTPError as e:
        return {
            "syntax_ok": False,
            "problems": [{"code": "SVC_ERR", "desc": str(e)}],
            "source": "prowide",
            "degraded": True,
        }


def parse_mt(raw_message: str) -> dict[str, Any]:
    """
    MT 전문을 구조화된 필드 목록으로 파싱한다.
    Mapper Agent가 MT→MX 업리프트 전에 필드 값을 추출할 때 사용.
    원본은 Prowide(Java)에서만 처리되며 LLM에는 전달하지 않는다.
    """
    try:
        resp = httpx.post(
            f"{PROWIDE_URL}/parse/mt",
            json={"content": raw_message},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        return {
            "parseable": False,
            "error": f"{e.response.status_code} {e.response.text[:200]}",
            "degraded": True,
        }
    except httpx.HTTPError as e:
        return {"parseable": False, "error": str(e), "degraded": True}


def prowide_translate(raw_message: str, direction: str = "mt_to_mx") -> dict[str, Any]:
    """MT → MX 또는 MX → MT 변환. Mapper Agent 전용.
    Prowide ISO 20022 SRU 라이브러리 도입 전까지 서버에서 not-implemented 응답을 반환한다."""
    try:
        resp = httpx.post(
            f"{PROWIDE_URL}/translate",
            json={"content": raw_message, "direction": direction},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        return {
            "ok": False,
            "error": f"{e.response.status_code} {e.response.text[:200]}",
            "degraded": True,
        }
    except httpx.HTTPError as e:
        return {"ok": False, "error": str(e), "degraded": True}


def health_check() -> bool:
    """Spring Boot Actuator /actuator/health 확인. False → prowide-svc 미기동."""
    try:
        resp = httpx.get(f"{PROWIDE_URL}/actuator/health", timeout=3.0)
        return resp.status_code == 200 and resp.json().get("status") == "UP"
    except httpx.HTTPError:
        return False
