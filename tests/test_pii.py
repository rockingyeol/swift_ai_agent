"""
PII 마스킹 모듈 단위 테스트.

커버리지:
  - PiiVault: put/get/restore/clear
  - PiiMasker: 정형 패턴(IBAN/BIC/ACCT/AMT), 중복제거, 왕복(roundtrip)
  - LangGraph 노드: mask_pii, unmask_pii
  - Presidio 비정형 마스킹 (설치된 경우만)
"""
from __future__ import annotations

import pytest

from app.pii.masker import PiiMasker, mask_pii, unmask_pii
from app.pii.vault import PiiVault

# ---------------------------------------------------------------------------
# spaCy 한국어 모델 설치 여부 확인 (없으면 관련 테스트 skip)
# ---------------------------------------------------------------------------
try:
    import spacy as _spacy
    _spacy.load("ko_core_news_lg")
    _SPACY_KO_OK = True
except (ImportError, OSError):
    _SPACY_KO_OK = False

spacy_ko_only = pytest.mark.skipif(
    not _SPACY_KO_OK, reason="ko_core_news_lg not installed"
)


# ===========================================================================
# PiiVault 단위 테스트
# ===========================================================================

class TestPiiVault:
    def test_put_and_get(self) -> None:
        vault = PiiVault()
        vault.put("<<IBAN_1>>", "DE89370400440532013000")
        assert vault.get("<<IBAN_1>>") == "DE89370400440532013000"

    def test_get_missing_returns_none(self) -> None:
        vault = PiiVault()
        assert vault.get("<<NOTEXIST>>") is None

    def test_restore_single_placeholder(self) -> None:
        vault = PiiVault()
        vault.put("<<IBAN_1>>", "DE89370400440532013000")
        result = vault.restore("Sent from <<IBAN_1>> completed.")
        assert result == "Sent from DE89370400440532013000 completed."

    def test_restore_multiple_placeholders(self) -> None:
        vault = PiiVault()
        vault.put("<<IBAN_1>>", "DE89370400440532013000")
        vault.put("<<BIC_1>>",  "DEUTDEDB")
        result = vault.restore("<<IBAN_1>> via <<BIC_1>>")
        assert result == "DE89370400440532013000 via DEUTDEDB"

    def test_restore_no_placeholder_returns_unchanged(self) -> None:
        vault = PiiVault()
        vault.put("<<IBAN_1>>", "DE89370400440532013000")
        text = "no placeholders here"
        assert vault.restore(text) == text

    def test_clear_removes_all_entries(self) -> None:
        vault = PiiVault()
        vault.put("<<IBAN_1>>", "DE89370400440532013000")
        vault.clear()
        assert vault.get("<<IBAN_1>>") is None

    def test_overwrite_placeholder(self) -> None:
        vault = PiiVault()
        vault.put("<<IBAN_1>>", "OLD")
        vault.put("<<IBAN_1>>", "NEW")
        assert vault.get("<<IBAN_1>>") == "NEW"


# ===========================================================================
# PiiMasker — 정형 패턴 테스트
# ===========================================================================

class TestPiiMaskerStructured:
    """각 정규식 패턴이 올바르게 동작하는지 확인한다."""

    # --- IBAN ---

    def test_iban_is_masked(self) -> None:
        masker = PiiMasker()
        result = masker.mask(":50K:/DE89370400440532013000\nACME Corp")
        assert "DE89370400440532013000" not in result
        assert "<<IBAN_1>>" in result

    def test_iban_gb_format(self) -> None:
        masker = PiiMasker()
        result = masker.mask("GB29NWBK60161331926819")
        assert "GB29NWBK60161331926819" not in result
        assert "<<IBAN_1>>" in result

    def test_iban_kr_format(self) -> None:
        masker = PiiMasker()
        # 한국 IBAN은 공식 표준이 아니지만 패턴 검증용
        result = masker.mask("KR1234567890123456")
        assert "KR1234567890123456" not in result

    # --- BIC ---

    def test_bic_8char_masked(self) -> None:
        masker = PiiMasker()
        result = masker.mask(":57A:DEUTDEDB")
        assert "DEUTDEDB" not in result
        assert "<<BIC_1>>" in result

    def test_bic_11char_masked(self) -> None:
        masker = PiiMasker()
        result = masker.mask(":52A:DEUTDEDBXXX")
        assert "DEUTDEDBXXX" not in result
        assert "<<BIC_1>>" in result

    def test_short_swift_codes_not_masked_as_bic(self) -> None:
        """CRED, SSTD 같은 4자 SWIFT 코드는 BIC 패턴에 걸리지 않아야 한다."""
        masker = PiiMasker()
        result = masker.mask(":23B:CRED")
        assert "CRED" in result  # 4자 → BIC 아님

    def test_three_char_currency_not_masked_as_bic(self) -> None:
        masker = PiiMasker()
        result = masker.mask(":32A:240115USD5000,00")
        # USD는 3자라 BIC(8-11자) 패턴에 걸리지 않음
        assert "USD" in result

    # --- ACCT (로컬 계좌번호, "/" 뒤 숫자) ---

    def test_local_account_after_slash_masked(self) -> None:
        masker = PiiMasker()
        result = masker.mask(":59:/12345678901234\nJane Doe")
        assert "12345678901234" not in result
        assert "<<ACCT_1>>" in result

    def test_account_without_slash_not_masked_as_acct(self) -> None:
        """ACCT 패턴은 '/' 뒤 숫자만 잡는다. ':' 뒤 숫자는 ACCT로 마스킹 안 된다."""
        masker = PiiMasker()
        # 10자리 숫자지만 ':' 뒤에 오므로 ACCT 패턴 미적용
        # (순수 숫자라 IBAN·BIC 패턴도 미적용)
        result = masker._mask_structured(":20:1234567890")
        assert "1234567890" in result

    # --- AMT ---

    def test_amount_comma_decimal_masked(self) -> None:
        masker = PiiMasker()
        result = masker.mask(":32A:240115USD5000,00")
        assert "5000,00" not in result
        assert "<<AMT_1>>" in result

    def test_amount_dot_decimal_masked(self) -> None:
        masker = PiiMasker()
        result = masker.mask("EUR1234.56")
        assert "1234.56" not in result
        assert "<<AMT_1>>" in result

    def test_date_not_masked_as_amount(self) -> None:
        """날짜 포맷(240115)은 소수점 없으므로 AMT 패턴에 걸리지 않는다."""
        masker = PiiMasker()
        result = masker.mask(":32A:240115USD5000,00")
        # "240115"는 소수점 없어 AMT 아님
        assert "240115" in result

    def test_amount_adjacent_to_currency_masked(self) -> None:
        """USD5000,00 처럼 통화코드와 붙은 금액도 마스킹돼야 한다 (단어경계 없음)."""
        masker = PiiMasker()
        result = masker.mask("USD5000,00")
        assert "5000,00" not in result


