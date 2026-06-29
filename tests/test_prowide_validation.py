"""
Prowide 통합 검증 테스트.

Java prowide-svc (포트 8080) 와 Python prowide_client.py 연동이
실제로 올바르게 동작하는지 확인한다.

─────────────────────────────────────────────────
실행 전 준비
─────────────────────────────────────────────────
  # 1) Java 서버 기동 확인
  docker compose ps prowide-svc          # Status: healthy 이어야 함
  curl http://localhost:8080/actuator/health

  # 2) 의존성 확인
  pip install httpx pytest

  # 3-a) pytest 실행 (결과 요약)
  pytest tests/test_prowide_validation.py -v

  # 3-b) 단독 실행 (상세 출력 + ANSI 컬러)
  python tests/test_prowide_validation.py

  # 3-c) MT101 시나리오만 실행
  pytest tests/test_prowide_validation.py -v -k "MT101"

환경변수:
  PROWIDE_URL  Prowide 서버 주소 (기본: http://localhost:8080)

─────────────────────────────────────────────────
포트 설정 확인
─────────────────────────────────────────────────
  서비스          | 호스트 포트 | 용도
  --------------- | ----------- | -------------------------
  prowide-svc     | 8080        | MT/MX 검증·파싱 API
  qdrant          | 6333        | 벡터 DB (RAG)
  swift-agent     | 8000        | Python FastAPI 메인 앱

  로컬 직접 실행 시: PROWIDE_URL=http://localhost:8080
  Docker 컨테이너 내부: PROWIDE_URL=http://prowide-svc:8080
─────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

import httpx
import pytest

# ── 프로젝트 루트를 sys.path에 추가 ────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── PROWIDE_URL: 환경변수 없으면 localhost 직접 호출 ────────────────────────
PROWIDE_URL = os.getenv("PROWIDE_URL", "http://localhost:8080")


# ===========================================================================
# 샘플 전문 상수
# ===========================================================================

# ───────────────────────────────────────────────────────────────────────────
# MT101  Request for Transfer
# ───────────────────────────────────────────────────────────────────────────
# 구조:
#   Sequence A (단일 발생, 필수):
#     :20:  Sender's Reference           (M, 16x)
#     :28D: Message Index/Total          (M, 5n/5n)
#     :30:  Requested Execution Date     (M, YYMMDD)
#     :50a: Ordering Customer            (M, option C/F/G/H/L)
#     :52a: Account Servicing Institution(O, option A/C/G)
#   Sequence B (반복 발생, 최소 1회 필수):
#     :21:  Transaction Reference        (M, 16x)
#     :32B: Currency/Amount              (M, 3!a 15d)  ← MT103의 :32A: 와 달리 날짜 없음
#     :57a: Account With Institution     (O, option A/B/C/D)
#     :59a: Beneficiary Customer         (M, option none/A/F)
#     :71A: Details of Charges           (O, 3!a)
#
# Java ValidationService 필수 필드 맵 (v2+):
#   "101": ["20", "28D", "30", "21", "32B"]
# ───────────────────────────────────────────────────────────────────────────

# ── 시나리오 A: 유효한 MT101 ─────────────────────────────────────────────────
# SWIFT MT101 Request for Transfer
# - Sequence A + Sequence B 완전 포함
# - 모든 필수 필드(20 / 28D / 30 / 21 / 32B) 포함
# - 32B = 3자 통화 + 쉼표 소수점 금액 (날짜 없음)
# - 52A / 57A BIC 유효 형식
MT101_VALID = textwrap.dedent("""\
    {1:F01BNPAFRPPXXXX0000000000}
    {2:O1010900240115DEUTDEFFXXXX00000000002401150900N}
    {4:
    :20:REF-MT101-001
    :28D:1/1
    :30:240115
    :50H:/DE89370400440532013000
    ORDERING CUSTOMER NAME
    123 SENDER STREET
    BERLIN GERMANY
    :52A:DEUTDEFFXXX
    :21:TRX-B-001
    :32B:EUR10000,00
    :57A:BNPAFRPPXXX
    :59:/FR7630006000011234567890189
    BENEFICIARY CORP
    456 RECEIVER AVE
    PARIS FRANCE
    :71A:SHA
    -}""")

# ── 시나리오 B-1: 필수 필드 누락 MT101 (28D / 32B 없음) ─────────────────────
MT101_MISSING_FIELDS = textwrap.dedent("""\
    {1:F01BNPAFRPPXXXX0000000000}
    {2:O1010900240115DEUTDEFFXXXX00000000002401150900N}
    {4:
    :20:REF-MT101-002
    :30:240115
    :50H:/DE89370400440532013000
    ORDERING CUSTOMER NAME
    :52A:DEUTDEFFXXX
    :21:TRX-B-001
    :57A:BNPAFRPPXXX
    :59:/FR7630006000011234567890189
    BENEFICIARY NAME
    :71A:SHA
    -}""")

# ── 시나리오 B-2: 포맷 오류 MT101 ────────────────────────────────────────────
# - :20: 16자 초과 → FMT_20
# - :32B: 금액 부분에 문자 포함 → FMT_AMT
# - :57A: BIC 형식 오류 → FMT_BIC
MT101_FORMAT_ERRORS = textwrap.dedent("""\
    {1:F01BNPAFRPPXXXX0000000000}
    {2:O1010900240115DEUTDEFFXXXX00000000002401150900N}
    {4:
    :20:REF-THAT-IS-WAY-TOO-LONG-FOR-SWIFT
    :28D:1/1
    :30:240115
    :50H:/DE89370400440532013000
    ORDERING CUSTOMER
    :52A:DEUTDEFFXXX
    :21:TRX-B-001
    :32B:XYZ123ABC,45
    :57A:INVALID_BIC_FORMAT
    :59:/FR7630006000011234567890189
    BENEFICIARY NAME
    :71A:SHA
    -}""")

# ── 시나리오 B-3: 중복 태그 MT101 (:20: 두 번) ───────────────────────────────
MT101_DUPLICATE_TAG = textwrap.dedent("""\
    {1:F01BNPAFRPPXXXX0000000000}
    {2:O1010900240115DEUTDEFFXXXX00000000002401150900N}
    {4:
    :20:REF-MT101-004
    :20:REF-MT101-DUPLICATE
    :28D:1/1
    :30:240115
    :50H:/DE89370400440532013000
    ORDERING CUSTOMER
    :21:TRX-B-001
    :32B:EUR5000,00
    :59:/FR7630006000011234567890189
    BENEFICIARY NAME
    -}""")

# ── 시나리오 B-4: 완전히 깨진 전문 ──────────────────────────────────────────
MT101_BROKEN = "THIS IS NOT A SWIFT MESSAGE AT ALL !!!"

# ── 시나리오 A: 유효한 MT103 ─────────────────────────────────────────────────
# SWIFT MT103 Single Customer Credit Transfer
# - 모든 필수 필드(20 / 23B / 32A / 50K / 59) 포함
# - 필드 포맷 규격 준수: 32A = YYMMDD + 3자 통화 + 쉼표 소수점 금액
MT103_VALID = textwrap.dedent("""\
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

