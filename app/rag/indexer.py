"""
Qdrant 인덱싱 모듈.

BGE-M3의 dense + sparse 벡터를 동시 생성하여 하이브리드 검색을 지원한다.

컬렉션 스키마:
  - dense  벡터: 차원 1024, 유사도 Cosine (BGE-M3 dense output)
  - sparse 벡터: BM25 계열 lexical weights (BGE-M3 sparse output)

페이로드 메타데이터 (MTFieldChunk 기준):
  - msg_type   : MT 타입 ("MT101" 등)
  - field_tag  : 필드 태그 ("20", "32B", "SYSTEM" 등)
  - doc_type   : "guidebook"
  - page_label : 시작 페이지 번호

레거시 SwiftChunk도 함께 인덱싱 가능.
"""
from __future__ import annotations

import os
import threading
from typing import Callable, Optional, Union

import structlog
from qdrant_client import QdrantClient

_log = structlog.get_logger(__name__)
from qdrant_client.models import (
    Distance,
    HnswConfigDiff,
    OptimizersConfigDiff,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

try:
    from tqdm import tqdm as _tqdm
    _TQDM_AVAILABLE = True
except ImportError:
    _TQDM_AVAILABLE = False

from app.rag.chunker import (
    MTEnrichedChunk,
    MTFieldChunk,
    MXEnrichedChunk,
    MXFieldChunk,
    SwiftChunk,
    chunk_id_to_point_id,
)

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
COLLECTION     = os.getenv("QDRANT_COLLECTION", "swift_guidebook")
QDRANT_URL     = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
EMBED_MODEL    = os.getenv("EMBED_MODEL", "BAAI/bge-m3")

_DENSE_DIM = 1024  # BGE-M3 dense 벡터 차원

# Union 타입: Enriched(신규 인제스트) + Base(기존) + Legacy(SwiftChunk) 모두 지원
AnyChunk = Union[MTEnrichedChunk, MXEnrichedChunk, MTFieldChunk, MXFieldChunk, SwiftChunk]


# ---------------------------------------------------------------------------
# BGE-M3 싱글턴
# ---------------------------------------------------------------------------
_embed_model: Optional[object] = None
_embed_lock   = threading.Lock()


def _get_embed_model():
    global _embed_model
    if _embed_model is not None:
        return _embed_model
    with _embed_lock:
        if _embed_model is None:
            try:
                from FlagEmbedding import BGEM3FlagModel
                _embed_model = BGEM3FlagModel(EMBED_MODEL, use_fp16=True)
            except ImportError as e:
                raise ImportError(
                    "FlagEmbedding 미설치. pip install FlagEmbedding 실행 후 재시도."
                ) from e
    return _embed_model


# ---------------------------------------------------------------------------
# Qdrant 클라이언트
# ---------------------------------------------------------------------------

def get_client() -> QdrantClient:
    """QDRANT_URL / QDRANT_API_KEY 환경변수 기반 클라이언트 반환."""
    return QdrantClient(
        url=QDRANT_URL,
        api_key=QDRANT_API_KEY,
        # client 와 server 마이너 버전 차이 경고 억제
        # (qdrant-client 1.18.x + server v1.13.x 조합에서 발생하는 UserWarning)
        check_compatibility=False,
    )


def check_connection(client: Optional[QdrantClient] = None) -> bool:
    """Qdrant 연결 여부 확인. 실패 시 False 반환."""
    c = client or get_client()
    try:
        c.get_collections()
        return True
    except Exception:
        return False


def collection_exists(client: QdrantClient, name: str = COLLECTION) -> bool:
    """컬렉션 존재 여부 확인.

    - 404 / UnexpectedResponse → 컬렉션 없음(False)
    - 네트워크 오류 → 경고 로그 후 False (호출자가 degraded 처리)
    - 그 외 예외 → 재발생 (버그 신호)
    """
    try:
        client.get_collection(name)
        return True
    except Exception as e:
        msg = str(e).lower()
        # 404 / "not found" → 컬렉션이 없는 정상 케이스
        if "not found" in msg or "404" in msg or "doesn't exist" in msg:
            return False
        # 연결/타임아웃 오류 → 경고 후 False (상위에서 degraded 처리)
        if any(k in msg for k in ("connection", "timeout", "refused", "unreachable")):
            _log.warning("collection_exists_connection_error", collection=name, error=str(e))
            return False
        # 그 외 → 예상치 못한 오류, 재발생
        _log.error("collection_exists_unexpected_error", collection=name, error=str(e))
        raise


# ---------------------------------------------------------------------------
# 컬렉션 관리
# ---------------------------------------------------------------------------

def create_collection(
    client: QdrantClient,
    name: str = COLLECTION,
    recreate: bool = False,
) -> None:
    """
    Dense + Sparse 하이브리드 컬렉션 생성.

    Named Vectors 구성:
      - "dense"  : 차원 1024, 유사도 Cosine
      - "sparse" : BGE-M3 lexical weights (SparseVectorParams)

    Args:
        recreate: True이면 기존 컬렉션 삭제 후 재생성.
    """
    if collection_exists(client, name):
        if recreate:
            client.delete_collection(name)
            print(f"기존 컬렉션 '{name}' 삭제됨.")
        else:
            print(f"컬렉션 '{name}' 이미 존재. 건너뜀 (--recreate 로 재생성 가능).")
            return

    client.create_collection(
        collection_name=name,
        vectors_config={
            "dense": VectorParams(
                size=_DENSE_DIM,
                distance=Distance.COSINE,
                hnsw_config=HnswConfigDiff(m=16, ef_construct=200),
            ),
        },
        sparse_vectors_config={
            "sparse": SparseVectorParams(),
        },
        optimizers_config=OptimizersConfigDiff(
            indexing_threshold=10_000,
        ),
    )
    print(
        f"컬렉션 '{name}' 생성 완료. "
        f"(dense={_DENSE_DIM}d/Cosine, sparse=BGE-M3 lexical)"
    )


# ---------------------------------------------------------------------------
# 인덱싱
# ---------------------------------------------------------------------------

def index_chunks(
    chunks: list[AnyChunk],
    batch_size: int = 16,
    collection: str = COLLECTION,
    client: Optional[QdrantClient] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> None:
    """
    청크 목록을 BGE-M3로 인코딩하여 Qdrant에 upsert한다.

    Args:
        chunks      : MTFieldChunk 또는 SwiftChunk 목록
        batch_size  : 배치 크기 (GPU: 32~64, CPU: 8~16 권장)
        collection  : 대상 Qdrant 컬렉션명
        client      : 미제공 시 환경변수에서 자동 생성
        progress_cb : (완료 수, 전체 수) → None 콜백;
                      미제공 시 tqdm 프로그레스 바 표시
    """
    if not chunks:
        return

    cl    = client or get_client()
    embed = _get_embed_model()
    total = len(chunks)

    # 진행 표시: tqdm 또는 콜백
    pbar = None
    if progress_cb is None and _TQDM_AVAILABLE:
        pbar = _tqdm(total=total, desc="Qdrant 적재", unit="청크")

    try:
        for start in range(0, total, batch_size):
            batch = chunks[start: start + batch_size]
            texts = [c.embedding_text() for c in batch]

            try:
                out = embed.encode(  # type: ignore[union-attr]
                    texts,
                    return_dense=True,
                    return_sparse=True,
                )
            except Exception as e:
                raise RuntimeError(f"BGE-M3 인코딩 실패 (배치 {start}): {e}") from e

            points: list[PointStruct] = []
            for j, chunk in enumerate(batch):
                lex: dict = out["lexical_weights"][j]

                # ── 페이로드: 각 청크의 전체 필드를 그대로 저장 ─────────────
                # MTFieldChunk → msg_type, field_tag, doc_type, page_label 포함
                # SwiftChunk   → message_type, field_tag, source_type, page 포함
                payload = chunk.model_dump()

                points.append(PointStruct(
                    id=chunk_id_to_point_id(chunk.chunk_id),
                    vector={
                        "dense": out["dense_vecs"][j].tolist(),
                        "sparse": SparseVector(
                            indices=[int(k) for k in lex.keys()],
                            values=[float(v) for v in lex.values()],
                        ),
                    },
                    payload=payload,
                ))

            try:
                cl.upsert(collection_name=collection, points=points)
            except Exception as e:
                raise RuntimeError(f"Qdrant upsert 실패 (배치 {start}): {e}") from e

            done = min(start + batch_size, total)
            if pbar:
                pbar.update(len(batch))
            elif progress_cb:
                progress_cb(done, total)
            else:
                print(f"  [{done}/{total}] 인덱싱 진행 중…")

    finally:
        if pbar:
            pbar.close()


# ---------------------------------------------------------------------------
# 컬렉션 정보 조회
# ---------------------------------------------------------------------------

def get_collection_info(client: Optional[QdrantClient] = None) -> dict:
    """컬렉션 상태 요약 딕셔너리 반환."""
    cl = client or get_client()
    try:
        info = cl.get_collection(COLLECTION)
        return {
            "name":           COLLECTION,
            "points":         info.points_count,
            "status":         str(info.status),
            "vectors_config": str(info.config.params.vectors),
        }
    except Exception as e:
        return {"error": str(e)}