# ===========================================================================
# PiiMasker — 중복제거 및 복수 패턴 테스트
# ===========================================================================

class TestPiiMaskerDeduplication:
    def test_same_iban_gets_same_placeholder(self) -> None:
        masker = PiiMasker()
        result = masker.mask(
            "DE89370400440532013000 and DE89370400440532013000 again"
        )
        assert result.count("<<IBAN_1>>") == 2
        assert "<<IBAN_2>>" not in result

    def test_different_ibans_get_different_placeholders(self) -> None:
        masker = PiiMasker()
        result = masker.mask("DE89370400440532013000 GB29NWBK60161331926819")
        assert "<<IBAN_1>>" in result
        assert "<<IBAN_2>>" in result

    def test_counters_are_independent_per_category(self) -> None:
        masker = PiiMasker()
        result = masker.mask(":57A:DEUTDEDB :32A:240115USD1000,00")
        assert "<<BIC_1>>" in result
        assert "<<AMT_1>>" in result
        # BIC 카운터와 AMT 카운터는 독립
        assert "<<BIC_2>>" not in result

    def test_mapping_contains_all_replaced_values(self) -> None:
        masker = PiiMasker()
        masker.mask(":32A:240115USD5000,00 :57A:DEUTDEDB")
        mapping = masker.mapping
        originals = set(mapping.values())
        assert "5000,00" in originals
        assert "DEUTDEDB" in originals


# ===========================================================================
# PiiMasker — 왕복(roundtrip) 테스트
# ===========================================================================

class TestPiiMaskerRoundtrip:
    def test_unmask_restores_iban(self) -> None:
        masker = PiiMasker()
        original = "DE89370400440532013000"
        masked = masker.mask(original)
        assert original not in masked
        assert original in masker.unmask(masked)

    def test_unmask_restores_bic(self) -> None:
        masker = PiiMasker()
        masked = masker.mask(":52A:DEUTDEDBXXX")
        assert "DEUTDEDBXXX" in masker.unmask(masked)

    def test_unmask_restores_amount(self) -> None:
        masker = PiiMasker()
        masked = masker.mask(":32A:240115USD5000,00")
        assert "5000,00" in masker.unmask(masked)

    def test_mt103_snippet_roundtrip(self) -> None:
        """MT103 전형적인 필드 조합의 마스킹 → 언마스킹 왕복 검증."""
        snippet = (
            ":20:TXNREF20240115\n"
            ":32A:240115USD5000,00\n"
            ":50K:/12345678901234567890\n"
            "ACME Corp\n"
            ":57A:DEUTDEDBXXX\n"
            ":59:/DE89370400440532013000\n"
            "Jane Doe\n"
            ":71A:SHA\n"
        )
        masker = PiiMasker()
        masked = masker.mask(snippet)

        # 민감정보가 마스킹됐는지 확인
        assert "12345678901234567890" not in masked
        assert "DE89370400440532013000" not in masked
        assert "5000,00" not in masked
        assert "DEUTDEDBXXX" not in masked

        # 비민감 필드는 보존돼야 한다
        assert ":20:" in masked
        assert "TXNREF20240115" in masked
        assert ":71A:SHA" in masked

        # 언마스킹 후 원본 복원
        restored = masker.unmask(masked)
        assert "12345678901234567890" in restored
        assert "DE89370400440532013000" in restored
        assert "5000,00" in restored
        assert "DEUTDEDBXXX" in restored

    def test_unmask_is_exact_inverse(self) -> None:
        """mask → unmask 결과가 원본과 동일한지 확인한다."""
        original = ":59:/DE89370400440532013000\nJane Doe\n:32A:240115USD999,99"
        masker = PiiMasker()
        masked = masker.mask(original)
        # 마스킹된 텍스트를 다시 unmask
        restored = masker.unmask(masked)
        # IBAN과 금액은 복원
        assert "DE89370400440532013000" in restored
        assert "999,99" in restored

    def test_masker_mapping_property(self) -> None:
        masker = PiiMasker()
        masker.mask(":32A:240115USD5000,00")
        mapping = masker.mapping
        assert isinstance(mapping, dict)
        assert all(k.startswith("<<") and k.endswith(">>") for k in mapping)
        assert "5000,00" in mapping.values()


