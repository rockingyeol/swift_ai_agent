"""
RAG 파이프라인 단위 테스트.

PDF / Qdrant 없이 실행 가능한 테스트를 우선 구현한다.
  - chunker: chunk_text() 로 합성 텍스트 청킹 검증
  - indexer: 연결 체크, 컬렉션 함수 시그니처 검증
  - retriever: build_qdrant_filter() 단위 테스트
  - 통합 테스트: Qdrant 실행 중일 때만 실행 (skip 마크)
"""
from __future__ import annotations

import pytest

from app.rag.chunker import (
    DocType,
    RuleType,
    SwiftChunk,
    chunk_text,
    classify_rule,
    make_chunk_id,
)
from app.rag.retriever import build_qdrant_filter

# ---------------------------------------------------------------------------
# Qdrant 가용 여부
# ---------------------------------------------------------------------------
try:
    from app.rag.indexer import check_connection, get_client
    _QDRANT_OK = check_connection()
except Exception:
    _QDRANT_OK = False

qdrant_only = pytest.mark.skipif(not _QDRANT_OK, reason="Qdrant not running")


# ===========================================================================
# classify_rule()
# ===========================================================================

class TestClassifyRule:
    def test_presence_rule(self) -> None:
        assert classify_rule("C1: if field 33B is present, field 36 is mandatory") == RuleType.PRESENCE

    def test_network_t_rule(self) -> None:
        assert classify_rule("T26: date format must be YYMMDD") == RuleType.NETWORK

    def test_network_d_rule(self) -> None:
        assert classify_rule("D49: field not allowed in this context") == RuleType.NETWORK

    def test_business_rule(self) -> None:
        assert classify_rule("BR-001: SettlementMethod must be INDA when agent is absent") == RuleType.BUSINESS

    def test_format_rule(self) -> None:
        assert classify_rule("format 35x: maximum 35 alphanumeric characters") == RuleType.FORMAT

    def test_usage(self) -> None:
        assert classify_rule("This field contains remittance information for the beneficiary.") == RuleType.USAGE


# ===========================================================================
# make_chunk_id() / SwiftChunk.embedding_text()
# ===========================================================================

class TestChunkModel:
    def test_make_chunk_id_deterministic(self) -> None:
        id1 = make_chunk_id("MT103", "field", "50K", "p10")
        id2 = make_chunk_id("MT103", "field", "50K", "p10")
        assert id1 == id2

    def test_make_chunk_id_unique_for_different_args(self) -> None:
        id1 = make_chunk_id("MT103", "field", "50K", "p10")
        id2 = make_chunk_id("MT103", "field", "59",  "p10")
        assert id1 != id2

    def test_embedding_text_no_metadata(self) -> None:
        chunk = SwiftChunk(
            chunk_id="x", level="rule", message_type="MT103",
            page=1, text="Rule C1 says X."
        )
        emb = chunk.embedding_text()
        assert "[MT103]" in emb
        assert "Rule C1 says X." in emb

    def test_embedding_text_with_field(self) -> None:
        chunk = SwiftChunk(
            chunk_id="x", level="rule", message_type="MT103",
            field_tag="50K", rule_id="C1", page=1, text="Ordering customer."
        )
        emb = chunk.embedding_text()
        assert "[MT103]" in emb
        assert "Field 50K:" in emb
        assert "Rule C1:" in emb

    def test_embedding_text_mx_element(self) -> None:
        chunk = SwiftChunk(
            chunk_id="x", level="element", message_type="pacs.008.001.08",
            field_tag="CdtTrfTxInf",
            element_path="CdtTrfTxInf/CdtrAcct/Id/IBAN",
            page=5, text="Creditor account IBAN."
        )
        emb = chunk.embedding_text()
        assert "pacs.008.001.08" in emb
        assert "CdtTrfTxInf/CdtrAcct/Id/IBAN" in emb


# ===========================================================================
# chunk_text() — MT 문서
# ===========================================================================

