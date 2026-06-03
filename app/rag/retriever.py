"""
하이브리드 검색 + Cross-Encoder 재정렬 + 필드 태그 필터링.

검색 전략:
  (1) 쿼리에서 SWIFT 필드 태그 자동 추출 → Qdrant 메타데이터 필터 구성
  (2) Dense + Sparse Prefetch → RRF 퓨전 (후보 30개)
  (3) bge-reranker-v2-m3 크로스인코더 재정렬
  (4) 상위 K개 반환

Qdrant query_points + Prefetch API (qdrant-client ≥ 1.9) 사용.
페이로드는 MTFieldChunk(신규) 또는 SwiftChunk(레거시) 자동 판별.
"""
from __future__ import annotations

import os
import re
import threading
from typing import Any, Optional, Union

from qdrant_client import QdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    FusionQuery,
    MatchAny,
    MatchValue,
    Prefetch,
    SparseVector,
    models,
)

from app.rag.chunker import (
    CbprSr2026Chunk,
    MTFieldChunk,
    MXFieldChunk,
    SwiftChunk,
    chunk_id_to_point_id,
)
from app.rag.indexer import COLLECTION, EMBED_MODEL, QDRANT_URL, _get_embed_model

RERANKER_MODEL  = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
_CANDIDATE_K    = 30
_RERANK_SCORE_T = 0.0

AnyChunk = Union[MTFieldChunk, MXFieldChunk, CbprSr2026Chunk, SwiftChunk]


# ---------------------------------------------------------------------------
# Reranker 싱글턴
# ---------------------------------------------------------------------------
_reranker: Optional[object] = None
_reranker_lock = threading.Lock()


def _get_reranker():
    global _reranker
    if _reranker is not None:
        return _reranker
    with _reranker_lock:
        if _reranker is None:
            try:
                from FlagEmbedding import FlagReranker
                _reranker = FlagReranker(RERANKER_MODEL, use_fp16=True)
            except ImportError as e:
                raise ImportError(
                    "FlagEmbedding 미설치. pip install FlagEmbedding 실행 후 재시도."
                ) from e
    return _reranker


# ---------------------------------------------------------------------------
# 쿼리에서 SWIFT 필드 태그 추출
# ---------------------------------------------------------------------------

# :XX: 형식 (SWIFT 표준 표기, 예: :32B:, :50F:)
_RE_SWIFT_TAG_COLON = re.compile(r":(\d{1,2}[A-Za-z]?):")

# "field XX", "필드 XX", "XX번 필드" 등 자연어 언급
# ※ (?<!\d) lookbehind: "MT101 필드"에서 "01"이 오추출되는 것을 방지
_RE_FIELD_MENTION = re.compile(
    r"(?:"
    r"(?:field|tag|필드|태그)\s+(?<!\d)(\d{1,2}[A-Za-z]?)"
    r"|(?<!\d)(\d{1,2}[A-Za-z]?)\s*번?\s*(?:field|tag|필드|태그)"
    r")",
    re.IGNORECASE,
)


def extract_field_tags_from_query(query: str) -> list[str]:
    """
    사용자 쿼리에서 SWIFT 필드 태그를 추출한다.

    인식 패턴:
      - :XX:   형식 (예: :32B:, :50F:)
      - "field XX" / "필드 XX" (영어·한국어)
      - "XX번 필드" / "XX 태그" (한국어)

    Returns:
        태그 문자열 목록 (중복 제거, 예: ["32B", "50"])
    """
    tags: set[str] = set()

    for m in _RE_SWIFT_TAG_COLON.finditer(query):
        tags.add(m.group(1))

    for m in _RE_FIELD_MENTION.finditer(query):
        candidate = m.group(1) or m.group(2)
        if candidate and re.match(r"^\d{1,2}[A-Za-z]?$", candidate):
            tags.add(candidate)

    return list(tags)


# ---------------------------------------------------------------------------
# MX / CBPR+ XPath 라우팅 정보 추출
# ---------------------------------------------------------------------------