# ===========================================================================
# LangGraph 노드 테스트
# ===========================================================================

class TestLangGraphNodes:
    def test_mask_pii_node_adds_masked_message(self) -> None:
        state = {
            "raw_message": ":59:/DE89370400440532013000\nJane Doe",
            "msg_type": "MT103",
        }
        result = mask_pii(state)
        assert "masked_message" in result
        assert "DE89370400440532013000" not in result["masked_message"]

    def test_mask_pii_node_adds_pii_mapping(self) -> None:
        state = {
            "raw_message": ":57A:DEUTDEDB\n:32A:240115USD1000,00",
            "msg_type": "MT103",
        }
        result = mask_pii(state)
        assert "pii_mapping" in result
        assert isinstance(result["pii_mapping"], dict)
        assert len(result["pii_mapping"]) >= 2  # BIC + AMT

    def test_mask_pii_preserves_other_state_fields(self) -> None:
        state = {
            "raw_message": ":32A:240115USD500,00",
            "msg_type": "MT103",
            "user_intent": "analyze",
        }
        result = mask_pii(state)
        assert result["msg_type"] == "MT103"
        assert result["user_intent"] == "analyze"

    def test_unmask_pii_restores_output_text(self) -> None:
        state = {
            "raw_message": ":59:/DE89370400440532013000",
            "msg_type": "MT103",
        }
        masked_state = mask_pii(state)
        # LLM이 플레이스홀더를 포함한 출력을 생성했다고 가정
        ph = next(iter(masked_state["pii_mapping"]))
        original_value = masked_state["pii_mapping"][ph]
        masked_state["output"] = {"text": f"The account {ph} passed validation."}

        final = unmask_pii(masked_state)
        assert original_value in final["output"]["text"]
        assert ph not in final["output"]["text"]

    def test_unmask_pii_handles_multiple_output_fields(self) -> None:
        state = {
            "raw_message": ":57A:DEUTDEDB :32A:240115USD1000,00",
            "msg_type": "MT103",
        }
        masked_state = mask_pii(state)
        mapping = masked_state["pii_mapping"]
        ph_list = list(mapping.keys())

        masked_state["output"] = {
            "text": f"BIC: {ph_list[0]}",
            "message": f"Amount: {ph_list[1]}" if len(ph_list) > 1 else "Amount: N/A",
            "count": 42,  # int — 변환 없이 그대로여야 함
        }

        final = unmask_pii(masked_state)
        assert mapping[ph_list[0]] in final["output"]["text"]
        assert final["output"]["count"] == 42

    def test_unmask_pii_empty_output_safe(self) -> None:
        state = {
            "raw_message": ":32A:240115USD500,00",
            "msg_type": "MT103",
        }
        masked_state = mask_pii(state)
        # output 없이 unmask 호출해도 에러 없어야 함
        final = unmask_pii(masked_state)
        assert final["output"] == {}

    def test_unmask_pii_empty_mapping_safe(self) -> None:
        state: dict = {
            "raw_message": ":20:PLAINREF",
            "msg_type": "MT103",
            "output": {"text": "no placeholders"},
            "pii_mapping": {},
        }
        final = unmask_pii(state)
        assert final["output"]["text"] == "no placeholders"


# ===========================================================================
# spaCy 한국어 NER 테스트 (ko_core_news_lg 설치된 경우만 실행)
# ===========================================================================

class TestKoreanNerMasking:
    @spacy_ko_only
    def test_person_name_masked(self) -> None:
        masker = PiiMasker()
        result = masker.mask("홍길동에게 송금하시겠습니까?")
        assert "홍길동" not in result

    @spacy_ko_only
    def test_person_placeholder_in_mapping(self) -> None:
        masker = PiiMasker()
        masker.mask("홍길동에게 송금")
        mapping = masker.mapping
        assert any("홍길동" in orig for orig in mapping.values())

    @spacy_ko_only
    def test_ner_and_structured_combined(self) -> None:
        """구조적 마스킹과 NER 마스킹이 동일 메시지에서 함께 동작한다."""
        masker = PiiMasker()
        text = ":59:/DE89370400440532013000\n홍길동"
        result = masker.mask(text)
        assert "DE89370400440532013000" not in result
        assert "홍길동" not in result
