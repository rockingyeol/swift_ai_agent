"""
Prowide REST 마이크로서비스 클라이언트.
원본 전문은 여기서만 처리되며 LLM에는 절대 전달하지 않는다.

## 검증 아키텍처 결정 사항
XML 구조·XSD 검증은 Prowide(오픈소스 Java 라이브러리)가 전담한다.
별도 Python XSD 검증 레이어를 두지 않는다 — 이유:
  1. Prowide는 ISO 20022 XSD를 내장하고 있어 구조 검증이 결정론적이다.
  2. generator가 생성한 XML은 /convert → analyzer 흐름으로 재검증 가능하다.
  3. 중복 검증은 유지 비용만 높이고 Prowide와 결과가 달라질 경우 혼선을 초래한다.
역할 분리:
  - Prowide : XML 구문 파싱 + XSD 구조 + MT 필드 포맷/필수 여부 (결정론적)
  - LLM + RAG : 의미·조건부 규칙·CBPR+ 권장 사항 (확률적, 참고용)
  - reconciler : 두 결과 병합 → 최종 판정 + HITL 트리거
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx

PROWIDE_URL = os.getenv("PROWIDE_URL", "http://prowide-svc.internal:8080")
_TIMEOUT    = float(os.getenv("PROWIDE_TIMEOUT", "10.0"))
_MAX_RETRIES = 3
_RETRY_BACKOFF = 0.5  # 초; 1회: 0.5s, 2회: 1.0s


def _post_with_retry(url: str, json: dict) -> httpx.Response:
    """POST 요청을 최대 _MAX_RETRIES 회 재시도한다 (지수 백오프)."""
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = httpx.post(url, json=json, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError:
            raise  # 4xx/5xx는 재시도 불필요
        except httpx.HTTPError as e:
            last_exc = e
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_BACKOFF * (2 ** attempt))
    raise last_exc  # type: ignore[misc]


def _ensure_mt_headers(raw: str, msg_type: str) -> str:
    """Block 1/2 헤더가 없는 MT 메시지에 기본 헤더를 추가한다.
    Prowide는 완전한 SWIFT 메시지 형식({1:}{2:}{4:...-})을 요구한다."""
    stripped = raw.strip()
    if stripped.startswith("{1:"):
        return raw  # 이미 헤더 있음
    mt_num = msg_type.upper().replace("MT", "") if msg_type else "103"
    header = f"{{1:F01BANKXXXXXX0000000000}}{{2:I{mt_num}BANKXXXXXX0}}\n"
    # {4: 로 시작하지 않으면 {4:\n 도 추가
    if not stripped.startswith("{4:"):
        stripped = "{4:\n" + stripped + "\n-}"
    return header + stripped


def prowide_syntax_verify(raw_message: str, msg_type: str) -> dict[str, Any]:
    """구문/네트워크 1차 검증. 결정론적이고 빠르며 PII 노출 없음."""
    is_mt   = not msg_type or msg_type.upper().startswith("MT")
    endpoint = "/validate/mt" if is_mt else "/validate/mx"
    # MT 전문: 블록 헤더 자동 보완
    message_to_send = _ensure_mt_headers(raw_message, msg_type) if is_mt else raw_message
    try:
        resp = _post_with_retry(
            f"{PROWIDE_URL}{endpoint}",
            json={"content": message_to_send},
        )
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


def get_mx_schema(msg_type: str) -> dict[str, Any]:
    """ISO 20022 MX 전문의 XSD 기반 필드 스키마를 Prowide에서 조회한다."""
    normalized = msg_type.replace("_", ".").lower()
    try:
        resp = httpx.get(
            f"{PROWIDE_URL}/schema/mx/{normalized}",
            timeout=_TIMEOUT,
        )
        if resp.status_code == 404:
            return {"error": f"Schema not found for {normalized}", "sections": []}
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        return {"error": str(e), "sections": []}


def prowide_translate(raw_message: str, direction: str = "mt_to_mx") -> dict[str, Any]:
    """MT → MX 또는 MX → MT 변환. Mapper Agent 전용.
    Prowide ISO 20022 SRU 라이브러리 도입 전까지 서버에서 not-implemented 응답을 반환한다."""
    try:
        resp = _post_with_retry(
            f"{PROWIDE_URL}/translate",
            json={"content": raw_message, "direction": direction},
        )
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
