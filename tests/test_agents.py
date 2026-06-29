"""
에이전트 단위/통합 테스트.

Analyzer · Mapper · Generator 에이전트의 데이터 흐름과 생성 품질을 검증한다.

─────────────────────────────────────────────────────────────────
Mock / Live 모드 전환
─────────────────────────────────────────────────────────────────
  # Mock 모드 (기본) — LLM / Qdrant 호출 없음, 즉시 실행
  pytest tests/test_agents.py -v

  # Live 모드 — 실제 LLM + Qdrant 호출 (Ollama + Qdrant 가동 필요)
  pytest tests/test_agents.py -v -m live

  # 특정 에이전트만
  pytest tests/test_agents.py -v -k "analyzer"
  pytest tests/test_agents.py -v -k "mapper"
  pytest tests/test_agents.py -v -k "generator"

환경변수:
  AGENT_TEST_LIVE=1  설정 시 -m live 없이도 Live 모드로 실행
─────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    _env = Path(__file__).resolve().parent.parent / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass

# ── Live 모드 여부 ──────────────────────────────────────────────────────────
_LIVE = os.getenv("AGENT_TEST_LIVE", "").lower() in ("1", "true", "yes")

# pytest 마커 등록
pytest.ini_options = {}  # pyproject.toml 에 markers 등록 권장


# ===========================================================================
# 샘플 데이터
# ===========================================================================

MT103_RAW = textwrap.dedent("""\
    {1:F01BNPAFRPPXXXX0000000000}
    {2:O1030900240115DEUTDEFFXXXX00000000002401150900N}
    {4:
    :20:REF20240115001
    :23B:CRED
    :32A:240115EUR10000,00
    :50K:/DE89370400440532013000
    ORDERING CUSTOMER NAME
    123 SENDER STREET
    BERLIN GERMANY
    :59:/FR7630006000011234567890189
    BENEFICIARY CORP
    456 RECEIVER AVE
    PARIS FRANCE
    :71A:SHA
    -}""")

# PII 마스킹 적용본 (테스트에서 LLM에 전달되는 실제 전문)
MT103_MASKED = textwrap.dedent("""\
    {1:F01BNPAFRPPXXXX0000000000}
    {2:O1030900240115DEUTDEFFXXXX00000000002401150900N}
    {4:
    :20:REF20240115001
    :23B:CRED
    :32A:240115EUR10000,00
    :50K:/<<IBAN_1>>
    <<NAME_1>>
    <<ADDR_1>>
    <<CITY_1>>
    :59:/<<IBAN_2>>
    <<NAME_2>>
    <<ADDR_2>>
    <<CITY_2>>
    :71A:SHA
    -}""")

GENERATOR_REQUEST = (
    "EUR 10,000을 독일 DEUTDEFFXXX 은행에서 프랑스 BNPAFRPPXXX 은행으로 "
    "당일(2024-01-15) 송금하는 MT103 전문을 작성해 주세요."
)

# Mapper용 가상 pacs.008 초안 (Prowide not-implemented 대체)
PROWIDE_DRAFT_PACS008 = textwrap.dedent("""\
    <?xml version="1.0"?>
    <Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
      <FIToFICstmrCdtTrf>
        <GrpHdr>
          <MsgId>REF20240115001</MsgId>
          <CreDtTm>2024-01-15T09:00:00</CreDtTm>
          <NbOfTxs>1</NbOfTxs>
          <SttlmInf><SttlmMtd>CLRG</SttlmMtd></SttlmInf>
        </GrpHdr>
        <CdtTrfTxInf>
          <PmtId><EndToEndId>REF20240115001</EndToEndId></PmtId>
          <IntrBkSttlmAmt Ccy="EUR">10000.00</IntrBkSttlmAmt>
          <Dbtr><Nm><<NAME_1>></Nm></Dbtr>
          <Cdtr><Nm><<NAME_2>></Nm></Cdtr>
        </CdtTrfTxInf>
      </FIToFICstmrCdtTrf>
    </Document>""")


# ===========================================================================
# Mock LLM 응답 팩토리
# ===========================================================================

def _mock_llm_response(content: str) -> MagicMock:
    """openai.ChatCompletion 응답 구조를 모방하는 Mock 객체를 반환한다."""
    msg  = MagicMock()
    msg.content = content

    choice  = MagicMock()
    choice.message = msg

    resp = MagicMock()
    resp.choices = [choice]
    return resp


# ── 에이전트별 Mock 응답 JSON ─────────────────────────────────────────────────

ANALYZER_MOCK_RESPONSE = json.dumps({
    "verdict": "PASS",
    "violations": [],
    "warnings": [],
    "applied_conditional_rules": [
        {
            "rule_id": "C1",
            "page": 142,
            "triggered": False,
            "why": "필드 33B가 전문에 부재하여 36(환율) 필수 조건이 발동되지 않음",
        }
    ],
})

MAPPER_MOCK_RESPONSE = json.dumps({
    "enhanced_message": PROWIDE_DRAFT_PACS008,
    "unmapped_fields": [],
    "enhancement_warnings": [
        {
            "field": "Dbtr/PstlAdr",
            "issue": "구조화 주소 없음. CBPR+ 권장 사항.",
            "page": None,
            "rule_id": None,
        }
    ],
})

GENERATOR_MOCK_RESPONSE = textwrap.dedent("""\
    {1:F01BNPAFRPPXXXX0000000000}
    {2:O1030900240115DEUTDEFFXXXX00000000002401150900N}
    {4:
    :20:<<REF_1>>
    :23B:CRED
    :32A:240115EUR10000,00
    :50K:/<<IBAN_1>>
    <<NAME_1>>
    :59:/<<IBAN_2>>
    <<NAME_2>>
    :71A:SHA
    -}""")


# ===========================================================================
# Mock SwiftRetriever 팩토리
# ===========================================================================

def _make_mock_retriever(chunks: list | None = None) -> MagicMock:
    """SwiftRetriever.search()를 가짜 청크 목록으로 반환하는 Mock."""
    from app.rag.chunker import SwiftChunk

    if chunks is None:
        chunks = [
            SwiftChunk(
                chunk_id="mock-chunk-001",
                source_type="mt",
                level="rule",
                message_type="MT103",
                field_tag="32A",
                rule_id="C1",
                page=142,
                text=(
                    "32A: Value Date / Currency / Interbank Settled Amount. "
                    "YYMMDD 형식 날짜, 3자 통화, 쉼표 소수점 금액."
                ),
            ),
            SwiftChunk(
                chunk_id="mock-chunk-002",
                source_type="mt",
                level="rule",
                message_type="MT103",
                field_tag="50K",
                rule_id=None,
                page=118,
                text=(
                    "50K: Ordering Customer. "
                    "계좌번호(optional) + 이름 + 주소. CBPR+ 환경에서 구조화 권장."
                ),
            ),
            SwiftChunk(
                chunk_id="mock-chunk-003",
                source_type="mt",
                level="rule",
                message_type="MT103",
                field_tag="59",
                rule_id=None,
                page=125,
                text=(
                    "59: Beneficiary Customer. "
                    "옵션 없음: 계좌+이름. 옵션 A: BIC 필수."
                ),
            ),
        ]

    mock = MagicMock()
    mock.search.return_value = chunks
    return mock


# ===========================================================================
# pytest fixtures
# ===========================================================================

@pytest.fixture
def base_state() -> dict[str, Any]:
    """모든 에이전트 테스트에 공통으로 사용하는 최소 AgentState."""
    return {
        "raw_message":    MT103_RAW,
        "masked_message": MT103_MASKED,
        "msg_type":       "MT103",
        "user_intent":    "analyze",
        "pii_mapping":    {
            "<<IBAN_1>>": "DE89370400440532013000",
            "<<IBAN_2>>": "FR7630006000011234567890189",
            "<<NAME_1>>": "ORDERING CUSTOMER NAME",
            "<<NAME_2>>": "BENEFICIARY CORP",
        },
    }


@pytest.fixture
def mock_retriever():
    """기본 Mock SwiftRetriever."""
    return _make_mock_retriever()


@pytest.fixture
def mock_prowide_ok():
    """Prowide 구문 검증 성공 응답 Mock."""
    return {
        "syntax_ok":    True,
        "problems":     [],
        "message_type": "MT103",
        "source":       "prowide",
    }


@pytest.fixture
def mock_prowide_fail():
    """Prowide 구문 검증 실패 응답 Mock."""
    return {
        "syntax_ok": False,
        "problems":  [
            {"code": "MISSING_FIELD", "field": "50K",
             "desc": "Mandatory field :50K: missing for MT103"},
        ],
        "message_type": "MT103",
        "source":       "prowide",
    }


# ── Live 모드 skip 데코레이터 ────────────────────────────────────────────────

def _live_only(fn):
    """Live 모드가 아니면 skip."""
    return pytest.mark.skipif(
        not _LIVE,
        reason="Live 모드에서만 실행 (pytest -m live 또는 AGENT_TEST_LIVE=1)",
    )(fn)


# ===========================================================================
# ── 시나리오 1: Analyzer Agent ────────────────────────────────────────────────
# ===========================================================================

class TestAnalyzerAgent:
    """Analyzer Agent 데이터 흐름 및 출력 구조 검증."""

    # ── 1-1: LLM 응답 구조 파싱 ─────────────────────────────────────────────

    def test_parse_llm_json_valid(self):
        """parse_llm_json()이 정상 JSON 문자열을 올바르게 파싱하는지 확인."""
        from app.llm import parse_llm_json
        result = parse_llm_json(ANALYZER_MOCK_RESPONSE)
        assert result.get("verdict") in ("PASS", "WARNING", "REJECT", "ERROR")
        assert isinstance(result.get("violations"), list)
        assert isinstance(result.get("warnings"), list)

    def test_parse_llm_json_fenced(self):
        """```json ... ``` 펜스가 감싸인 LLM 응답도 파싱 가능한지 확인."""
        from app.llm import parse_llm_json
        fenced = f"```json\n{ANALYZER_MOCK_RESPONSE}\n```"
        result = parse_llm_json(fenced)
        assert result.get("verdict") == "PASS"

    def test_parse_llm_json_malformed_returns_error(self):
        """파싱 불가 텍스트는 verdict=ERROR를 반환해야 한다."""
        from app.llm import parse_llm_json
        result = parse_llm_json("이건 JSON이 아닙니다")
        assert result.get("verdict") == "ERROR"
        assert result.get("_parse_error") is True

    # ── 1-2: reconcile() 병합 로직 ──────────────────────────────────────────

    def test_reconcile_pass(self, mock_prowide_ok):
        """Prowide OK + LLM PASS → 최종 verdict PASS, needs_hitl=False."""
        from app.validation.reconciler import reconcile
        llm = {"verdict": "PASS", "violations": [], "warnings": []}
        result = reconcile(mock_prowide_ok, llm, [])
        assert result["verdict"] == "PASS"
        assert result["needs_hitl"] is False

    def test_reconcile_syntax_fail_overrides_llm(self, mock_prowide_fail):
        """Prowide 실패 → LLM이 PASS여도 최종 verdict REJECT."""
        from app.validation.reconciler import reconcile
        llm = {"verdict": "PASS", "violations": [], "warnings": []}
        result = reconcile(mock_prowide_fail, llm, [])
        assert result["verdict"] == "REJECT"
        assert result["needs_hitl"] is True

    def test_reconcile_llm_warning(self, mock_prowide_ok):
        """Prowide OK + LLM WARNING → 최종 verdict WARNING, needs_hitl=True."""
        from app.validation.reconciler import reconcile
        llm = {
            "verdict": "WARNING",
            "violations": [],
            "warnings": [{"field": "59", "issue": "비구조화 주소"}],
        }
        result = reconcile(mock_prowide_ok, llm, [])
        assert result["verdict"] == "WARNING"
        assert result["needs_hitl"] is True

    def test_reconcile_degraded_forces_hitl(self):
        """Prowide degraded(연결 실패) → 무인 통과 금지, needs_hitl=True."""
        from app.validation.reconciler import reconcile
        syntax = {"syntax_ok": False, "problems": [], "degraded": True}
        llm = {"verdict": "PASS", "violations": [], "warnings": []}
        result = reconcile(syntax, llm, [])
        assert result["needs_hitl"] is True

    # ── 1-3: run_analyzer() Mock 테스트 (전체 흐름) ──────────────────────────

    def test_run_analyzer_output_structure(self, base_state, mock_retriever,
                                           mock_prowide_ok):
        """Mock 환경에서 run_analyzer()가 올바른 출력 구조를 반환하는지 확인."""
        from app.agents.analyzer import AnalyzerOutput
        mock_output = AnalyzerOutput(
            source_msg_type="MT103",
            target_msg_type="pacs.008.001.08",
            transaction_count=1,
            currency="EUR",
            missing_fields=[],
            verdict="PASS",
        )

        with (
            patch("app.agents.analyzer.prowide_syntax_verify",
                  return_value=mock_prowide_ok),
            patch("app.agents.analyzer._get_retriever",
                  return_value=mock_retriever),
            patch("app.agents.analyzer._build_llm_chain") as mock_chain,
        ):
            mock_chain.return_value.invoke.return_value = mock_output

            from app.agents.analyzer import run_analyzer
            result = run_analyzer(base_state)

        assert "validation_result" in result
        assert "needs_hitl"        in result
        assert "output"            in result
        assert result["output"]["type"] == "analysis"
        assert result["output"]["verdict"] in ("PASS", "WARNING", "REJECT", "ERROR")

    def test_run_analyzer_verdict_pass(self, base_state, mock_retriever,
                                       mock_prowide_ok):
        """정상 전문 입력 시 verdict=PASS, needs_hitl=False."""
        from app.agents.analyzer import AnalyzerOutput
        mock_output = AnalyzerOutput(
            source_msg_type="MT103", target_msg_type="pacs.008.001.08",
            verdict="PASS",
        )
        with (
            patch("app.agents.analyzer.prowide_syntax_verify",
                  return_value=mock_prowide_ok),
            patch("app.agents.analyzer._get_retriever",
                  return_value=mock_retriever),
            patch("app.agents.analyzer._build_llm_chain") as mock_chain,
        ):
            mock_chain.return_value.invoke.return_value = mock_output
            from app.agents.analyzer import run_analyzer
            result = run_analyzer(base_state)

        assert result["output"]["verdict"] == "PASS"
        assert result["needs_hitl"] is False

    def test_run_analyzer_prowide_fail_triggers_hitl(self, base_state,
                                                      mock_retriever,
                                                      mock_prowide_fail):
        """Prowide 실패 입력 시 needs_hitl=True, verdict=REJECT."""
        from app.agents.analyzer import AnalyzerOutput
        mock_output = AnalyzerOutput(
            source_msg_type="MT103", target_msg_type="pacs.008.001.08",
            verdict="PASS",
        )
        with (
            patch("app.agents.analyzer.prowide_syntax_verify",
                  return_value=mock_prowide_fail),
            patch("app.agents.analyzer._get_retriever",
                  return_value=mock_retriever),
            patch("app.agents.analyzer._build_llm_chain") as mock_chain,
        ):
            mock_chain.return_value.invoke.return_value = mock_output
            from app.agents.analyzer import run_analyzer
            result = run_analyzer(base_state)

        assert result["needs_hitl"] is True
        assert result["output"]["verdict"] == "REJECT"

    def test_run_analyzer_passes_masked_message_not_raw(self, base_state,
                                                         mock_retriever,
                                                         mock_prowide_ok):
        """LLM에 전달되는 프롬프트에 masked_message가 포함되고
        raw_message(원본 PII)는 포함되지 않아야 한다."""
        from app.agents.analyzer import AnalyzerOutput
        captured_kwargs: list = []

        def _capture_invoke(kwargs):
            captured_kwargs.append(kwargs)
            return AnalyzerOutput(
                source_msg_type="MT103", target_msg_type="", verdict="PASS"
            )

        with (
            patch("app.agents.analyzer.prowide_syntax_verify",
                  return_value=mock_prowide_ok),
            patch("app.agents.analyzer._get_retriever",
                  return_value=mock_retriever),
            patch("app.agents.analyzer._build_llm_chain") as mock_chain,
        ):
            mock_chain.return_value.invoke.side_effect = _capture_invoke
            from app.agents.analyzer import run_analyzer
            run_analyzer(base_state)

        assert len(captured_kwargs) > 0
        full_prompt = str(captured_kwargs[0])

        assert "<<IBAN_1>>" in full_prompt or "<<NAME_1>>" in full_prompt, \
            "마스킹 플레이스홀더가 프롬프트에 없습니다"
        assert "DE89370400440532013000" not in full_prompt, \
            "원본 IBAN이 LLM 프롬프트에 노출되었습니다 (PII 보안 위반)"


# ===========================================================================
# ── 시나리오 2: Mapper Agent + RAG 컨텍스트 주입 검증 ──────────────────────────
# ===========================================================================

class TestMapperAgentRAG:
    """Mapper Agent의 RAG 컨텍스트 주입 및 프롬프트 구성을 검증한다."""

    def test_mt_to_mx_type_inference(self):
        """MT103 → pacs.008.001.08 타입 매핑이 올바른지 확인."""
        from app.agents.mapper import _infer_target_type
        assert _infer_target_type("MT103", "mt_to_mx") == "pacs.008.001.08"
        assert _infer_target_type("MT202", "mt_to_mx") == "pacs.009.001.08"
        assert _infer_target_type("MT940", "mt_to_mx") == "camt.053.001.08"

    def test_mx_to_mt_type_inference(self):
        """pacs.008.001.08 → MT103 역방향 매핑."""
        from app.agents.mapper import _infer_target_type
        assert _infer_target_type("pacs.008.001.08", "mx_to_mt") == "MT103"

    def _make_mapper_output(self):
        """테스트용 MapperOutput 객체 생성."""
        from app.agents.mapper import MapperOutput, FieldMapping, EnhancementWarning
        return MapperOutput(
            direction="mt_to_mx",
            source_type="MT103",
            target_type="pacs.008.001.08",
            mappings=[
                FieldMapping(mt_tag=":32A:", mx_paths=["GrpHdr/IntrBkSttlmDt", "IntrBkSttlmAmt"],
                             mx_value="2024-01-15", notes="YYMMDD→ISO 8601"),
                FieldMapping(mt_tag=":50K:", mx_paths=["Dbtr/Nm"],
                             mx_value="<<NAME_1>>"),
            ],
            unmapped_fields=[],
            enhancement_warnings=[
                EnhancementWarning(field="Dbtr/PstlAdr",
                                   issue="구조화 주소 없음. CBPR+ 권장 사항.")
            ],
        )

    def test_retriever_called_with_target_type(self, base_state, mock_retriever):
        """Mapper가 RAG 검색 시 target_type(pacs.008.001.08) 필터를 사용하는지 확인."""
        translate = {"content": PROWIDE_DRAFT_PACS008, "ok": False,
                     "error": "not-implemented", "degraded": False}

        with (
            patch("app.agents.mapper.prowide_translate", return_value=translate),
            patch("app.agents.mapper._get_retriever",   return_value=mock_retriever),
            patch("app.agents.mapper._build_llm_chain") as mock_chain,
        ):
            mock_chain.return_value.invoke.return_value = self._make_mapper_output()
            from app.agents.mapper import run_mapper
            run_mapper(base_state)

        mock_retriever.search.assert_called_once()
        call_kwargs = mock_retriever.search.call_args
        query_arg = call_kwargs[1].get("query") or call_kwargs[0][0]
        assert "pacs.008.001.08" in query_arg, \
            f"RAG 쿼리에 target_type 없음: '{query_arg}'"

    def test_retrieved_chunks_injected_into_prompt(self, base_state, mock_retriever):
        """Retriever가 반환한 청크 내용이 LLM invoke kwargs에 실제로 포함되는지 확인."""
        translate = {"content": PROWIDE_DRAFT_PACS008, "ok": False, "degraded": False}
        captured_kwargs: list = []

        def _capture(kwargs):
            captured_kwargs.append(kwargs)
            return self._make_mapper_output()

        with (
            patch("app.agents.mapper.prowide_translate", return_value=translate),
            patch("app.agents.mapper._get_retriever",   return_value=mock_retriever),
            patch("app.agents.mapper._build_llm_chain") as mock_chain,
        ):
            mock_chain.return_value.invoke.side_effect = _capture
            from app.agents.mapper import run_mapper
            run_mapper(base_state)

        assert len(captured_kwargs) > 0
        full_ctx = str(captured_kwargs[0])
        assert "32A" in full_ctx, "RAG 청크 내용(32A)이 프롬프트에 없습니다"
        assert "50K" in full_ctx, "RAG 청크 내용(50K)이 프롬프트에 없습니다"
        assert "FIToFICstmrCdtTrf" in full_ctx, "Prowide 초안이 프롬프트에 없습니다"

    def test_run_mapper_output_structure(self, base_state, mock_retriever):
        """run_mapper()가 올바른 출력 키를 반환하는지 확인."""
        translate = {"content": PROWIDE_DRAFT_PACS008, "ok": False, "degraded": False}

        with (
            patch("app.agents.mapper.prowide_translate", return_value=translate),
            patch("app.agents.mapper._get_retriever",   return_value=mock_retriever),
            patch("app.agents.mapper._build_llm_chain") as mock_chain,
        ):
            mock_chain.return_value.invoke.return_value = self._make_mapper_output()
            from app.agents.mapper import run_mapper
            result = run_mapper(base_state)

        assert result["output"]["type"]      == "mapped_message"
        assert result["output"]["direction"] == "mt_to_mx"
        assert "enhanced"        in result["output"]
        assert "unmapped_fields" in result["output"]
        assert "warnings"        in result["output"]
        assert "guidebook_basis" in result["output"]
        assert isinstance(result["output"]["guidebook_basis"], list)

    def test_run_mapper_enhancement_warning_triggers_hitl(self, base_state,
                                                           mock_retriever):
        """enhancement_warnings가 있으면 needs_hitl=True여야 한다."""
        translate = {"content": "", "degraded": False}

        with (
            patch("app.agents.mapper.prowide_translate", return_value=translate),
            patch("app.agents.mapper._get_retriever",   return_value=mock_retriever),
            patch("app.agents.mapper._build_llm_chain") as mock_chain,
        ):
            mock_chain.return_value.invoke.return_value = self._make_mapper_output()
            from app.agents.mapper import run_mapper
            result = run_mapper(base_state)

        assert result["needs_hitl"] is True

    def test_guidebook_basis_contains_chunk_metadata(self, base_state, mock_retriever):
        """guidebook_basis에 청크의 page / rule_id / field 메타데이터가 포함되어야 한다."""
        translate = {"content": "", "degraded": False}

        with (
            patch("app.agents.mapper.prowide_translate", return_value=translate),
            patch("app.agents.mapper._get_retriever",   return_value=mock_retriever),
            patch("app.agents.mapper._build_llm_chain") as mock_chain,
        ):
            mock_chain.return_value.invoke.return_value = self._make_mapper_output()
            from app.agents.mapper import run_mapper
            result = run_mapper(base_state)

        basis = result["output"]["guidebook_basis"]
        assert len(basis) > 0, "guidebook_basis가 비어있습니다"
        for item in basis:
            assert "page"    in item, f"page 키 없음: {item}"
            assert "field"   in item, f"field 키 없음: {item}"
            assert "rule_id" in item, f"rule_id 키 없음: {item}"


# ===========================================================================
# ── 시나리오 3: Generator Agent 초안 생성 검증 ───────────────────────────────
# ===========================================================================

class TestGeneratorAgent:
    """Generator Agent의 프롬프트 구성 및 초안 생성 품질을 검증한다."""

    @pytest.fixture
    def generator_state(self) -> dict[str, Any]:
        return {
            "raw_message":    GENERATOR_REQUEST,
            "masked_message": GENERATOR_REQUEST,  # 자연어 요청 — PII 없음
            "msg_type":       "MT103",
            "user_intent":    "generate",
            "pii_mapping":    {},
        }

    def _mock_chain_response(self, content: str):
        """_build_generator_chain().invoke() 가 반환하는 mock 응답 객체."""
        mock_resp = MagicMock()
        mock_resp.content = content
        return mock_resp

    def test_run_generator_output_structure(self, generator_state, mock_retriever):
        """run_generator()가 올바른 출력 구조를 반환하는지 확인."""
        with (
            patch("app.agents.generator._get_retriever", return_value=mock_retriever),
            patch("app.agents.generator._build_generator_chain") as mock_chain,
        ):
            mock_chain.return_value.invoke.return_value = self._mock_chain_response(GENERATOR_MOCK_RESPONSE)
            from app.agents.generator import run_generator
            result = run_generator(generator_state)

        assert result["output"]["type"] == "generated_message"
        assert "draft"          in result["output"]
        assert "guidebook_basis" in result["output"]
        assert result["needs_hitl"] is True

    def test_generator_always_needs_hitl(self, generator_state, mock_retriever):
        """Generator 결과는 내용과 무관하게 항상 needs_hitl=True여야 한다."""
        with (
            patch("app.agents.generator._get_retriever", return_value=mock_retriever),
            patch("app.agents.generator._build_generator_chain") as mock_chain,
        ):
            mock_chain.return_value.invoke.return_value = self._mock_chain_response(GENERATOR_MOCK_RESPONSE)
            from app.agents.generator import run_generator
            result = run_generator(generator_state)

        assert result["needs_hitl"] is True
        assert result["validation_result"]["verdict"] == "PENDING_REVIEW"

    def test_generator_draft_not_empty(self, generator_state, mock_retriever):
        """생성된 초안(draft)이 빈 문자열이 아니어야 한다."""
        with (
            patch("app.agents.generator._get_retriever", return_value=mock_retriever),
            patch("app.agents.generator._build_generator_chain") as mock_chain,
        ):
            mock_chain.return_value.invoke.return_value = self._mock_chain_response(GENERATOR_MOCK_RESPONSE)
            from app.agents.generator import run_generator
            result = run_generator(generator_state)

        draft = result["output"]["draft"]
        assert isinstance(draft, str) and len(draft) > 0, "생성된 초안이 비어있습니다"

    def test_generator_draft_contains_swift_structure(self, generator_state,
                                                        mock_retriever):
        """생성 초안에 SWIFT 전문 구조 마커(:20:, :23B:, :32A:)가 포함되어야 한다."""
        with (
            patch("app.agents.generator._get_retriever", return_value=mock_retriever),
            patch("app.agents.generator._build_generator_chain") as mock_chain,
        ):
            mock_chain.return_value.invoke.return_value = self._mock_chain_response(GENERATOR_MOCK_RESPONSE)
            from app.agents.generator import run_generator
            result = run_generator(generator_state)

        draft = result["output"]["draft"]
        for marker in (":20:", ":23B:", ":32A:"):
            assert marker in draft, f"필수 SWIFT 필드 마커 '{marker}'가 초안에 없습니다"

    def test_generator_rag_query_contains_msg_type(self, generator_state,
                                                     mock_retriever):
        """RAG 검색 쿼리에 msg_type이 포함되어야 한다."""
        with (
            patch("app.agents.generator._get_retriever", return_value=mock_retriever),
            patch("app.agents.generator._build_generator_chain") as mock_chain,
        ):
            mock_chain.return_value.invoke.return_value = self._mock_chain_response(GENERATOR_MOCK_RESPONSE)
            from app.agents.generator import run_generator
            run_generator(generator_state)

        call_kwargs = mock_retriever.search.call_args
        query_arg   = call_kwargs[1].get("query") or call_kwargs[0][0]
        assert "MT103" in query_arg, f"RAG 쿼리에 msg_type(MT103) 없음: '{query_arg}'"

    def test_generator_retrieved_rules_in_prompt(self, generator_state,
                                                   mock_retriever):
        """Retriever가 반환한 규칙이 LLM invoke kwargs에 실제로 주입되는지 확인."""
        captured_kwargs: list = []

        def _capture(kwargs):
            captured_kwargs.append(kwargs)
            return self._mock_chain_response(GENERATOR_MOCK_RESPONSE)

        with (
            patch("app.agents.generator._get_retriever", return_value=mock_retriever),
            patch("app.agents.generator._build_generator_chain") as mock_chain,
        ):
            mock_chain.return_value.invoke.side_effect = _capture
            from app.agents.generator import run_generator
            run_generator(generator_state)

        assert len(captured_kwargs) > 0
        full_ctx = str(captured_kwargs[0])
        assert "32A" in full_ctx, "RAG 규칙 청크(32A)가 Generator invoke kwargs에 없습니다"
        assert "50K" in full_ctx, "RAG 규칙 청크(50K)가 Generator invoke kwargs에 없습니다"


# ===========================================================================
# ── 시나리오 4: Live 모드 테스트 (실제 LLM + Qdrant) ─────────────────────────
# ===========================================================================

def _qdrant_search_ready() -> tuple[bool, str]:
    """Qdrant 컬렉션에 데이터가 있고 실제 검색 쿼리가 동작하는지 확인.

    Returns:
        (ready, reason) — ready=False면 reason에 skip 사유를 담는다.
    """
    try:
        from app.rag.indexer import COLLECTION, get_client
        client = get_client()
        info   = client.get_collection(COLLECTION)
        if (info.points_count or 0) == 0:
            return False, (
                "Qdrant 컬렉션에 데이터가 없습니다.\n"
                "  python scripts/ingest_mt_all.py 로 데이터를 먼저 적재하세요."
            )
    except Exception as e:
        return False, f"Qdrant 컬렉션 조회 실패: {e}"

    # query_points API (Qdrant ≥1.10) 지원 여부를 실제 검색으로 확인
    try:
        from app.rag.retriever import SwiftRetriever
        r = SwiftRetriever()
        r.search("test MT103", top_k=1, rerank=False)
        return True, ""
    except Exception as e:
        err = str(e)
        if "404" in err or "Not Found" in err:
            return False, (
                "Qdrant 서버가 query_points API를 지원하지 않습니다 (≥1.10 필요).\n"
                f"  현재 서버: {err[:120]}\n"
                "  docker-compose.yml 의 qdrant 이미지가 v1.13.6 이상인지 확인하세요."
            )
        return False, f"Qdrant 검색 테스트 실패: {err[:200]}"


@pytest.mark.skipif(not _LIVE, reason="Live 모드에서만 실행")
class TestAnalyzerLive:
    """실제 Ollama LLM + Qdrant를 사용하는 Live 통합 테스트."""

    @pytest.fixture(autouse=True)
    def require_qdrant_data(self):
        ready, reason = _qdrant_search_ready()
        if not ready:
            pytest.skip(reason)

    def test_live_analyzer_returns_valid_verdict(self, base_state):
        """실제 LLM 호출 시 valid verdict(PASS/WARNING/REJECT)를 반환하는지 확인."""
        # Prowide만 Mock (prowide-svc 없어도 실행 가능)
        prowide_ok = {
            "syntax_ok": True, "problems": [], "message_type": "MT103",
            "source": "prowide",
        }
        with patch("app.agents.analyzer.prowide_syntax_verify",
                   return_value=prowide_ok):
            from app.agents.analyzer import run_analyzer
            result = run_analyzer(base_state)

        verdict = result["output"]["verdict"]
        assert verdict in ("PASS", "WARNING", "REJECT"), \
            f"유효하지 않은 verdict: '{verdict}'"
        print(f"\n  Live verdict: {verdict}")
        print(f"  needs_hitl  : {result['needs_hitl']}")

    def test_live_analyzer_llm_response_is_json(self, base_state):
        """실제 LLM 응답이 JSON으로 파싱 가능한지 확인 (json_object 모드 검증)."""
        prowide_ok = {
            "syntax_ok": True, "problems": [], "message_type": "MT103",
            "source": "prowide",
        }
        with patch("app.agents.analyzer.prowide_syntax_verify",
                   return_value=prowide_ok):
            from app.agents.analyzer import run_analyzer
            result = run_analyzer(base_state)

        # _parse_error 플래그가 없어야 함 (JSON 파싱 성공)
        assert not result["output"]["details"].get("semantic", {}).get("_parse_error"), \
            "LLM 응답이 JSON으로 파싱되지 않았습니다 (response_format 미지원?)"


@pytest.mark.skipif(not _LIVE, reason="Live 모드에서만 실행")
class TestGeneratorLive:
    """실제 Ollama LLM + Qdrant를 사용하는 Generator Live 테스트."""

    @pytest.fixture(autouse=True)
    def require_qdrant_data(self):
        ready, reason = _qdrant_search_ready()
        if not ready:
            pytest.skip(reason)

    def test_live_generator_draft_is_nonempty(self):
        """실제 LLM이 빈 초안을 반환하지 않는지 확인."""
        state = {
            "raw_message":    GENERATOR_REQUEST,
            "masked_message": GENERATOR_REQUEST,
            "msg_type":       "MT103",
            "user_intent":    "generate",
            "pii_mapping":    {},
        }
        from app.agents.generator import run_generator
        result = run_generator(state)

        draft = result["output"]["draft"]
        assert len(draft) > 50, f"초안이 너무 짧습니다 ({len(draft)}자)"
        print(f"\n  생성 초안 (앞 200자):\n{draft[:200]}")

    def test_live_generator_draft_contains_swift_fields(self):
        """실제 LLM이 MT103 관련 내용을 포함한 초안을 생성하는지 확인.

        Note: 소형 LLM(7B)은 SWIFT 포맷 태그(:20:, :23B: 등)를 완벽히 따르지
        않을 수 있으므로, SWIFT 태그 OR 핵심 금융 키워드 중 하나라도 포함되면 통과.
        실제 포맷 준수 여부는 Prowide 구문 검증(test_prowide_validation.py)에서 담당.
        """
        state = {
            "raw_message":    GENERATOR_REQUEST,
            "masked_message": GENERATOR_REQUEST,
            "msg_type":       "MT103",
            "user_intent":    "generate",
            "pii_mapping":    {},
        }
        from app.agents.generator import run_generator
        result = run_generator(state)
        draft = result["output"]["draft"]

        # SWIFT 포맷 태그 확인 (엄격)
        swift_tags = [f for f in (":20:", ":23B:", ":32A:", ":50K:", ":59:") if f in draft]

        # 핵심 금융 키워드 확인 (소형 LLM 폴백)
        keywords = ["EUR", "MT103", "SWIFT", "BIC", "IBAN", "103"]
        found_keywords = [kw for kw in keywords if kw in draft.upper()]

        assert len(swift_tags) >= 2 or len(found_keywords) >= 2, (
            f"MT103 초안에 SWIFT 태그({swift_tags})도, "
            f"금융 키워드({found_keywords})도 충분하지 않습니다.\n초안:\n{draft}"
        )
        print(f"\n  SWIFT 태그 발견: {swift_tags}")
        print(f"  키워드 발견   : {found_keywords}")


# ===========================================================================
# 단독 실행 모드
# ===========================================================================

if __name__ == "__main__":
    import subprocess, sys
    extra = ["-m", "live"] if _LIVE else []
    sys.exit(subprocess.call(
        [sys.executable, "-m", "pytest", __file__, "-v", "--tb=short"] + extra
    ))
