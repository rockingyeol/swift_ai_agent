"""
PII 마스킹 게이트 (plan.md [2] 참조).

정형 (계좌/IBAN/BIC/금액): 정규식
비정형 (이름/주소): Presidio + spaCy ko_core_news_lg (미설치 시 no-op)

원본 ↔ 플레이스홀더 매핑은 LangGraph state["pii_mapping"]에 직렬화해 보관한다.
LLM에는 masked_message만 전달되며, raw_message는 절대 노출되지 않는다.
"""
from __future__ import annotations

import re
import threading
from typing import Optional

from app.graph.state import AgentState

# ---------------------------------------------------------------------------
# Structured PII patterns — 적용 순서가 중요: IBAN > BIC > ACCT > AMT
# ---------------------------------------------------------------------------
_STRUCTURED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # IBAN: 국가코드(2) + 체크(2) + BBAN(4~30)
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{4,30}\b")),
    # BIC: 기관(4) + 국가(2) + 위치(2) + 지점(3, 선택) = 8 또는 11자
    ("BIC",  re.compile(r"\b[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b")),
    # 로컬 계좌: SWIFT "/" 구분자 뒤 숫자열 (IBAN 마스킹 후 처리하므로 이중 치환 없음)
    ("ACCT", re.compile(r"(?<=/)\d{10,34}")),
    # 금액: SWIFT는 콤마 소수점(5000,00). (?!\d)로 더 긴 숫자에 포함되는 경우 제외
    ("AMT",  re.compile(r"\d{1,15}[,.]\d{2}(?!\d)")),
]

# ---------------------------------------------------------------------------
# spaCy 한국어 NER 싱글턴 (lazy init, 스레드 안전)
# Presidio의 analyze()는 자체 recognizer 등록 엔터티만 반환하므로
# 한국어 레이블(PS/LC/OG)은 spaCy를 직접 사용해 처리한다.
# ---------------------------------------------------------------------------
_KO_NER_LABELS: frozenset[str] = frozenset({"PS", "LC", "OG"})  # 사람/장소/기관

_spacy_lock = threading.Lock()
_spacy_ko: Optional[object] = None


def _get_spacy_ko() -> object:
    global _spacy_ko
    if _spacy_ko is not None:
        return _spacy_ko
    with _spacy_lock:
        if _spacy_ko is None:
            import spacy
            _spacy_ko = spacy.load("ko_core_news_lg")
    return _spacy_ko


# ---------------------------------------------------------------------------
# PiiMasker — 단일 메시지 세션
# ---------------------------------------------------------------------------

class PiiMasker:
    """
    단일 메시지 PII 마스킹 세션. 스레드 독립 사용(one per message).

        masker = PiiMasker()
        masked = masker.mask(raw_text)
        mapping = masker.mapping          # state 직렬화용
        restored = masker.unmask(masked)  # 또는 unmask_pii 노드 사용
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._orig_to_ph: dict[str, str] = {}  # 중복제거: original → placeholder
        self._ph_to_orig: dict[str, str] = {}  # 복원용:   placeholder → original

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mask(self, text: str) -> str:
        """정형 → 비정형 순서로 PII를 플레이스홀더로 치환한다."""
        text = self._mask_structured(text)
        text = self._mask_unstructured(text)
        return text

    def unmask(self, text: str) -> str:
        """플레이스홀더를 원본값으로 복원한다."""
        for ph, orig in self._ph_to_orig.items():
            text = text.replace(ph, orig)
        return text

    @property
    def mapping(self) -> dict[str, str]:
        """{placeholder: original} 직렬화 딕셔너리 (AgentState["pii_mapping"] 용)."""
        return dict(self._ph_to_orig)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _placeholder(self, category: str, original: str) -> str:
        """동일 원본값은 항상 같은 플레이스홀더를 반환한다 (세션 내 중복제거)."""
        if original in self._orig_to_ph:
            return self._orig_to_ph[original]
        n = self._counters.get(category, 0) + 1
        self._counters[category] = n
        ph = f"<<{category}_{n}>>"
        self._orig_to_ph[original] = ph
        self._ph_to_orig[ph] = original
        return ph

    def _mask_structured(self, text: str) -> str:
        for category, pattern in _STRUCTURED_PATTERNS:
            # 람다 기본인수로 category 캡처 (루프 클로저 문제 방지)
            text = pattern.sub(
                lambda m, cat=category: self._placeholder(cat, m.group(0)),
                text,
            )
        return text

    def _mask_unstructured(self, text: str) -> str:
        """spaCy 한국어 NER(사람·장소·기관). 모델 미설치 시 no-op."""
        try:
            nlp = _get_spacy_ko()
        except (ImportError, OSError):
            return text

        doc = nlp(text)  # type: ignore[operator]
        entities = [ent for ent in doc.ents if ent.label_ in _KO_NER_LABELS]
        # 오프셋 보존을 위해 뒤에서 앞으로 치환
        for ent in sorted(entities, key=lambda e: e.start_char, reverse=True):
            ph = self._placeholder(ent.label_, ent.text)
            text = text[: ent.start_char] + ph + text[ent.end_char :]
        return text


# ---------------------------------------------------------------------------
# LangGraph 노드
# ---------------------------------------------------------------------------

def mask_pii(state: AgentState) -> AgentState:
    """
    [2] PII 마스킹 게이트 노드.
    raw_message → masked_message 생성, pii_mapping을 state에 저장.
    """
    masker = PiiMasker()
    masked = masker.mask(state["raw_message"])
    return {
        **state,
        "masked_message": masked,
        "pii_mapping": masker.mapping,
    }


def unmask_pii(state: AgentState) -> AgentState:
    """
    [7] 언마스킹 노드.
    output 딕셔너리의 모든 string 값에서 플레이스홀더를 원본으로 복원한다.
    """
    mapping: dict[str, str] = state.get("pii_mapping") or {}
    output = dict(state.get("output") or {})

    def _restore(text: str) -> str:
        for ph, orig in mapping.items():
            text = text.replace(ph, orig)
        return text

    output = {k: _restore(v) if isinstance(v, str) else v for k, v in output.items()}
    return {**state, "output": output}