# XPath 패턴: "/Document/FIToFI..." 또는 "Document/FIToFI..."
_RE_XPATH_PATTERN = re.compile(
    r"(/Document/[A-Za-z/]+|Document/[A-Za-z/]+)"
)

# MX 메시지 타입 키워드 패턴 — "pacs.008", "camt.056" 등
# ※ \b 대신 ASCII 전용 lookaround 사용:
#   Python Unicode 모드에서 \b는 한국어 등 유니코드 문자를 \w로 처리하므로
#   "pacs.008의" 같은 문자열의 경계를 제대로 인식하지 못한다.
_RE_MX_MSG_TYPE = re.compile(
    r"(?<![a-zA-Z0-9])([a-z]{3,4}\.\d{3})(?![a-zA-Z0-9])",
    re.IGNORECASE,
)

# SR2026 / CR / XPath 관련 질의 감지
_RE_SR2026_KEYWORDS = re.compile(
    r"\b(SR2026|SR\s*2026|CBPR\+?|xpath|XPath|CR\s*\d+|변경\s*사항|impacted)\b",
    re.IGNORECASE,
)


class MXRoutingInfo:
    """MX / CBPR+ 질의 라우팅 정보 컨테이너."""

    __slots__ = ("msg_types", "xpath", "is_sr2026_query", "is_mx_query")

    def __init__(
        self,
        msg_types: list[str],
        xpath: Optional[str],
        is_sr2026_query: bool,
        is_mx_query: bool,
    ) -> None:
        self.msg_types       = msg_types
        self.xpath           = xpath
        self.is_sr2026_query = is_sr2026_query
        self.is_mx_query     = is_mx_query


def extract_mx_routing_info(query: str) -> MXRoutingInfo:
    """
    사용자 쿼리에서 MX / CBPR+ 라우팅 정보를 추출한다.

    감지 항목:
      - MX 메시지 타입  : "pacs.008", "camt.056" 등
      - XPath 문자열    : "/Document/FIToFICstmrCdtTrf/..."
      - SR2026/CBPR+ 질의 여부 : "SR2026", "CR 2006", "xpath" 등의 키워드

    Returns:
        MXRoutingInfo — msg_types, xpath, is_sr2026_query, is_mx_query 포함
    """
    # MX 메시지 타입 감지
    msg_types = list({m.group(1).lower() for m in _RE_MX_MSG_TYPE.finditer(query)})

    # XPath 감지
    xpath_m = _RE_XPATH_PATTERN.search(query)
    xpath   = xpath_m.group(1) if xpath_m else None

    # SR2026 / CBPR+ 질의 여부
    is_sr2026 = bool(_RE_SR2026_KEYWORDS.search(query))

    # MX 질의 여부 (msg_type 또는 XPath 또는 ISO 20022 관련 키워드)
    is_mx = bool(msg_types or xpath or re.search(
        r"\b(ISO\s*20022|MX|pacs|camt|pain|pacs008|pacs\.008)\b",
        query,
        re.IGNORECASE,
    ))

    return MXRoutingInfo(
        msg_types=msg_types,
        xpath=xpath,
        is_sr2026_query=is_sr2026,
        is_mx_query=is_mx,
    )


# ---------------------------------------------------------------------------
# Qdrant 필터 빌더
# ---------------------------------------------------------------------------

def build_qdrant_filter(filters: dict[str, Any]) -> Optional[Filter]:
    """
    검색 필터 딕셔너리를 Qdrant Filter 객체로 변환한다.

    지원 키 (MTFieldChunk 기준):
      msg_type    : str | list[str]   — "MT101", ["MT101","MT103"]
      field_tag   : str | list[str]   — "32B", ["32B","50a"]
      doc_type    : str               — "guidebook"
      section_type: str | list[str]   — "field_spec", "message_rule" …

    레거시 SwiftChunk 키도 함께 지원:
      message_type, source_type, rule_type, level
    """
    if not filters:
        return None

    conditions = []
    for key, val in filters.items():
        if val is None:
            continue
        if isinstance(val, list):
            if len(val) == 1:
                conditions.append(
                    FieldCondition(key=key, match=MatchValue(value=val[0]))
                )
            elif len(val) > 1:
                conditions.append(
                    FieldCondition(key=key, match=MatchAny(any=val))
                )
        else:
            conditions.append(
                FieldCondition(key=key, match=MatchValue(value=val))
            )

    return Filter(must=conditions) if conditions else None


