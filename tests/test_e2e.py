"""
E2E 통합 테스트 — LangGraph 파이프라인 종단간 검증.

실제 그래프 코드(PII 마스킹·언마스킹, 의도 분류, 에이전트 실행,
HITL 중단·재개, 감사 기록)를 그대로 실행하고,
외부 I/O(Prowide HTTP · vLLM · Qdrant)만 경량 패치로 대체한다.

커버하는 시나리오:
  1.  MT103 분석 — PASS 경로 (HITL 없이 완료)
  2.  MT103 분석 — REJECT → HITL 중단 → 검수자 승인 재개
  3.  MT103 분석 — REJECT → HITL 중단 → 검수자 거부 재개
  4.  MT103 → pacs.008 업리프트(Mapper, degraded)
  5.  자연어 전문 생성(Generator) — 항상 HITL 발동
  6.  PII 마스킹 라운드트립 — IBAN·BIC·금액 LLM 미노출 검증
  7.  user_intent 없이 키워드 기반 자동 라우팅
  8.  Prowide 서비스 다운(degraded) → fail-safe HITL
  9.  MX 전문 분석 — /validate/mx 엔드포인트 호출 확인
"""
from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.graph.graph import build_graph
from app.graph.state import AgentState
from app.rag.chunker import SwiftChunk


# ──────────────────────────────────────────────────────────────────────────────
# 샘플 SWIFT 전문
# ──────────────────────────────────────────────────────────────────────────────

MT103_VALID = """\
{1:F01BNKBKRSEAXXX0000000000}
{2:I103DEUTDEDBXXXXN}
{4:
:20:TXREF20240115001
:23B:CRED
:32A:240115EUR5000,00
:50K:/DE89370400440532013000
MUELLER HANS
HAUPTSTRASSE 1
10115 BERLIN
:52A:BNKBKRSEA
:59:/FR7630006000011234567890189
DUPONT MARIE
12 RUE DE LA PAIX
75001 PARIS
:70:INVOICE 2024-001
:71A:SHA
-}"""

MT103_SYNTAX_ERROR = """\
{1:F01BNKBKRSEAXXX0000000000}
{2:I103DEUTDEDBXXXXN}
{4:
:20:TXREF20240115002
:23B:CRED
:32A:BADDATE EUR9999,99
:50K:MISSING_ACCOUNT
-}"""

MT103_FOR_MAP = """\
{1:F01BNKBKRSEAXXX0000000000}
{2:I103DEUTDEDBXXXXN}
{4:
:20:TXREF20240115003
:23B:CRED
:32A:240115USD10000,00
:50K:/US33CHASUS33XXXXXXXXXXXXXX
SENDER CORP
:59:/GB29NWBK60161331926819
RECEIVER LTD
:71A:SHA
-}"""

GENERATE_REQUEST = "MT103 전문 생성 generate 요청: 송금인 홍길동, 수취인 김영희, 금액 USD 5000,00"

MX_PACS008 = """\
<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
  <FIToFICstmrCdtTrf>
    <GrpHdr>
      <MsgId>MSG20240115001</MsgId>
      <CreDtTm>2024-01-15T10:00:00</CreDtTm>
      <NbOfTxs>1</NbOfTxs>
    </GrpHdr>
  </FIToFICstmrCdtTrf>
</Document>"""


# ──────────────────────────────────────────────────────────────────────────────
# 팩토리 헬퍼
# ──────────────────────────────────────────────────────────────────────────────

def _swift_chunk(**kwargs: Any) -> SwiftChunk:
    defaults: dict[str, Any] = dict(
        chunk_id="00000000-0000-0000-0000-000000000001",
        source_type="mt",
        level="rule",
        message_type="MT103",
        field_tag="50K",
        rule_id="C1",
        rule_type="presence",
        page=42,
        parent_id=None,
        text="MT103 field 50K Ordering Customer: mandatory unless field 52A present (C1).",
    )
    defaults.update(kwargs)
    return SwiftChunk(**defaults)


def _llm_mock(content: str) -> MagicMock:
    """chat.completions.create() 응답을 흉내내는 OpenAI 클라이언트 Mock."""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    client = MagicMock()
    client.chat.completions.create.return_value = resp
    return client


