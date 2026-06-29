"""
Rule Engine(Prowide) + LLM 결과 병합 및 HITL 판정.

책임 분리 원칙:
  Prowide (결정론적)  — 구문 오류, 필드 존재·포맷 위반 → 최종 권위
  LLM (확률적)       — 의미/조건부 규칙, CBPR+ 권장 사항 → 참고
  degraded           — 룰엔진 장애 시 무인 통과 금지 → fail-safe HITL

LLM 결과 신뢰 범위:
  - Prowide가 이미 검증한 필드(형식·길이·필수 여부)에 대한 LLM 위반은 환각으로 간주, 필터링
  - CBPR+/MX 권장 사항은 위반(violations)이 아니라 경고(warnings)로 재분류
  - 조건부 규칙(C1, C2 등)은 LLM의 영역이므로 그대로 유지
"""
from __future__ import annotations

import structlog
from typing import Any

log = structlog.get_logger(__name__)


# CBPR+ 권장 사항 키워드 — violations에 있으면 warnings로 재분류
# MT 용어: :50F:/:59F: 구조화 옵션 전환 권장
# MX 전문 타입명(pacs/pain/camt 등)은 제외 — 해당 단어가 포함된 실제 위반이 오탐으로 내려가는 것을 방지
_ADVISORY_KEYWORDS = (
    "cbpr+", "구조화 주소", "권장", "recommended",
    ":50f:", ":59f:", "구조화 옵션", "전환 권장",
)


def _is_advisory(issue: str) -> bool:
    """위반 내용이 강제 규칙이 아닌 CBPR+ 권장 사항인지 판별."""
    lower = issue.lower()
    return any(kw in lower for kw in _ADVISORY_KEYWORDS)


def reconcile(
    syntax: dict[str, Any],
    llm_result: dict[str, Any],
    rule_chunks: list[Any],
) -> dict[str, Any]:
    syntax_problems = syntax.get("problems", [])
    syntax_degraded = bool(syntax.get("degraded", False))
    llm_verdict     = llm_result.get("verdict", "ERROR")
    syntax_ok       = not syntax_problems and not syntax_degraded

    raw_violations = [v for v in llm_result.get("violations", []) if isinstance(v, dict)]
    raw_warnings   = [w for w in llm_result.get("warnings",   []) if isinstance(w, dict)]

    # ── LLM 결과 정제 ─────────────────────────────────────────────────────────
    # CBPR+ 권장 사항만 warnings로 재분류 — Prowide 통과 여부와 무관하게 LLM 위반은 유지
    promoted_to_warnings = []
    filtered_violations  = []
    for v in raw_violations:
        if "field" not in v or "issue" not in v:
            log.warning("invalid_violation_structure", violation=v)
            continue
        field = v.get("field", "").strip(":")
        issue = v.get("issue", "")
        if _is_advisory(issue):
            promoted_to_warnings.append({**v, "_reclassified": True})
            log.debug("llm_violation_reclassified_as_warning", field=field)
        else:
            filtered_violations.append(v)

    llm_violations = filtered_violations
    llm_warnings   = raw_warnings + promoted_to_warnings

    # ── 최종 verdict 결정 ─────────────────────────────────────────────────────
    if syntax_degraded:
        final_verdict = "ERROR"

    elif syntax_problems:
        # Prowide 구문 오류 → REJECT 확정 (LLM 결과 무관)
        final_verdict = "REJECT"

    elif llm_violations:
        # 정제 후에도 LLM 위반이 남아 있으면 REJECT
        # (조건부 규칙 위반 등 Prowide 범위 밖 의미적 이슈)
        final_verdict = "REJECT"

    elif llm_warnings:
        # 위반은 없고 경고만 있으면 WARNING
        final_verdict = "WARNING"

    else:
        # Prowide OK + 위반 0 + 경고 0
        # → LLM 내부 오류(verdict="ERROR")나 예상 밖 값과 무관하게 PASS 확정
        # (LLM이 "ERROR"를 돌려줘도 실제 발견된 문제가 없으면 통과)
        final_verdict = "PASS"
        if llm_verdict not in ("PASS", "WARNING", "REJECT", ""):
            log.info(
                "llm_verdict_ignored_no_findings",
                llm_verdict=llm_verdict,
                note="No violations or warnings found; overriding to PASS",
            )

    if syntax_ok and llm_result.get("verdict") == "REJECT" and final_verdict != "REJECT":
        log.info(
            "llm_reject_downgraded_to_advisory",
            llm_verdict="REJECT",
            final_verdict=final_verdict,
            total_violations=len(raw_violations),
            remaining_violations=len(llm_violations),
            promoted_to_warnings=len(promoted_to_warnings),
            reason="All LLM violations reclassified as CBPR+ advisories",
        )

    # ── HITL 필요 여부 ────────────────────────────────────────────────────────
    needs_hitl = (
        bool(syntax_problems)
        or syntax_degraded
        or final_verdict in ("REJECT", "WARNING", "ERROR")
    )

    return {
        "verdict":    final_verdict,
        "needs_hitl": needs_hitl,
        "rule_engine": {
            "problems": syntax_problems,
            "degraded": syntax_degraded,
        },
        "semantic": {
            "violations":        llm_violations,
            "warnings":          llm_warnings,
            "conditional_rules": llm_result.get("applied_conditional_rules", []),
        },
        "guidebook_basis": [
            {
                "page":    getattr(c, "page_label", None) or getattr(c, "page", None),
                "rule_id": getattr(c, "rule_id", None),
                "field":   getattr(c, "field_tag", None),
                "source":  getattr(c, "source_file", None) or getattr(c, "doc_type", None)
                           or getattr(c, "source", None),
            }
            for c in rule_chunks
        ],
    }
