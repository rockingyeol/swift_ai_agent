"""Reconciler 단위 테스트."""
from __future__ import annotations

import pytest

from app.validation.reconciler import reconcile


def test_reconcile_pass() -> None:
    result = reconcile(
        syntax={"syntax_ok": True, "problems": [], "source": "prowide"},
        llm_result={"verdict": "PASS", "violations": [], "warnings": []},
        rule_chunks=[],
    )
    assert result["verdict"] == "PASS"
    assert result["needs_hitl"] is False


def test_reconcile_degraded_forces_hitl() -> None:
    result = reconcile(
        syntax={"syntax_ok": False, "problems": [], "source": "prowide", "degraded": True},
        llm_result={"verdict": "PASS", "violations": []},
        rule_chunks=[],
    )
    assert result["needs_hitl"] is True