def _prowide_resp(**data: Any) -> MagicMock:
    """httpx.post() 성공 응답 Mock."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = data
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


def _new_graph():
    """테스트별 독립 MemorySaver를 가진 컴파일된 그래프."""
    return build_graph().compile(checkpointer=MemorySaver())


def _config() -> dict:
    """충돌 없는 신규 thread_id 설정."""
    return {"configurable": {"thread_id": str(uuid.uuid4())}}


# ──────────────────────────────────────────────────────────────────────────────
# Pytest fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_singletons():
    """LLM 싱글턴과 에이전트별 리트리버 싱글턴을 각 테스트 전후 초기화."""
    import app.agents.analyzer as _ana
    import app.agents.generator as _gen
    import app.agents.mapper as _map
    import app.llm as _llm

    _llm._client = None
    _ana._retriever = None
    _gen._retriever = None
    _map._retriever = None
    yield
    _llm._client = None
    _ana._retriever = None
    _gen._retriever = None
    _map._retriever = None


@pytest.fixture
def mock_retriever():
    """세 에이전트의 _get_retriever() 를 모두 고정 청크 반환 Mock으로 대체."""
    mock_ret = MagicMock()
    mock_ret.search.return_value = [_swift_chunk()]
    with (
        patch("app.agents.analyzer._get_retriever", return_value=mock_ret),
        patch("app.agents.generator._get_retriever", return_value=mock_ret),
        patch("app.agents.mapper._get_retriever", return_value=mock_ret),
    ):
        yield mock_ret


@pytest.fixture
def audit_tmp(tmp_path):
    """감사 로그를 테스트 전용 임시 파일로 리디렉션."""
    log_file = str(tmp_path / "audit.jsonl")
    with patch("app.audit.logger._LOG_PATH", log_file):
        yield log_file


# ──────────────────────────────────────────────────────────────────────────────
# 시나리오 1 — MT103 분석 · PASS (HITL 불필요)
# ──────────────────────────────────────────────────────────────────────────────

class TestAnalyzePass:

    def test_full_pipeline_pass(self, mock_retriever, audit_tmp):
        prowide = _prowide_resp(parseable=True, problems=[], messageType="MT103")
        llm_json = json.dumps({
            "verdict": "PASS",
            "violations": [],
            "warnings": [],
            "applied_conditional_rules": [
                {"rule_id": "C1", "page": 42, "triggered": False,
                 "why": "Field 33B absent — C1 not triggered"},
            ],
        })

        graph = _new_graph()
        cfg = _config()

        with (
            patch("app.validation.prowide_client.httpx.post", return_value=prowide),
            patch("app.llm.get_llm", return_value=_llm_mock(llm_json)),
        ):
            result = graph.invoke(
                {"raw_message": MT103_VALID, "msg_type": "MT103", "user_intent": "analyze"},
                config=cfg,
            )

        # 파이프라인 END 까지 완료
        assert result is not None
        assert result.get("error") is None

        # 판정
        assert result["output"]["type"] == "analysis"
        assert result["output"]["verdict"] == "PASS"

        # HITL 미발동
        assert result["needs_hitl"] is False
        assert result.get("hitl_decision") is None

        # PII 마스킹: masked_message 에 IBAN 플레이스홀더 존재
        assert "<<IBAN_" in result["masked_message"]
        assert "DE89370400440532013000" not in result["masked_message"]

        # 감사 로그 기록
        with open(audit_tmp, encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
        assert entries, "감사 로그가 비어 있음"
        last = entries[-1]
        assert last["verdict"] == "PASS"
        assert last["msg_type"] == "MT103"
        assert last["needs_hitl"] is False


# ──────────────────────────────────────────────────────────────────────────────
# 시나리오 2 — MT103 분석 · REJECT → HITL 중단 → 검수자 승인
# ──────────────────────────────────────────────────────────────────────────────

class TestAnalyzeHitlApprove:

    def test_interrupt_and_resume_approve(self, mock_retriever, audit_tmp):
        prowide = _prowide_resp(
            parseable=False,
            problems=[{"code": "F50K", "desc": "Field 50K missing or malformed"}],
            messageType=None,
        )
        llm_json = json.dumps({
            "verdict": "REJECT",
            "violations": [{"field": "50K", "issue": "Ordering customer malformed",
                            "rule_id": "C1", "page": 42}],
            "warnings": [],
            "applied_conditional_rules": [],
        })

        graph = _new_graph()
        cfg = _config()

        with (
            patch("app.validation.prowide_client.httpx.post", return_value=prowide),
            patch("app.llm.get_llm", return_value=_llm_mock(llm_json)),
        ):
            graph.invoke(
                {"raw_message": MT103_SYNTAX_ERROR, "msg_type": "MT103", "user_intent": "analyze"},
                config=cfg,
            )

        # HITL interrupt 확인
        snapshot = graph.get_state(cfg)
        assert snapshot.next, "REJECT 판정 후 HITL interrupt 가 발동돼야 함"

        # 검수자 승인으로 재개
        with (
            patch("app.validation.prowide_client.httpx.post", return_value=prowide),
            patch("app.llm.get_llm", return_value=_llm_mock(llm_json)),
        ):
            final = graph.invoke(
                Command(resume={"action": "approve", "comment": "Manual review passed"}),
                config=cfg,
            )

        assert final["hitl_decision"] == "approve"
        assert final["hitl_comment"] == "Manual review passed"
        assert final.get("output", {}).get("status") != "rejected"

        # 감사 로그
        with open(audit_tmp, encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
        last = entries[-1]
        assert last["hitl_decision"] == "approve"
        assert last["verdict"] == "REJECT"   # reconcile 판정은 REJECT 유지


# ──────────────────────────────────────────────────────────────────────────────
# 시나리오 3 — MT103 분석 · HITL 중단 → 검수자 거부
# ──────────────────────────────────────────────────────────────────────────────

class TestAnalyzeHitlReject:

    def test_interrupt_and_resume_reject(self, mock_retriever, audit_tmp):
        prowide = _prowide_resp(
            parseable=False,
            problems=[{"code": "F32A", "desc": "Invalid date in field 32A"}],
            messageType=None,
        )
        llm_json = json.dumps({
            "verdict": "REJECT",
            "violations": [{"field": "32A", "issue": "Invalid date format",
                            "rule_id": None, "page": 55}],
            "warnings": [],
            "applied_conditional_rules": [],
        })

        graph = _new_graph()
        cfg = _config()

        with (
            patch("app.validation.prowide_client.httpx.post", return_value=prowide),
            patch("app.llm.get_llm", return_value=_llm_mock(llm_json)),
        ):
            graph.invoke(
                {"raw_message": MT103_SYNTAX_ERROR, "msg_type": "MT103", "user_intent": "analyze"},
                config=cfg,
            )

        snapshot = graph.get_state(cfg)
        assert snapshot.next

        final = graph.invoke(
            Command(resume={"action": "reject", "comment": "Cannot process — critical errors"}),
            config=cfg,
        )

        assert final["hitl_decision"] == "reject"
        output = final["output"]
        assert output["status"] == "rejected"
        assert "Cannot process" in output["reason"]

        with open(audit_tmp, encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
        assert entries[-1]["hitl_decision"] == "reject"


# ──────────────────────────────────────────────────────────────────────────────
# 시나리오 4 — MT103 → pacs.008 업리프트 (Mapper · degraded)
# ──────────────────────────────────────────────────────────────────────────────

class TestMapperUplift:

    def test_mt_to_mx_degraded_triggers_hitl(self, mock_retriever, audit_tmp):
        translate_resp = _prowide_resp(
            ok=False, degraded=True, content="", error="not-implemented"
        )
        llm_json = json.dumps({
            "enhanced_message": (
                '<?xml version="1.0"?>'
                '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">'
                "<FIToFICstmrCdtTrf><GrpHdr>"
                "<MsgId>TXREF20240115003</MsgId>"
                "</GrpHdr></FIToFICstmrCdtTrf></Document>"
            ),
            "unmapped_fields": ["52A"],
            "enhancement_warnings": ["Field 52A (InstrAgent) — manual mapping required"],
        })

        graph = _new_graph()
        cfg = _config()

        with (
            patch("app.validation.prowide_client.httpx.post", return_value=translate_resp),
            patch("app.llm.get_llm", return_value=_llm_mock(llm_json)),
        ):
            graph.invoke(
                {"raw_message": MT103_FOR_MAP, "msg_type": "MT103", "user_intent": "map"},
                config=cfg,
            )

        # degraded → needs_hitl=True → 중단 확인
        snapshot = graph.get_state(cfg)
        assert snapshot.next, "degraded 매핑은 HITL 중단이 필요함"

        # 검수자 승인 재개
        with (
            patch("app.validation.prowide_client.httpx.post", return_value=translate_resp),
            patch("app.llm.get_llm", return_value=_llm_mock(llm_json)),
        ):
            final = graph.invoke(
                Command(resume={"action": "approve", "comment": "Mapper output verified"}),
                config=cfg,
            )

        output = final["output"]
        assert output["type"] == "mapped_message"
        assert output["direction"] == "mt_to_mx"
        assert "Document" in output["enhanced"]
        assert "52A" in output["unmapped_fields"]
        assert final["hitl_decision"] == "approve"

        with open(audit_tmp, encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
        assert entries[-1]["hitl_decision"] == "approve"


# ──────────────────────────────────────────────────────────────────────────────
# 시나리오 5 — 전문 생성 (Generator · 항상 HITL)
# ──────────────────────────────────────────────────────────────────────────────

class TestGenerator:

    def test_generate_always_triggers_hitl(self, mock_retriever, audit_tmp):
        draft = """\
{1:F01BNKBKRSEAXXX0000000000}
{2:I103DEUTDEDBXXXXN}
{4:
:20:GEN20240115001
:23B:CRED
:32A:240115USD5000,00
:50K:/GENERATED_ACCOUNT
HONG GILDONG
:59:/GENERATED_ACCOUNT2
KIM YOUNGHEE
:71A:SHA
-}"""

        graph = _new_graph()
        cfg = _config()

        with patch("app.llm.get_llm", return_value=_llm_mock(draft)):
            graph.invoke(
                {"raw_message": GENERATE_REQUEST, "msg_type": "MT103", "user_intent": "generate"},
                config=cfg,
            )

        # Generator는 항상 HITL 발동
        snapshot = graph.get_state(cfg)
        assert snapshot.next, "Generator 결과는 항상 사람 검수 필요"

        with patch("app.llm.get_llm", return_value=_llm_mock(draft)):
            final = graph.invoke(
                Command(resume={"action": "modify", "comment": "Accepted with edits"}),
                config=cfg,
            )

        assert final["output"]["type"] == "generated_message"
        assert len(final["output"]["draft"]) > 0
        assert final["hitl_decision"] == "modify"

        with open(audit_tmp, encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
        assert entries[-1]["hitl_decision"] == "modify"


# ──────────────────────────────────────────────────────────────────────────────
# 시나리오 6 — PII 마스킹 라운드트립
# ──────────────────────────────────────────────────────────────────────────────

class TestPiiRoundtrip:
    """IBAN · 금액이 LLM 프롬프트에 노출되지 않고 pii_mapping 에 보존되는지 검증."""

    _MT = """\
{1:F01BNKBKRSEAXXX0000000000}
{2:I103DEUTDEDBXXXXN}
{4:
:20:PIICHK001
:23B:CRED
:32A:240115EUR1234,56
:50K:/DE89370400440532013000
TEST SENDER
:59:/GB29NWBK60161331926819
TEST RECEIVER
:71A:SHA
-}"""

    def test_pii_not_exposed_to_llm(self, mock_retriever, audit_tmp):
        captured: list[str] = []

        def _fake_create(**kwargs: Any):
            for msg in kwargs.get("messages", []):
                if msg.get("role") == "user":
                    captured.append(msg["content"])
            llm_resp_json = json.dumps({
                "verdict": "PASS", "violations": [], "warnings": [],
                "applied_conditional_rules": [],
            })
            m = MagicMock(); m.content = llm_resp_json
            c = MagicMock(); c.message = m
            r = MagicMock(); r.choices = [c]
            return r

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = _fake_create

        prowide = _prowide_resp(parseable=True, problems=[], messageType="MT103")
        graph = _new_graph()
        cfg = _config()

        with (
            patch("app.validation.prowide_client.httpx.post", return_value=prowide),
            patch("app.llm.get_llm", return_value=mock_client),
        ):
            result = graph.invoke(
                {"raw_message": self._MT, "msg_type": "MT103", "user_intent": "analyze"},
                config=cfg,
            )

        assert captured, "LLM이 한 번 이상 호출되어야 함"

        # LLM 입력에 원본 PII 미노출
        for llm_input in captured:
            assert "DE89370400440532013000" not in llm_input, \
                "원본 송금인 IBAN 이 LLM 프롬프트에 노출됨"
            assert "GB29NWBK60161331926819" not in llm_input, \
                "원본 수취인 IBAN 이 LLM 프롬프트에 노출됨"
            assert "1234,56" not in llm_input, \
                "원본 금액이 LLM 프롬프트에 노출됨"

        # masked_message 에 플레이스홀더 존재
        masked = result["masked_message"]
        assert "<<IBAN_" in masked
        assert "<<AMT_" in masked

        # pii_mapping 에 원본값 보존
        mapping: dict[str, str] = result["pii_mapping"]
        assert any("DE89370400440532013000" in v for v in mapping.values()), \
            "pii_mapping 에 송금인 IBAN 이 없음"
        assert any("GB29NWBK60161331926819" in v for v in mapping.values()), \
            "pii_mapping 에 수취인 IBAN 이 없음"

        # raw_message 가 state 에 보존됨
        assert result["raw_message"] == self._MT


# ──────────────────────────────────────────────────────────────────────────────
# 시나리오 7 — user_intent 없이 키워드 기반 자동 라우팅
# ──────────────────────────────────────────────────────────────────────────────

class TestIntentRouting:

    @pytest.mark.parametrize("raw,expected_agent,prowide_data,llm_content", [
        (
            "MT103 전문 validate 검증해줘: :20:REF001 :32A:240115EUR100,00",
            "analyzer",
            {"parseable": True, "problems": [], "messageType": "MT103"},
            json.dumps({"verdict": "PASS", "violations": [], "warnings": [],
                        "applied_conditional_rules": []}),
        ),
        (
            "MT103 전문 생성 generate 해줘 — 송금인 테스트",
            "generator",
            {"parseable": True, "problems": [], "messageType": "MT103"},
            "Generated MT103 draft",
        ),
        (
            "MT103 전문 map 변환 convert 해줘: :20:REF002",
            "mapper",
            {"ok": False, "degraded": True, "content": "", "error": "not-implemented"},
            json.dumps({"enhanced_message": "<Document/>",
                        "unmapped_fields": [], "enhancement_warnings": []}),
        ),
    ])
    def test_keyword_routing(
        self,
        raw: str,
        expected_agent: str,
        prowide_data: dict,
        llm_content: str,
        mock_retriever,
        audit_tmp,
    ):
        graph = _new_graph()
        cfg = _config()

        prowide = _prowide_resp(**prowide_data)

        with (
            patch("app.validation.prowide_client.httpx.post", return_value=prowide),
            patch("app.llm.get_llm", return_value=_llm_mock(llm_content)),
        ):
            # user_intent 미설정 — supervisor 가 키워드 분류
            result = graph.invoke(
                {"raw_message": raw, "msg_type": "MT103"},
                config=cfg,
            )

        assert result["routed_agent"] == expected_agent, (
            f"expected '{expected_agent}', got '{result.get('routed_agent')}'"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 시나리오 8 — Prowide 서비스 다운 → fail-safe HITL
# ──────────────────────────────────────────────────────────────────────────────

class TestProwideDown:

    def test_connection_error_triggers_hitl(self, mock_retriever, audit_tmp):
        import httpx as _httpx

        llm_json = json.dumps({
            "verdict": "PASS", "violations": [], "warnings": [],
            "applied_conditional_rules": [],
        })

        graph = _new_graph()
        cfg = _config()

        with (
            patch(
                "app.validation.prowide_client.httpx.post",
                side_effect=_httpx.ConnectError("connection refused"),
            ),
            patch("app.llm.get_llm", return_value=_llm_mock(llm_json)),
        ):
            graph.invoke(
                {"raw_message": MT103_VALID, "msg_type": "MT103", "user_intent": "analyze"},
                config=cfg,
            )

        # degraded=True → reconcile → needs_hitl=True → 중단
        snapshot = graph.get_state(cfg)
        assert snapshot.next, "Prowide 장애 시 HITL 로 중단돼야 함"

        # state 에 degraded 플래그 확인
        vr = snapshot.values.get("validation_result", {})
        assert vr.get("rule_engine", {}).get("degraded") is True, \
            "rule_engine.degraded 가 True 여야 함"


# ──────────────────────────────────────────────────────────────────────────────
# 시나리오 9 — MX 전문 분석 → /validate/mx 엔드포인트 호출
# ──────────────────────────────────────────────────────────────────────────────

class TestMxAnalysis:

    def test_mx_uses_validate_mx_endpoint(self, mock_retriever, audit_tmp):
        prowide = _prowide_resp(
            parseable=True, problems=[], messageType="pacs.008.001.08"
        )
        llm_json = json.dumps({
            "verdict": "PASS", "violations": [], "warnings": [],
            "applied_conditional_rules": [],
        })

        graph = _new_graph()
        cfg = _config()

        with (
            patch(
                "app.validation.prowide_client.httpx.post",
                return_value=prowide,
            ) as mock_post,
            patch("app.llm.get_llm", return_value=_llm_mock(llm_json)),
        ):
            result = graph.invoke(
                {
                    "raw_message": MX_PACS008,
                    "msg_type":    "pacs.008.001.08",
                    "user_intent": "analyze",
                },
                config=cfg,
            )

        # /validate/mx 엔드포인트가 호출됐는지 확인
        called_urls = [str(call.args[0]) for call in mock_post.call_args_list]
        assert any("/validate/mx" in url for url in called_urls), (
            f"/validate/mx 호출 없음. 실제 호출: {called_urls}"
        )

        assert result["output"]["verdict"] == "PASS"
        assert result["needs_hitl"] is False

        with open(audit_tmp, encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
        assert entries[-1]["msg_type"] == "pacs.008.001.08"