class TestChunkTextMT:
    """PDF 없이 합성 텍스트로 MT 청킹 검증."""

    _MT103_PAGE1 = """\
MT103 Single Customer Credit Transfer

This message is sent by or on behalf of the ordering customer bank,
directly or through correspondent(s), to the bank of the beneficiary customer.
"""

    _MT103_PAGE2 = """\
20: Transaction Reference Number

Format: 16x
This field contains the unique reference assigned by the sender.

32A: Value Date/Currency/Interbank Settled Amount

Format: 6!n3!a15d
Mandatory field. C1: If field 33B is present and currency differs from 32A,
field 36 (Exchange Rate) must be present.
"""

    _MT103_PAGE3 = """\
50K: Ordering Customer

Format: 35x (subfield 1: Account) / 35x (subfield 2-5: Name and Address)
C2: Either subfield 1 (Account) or subfield 2-5 (Name/Address) must be present.
T26: Characters must conform to SWIFT character set.
"""

    def _chunks(self) -> list[SwiftChunk]:
        return chunk_text(
            [self._MT103_PAGE1, self._MT103_PAGE2, self._MT103_PAGE3],
            doc_type=DocType.MT,
        )

    def test_message_chunk_created(self) -> None:
        chunks = self._chunks()
        msg_chunks = [c for c in chunks if c.level == "message"]
        assert len(msg_chunks) >= 1
        assert any(c.message_type == "MT103" for c in msg_chunks)

    def test_field_chunks_created(self) -> None:
        chunks = self._chunks()
        field_chunks = [c for c in chunks if c.level == "field"]
        tags = {c.field_tag for c in field_chunks}
        assert "20" in tags or "32A" in tags or "50K" in tags

    def test_rule_chunks_have_rule_id(self) -> None:
        chunks = self._chunks()
        rule_chunks = [c for c in chunks if c.level == "rule" and c.rule_id]
        assert len(rule_chunks) >= 1

    def test_rule_chunk_has_parent_id(self) -> None:
        chunks = self._chunks()
        rule_chunks = [c for c in chunks if c.level == "rule" and c.rule_id]
        # 규칙 청크는 parent_id로 필드 청크를 참조해야 한다
        assert any(c.parent_id is not None for c in rule_chunks)

    def test_chunk_hierarchy_levels(self) -> None:
        chunks = self._chunks()
        levels = {c.level for c in chunks}
        assert "message" in levels
        assert "rule" in levels

    def test_all_chunks_have_message_type(self) -> None:
        chunks = self._chunks()
        # message 레벨 이외의 청크도 message_type이 설정돼야 함
        for c in chunks:
            assert c.message_type, f"chunk {c.chunk_id} has no message_type"

    def test_no_duplicate_chunk_ids(self) -> None:
        chunks = self._chunks()
        ids = [c.chunk_id for c in chunks]
        assert len(ids) == len(set(ids)), "중복 chunk_id 발생"

    def test_source_type_is_mt(self) -> None:
        chunks = self._chunks()
        # 모든 청크의 source_type이 "mt"여야 함
        assert all(c.source_type == "mt" for c in chunks)


# ===========================================================================
# chunk_text() — MX 문서
# ===========================================================================

class TestChunkTextMX:
    """ISO 20022 / MX 형식 합성 텍스트 청킹 검증."""

    _PACS008_PAGE1 = """\
pacs.008.001.08 FIToFICstmrCdtTrf

Financial Institution To Financial Institution Customer Credit Transfer.
Used to transfer funds from a debtor to a creditor.
"""

    _PACS008_PAGE2 = """\
GrpHdr/MsgId [1..1] Max35Text

Point to point reference assigned by the instructing party.
Must be unique per instructed party for a pre-agreed period.

GrpHdr/CreDtTm [1..1] ISODateTime

Date and time at which the message was created.
"""

    _PACS008_PAGE3 = """\
CdtTrfTxInf/CdtrAcct/Id/IBAN [0..1] IBAN2007Identifier

International Bank Account Number of the creditor.
BR-001: If IBAN is absent, Othr/Id must be present.
"""

    def _chunks(self) -> list[SwiftChunk]:
        return chunk_text(
            [self._PACS008_PAGE1, self._PACS008_PAGE2, self._PACS008_PAGE3],
            doc_type=DocType.MX,
        )

    def test_mx_message_chunk_created(self) -> None:
        chunks = self._chunks()
        msg = [c for c in chunks if c.level == "message"]
        assert any("pacs.008.001.08" in c.message_type for c in msg)

    def test_mx_source_type(self) -> None:
        chunks = self._chunks()
        # MX 청크는 source_type이 "mx"여야 함
        assert any(c.source_type == "mx" for c in chunks)

    def test_mx_element_path_captured(self) -> None:
        chunks = self._chunks()
        field_chunks = [c for c in chunks if c.level in ("field", "element")]
        paths = [c.element_path for c in field_chunks if c.element_path]
        assert any("GrpHdr" in (p or "") for p in paths)

    def test_mx_business_rule_detected(self) -> None:
        chunks = self._chunks()
        rule_chunks = [c for c in chunks if c.rule_id and "BR" in c.rule_id]
        assert len(rule_chunks) >= 1

    def test_mx_rule_type_business(self) -> None:
        chunks = self._chunks()
        br_chunks = [c for c in chunks if c.rule_id and c.rule_id.startswith("BR")]
        assert all(c.rule_type == RuleType.BUSINESS for c in br_chunks)