# ── 시나리오 B-1: 필수 필드 누락 MT103 (50K / 59 없음) ──────────────────────
MT103_MISSING_FIELDS = textwrap.dedent("""\
    {1:F01BNPAFRPPXXXX0000000000}
    {2:O1030900240115DEUTDEFFXXXX00000000002401150900N}
    {4:
    :20:REF20240115002
    :23B:CRED
    :32A:240115EUR5000,00
    -}""")

# ── 시나리오 B-2: 포맷 오류 MT103 (32A 날짜 오류 + Tag 20 길이 초과) ─────────
MT103_FORMAT_ERRORS = textwrap.dedent("""\
    {1:F01BNPAFRPPXXXX0000000000}
    {2:O1030900240115DEUTDEFFXXXX00000000002401150900N}
    {4:
    :20:REF-THAT-IS-WAY-TOO-LONG-FOR-SWIFT
    :23B:CRED
    :32A:BADDATE_EUR9999,99
    :50K:/DE89370400440532013000
    ORDERING CUSTOMER
    :59:/FR7630006000011234567890189
    BENEFICIARY NAME
    :71A:SHA
    -}""")

# ── 시나리오 B-3: 완전히 깨진 전문 ──────────────────────────────────────────
MT103_BROKEN = "THIS IS NOT A SWIFT MESSAGE AT ALL !!!"

# ── 시나리오 C: 유효한 pacs.008 MX XML ──────────────────────────────────────
# ISO 20022 FIToFICustomerCreditTransfer (pacs.008.001.08) 최소 구조
# Java 서버는 현재 XML 정형성(well-formed) 검사만 수행
MX_PACS008_VALID = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
      <FIToFICstmrCdtTrf>
        <GrpHdr>
          <MsgId>MSGID20240115001</MsgId>
          <CreDtTm>2024-01-15T09:00:00</CreDtTm>
          <NbOfTxs>1</NbOfTxs>
          <SttlmInf>
            <SttlmMtd>CLRG</SttlmMtd>
          </SttlmInf>
        </GrpHdr>
        <CdtTrfTxInf>
          <PmtId>
            <EndToEndId>E2EIDREF001</EndToEndId>
            <TxId>TXID20240115001</TxId>
          </PmtId>
          <IntrBkSttlmAmt Ccy="EUR">10000.00</IntrBkSttlmAmt>
          <IntrBkSttlmDt>2024-01-15</IntrBkSttlmDt>
          <Dbtr>
            <Nm>ORDERING CUSTOMER NAME</Nm>
          </Dbtr>
          <DbtrAgt>
            <FinInstnId>
              <BICFI>DEUTDEFFXXX</BICFI>
            </FinInstnId>
          </DbtrAgt>
          <CdtrAgt>
            <FinInstnId>
              <BICFI>BNPAFRPPXXX</BICFI>
            </FinInstnId>
          </CdtrAgt>
          <Cdtr>
            <Nm>BENEFICIARY CORP</Nm>
          </Cdtr>
          <CdtrAcct>
            <Id>
              <IBAN>FR7630006000011234567890189</IBAN>
            </Id>
          </CdtrAcct>
        </CdtTrfTxInf>
      </FIToFICstmrCdtTrf>
    </Document>""")

# ── 시나리오 C-2: 깨진 MX XML (닫는 태그 누락) ──────────────────────────────
MX_PACS008_BROKEN_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
      <FIToFICstmrCdtTrf>
        <GrpHdr>
          <MsgId>MSGID-BROKEN</MsgId>
        <!-- 닫는 태그 의도적 누락 -->
    """)


