# =============================================================================
# Swift AI Agent — Python FastAPI
# Multi-stage build: builder(deps) → spacy-model → runtime
#
# 스테이지 구성:
#   builder      : 빌드 도구 + venv + Python 패키지 설치 (캐시 레이어 분리)
#   spacy-model  : spaCy 한국어 NER 모델 다운로드 (별도 레이어로 분리)
#   runtime      : 최소 런타임 이미지 — root 미사용, appuser 실행
# =============================================================================

# ── Stage 1: dependency builder ───────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# 빌드 전용 시스템 패키지 (최종 이미지에는 포함되지 않음)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        libffi-dev \
        git \
        curl \
    && rm -rf /var/lib/apt/lists/*

# 독립 venv 생성 — 런타임 스테이지로 복사하기 위해 고정 경로 사용
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

# pip + setuptools 최신 버전으로 업그레이드
# setuptools>=68 필수: pyproject.toml [build-system] backends.legacy 지원
RUN pip install --upgrade pip setuptools>=68 wheel --no-cache-dir

# ── Layer cache 최적화 핵심 ────────────────────────────────────────────────────
# pyproject.toml 만 먼저 복사 → 의존성만 설치 → 소스 변경 시 이 레이어 재사용
COPY pyproject.toml .

# tomllib(Python 3.11 표준 라이브러리)로 deps 추출 후 일괄 설치
# pyproject.toml 이 변경되지 않으면 Docker 레이어 캐시가 유지됨
# heredoc 대신 -c 단일라인 사용 — Docker BuildKit 버전 제약 없이 동작
RUN python3 -c "\
import tomllib, subprocess, sys; \
deps = tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']; \
subprocess.run([sys.executable,'-m','pip','install','--no-cache-dir']+deps, check=True)"


# ── Stage 2: spaCy 한국어 NER 모델 다운로드 ────────────────────────────────────
# 소스 복사 없이 deps 레이어 위에서 모델만 다운로드
# → 소스 변경 시 이 레이어는 재사용됨 (deps 변경 시에만 무효화)
FROM builder AS spacy-model
RUN python -m spacy download ko_core_news_lg --no-cache-dir 2>/dev/null || \
    python -m spacy download ko_core_news_lg


# ── Stage 3: 최소 런타임 이미지 ────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# 비-root 사용자 생성 (UID/GID 1001 고정 — 볼륨 마운트 퍼미션 일관성)
RUN groupadd -r appgroup --gid 1001 \
    && useradd -r -g appgroup --uid 1001 -d /app -s /sbin/nologin appuser

# 런타임 시스템 의존성 (OpenMP — FlagEmbedding/BGE-M3 필요)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# venv 복사 (모든 패키지 + spaCy 모델 포함)
COPY --from=spacy-model /venv /venv
ENV PATH="/venv/bin:$PATH"

# HuggingFace 모델 캐시 경로 — 볼륨 마운트로 재시작 시 재다운로드 방지
ENV HF_HOME="/app/.hf_cache"

# PYTHONPATH: 소스를 패키지로 설치하지 않고 /app 에서 직접 import
# uvicorn 이 `app.main:app` 을 찾을 때 /app 디렉토리를 참조함
ENV PYTHONPATH="/app"

WORKDIR /app

# 앱 소스 복사 및 소유권 설정
COPY --chown=appuser:appgroup . .

# 로그·캐시 디렉토리 사전 생성 (볼륨 마운트 시 퍼미션 보존)
RUN mkdir -p /app/logs /app/.hf_cache /app/schema_cache \
    && chown -R appuser:appgroup /app/logs /app/.hf_cache /app/schema_cache

# 비-root 사용자로 전환
USER appuser

EXPOSE 8000

# Kubernetes/Docker 헬스체크
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# 프로덕션용 uvicorn 실행
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--log-level", "info", \
     "--access-log"]