# ===========================================================================
# build_qdrant_filter()
# ===========================================================================

class TestBuildQdrantFilter:
    def test_none_for_empty_filters(self) -> None:
        assert build_qdrant_filter({}) is None
        assert build_qdrant_filter({}) is None

    def test_single_str_value(self) -> None:
        f = build_qdrant_filter({"message_type": "MT103"})
        assert f is not None
        assert len(f.must) == 1

    def test_list_value_generates_match_any(self) -> None:
        from qdrant_client.models import MatchAny
        f = build_qdrant_filter({"rule_type": ["presence", "network"]})
        assert f is not None
        cond = f.must[0]
        assert isinstance(cond.match, MatchAny)

    def test_single_list_generates_match_value(self) -> None:
        from qdrant_client.models import MatchValue
        f = build_qdrant_filter({"rule_type": ["presence"]})
        assert f is not None
        cond = f.must[0]
        assert isinstance(cond.match, MatchValue)

    def test_multiple_keys(self) -> None:
        f = build_qdrant_filter({
            "message_type": "MT103",
            "source_type": "mt",
        })
        assert f is not None
        assert len(f.must) == 2

    def test_none_value_is_skipped(self) -> None:
        f = build_qdrant_filter({"message_type": "MT103", "field_tag": None})
        assert f is not None
        assert len(f.must) == 1  # None 값은 무시됨


# ===========================================================================
# Qdrant 통합 테스트 (Qdrant 실행 중일 때만)
# ===========================================================================

class TestQdrantIntegration:
    @qdrant_only
    def test_check_connection(self) -> None:
        from app.rag.indexer import check_connection
        assert check_connection() is True

    @qdrant_only
    def test_create_collection(self) -> None:
        from app.rag.indexer import create_collection, collection_exists, get_client
        client = get_client()
        create_collection(client, name="swift_test_tmp", recreate=True)
        assert collection_exists(client, "swift_test_tmp")
        client.delete_collection("swift_test_tmp")

    @qdrant_only
    def test_retriever_returns_top_k(self) -> None:
        """검색 테스트: 컬렉션에 더미 청크를 직접 삽입한 후 검색한다."""
        from app.rag.indexer import (
            create_collection, collection_exists, get_client, index_chunks
        )
        from app.rag.retriever import SwiftRetriever

        TEST_COLL = "swift_retriever_test"
        client = get_client()
        create_collection(client, name=TEST_COLL, recreate=True)

        # 합성 청크 생성 및 인덱싱
        pages = [
            "MT103 Single Customer Credit Transfer\n\n"
            "32A: Mandatory field. C1: If field 33B is present field 36 is mandatory.",
        ]
        test_chunks = chunk_text(pages, doc_type=DocType.MT)
        index_chunks(test_chunks, collection=TEST_COLL, client=client)

        # 검색
        retriever = SwiftRetriever(collection=TEST_COLL)
        results = retriever.search(
            query="MT103 field 32A C1 rule",
            top_k=3,
            rerank=False,   # reranker 모델 다운로드 불필요
        )
        assert len(results) <= 3
        assert all(isinstance(r, SwiftChunk) for r in results)

        # 정리
        client.delete_collection(TEST_COLL)