# ===========================================================================
# 출력 헬퍼
# ===========================================================================

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
GRAY   = "\033[90m"


def _color(text: str, code: str) -> str:
    """ANSI 컬러 적용 (pytest 캡처 환경에서도 안전)."""
    if not sys.stdout.isatty():
        return text
    return f"{code}{text}{RESET}"


def _section(title: str) -> None:
    bar = "─" * 64
    print(f"\n{_color(bar, CYAN)}")
    print(f"  {_color(title, BOLD)}")
    print(f"{_color(bar, CYAN)}")


def _print_message(label: str, content: str, max_lines: int = 20) -> None:
    lines = content.strip().splitlines()
    print(f"\n{_color(f'  ▶ {label}', BOLD)}")
    for i, line in enumerate(lines):
        if i >= max_lines:
            print(f"  {_color(f'  ... ({len(lines) - max_lines}줄 생략)', GRAY)}")
            break
        print(f"  {_color('  │', GRAY)} {line}")


def _print_response(resp: dict[str, Any]) -> None:
    pretty = json.dumps(resp, ensure_ascii=False, indent=2)
    print(f"\n{_color('  ◀ Java 서버 응답', BOLD)}")
    for line in pretty.splitlines():
        print(f"    {line}")


def _print_result(ok: bool, detail: str = "") -> None:
    icon  = _color("  ✓ PASS", GREEN) if ok else _color("  ✗ FAIL", RED)
    print(f"\n{icon}  {detail}")


def _print_client_result(label: str, result: dict[str, Any]) -> None:
    """prowide_client 래퍼 결과를 보기 좋게 출력."""
    syntax_ok = result.get("syntax_ok", False)
    degraded  = result.get("degraded", False)
    problems  = result.get("problems", [])
    print(f"\n{_color(f'  ◀ prowide_client 결과 ({label})', BOLD)}")
    print(f"    syntax_ok : {_color(str(syntax_ok), GREEN if syntax_ok else RED)}")
    print(f"    msg_type  : {result.get('message_type', '—')}")
    if degraded:
        print(f"    {_color('    ⚠ degraded (서버 연결 실패)', YELLOW)}")
    if problems:
        print(f"    problems  ({len(problems)}개):")
        for p in problems:
            code  = p.get("code", "?")
            field = p.get("field", "")
            desc  = p.get("desc", "")
            field_str = f"[:{field}:]" if field else ""
            print(f"      - {_color(code, YELLOW)} {field_str}  {desc}")


# ===========================================================================
# HTTP 헬퍼 (pytest fixture / 단독 실행 공용)
# ===========================================================================