# ---------------------------------------------------------------------------
# 페이로드 → 청크 객체 복원
# ---------------------------------------------------------------------------

def _payload_to_chunk(payload: dict[str, Any]) -> Optional[AnyChunk]:
    """
    Qdrant 페이로드를 청크 객체로 복원한다.

    판별 기준 (doc_type 우선):
      - "cbpr_sr2026_cr"  → CbprSr2026Chunk
      - "mx_guide"        → MXFieldChunk
      - "guidebook" + "msg_type" → MTFieldChunk (신규 MT)
      - 그 외             → SwiftChunk (레거시)
    """
    if not payload:
        return None
    try:
        doc_type = payload.get("doc_type", "")
        if doc_type == "cbpr_sr2026_cr":
            return CbprSr2026Chunk(**payload)
        if doc_type == "mx_guide":
            return MXFieldChunk(**payload)
        if "msg_type" in payload:
            return MTFieldChunk(**payload)
        return SwiftChunk(**payload)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# SwiftRetriever
# ---------------------------------------------------------------------------

class SwiftRetriever:
    """
    SWIFT 가이드북 하이브리드 검색기.

    사용 예:
        retriever = SwiftRetriever()

        # 기본 하이브리드 검색
        chunks = retriever.search("MT101 field 32B rules", top_k=5)

        # 필드 태그 자동 추출 + 필터 검색
        chunks = retriever.search(":32B: 필드 조건", auto_filter=True)

        # 수동 필터
        chunks = retriever.search(
            "ordering customer rules",
            filters={"msg_type": "MT101", "field_tag": "50a"},
        )
    """

    def __init__(
        self,
        collection: str = COLLECTION,
        qdrant_url: str = QDRANT_URL,
        candidate_k: int = _CANDIDATE_K,
    ) -> None:
        self._collection  = collection
        self._candidate_k = candidate_k
        self._client      = QdrantClient(url=qdrant_url)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        filters: Optional[dict[str, Any]] = None,
        top_k: int = 5,
        rerank: bool = True,
        auto_filter: bool = False,
    ) -> list[AnyChunk]:
        """
        하이브리드 검색 → 재정렬 → 상위 K개 반환.

        자동 라우팅 우선순위 (auto_filter=True):
          1. XPath 패턴 감지   → field_path 필터 + doc_type 필터 적용
          2. SR2026/CBPR+ 키워드 → doc_type="cbpr_sr2026_cr" 필터 적용
          3. MX msg_type 감지  → msg_type 필터 적용 (mx_guide + sr2026 모두)
          4. MT 필드 태그 감지  → field_tag 필터 적용 (guidebook)

        Args:
            query       : 자연어 또는 SWIFT 키워드 쿼리
            filters     : Qdrant 메타데이터 필터 딕셔너리 (수동 지정)
            top_k       : 최종 반환 청크 수
            rerank      : True이면 bge-reranker 재정렬 수행
            auto_filter : True이면 쿼리에서 라우팅 정보를 자동 추출하여
                          메타데이터 필터를 동적으로 추가
        """
        effective_filters = dict(filters or {})

        if auto_filter:
            effective_filters = self._apply_auto_filters(query, effective_filters)

        candidates = self._hybrid_search(query, effective_filters)
        if not candidates:
            return []

        if rerank and len(candidates) > 1:
            candidates = self._rerank(query, candidates)

        return candidates[:top_k]

    def _apply_auto_filters(
        self,
        query: str,
        base_filters: dict[str, Any],
    ) -> dict[str, Any]:
        """
        쿼리를 분석하여 최적 Qdrant 메타데이터 필터를 자동 구성한다.

        라우팅 전략:
          - XPath 포함 → field_path 부분 매칭 + doc_type 우선순위 지정
          - SR2026 키워드 → doc_type="cbpr_sr2026_cr"로 한정
          - MX msg_type → msg_type 필터
          - MT field_tag → field_tag 필터 (MX 라우팅 없을 때만)
        """
        filters = dict(base_filters)

        # MX 라우팅 정보 추출
        mx_info = extract_mx_routing_info(query)

        # ── 1. XPath 패턴이 쿼리에 있으면 → SR2026 CR 청크 우선 검색 ────────
        if mx_info.xpath and "doc_type" not in filters:
            filters["doc_type"] = "cbpr_sr2026_cr"
            # xpath는 정확히 일치하지 않을 수 있으므로 필터보다 시맨틱 검색에 위임
            return filters

        # ── 2. SR2026 / CBPR+ 키워드 → CR 문서로 한정 ─────────────────────
        if mx_info.is_sr2026_query and "doc_type" not in filters:
            filters["doc_type"] = "cbpr_sr2026_cr"
            if mx_info.msg_types and "msg_type" not in filters:
                msg = mx_info.msg_types[0]
                filters["msg_type"] = msg
            return filters

        # ── 3. MX msg_type 감지 (SR2026 아닌 일반 MX 질의) ─────────────────
        if mx_info.is_mx_query and "msg_type" not in filters:
            if mx_info.msg_types:
                filters["msg_type"] = (
                    mx_info.msg_types if len(mx_info.msg_types) > 1
                    else mx_info.msg_types[0]
                )
            if "doc_type" not in filters:
                filters["doc_type"] = "mx_guide"
            return filters

        # ── 4. MT 필드 태그 자동 추출 (MX 라우팅 없을 때) ───────────────────
        if "field_tag" not in filters:
            tags = extract_field_tags_from_query(query)
            if tags:
                filters["field_tag"] = tags if len(tags) > 1 else tags[0]

        return filters

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _encode_query(
        self, query: str
    ) -> tuple[list[float], list[int], list[float]]:
        """쿼리를 dense 벡터 + sparse 인덱스/값으로 인코딩한다."""
        embed = _get_embed_model()
        out   = embed.encode(  # type: ignore[union-attr]
            [query],
            return_dense=True,
            return_sparse=True,
        )
        dense_vec: list[float] = out["dense_vecs"][0].tolist()
        lex: dict[int, float]  = out["lexical_weights"][0]
        return dense_vec, [int(k) for k in lex.keys()], [float(v) for v in lex.values()]

    def _hybrid_search(
        self,
        query: str,
        filters: dict[str, Any],
    ) -> list[AnyChunk]:
        """
        Qdrant Prefetch + RRF 퓨전으로 하이브리드 검색.

        dense 검색 + sparse 검색을 각각 실행하고 RRF로 병합한다.
        filters가 있으면 두 검색 모두에 동일하게 적용된다.
        """
        dense_vec, indices, values = self._encode_query(query)
        qdrant_filter = build_qdrant_filter(filters)

        prefetch = [
            Prefetch(
                query=dense_vec,
                using="dense",
                limit=self._candidate_k,
                filter=qdrant_filter,
            ),
            Prefetch(
                query=SparseVector(indices=indices, values=values),
                using="sparse",
                limit=self._candidate_k,
                filter=qdrant_filter,
            ),
        ]

        results = self._client.query_points(
            collection_name=self._collection,
            prefetch=prefetch,
            query=FusionQuery(fusion=models.Fusion.RRF),
            limit=self._candidate_k,
            with_payload=True,
        )

        chunks: list[AnyChunk] = []
        for point in results.points:
            if point.payload:
                chunk = _payload_to_chunk(point.payload)
                if chunk is not None:
                    chunks.append(chunk)
        return chunks

    def _rerank(self, query: str, chunks: list[AnyChunk]) -> list[AnyChunk]:
        """bge-reranker-v2-m3 크로스인코더로 재정렬한다."""
        reranker = _get_reranker()
        pairs    = [[query, c.embedding_text()] for c in chunks]
        scores: list[float] = reranker.compute_score(  # type: ignore[union-attr]
            pairs, normalize=True
        )
        ranked = sorted(zip(scores, chunks), key=lambda x: x[0], reverse=True)
        return [c for score, c in ranked if score >= _RERANK_SCORE_T]
