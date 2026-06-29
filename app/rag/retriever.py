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
    MTEnrichedChunk,
    MTFieldChunk,
    MXEnrichedChunk,
    MXFieldChunk,
    SwiftChunk,
    chunk_id_to_point_id,
)
from app.rag.indexer import COLLECTION, EMBED_MODEL, QDRANT_URL, _get_embed_model

RERANKER_MODEL  = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
_CANDIDATE_K    = 30
_RERANK_SCORE_T = 0.0

AnyChunk = Union[
    MTEnrichedChunk, MXEnrichedChunk,   # 고도화 인제스트 (신규)
    MTFieldChunk,    MXFieldChunk,       # 기본 인제스트
    CbprSr2026Chunk,                     # CBPR+ SR2026 CSV
    SwiftChunk,                          # 레거시
]


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

# MX 메시지 타입 키워드 패턴
# 전체 버전(pacs.008.001.08) 우선 매칭, 없으면 기본형(pacs.008) 매칭
# ※ \b 대신 ASCII 전용 lookaround 사용:
#   Python Unicode 모드에서 \b는 한국어 등 유니코드 문자를 \w로 처리하므로
#   "pacs.008의" 같은 문자열의 경계를 제대로 인식하지 못한다.
_RE_MX_MSG_TYPE = re.compile(
    r"(?<![a-zA-Z0-9])([a-z]{3,4}\.\d{3}(?:\.\d{3}\.\d{2,3})?)(?![a-zA-Z0-9.])",
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

    공통 키:
      msg_type     : str | list[str]  — "MT101", "pacs.008.001.14"
      doc_type     : str              — "guidebook" | "mx_guide" | "cbpr_sr2026_cr"
      category     : str | list[str]  — MT: "Category1" / MX: "pacs", "camt"
      doc_category : str              — "MT" | "MX"  (고도화 인제스트 전용)

    MTFieldChunk / MTEnrichedChunk 키:
      field_tag    : str | list[str]  — "32B", ["32B","50a"]
      section_type : str | list[str]  — "field_spec", "message_rule", "usage_rule"
      sequence     : str              — "A" | "B" | "none"  (MTEnrichedChunk 전용)

    MXFieldChunk / MXEnrichedChunk 키:
      xml_tag      : str              — "MsgId", "IntrBkSttlmAmt"
      field_path   : str              — "GrpHdr/MsgId"
      doc_subtype  : str              — "cbpr_plus" | "standard"                    (MXEnrichedChunk 전용)
      section      : str              — "Element_Specifications", "Business_Rules"  (MXEnrichedChunk 전용)
      element_name : str              — "MsgId", "GrpHdr"                           (MXEnrichedChunk 전용)
      mult_norm    : str              — "1..1", "0..1", "1..*"                       (MXEnrichedChunk 전용)

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

    판별 기준 (doc_category 우선, doc_type 보조):
      doc_category="MT" + doc_type="guidebook" → MTEnrichedChunk  (신규 고도화)
      doc_category="MX" + doc_type="mx_guide"  → MXEnrichedChunk  (신규 고도화)
      doc_type="cbpr_sr2026_cr"                → CbprSr2026Chunk
      doc_type="mx_guide"  (구형)              → MXFieldChunk
      doc_type="guidebook" (구형)              → MTFieldChunk
      그 외                                    → SwiftChunk (레거시)

    Pydantic v2 기본 설정(extra='ignore')에 의해
    모델에 없는 페이로드 필드는 자동으로 무시된다.
    """
    if not payload:
        return None
    try:
        doc_type     = payload.get("doc_type", "")
        doc_category = payload.get("doc_category", "")

        # ── 고도화 인제스트 (MTEnrichedChunk / MXEnrichedChunk) ─────────────
        if doc_category == "MT" and doc_type == "guidebook":
            return MTEnrichedChunk(**payload)

        if doc_category == "MX" and doc_type == "mx_guide":
            return MXEnrichedChunk(**payload)

        # ── 기본 인제스트 ─────────────────────────────────────────────────
        if doc_type == "cbpr_sr2026_cr":
            return CbprSr2026Chunk(**payload)

        if doc_type == "mx_guide":
            return MXFieldChunk(**payload)

        if doc_type == "guidebook" or "msg_type" in payload:
            return MTFieldChunk(**payload)

        # ── 레거시 ────────────────────────────────────────────────────────
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
        self._client      = QdrantClient(
            url=qdrant_url,
            check_compatibility=False,
        )
        # base type → 최고 버전 맵 (lazy 빌드, 예: "pacs.008" → "pacs.008.001.14")
        # CBPRPlus가 있으면 CBPRPlus 버전 우선
        self._mx_version_map: Optional[dict[str, str]] = None
        self._version_map_time: float = 0.0
        self._version_map_lock = threading.Lock()
        self._VERSION_MAP_TTL = 3600.0  # 1시간

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
        include_parents: bool = False,
    ) -> list[AnyChunk]:
        """
        하이브리드 검색 → 재정렬 → 상위 K개 반환.

        자동 라우팅 우선순위 (auto_filter=True):
          1. XPath 패턴 감지   → field_path 필터 + doc_type 필터 적용
          2. SR2026/CBPR+ 키워드 → doc_type="cbpr_sr2026_cr" 필터 적용
          3. MX msg_type 감지  → msg_type 필터 적용 (mx_guide + sr2026 모두)
          4. MT 필드 태그 감지  → field_tag 필터 적용 (guidebook)

        Args:
            query           : 자연어 또는 SWIFT 키워드 쿼리
            filters         : Qdrant 메타데이터 필터 딕셔너리 (수동 지정)
            top_k           : 최종 반환 청크 수 (부모 청크는 포함하지 않음)
            rerank          : True이면 bge-reranker 재정렬 수행
            auto_filter     : True이면 쿼리에서 라우팅 정보를 자동 추출하여
                              메타데이터 필터를 동적으로 추가
            include_parents : True이면 rule 청크의 parent_id로 field 부모 청크를
                              함께 끌어와 반환 (small-to-big retrieval).
                              부모 청크는 top_k 계산에 포함되지 않고 뒤에 추가된다.
        """
        effective_filters = dict(filters or {})

        if auto_filter:
            effective_filters = self._apply_auto_filters(query, effective_filters)

        # MX 검색 시 CBPRPlus 우선 → 결과 없으면 standard fallback
        candidates = self._search_with_cbprplus_priority(query, effective_filters)
        if not candidates:
            return []

        if rerank and len(candidates) > 1:
            try:
                candidates = self._rerank(query, candidates)
            except Exception as exc:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "rerank_failed_using_hybrid_order: %s", str(exc)
                )

        top_candidates = candidates[:top_k]

        if include_parents:
            top_candidates = self._expand_with_parents(top_candidates)

        return top_candidates

    def _search_with_cbprplus_priority(
        self,
        query: str,
        filters: dict[str, Any],
    ) -> list[AnyChunk]:
        """
        MX (doc_type=mx_guide) 검색 시 CBPRPlus 우선 검색.

        1단계: doc_subtype=cbpr_plus 필터 추가하여 검색
        2단계: 결과가 없으면 doc_subtype 필터 없이 전체 검색 (standard fallback)
        MX 외 검색(MT, CBPR+ SR2026 CR 등)은 우선순위 로직 없이 바로 검색.
        """
        is_mx_guide = filters.get("doc_type") == "mx_guide"
        if not is_mx_guide:
            return self._hybrid_search(query, filters)

        # 1단계: CBPRPlus 우선
        cbpr_filters = {**filters, "doc_subtype": "cbpr_plus"}
        candidates = self._hybrid_search(query, cbpr_filters)
        if candidates:
            return candidates

        # 2단계: CBPRPlus 미적재 msg_type → standard fallback
        return self._hybrid_search(query, filters)

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
                # 기본형(pacs.008)이면 Qdrant에 저장된 최고 버전으로 해석
                resolved = [self._resolve_mx_msg_type(t) for t in mx_info.msg_types]
                filters["msg_type"] = (
                    resolved if len(resolved) > 1 else resolved[0]
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

    def _expand_with_parents(self, chunks: list[AnyChunk]) -> list[AnyChunk]:
        """
        rule 청크의 parent_id 로 field 부모 청크를 Qdrant 에서 조회하여 추가한다.

        - parent_id 는 SwiftChunk 에만 존재하며 UUID 문자열(= Qdrant point id).
        - 이미 결과에 포함된 청크는 중복 추가하지 않는다.
        - Qdrant 조회 실패 시 원본 chunks 를 그대로 반환한다.
        """
        existing_ids: set[str] = {c.chunk_id for c in chunks}
        parent_ids: list[str] = list({
            c.parent_id  # type: ignore[union-attr]
            for c in chunks
            if getattr(c, "parent_id", None) and c.parent_id not in existing_ids
        })

        if not parent_ids:
            return chunks

        try:
            points = self._client.retrieve(
                collection_name=self._collection,
                ids=parent_ids,
                with_payload=True,
            )
            parent_chunks: list[AnyChunk] = []
            for point in points:
                if point.payload:
                    chunk = _payload_to_chunk(point.payload)
                    if chunk is not None:
                        parent_chunks.append(chunk)
            return chunks + parent_chunks
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "expand_parents_failed", extra={"error": str(exc)}
            )
            return chunks

    def _get_mx_version_map(self) -> dict[str, str]:
        """
        MX 청크에서 base_type → 최고 full_version 맵을 빌드하여 캐시한다.
        CBPRPlus 버전이 있으면 CBPRPlus 우선, 없으면 standard 최고 버전.

        예: {"pacs.008": "pacs.008.001.08", "admi.024": "admi.024.001.01",
              "pacs.006": "pacs.006.001.14"}
        """
        import time as _time
        if self._mx_version_map is not None:
            if _time.time() - self._version_map_time < self._VERSION_MAP_TTL:
                return self._mx_version_map
            self._mx_version_map = None  # TTL 만료

        with self._version_map_lock:
            if self._mx_version_map is not None and \
               _time.time() - self._version_map_time < self._VERSION_MAP_TTL:
                return self._mx_version_map

            # base → {cbpr_plus: highest, standard: highest}
            staging: dict[str, dict[str, str]] = {}

            qdrant_filter = build_qdrant_filter({"doc_type": "mx_guide"})
            offset = None
            _MAX_SCROLL = 200  # 최대 200회(×500 = 10만 포인트) 초과 시 중단
            _scroll_count = 0
            while _scroll_count < _MAX_SCROLL:
                points, next_offset = self._client.scroll(
                    collection_name=self._collection,
                    scroll_filter=qdrant_filter,
                    limit=500,
                    offset=offset,
                    with_payload=["msg_type", "doc_subtype"],
                )
                _scroll_count += 1
                for pt in points:
                    p = pt.payload or {}
                    full = p.get("msg_type", "")
                    parts = full.split(".")
                    if len(parts) < 2:
                        continue
                    base = f"{parts[0]}.{parts[1]}"
                    sub = p.get("doc_subtype", "standard")
                    if base not in staging:
                        staging[base] = {}
                    prev = staging[base].get(sub, "")
                    # 버전 비교: 제로패딩된 SWIFT 버전은 lexicographic 정렬로 충분
                    if not prev or full > prev:
                        staging[base][sub] = full
                if next_offset is None:
                    break
                offset = next_offset

            # CBPRPlus 우선, 없으면 standard 최고 버전
            result: dict[str, str] = {}
            for base, versions in staging.items():
                result[base] = versions.get("cbpr_plus") or versions.get("standard", base)

            self._mx_version_map = result
            self._version_map_time = _time.time()
            return result

    def _resolve_mx_msg_type(self, queried: str) -> str:
        """
        사용자가 입력한 msg_type을 Qdrant 저장 버전으로 해석한다.

        - 전체 버전 입력 (pacs.008.001.08) → 그대로 반환
        - 기본형 입력 (pacs.008, admi.024) → 캐시에서 최고 버전 반환
        """
        if queried.count(".") >= 3:
            return queried  # 이미 full version
        version_map = self._get_mx_version_map()
        resolved = version_map.get(queried.lower(), queried)
        if resolved == queried and queried.lower() not in version_map:
            import structlog as _slog
            _slog.get_logger(__name__).warning(
                "mx_msg_type_unresolved",
                queried=queried,
                available_count=len(version_map),
            )
        return resolved