def _post(endpoint: str, content: str) -> dict[str, Any]:
    """Java 서버에 직접 POST 요청."""
    url = f"{PROWIDE_URL}{endpoint}"
    resp = httpx.post(url, json={"content": content}, timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def _check_server() -> bool:
    try:
        r = httpx.get(f"{PROWIDE_URL}/actuator/health", timeout=3.0)
        return r.status_code == 200 and r.json().get("status") == "UP"
    except httpx.HTTPError:
        return False


# ===========================================================================
# pytest fixtures
# ===========================================================================

@pytest.fixture(scope="session", autouse=True)
def require_prowide_server():
    """서버가 살아있지 않으면 모든 테스트를 skip.

    prowide_client.py는 모듈 로드 시점에 PROWIDE_URL을 고정하므로
    (기본값: http://prowide-svc.internal:8080 — Docker 내부 DNS),
    로컬 실행 시 모듈 변수를 테스트 파일의 PROWIDE_URL로 덮어씁니다.
    """
    if not _check_server():
        pytest.skip(
            f"prowide-svc 서버에 연결할 수 없습니다 ({PROWIDE_URL}).\n"
            "  docker compose up -d prowide-svc 후 다시 실행하세요."
        )

    # prowide_client 모듈 변수를 현재 환경의 URL로 패치
    import app.validation.prowide_client as _pc
    _pc.PROWIDE_URL = PROWIDE_URL


# ===========================================================================
# ── 시나리오 A: 정상 MT101 검증 ──────────────────────────────────────────────
# ===========================================================================

class TestScenarioA_ValidMT101:
    """유효한 MT101(Request for Transfer) 전문이 올바르게 파싱·검증되는지 확인."""

    def test_A1_mt101_validate_parseable(self):
        """POST /validate/mt → parseable=true, problems=[], messageType=MT101."""
        resp = _post("/validate/mt", MT101_VALID)
        assert resp.get("parseable") is True, \
            f"parseable=True 기대. 응답: {resp}"
        assert resp.get("problems") == [], \
            f"problems가 비어있어야 합니다. 실제: {resp.get('problems')}"

    def test_A2_mt101_message_type(self):
        """응답 messageType이 MT101이어야 한다."""
        resp = _post("/validate/mt", MT101_VALID)
        assert resp.get("messageType") == "MT101", \
            f"messageType='MT101' 기대, 실제: {resp.get('messageType')}"

    def test_A3_mt101_parse_mandatory_fields(self):
        """POST /parse/mt → 필수 필드(20/28D/30/21/32B) 모두 추출되는지 확인."""
        resp = _post("/parse/mt", MT101_VALID)
        assert resp.get("parseable") is True, f"파싱 실패: {resp}"
        fields = {f["tag"]: f["value"] for f in resp.get("fields", [])}
        for tag in ("20", "28D", "30", "21", "32B"):
            assert tag in fields, \
                f"필드 :{tag}: 가 파싱 결과에 없습니다. 추출된 태그: {list(fields.keys())}"

    def test_A4_mt101_parse_field_values(self):
        """파싱된 필드 값이 입력 전문과 일치하는지 확인."""
        resp = _post("/parse/mt", MT101_VALID)
        fields = {f["tag"]: f["value"] for f in resp.get("fields", [])}

        # Sender's Reference
        assert "REF-MT101-001" in fields.get("20", ""), \
            f":20: 값 불일치: {fields.get('20')}"
        # Requested Execution Date
        assert "240115" in fields.get("30", ""), \
            f":30: 값 불일치: {fields.get('30')}"
        # Currency/Amount — 32B = EUR + 금액 (날짜 없음)
        assert "EUR" in fields.get("32B", ""), \
            f":32B:에 통화코드 EUR 없음: {fields.get('32B')}"
        assert "10000" in fields.get("32B", ""), \
            f":32B:에 금액 없음: {fields.get('32B')}"

    def test_A5_mt101_parse_optional_fields(self):
        """선택 필드(52A BIC, 57A BIC, 59 수취인)도 파싱되는지 확인."""
        resp = _post("/parse/mt", MT101_VALID)
        fields = {f["tag"]: f["value"] for f in resp.get("fields", [])}

        # Account Servicing Institution BIC
        assert "52A" in fields, f":52A: 파싱 안 됨. 태그: {list(fields.keys())}"
        assert "DEUTDEFFXXX" in fields.get("52A", ""), \
            f":52A: BIC 값 불일치: {fields.get('52A')}"
        # Account With Institution BIC
        assert "57A" in fields, f":57A: 파싱 안 됨"
        assert "BNPAFRPPXXX" in fields.get("57A", ""), \
            f":57A: BIC 값 불일치: {fields.get('57A')}"

    def test_A6_mt101_block1_block2_extracted(self):
        """block1(LT 주소), block2(메시지 타입 헤더) 정보가 파싱되는지 확인."""
        resp = _post("/parse/mt", MT101_VALID)
        assert resp.get("block1") is not None, "block1 없음"
        assert resp.get("block2") is not None, "block2 없음"
        # block2 Output 헤더에 "101"이 포함되어야 함
        assert "101" in resp.get("block2", ""), \
            f"block2에 메시지 타입 101 없음: {resp.get('block2')}"

    def test_A7_mt101_prowide_client_wrapper(self):
        """prowide_client.prowide_syntax_verify() 래퍼 → syntax_ok=True."""
        from app.validation.prowide_client import prowide_syntax_verify
        result = prowide_syntax_verify(MT101_VALID, "MT101")
        assert not result.get("degraded"), \
            f"서버 연결 실패(degraded). problems={result.get('problems')}"
        assert result.get("syntax_ok") is True, \
            f"syntax_ok=True 기대. 실제: {result}"


# ===========================================================================
# ── 시나리오 B: 에러 MT101 검증 ──────────────────────────────────────────────
# ===========================================================================

class TestScenarioB_ErrorMT101:
    """잘못된 MT101 전문들이 정확한 에러 코드와 함께 탐지되는지 확인."""

    def test_B1_mt101_missing_mandatory_fields(self):
        """28D / 32B 누락 → MISSING_FIELD 에러 발생."""
        resp = _post("/validate/mt", MT101_MISSING_FIELDS)
        problems = resp.get("problems", [])
        codes    = [p.get("code") for p in problems]
        assert "MISSING_FIELD" in codes, \
            f"MISSING_FIELD 코드 없음. 실제 codes={codes}\n응답={resp}"
        missing = [p.get("field") for p in problems if p.get("code") == "MISSING_FIELD"]
        assert "28D" in missing, f":28D: 누락 미감지. missing={missing}"
        assert "32B" in missing, f":32B: 누락 미감지. missing={missing}"

    def test_B2_mt101_format_error_tag20_too_long(self):
        """Tag 20 길이 초과(16자 넘음) → FMT_20 에러."""
        resp = _post("/validate/mt", MT101_FORMAT_ERRORS)
        codes = [p.get("code") for p in resp.get("problems", [])]
        assert "FMT_20" in codes, \
            f"FMT_20 코드 없음. 실제 codes={codes}"

    def test_B3_mt101_format_error_32b_bad_amount(self):
        """Tag 32B 금액 부분에 문자 포함 → FMT_AMT 에러."""
        resp = _post("/validate/mt", MT101_FORMAT_ERRORS)
        problems = resp.get("problems", [])
        # FMT_AMT 또는 FMT_CCY (32B 포맷 오류)
        fmt_32b = [p for p in problems
                   if p.get("field") == "32B" and "FMT_" in p.get("code", "")]
        assert len(fmt_32b) > 0, \
            f"32B 포맷 에러 없음. 전체 problems={problems}"

    def test_B4_mt101_format_error_57a_bad_bic(self):
        """Tag 57A BIC 형식 오류 → FMT_BIC 에러."""
        resp = _post("/validate/mt", MT101_FORMAT_ERRORS)
        codes = [p.get("code") for p in resp.get("problems", [])]
        assert "FMT_BIC" in codes, \
            f"FMT_BIC 코드 없음. 실제 codes={codes}"

    def test_B5_mt101_duplicate_tag(self):
        """중복된 :20: 태그 → DUP_TAG 에러."""
        resp = _post("/validate/mt", MT101_DUPLICATE_TAG)
        codes = [p.get("code") for p in resp.get("problems", [])]
        assert "DUP_TAG" in codes, \
            f"DUP_TAG 코드 없음. 실제 codes={codes}"

    def test_B6_mt101_broken_message(self):
        """완전히 깨진 전문 → parseable=false, PARSE_FAILED."""
        resp = _post("/validate/mt", MT101_BROKEN)
        assert resp.get("parseable") is False, \
            f"parseable=False 기대. 실제: {resp}"
        codes = [p.get("code") for p in resp.get("problems", [])]
        assert "PARSE_FAILED" in codes, \
            f"PARSE_FAILED 코드 없음. 실제 codes={codes}"

    def test_B7_mt101_problems_have_required_fields(self):
        """모든 problem 항목이 code / desc 필드를 반드시 갖는지 확인."""
        resp = _post("/validate/mt", MT101_FORMAT_ERRORS)
        for p in resp.get("problems", []):
            assert "code" in p, f"problem에 'code' 없음: {p}"
            assert "desc" in p, f"problem에 'desc' 없음: {p}"

    def test_B8_mt101_prowide_client_syntax_not_ok(self):
        """prowide_client 래퍼: 필드 누락 MT101 → syntax_ok=False."""
        from app.validation.prowide_client import prowide_syntax_verify
        result = prowide_syntax_verify(MT101_MISSING_FIELDS, "MT101")
        assert not result.get("degraded"), "서버 연결 실패 (degraded)"
        assert result.get("syntax_ok") is False, \
            f"syntax_ok=False 기대. 실제: {result}"


# ===========================================================================
# ── 시나리오 A: 정상 MT103 검증 ──────────────────────────────────────────────
# ===========================================================================

class TestScenarioA_ValidMT103:
    """유효한 MT103 전문을 파싱·검증했을 때 성공 응답을 받는지 확인."""

    def test_A1_validate_mt_parseable(self):
        """POST /validate/mt → parseable=true, problems=[]"""
        resp = _post("/validate/mt", MT103_VALID)
        assert resp.get("parseable") is True, \
            f"parseable이 True여야 합니다. 응답: {resp}"
        assert resp.get("problems") == [], \
            f"problems가 비어있어야 합니다. 실제: {resp.get('problems')}"

    def test_A2_validate_mt_message_type(self):
        """응답 messageType이 MT103이어야 한다."""
        resp = _post("/validate/mt", MT103_VALID)
        assert resp.get("messageType") == "MT103", \
            f"messageType='MT103' 기대, 실제: {resp.get('messageType')}"

    def test_A3_parse_mt_fields(self):
        """POST /parse/mt → 필수 필드(20/23B/32A/50K/59) 모두 추출되는지 확인."""
        resp = _post("/parse/mt", MT103_VALID)
        assert resp.get("parseable") is True
        fields = {f["tag"]: f["value"] for f in resp.get("fields", [])}
        for tag in ("20", "23B", "32A", "50K", "59"):
            assert tag in fields, f"필드 :{tag}: 가 파싱 결과에 없습니다. fields={list(fields.keys())}"

    def test_A4_parse_mt_field_values(self):
        """파싱된 주요 필드 값이 입력 전문과 일치하는지 확인."""
        resp = _post("/parse/mt", MT103_VALID)
        fields = {f["tag"]: f["value"] for f in resp.get("fields", [])}
        assert "REF20240115001" in fields.get("20", ""), \
            f"필드 20 값 불일치: {fields.get('20')}"
        assert "CRED" in fields.get("23B", ""), \
            f"필드 23B 값 불일치: {fields.get('23B')}"
        assert "EUR" in fields.get("32A", ""), \
            f"필드 32A에 통화코드 EUR 없음: {fields.get('32A')}"

    def test_A5_prowide_client_wrapper(self):
        """prowide_client.prowide_syntax_verify() 래퍼가 syntax_ok=True를 반환하는지."""
        from app.validation.prowide_client import prowide_syntax_verify
        result = prowide_syntax_verify(MT103_VALID, "MT103")
        assert not result.get("degraded"), \
            f"서버 연결 실패(degraded). problems={result.get('problems')}"
        assert result.get("syntax_ok") is True, \
            f"syntax_ok=True 기대. 실제: {result}"


# ===========================================================================
# ── 시나리오 B: 에러 MT103 검증 ──────────────────────────────────────────────
# ===========================================================================

class TestScenarioB_ErrorMT103:
    """잘못된 MT103 전문들이 정확한 에러 코드와 함께 거부되는지 확인."""

    def test_B1_missing_mandatory_fields(self):
        """50K, 59 누락 → MISSING_FIELD 에러 코드 두 개 이상 존재."""
        resp = _post("/validate/mt", MT103_MISSING_FIELDS)
        problems = resp.get("problems", [])
        codes = [p.get("code") for p in problems]
        assert "MISSING_FIELD" in codes, \
            f"MISSING_FIELD 에러 코드 없음. 실제 codes={codes}"
        missing_fields = [p.get("field") for p in problems if p.get("code") == "MISSING_FIELD"]
        assert "50K" in missing_fields, f"50K 누락 감지 안 됨. missing={missing_fields}"
        assert "59"  in missing_fields, f"59 누락 감지 안 됨. missing={missing_fields}"

    def test_B2_format_error_tag20_too_long(self):
        """Tag 20 길이 초과(16자 넘음) → FMT_20 에러."""
        resp = _post("/validate/mt", MT103_FORMAT_ERRORS)
        problems = resp.get("problems", [])
        codes = [p.get("code") for p in problems]
        assert "FMT_20" in codes, \
            f"FMT_20 에러 코드 없음. 실제 codes={codes}"

    def test_B3_format_error_tag32a_bad_date(self):
        """Tag 32A 날짜 부분 오류 → FMT_DATE 또는 FMT_32A 에러."""
        resp = _post("/validate/mt", MT103_FORMAT_ERRORS)
        problems = resp.get("problems", [])
        codes = [p.get("code") for p in problems]
        assert any(c in codes for c in ("FMT_DATE", "FMT_32A", "FMT_CCY")), \
            f"32A 포맷 에러 코드 없음. 실제 codes={codes}"

    def test_B4_completely_broken_message(self):
        """전혀 SWIFT 형식이 아닌 텍스트 → parseable=false."""
        resp = _post("/validate/mt", MT103_BROKEN)
        assert resp.get("parseable") is False, \
            f"parseable=False 기대. 실제: {resp}"
        problems = resp.get("problems", [])
        assert len(problems) > 0, "problems 목록이 비어있습니다"

    def test_B5_problems_structure(self):
        """모든 problem 항목이 code / desc 필드를 갖는지 확인."""
        resp = _post("/validate/mt", MT103_MISSING_FIELDS)
        for p in resp.get("problems", []):
            assert "code" in p, f"problem에 'code' 필드 없음: {p}"
            assert "desc" in p, f"problem에 'desc' 필드 없음: {p}"

    def test_B6_prowide_client_syntax_not_ok(self):
        """prowide_client 래퍼: 필드 누락 전문 → syntax_ok=False."""
        from app.validation.prowide_client import prowide_syntax_verify
        result = prowide_syntax_verify(MT103_MISSING_FIELDS, "MT103")
        assert not result.get("degraded"), "서버 연결 실패 (degraded)"
        assert result.get("syntax_ok") is False, \
            f"syntax_ok=False 기대. 실제: {result}"


# ===========================================================================
# ── 시나리오 C: MX pacs.008 검증 ─────────────────────────────────────────────
# ===========================================================================

class TestScenarioC_MXValidation:
    """ISO 20022 pacs.008 XML 전문이 올바르게 처리되는지 확인."""

    def test_C1_valid_xml_parseable(self):
        """유효한 pacs.008 XML → parseable=true, problems=[]"""
        resp = _post("/validate/mx", MX_PACS008_VALID)
        assert resp.get("parseable") is True, \
            f"parseable=True 기대. 실제: {resp}"
        assert resp.get("problems") == [], \
            f"problems가 비어있어야 합니다. 실제: {resp.get('problems')}"

    def test_C2_valid_xml_has_note(self):
        """응답에 XSD 검증 한계 안내 note 포함 여부 확인."""
        resp = _post("/validate/mx", MX_PACS008_VALID)
        note = resp.get("note", "")
        assert "well-formed" in note.lower() or "xsd" in note.lower(), \
            f"note 메시지 형식 불일치: '{note}'"

    def test_C3_broken_xml_rejected(self):
        """닫는 태그 누락된 XML → parseable=false, XML_ERROR 에러."""
        resp = _post("/validate/mx", MX_PACS008_BROKEN_XML)
        assert resp.get("parseable") is False, \
            f"parseable=False 기대. 실제: {resp}"
        problems = resp.get("problems", [])
        codes = [p.get("code") for p in problems]
        assert "XML_ERROR" in codes, \
            f"XML_ERROR 코드 없음. 실제 codes={codes}"

    def test_C4_empty_mx_rejected(self):
        """빈 문자열 → parseable=false, EMPTY_MSG 에러."""
        resp = _post("/validate/mx", "")
        assert resp.get("parseable") is False
        codes = [p.get("code") for p in resp.get("problems", [])]
        assert "EMPTY_MSG" in codes, f"EMPTY_MSG 없음. codes={codes}"

    def test_C5_prowide_client_mx_wrapper(self):
        """prowide_client.prowide_syntax_verify() MX 경로 → syntax_ok=True."""
        from app.validation.prowide_client import prowide_syntax_verify
        result = prowide_syntax_verify(MX_PACS008_VALID, "pacs.008")
        assert not result.get("degraded"), "서버 연결 실패 (degraded)"
        assert result.get("syntax_ok") is True, \
            f"syntax_ok=True 기대. 실제: {result}"


# ===========================================================================
# ── 추가: 서버 헬스체크 ──────────────────────────────────────────────────────
# ===========================================================================

class TestHealthCheck:
    def test_actuator_health(self):
        """Spring Boot Actuator /actuator/health → status=UP."""
        resp = httpx.get(f"{PROWIDE_URL}/actuator/health", timeout=5.0)
        assert resp.status_code == 200
        assert resp.json().get("status") == "UP"

    def test_prowide_client_health_check(self):
        """prowide_client.health_check() → True."""
        from app.validation.prowide_client import health_check
        assert health_check() is True, \
            f"health_check()=False. PROWIDE_URL={PROWIDE_URL}"


# ===========================================================================
# 단독 실행 모드 — 상세 출력
# ===========================================================================

def _run_scenario_mt101_a():
    _section("시나리오 A (MT101) — 정상 Request for Transfer 검증")
    _print_message("전송 전문 (MT101_VALID)", MT101_VALID)

    print(f"\n  {_color('[POST /validate/mt]', CYAN)}")
    resp = _post("/validate/mt", MT101_VALID)
    _print_response(resp)
    ok = resp.get("parseable") is True and resp.get("problems") == []
    _print_result(ok, f"parseable={resp.get('parseable')}  messageType={resp.get('messageType')}  problems={resp.get('problems')}")

    print(f"\n  {_color('[POST /parse/mt]', CYAN)}")
    resp2 = _post("/parse/mt", MT101_VALID)
    _print_response(resp2)
    fields  = {f["tag"]: f["value"] for f in resp2.get("fields", [])}
    present = [t for t in ("20", "28D", "30", "21", "32B") if t in fields]
    ok2     = len(present) == 5
    _print_result(ok2, f"필수 필드 추출: {present}")

    print(f"\n  {_color('[prowide_client.prowide_syntax_verify()]', CYAN)}")
    from app.validation.prowide_client import prowide_syntax_verify
    result = prowide_syntax_verify(MT101_VALID, "MT101")
    _print_client_result("MT101_VALID", result)
    _print_result(result.get("syntax_ok") is True, "syntax_ok=True")


def _run_scenario_mt101_b():
    _section("시나리오 B-1 (MT101) — 필수 필드 누락 (28D / 32B)")
    _print_message("전송 전문 (MT101_MISSING_FIELDS)", MT101_MISSING_FIELDS)
    resp = _post("/validate/mt", MT101_MISSING_FIELDS)
    _print_response(resp)
    missing = [p for p in resp.get("problems", []) if p.get("code") == "MISSING_FIELD"]
    ok = len(missing) >= 2
    _print_result(ok, f"MISSING_FIELD 감지: {[p.get('field') for p in missing]}")

    _section("시나리오 B-2 (MT101) — 포맷 오류 (Tag 20 / 32B / 57A BIC)")
    _print_message("전송 전문 (MT101_FORMAT_ERRORS)", MT101_FORMAT_ERRORS)
    resp2 = _post("/validate/mt", MT101_FORMAT_ERRORS)
    _print_response(resp2)
    fmt_codes = [p.get("code") for p in resp2.get("problems", [])]
    ok2 = "FMT_20" in fmt_codes and "FMT_BIC" in fmt_codes
    _print_result(ok2, f"포맷 에러 코드: {fmt_codes}")

    _section("시나리오 B-3 (MT101) — 중복 태그 (:20: 두 번)")
    _print_message("전송 전문 (MT101_DUPLICATE_TAG)", MT101_DUPLICATE_TAG)
    resp3 = _post("/validate/mt", MT101_DUPLICATE_TAG)
    _print_response(resp3)
    codes3 = [p.get("code") for p in resp3.get("problems", [])]
    _print_result("DUP_TAG" in codes3, f"DUP_TAG 감지: {'DUP_TAG' in codes3}")

    _section("시나리오 B-4 (MT101) — 완전히 깨진 전문")
    _print_message("전송 전문 (MT101_BROKEN)", MT101_BROKEN)
    resp4 = _post("/validate/mt", MT101_BROKEN)
    _print_response(resp4)
    _print_result(resp4.get("parseable") is False, f"parseable={resp4.get('parseable')}")

    _section("시나리오 B-5 (MT101) — prowide_client 래퍼 에러 확인")
    from app.validation.prowide_client import prowide_syntax_verify
    result = prowide_syntax_verify(MT101_MISSING_FIELDS, "MT101")
    _print_client_result("MT101_MISSING_FIELDS", result)
    _print_result(result.get("syntax_ok") is False, "syntax_ok=False 기대")


def _run_scenario_a():
    _section("시나리오 A (MT103) — 정상 Single Customer Credit Transfer 검증")

    _print_message("전송 전문 (MT103_VALID)", MT103_VALID)

    # A-1: /validate/mt
    print(f"\n  {_color('[POST /validate/mt]', CYAN)}")
    resp = _post("/validate/mt", MT103_VALID)
    _print_response(resp)
    ok = resp.get("parseable") is True and resp.get("problems") == []
    _print_result(ok, f"parseable={resp.get('parseable')}  problems={resp.get('problems')}")

    # A-2: /parse/mt
    print(f"\n  {_color('[POST /parse/mt]', CYAN)}")
    resp2 = _post("/parse/mt", MT103_VALID)
    _print_response(resp2)
    fields = {f["tag"]: f["value"] for f in resp2.get("fields", [])}
    present = [t for t in ("20", "23B", "32A", "50K", "59") if t in fields]
    ok2 = len(present) == 5
    _print_result(ok2, f"추출된 필수 필드: {present}")

    # A-3: prowide_client 래퍼
    print(f"\n  {_color('[prowide_client.prowide_syntax_verify()]', CYAN)}")
    from app.validation.prowide_client import prowide_syntax_verify
    result = prowide_syntax_verify(MT103_VALID, "MT103")
    _print_client_result("MT103_VALID", result)
    _print_result(result.get("syntax_ok") is True, "syntax_ok=True")


def _run_scenario_b():
    _section("시나리오 B-1 — 필수 필드 누락 MT103")
    _print_message("전송 전문 (MT103_MISSING_FIELDS)", MT103_MISSING_FIELDS)
    resp = _post("/validate/mt", MT103_MISSING_FIELDS)
    _print_response(resp)
    missing = [p for p in resp.get("problems", []) if p.get("code") == "MISSING_FIELD"]
    ok = len(missing) >= 2
    _print_result(ok, f"MISSING_FIELD 감지: {[p.get('field') for p in missing]}")

    _section("시나리오 B-2 — 포맷 오류 MT103 (Tag 20 / 32A)")
    _print_message("전송 전문 (MT103_FORMAT_ERRORS)", MT103_FORMAT_ERRORS)
    resp2 = _post("/validate/mt", MT103_FORMAT_ERRORS)
    _print_response(resp2)
    fmt_codes = [p.get("code") for p in resp2.get("problems", [])]
    ok2 = "FMT_20" in fmt_codes or any("FMT_" in c for c in fmt_codes)
    _print_result(ok2, f"포맷 에러 코드: {fmt_codes}")

    _section("시나리오 B-3 — 완전히 깨진 전문")
    _print_message("전송 전문 (MT103_BROKEN)", MT103_BROKEN)
    resp3 = _post("/validate/mt", MT103_BROKEN)
    _print_response(resp3)
    ok3 = resp3.get("parseable") is False
    _print_result(ok3, f"parseable={resp3.get('parseable')}")

    _section("시나리오 B-4 — prowide_client 래퍼 (에러 전문)")
    from app.validation.prowide_client import prowide_syntax_verify
    result = prowide_syntax_verify(MT103_MISSING_FIELDS, "MT103")
    _print_client_result("MT103_MISSING_FIELDS", result)
    _print_result(result.get("syntax_ok") is False, "syntax_ok=False 기대")


def _run_scenario_c():
    _section("시나리오 C-1 — 정상 pacs.008 MX XML 검증")
    _print_message("전송 전문 (MX_PACS008_VALID)", MX_PACS008_VALID)
    resp = _post("/validate/mx", MX_PACS008_VALID)
    _print_response(resp)
    ok = resp.get("parseable") is True and resp.get("problems") == []
    _print_result(ok, f"parseable={resp.get('parseable')}  note='{resp.get('note', '')[:60]}'")

    _section("시나리오 C-2 — 깨진 MX XML (닫는 태그 누락)")
    _print_message("전송 전문 (MX_PACS008_BROKEN_XML)", MX_PACS008_BROKEN_XML)
    resp2 = _post("/validate/mx", MX_PACS008_BROKEN_XML)
    _print_response(resp2)
    ok2 = resp2.get("parseable") is False
    codes = [p.get("code") for p in resp2.get("problems", [])]
    _print_result(ok2, f"parseable=False, XML_ERROR={'XML_ERROR' in codes}")

    _section("시나리오 C-3 — prowide_client 래퍼 (MX)")
    from app.validation.prowide_client import prowide_syntax_verify
    result = prowide_syntax_verify(MX_PACS008_VALID, "pacs.008")
    _print_client_result("MX_PACS008_VALID", result)
    _print_result(result.get("syntax_ok") is True, "syntax_ok=True")


def _run_health():
    _section("서버 헬스체크")
    print(f"  PROWIDE_URL = {PROWIDE_URL}")
    alive = _check_server()
    _print_result(alive, f"Spring Boot Actuator /actuator/health {'UP' if alive else 'DOWN'}")
    if not alive:
        print(f"\n  {_color('  서버가 응답하지 않습니다. 아래 명령으로 기동하세요:', YELLOW)}")
        print("    docker compose up -d prowide-svc")
    return alive


if __name__ == "__main__":
    print(_color("\n" + "═" * 64, CYAN))
    print(_color("  Prowide 통합 검증 테스트  (단독 실행 모드)", BOLD))
    print(_color("═" * 64, CYAN))

    if not _run_health():
        sys.exit(1)

    # prowide_client 모듈 변수를 현재 PROWIDE_URL로 패치
    # (pytest fixture와 동일한 처리 — 모듈 로드 시 고정된 내부 DNS 덮어쓰기)
    import app.validation.prowide_client as _pc
    _pc.PROWIDE_URL = PROWIDE_URL

    try:
        # MT101 시나리오
        _run_scenario_mt101_a()
        _run_scenario_mt101_b()
        # MT103 시나리오
        _run_scenario_a()
        _run_scenario_b()
        # MX pacs.008 시나리오
        _run_scenario_c()
    except httpx.HTTPError as exc:
        print(f"\n{_color('  HTTP 오류: ' + str(exc), RED)}")
        sys.exit(1)

    print(_color("\n" + "═" * 64, CYAN))
    print(_color("  모든 시나리오 실행 완료", BOLD))
    print(_color("═" * 64 + "\n", CYAN))
