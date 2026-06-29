# SWIFT AI Agent — 프로젝트 아키텍처 및 활용 매뉴얼

> 최종 업데이트: 2026-06-28
> 대상 독자: 신규 합류 개발자, 운영 담당자, 업무 기획자

---

## 목차

1. [프로젝트 개요 및 목적](#1-프로젝트-개요-및-목적)
2. [실 사용 예시 및 기대 효과](#2-실-사용-예시-및-기대-효과)
3. [시스템 아키텍처 구성도 설명](#3-시스템-아키텍처-구성도-설명)
4. [주요 컴포넌트 역할 및 데이터 흐름](#4-주요-컴포넌트-역할-및-데이터-흐름)
5. [API 엔드포인트 레퍼런스](#5-api-엔드포인트-레퍼런스)
6. [환경 설정 및 배포 가이드](#6-환경-설정-및-배포-가이드)
7. [운영 모니터링 가이드](#7-운영-모니터링-가이드)

---

## 1. 프로젝트 개요 및 목적

### 1.1 도입 배경

국제 은행 간 자금 이체는 **SWIFT(국제은행간통신협회)** 가 정의한 표준 전문(메시지) 형식으로 이루어집니다. 현재 글로벌 금융권은 두 가지 전문 형식을 병행 사용합니다.

| 구분 | 형식 | 설명 | 예시 |
|------|------|------|------|
| 구형 | **MT (Message Type)** | 텍스트 블록 기반 레거시 형식 | MT103 (고객 송금), MT202 (은행 간 송금) |
| 신형 | **MX (ISO 20022)** | XML 기반 차세대 국제 표준 | pacs.008 (고객 송금), pacs.009 (은행 간 송금) |

SWIFT는 2025년부터 **CBPRPlus(Cross-Border Payments and Reporting Plus)** 가이드라인을 통해 MX 전환을 의무화하고 있습니다. 이에 따라 금융기관은 다음과 같은 실무 과제를 안고 있습니다.

- MT 전문을 MX로 변환(업리프트)하는 과정에서 **필드 매핑 규칙이 수백 페이지 분량의 가이드북**에 산재
- 전문 하나의 의미론적 유효성 검증에 **숙련 담당자가 30분~1시간** 소요
- CBPR+ 조건부 규칙(예: "고객 주소가 있으면 :50F: 구조화 형식 필수")이 복잡하여 **수동 오류 빈발**
- 신규 담당자 온보딩에 **수개월의 학습 기간** 필요

### 1.2 해결하고자 하는 문제

```
기존 방식:
  담당자 → 전문 수동 확인 → 가이드북 참조 → 오류 검토 → 수동 변환/생성
  (소요 시간: 30분~수 시간 / 건 / 오류율: 담당자 경험에 의존)

AI 에이전트 도입 후:
  담당자 → API 호출 → AI 자동 처리 → 검수 승인 → 완료
  (소요 시간: 수 초 ~ 수 분 / 건 / 일관성: 규칙 기반 + LLM 보완)
```

### 1.3 프로젝트 핵심 목적

**SWIFT AI Agent**는 MT/MX SWIFT 전문의 다음 4가지 업무를 AI로 자동화합니다.

| 기능 | 설명 |
|------|------|
| **분석(Analyze)** | 전문의 구문 오류·의미 위반·조건부 규칙 위반을 자동 감지하고 판정(PASS/WARNING/REJECT) |
| **생성(Generate)** | 자연어 요청을 받아 적합한 MT 또는 MX 전문 초안을 자동 생성 |
| **변환(Map)** | MT 전문을 MX로, 또는 MX를 MT로 필드 단위로 변환(업리프트/다운리프트) |
| **설명(Explain)** | 전문 유형의 목적·사용 시나리오·주요 필드를 자연어로 설명 |

---

## 2. 실 사용 예시 및 기대 효과

### 2.1 시나리오 A — 전문 유효성 검증 (Analyze)

**상황:** 해외 송금 처리 담당자가 수신한 MT103 전문이 올바른지 검증해야 한다.

```json
POST /convert
{
  "raw_message": "{1:F01BNKAKRSEAXXX...}{4:\n:20:REF001\n:32A:240115EUR5000,00\n-}",
  "msg_type": "MT103",
  "user_intent": "analyze"
}
```

**AI 처리 과정:**
1. IBAN/BIC 등 개인정보(PII)를 플레이스홀더로 마스킹
2. Prowide 엔진으로 구문·필드 형식 검증 (결정론적)
3. RAG로 관련 CBPR+ 가이드라인 검색
4. LLM으로 의미·조건부 규칙 위반 감지
5. 두 결과 병합 → 최종 판정

**결과 예시:**
```json
{
  "status": "completed",
  "output": {
    "verdict": "WARNING",
    "field_analysis": [
      {
        "field_tag": "50K",
        "issue": "CBPR+ 권장: 구조화 주소(:50F:) 사용 권장",
        "severity": "warning"
      }
    ]
  },
  "validation_result": {
    "verdict": "WARNING",
    "needs_hitl": true
  }
}
```

---

### 2.2 시나리오 B — 전문 생성 (Generate)

**상황:** 결제팀이 특정 거래에 대한 pacs.008 MX 전문을 새로 작성해야 한다.

```json
POST /convert
{
  "raw_message": "독일 프랑크푸르트 DEUTDEDB 은행에서 한국 BNKAKRSE 은행으로 EUR 15,000 송금. 수취인: 홍길동, 계좌: DE89370400440532013000",
  "user_intent": "generate"
}
```

**결과:** AI가 CBPR+ 가이드라인에 맞는 XML 초안을 생성하고, 담당자가 HITL(인간 검수) 화면에서 내용 확인 후 `approve` 또는 `modify` 결정.

---

### 2.3 시나리오 C — MT↔MX 변환 (Map)

**상황:** 레거시 MT202 전문을 ISO 20022 pacs.009 MX 전문으로 업리프트해야 한다.

```json
POST /convert
{
  "raw_message": "{4:\n:20:TREF20240115\n:21:REL20240115\n:32A:240115USD100000,00\n...-}",
  "msg_type": "MT202",
  "user_intent": "map"
}
```

**결과:**
- Prowide가 1차 구조 변환 수행
- LLM이 필드 매핑 명세(FieldMapping) 생성 — 각 필드가 어느 MX 경로로 이동하는지 설명
- 가이드북 근거(페이지 번호, 규칙 ID) 함께 제공

---

### 2.4 시나리오 D — 전문 유형 설명 (Explain)

**상황:** 신입 직원이 MT103 전문의 목적과 주요 필드를 빠르게 파악하고 싶다.

```json
POST /convert
{
  "raw_message": "MT103 전문이 무엇인지 설명해줘",
  "user_intent": "explain"
}
```

**결과:** 전문 공식 명칭, 한국어 명칭, 사용 시나리오, 주요 필드 설명, 관련 전문 목록을 구조화된 형태로 반환.

---

### 2.5 기대 효과 요약

| 항목 | 도입 전 | 도입 후 |
|------|---------|---------|
| 전문 검증 시간 | 건당 30분~1시간 | 수 초 (AI 자동) + 수 분 (검수 승인) |
| 변환 작업 | 가이드북 수동 참조 | 필드 매핑 명세 자동 생성 |
| 오류 탐지율 | 담당자 숙련도 의존 | Prowide + LLM 이중 검증 |
| 신규 직원 적응 | 수개월 학습 필요 | Explain 기능으로 즉시 참조 가능 |
| 감사 추적 | 수동 기록 | 모든 처리 결과 JSONL 자동 저장 |

---

## 3. 시스템 아키텍처 구성도 설명

### 3.1 전체 구성 개요

```
┌─────────────────────────────────────────────────────────────────────┐
│                       클라이언트 / 외부 시스템                          │
│            (웹 UI, 은행 시스템, REST API 클라이언트)                     │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ HTTP REST
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   swift-agent  (Python / FastAPI)                    │
│                                                                      │
│  ┌───────────┐   ┌────────────────────────────────────────────────┐ │
│  │  REST API │   │            LangGraph 파이프라인                  │ │
│  │  Layer    │──▶│  pii_mask → supervisor → [agent] →             │ │
│  │           │   │  hitl_checkpoint → unmask → audit → END        │ │
│  └───────────┘   └────────────────────────────────────────────────┘ │
│                          │              │              │             │
│                   ┌──────┘    ┌─────────┘    ┌────────┘             │
│                   ▼           ▼              ▼                       │
│             ┌──────────┐ ┌────────┐ ┌──────────────┐               │
│             │ Prowide  │ │  LLM   │ │  Qdrant RAG  │               │
│             │  Client  │ │ Client │ │   Retriever  │               │
│             └────┬─────┘ └───┬────┘ └──────┬───────┘               │
└──────────────────│───────────│─────────────│─────────────────────────┘
                   │           │             │
        ┌──────────┘   ┌───────┘    ┌───────┘
        ▼              ▼            ▼
┌──────────────┐ ┌────────────┐ ┌──────────────┐
│ prowide-svc  │ │  LLM 서비스 │ │    Qdrant    │
│ (Java Spring │ │ (vLLM /    │ │  Vector DB   │
│  Boot)       │ │  Claude /  │ │              │
│              │ │  Ollama)   │ │  · MT 가이드  │
│ · MT 구문검증 │ │            │ │  · MX 가이드  │
│ · MX XSD검증 │ │            │ │  · CBPR+ 규칙│
│ · MT↔MX변환  │ │            │ │              │
└──────────────┘ └────────────┘ └──────────────┘
```

### 3.2 기술 스택 요약

| 레이어 | 기술 | 버전 | 역할 |
|--------|------|------|------|
| **API 서버** | Python + FastAPI | 3.11 / 0.111+ | REST 엔드포인트, 비동기 처리 |
| **AI 파이프라인** | LangGraph + LangChain | 0.2+ / 0.3+ | 에이전트 그래프 오케스트레이션 |
| **LLM** | Claude (Anthropic) / vLLM / Ollama | — | 의미 분석, 전문 생성, 매핑 |
| **임베딩/재정렬** | BGE-M3 + BGE-Reranker-v2-m3 | FlagEmbedding 1.2+ | 하이브리드 벡터 검색 |
| **벡터 DB** | Qdrant | v1.13.6 | 가이드북 RAG 저장 및 검색 |
| **룰 엔진** | Prowide (Java Spring Boot) | — | SWIFT 구문/XSD 결정론적 검증 |
| **PII 마스킹** | Presidio + spaCy | 2.2+ / 3.7+ | 개인정보 플레이스홀더 치환 |
| **컨테이너** | Docker Compose | v2 | 4-서비스 통합 기동 |
| **로깅** | structlog | 24.0+ | 구조화 JSON 로그 |

---

### 3.3 컨테이너 구성 및 기동 순서

시스템은 Docker Compose로 4개 컨테이너가 함께 기동됩니다. 의존성 순서가 있으므로 **healthcheck 통과 후 다음 서비스가 기동**됩니다.

```
[1] qdrant          (포트 6333)
      healthcheck: TCP 6333 포트 응답 확인
         ↓ healthy
[2] prowide-svc     (포트 8080)
      healthcheck: /actuator/health → {"status":"UP"}
         ↓ healthy
[3] swift-agent     (포트 8000)
      healthcheck: GET /health → {"status":"ok"}
```

---

## 4. 주요 컴포넌트 역할 및 데이터 흐름

### 4.1 요청부터 응답까지 — 전체 흐름

아래는 `POST /convert` 요청이 처리되는 전 과정입니다.

```
클라이언트 요청
     │
     ▼
[FastAPI /convert]
  · thread_id 발급 (UUID)
  · 초기 상태 구성
     │
     ▼
[1. pii_mask 노드]  ─────────────────────────────────────────────────
  · IBAN, BIC, 계좌번호, 금액 → <<IBAN_1>>, <<BIC_1>> 플레이스홀더 치환
  · 한국어 텍스트는 spaCy NER로 인명/기관명 추가 마스킹
  · 원본↔플레이스홀더 매핑을 state["pii_mapping"]에 저장
  · LLM에는 마스킹본(masked_message)만 전달됨
     │
     ▼
[2. supervisor 노드]  ──────────────────────────────────────────────
  · 키워드 매칭으로 의도 빠른 분류 시도
    - 키워드 미일치 시 LLM fallback 분류
  · user_intent: analyze / generate / map / explain / schema
  · routed_agent 결정 → 해당 에이전트 노드로 분기
     │
     ├──────────────┬──────────────┬─────────────┬──────────────┐
     ▼              ▼              ▼             ▼              ▼
[3a. analyzer] [3b. generator] [3c. mapper] [3d. explainer] [3e. schema]
     │              │              │             │              │
     └──────────────┴──────────────┘             └──────────────┘
            │ (HITL 필요)                             │ (HITL 불필요)
            ▼                                         │
[4. hitl_checkpoint 노드]                             │
  · needs_hitl=True: interrupt() 호출 → 대기          │
  · API 응답: status=pending_hitl                     │
  · 검수자 POST /resume 호출                          │
  · needs_hitl=False: 바로 통과                       │
            │                                         │
            ├── reject 결정 ──▶ [reject 노드]         │
            │                       │                 │
            └── approve/modify ─────┴─────────────────┘
                                    │
                                    ▼
[5. unmask 노드]  ──────────────────────────────────────────────────
  · state["output"] 전체를 순회하며 <<IBAN_1>> → 실제 IBAN 복원
  · 복원 후 남은 플레이스홀더 있으면 경고 로그
     │
     ▼
[6. audit 노드]  ───────────────────────────────────────────────────
  · 처리 결과를 JSONL 형식으로 감사 로그에 기록
  · thread_id, msg_type, verdict, routed_agent, timestamp 포함
     │
     ▼
[FastAPI 응답 반환]
  · status: completed / pending_hitl / failed
  · output: 에이전트별 산출물
  · validation_result: 검증 상세
```

---

### 4.2 각 에이전트 상세 역할

#### Analyzer (분석 에이전트)

**목적:** 기존 전문의 유효성을 이중 검증(Prowide + LLM)하고 최종 판정을 내린다.

```
입력: raw_message, masked_message, msg_type
  │
  ├─[1] Prowide 구문 검증 (결정론적)
  │       · MT: 필드 존재·순서·형식 규칙
  │       · MX: ISO 20022 XSD 스키마 검증
  │       · 결과: syntax_ok, problems[]
  │
  ├─[2] RAG 검색 (Qdrant)
  │       · 해당 msg_type + doc_category 필터
  │       · CBPRPlus 가이드 우선, 없으면 standard fallback
  │       · Dense + Sparse 하이브리드 검색 → BGE-Reranker 재정렬
  │       · 관련 규칙 청크 상위 8개 반환
  │
  ├─[3] LLM 의미 분석 (구조화 출력)
  │       · 검색된 가이드라인을 프롬프트에 주입
  │       · 위반(violations), 경고(warnings), 조건부 규칙 감지
  │       · verdict: PASS / WARNING / REJECT
  │
  └─[4] reconcile() — 결과 병합
          · Prowide 오류 있으면 → REJECT 확정
          · CBPR+ 권장 사항은 위반→경고로 재분류
          · 최종 verdict + needs_hitl 결정
```

**판정 기준:**

| 판정 | 조건 |
|------|------|
| `PASS` | Prowide 오류 없음 + LLM 위반 없음 |
| `WARNING` | Prowide 오류 없음 + CBPR+ 권장 사항만 존재 |
| `REJECT` | Prowide 오류 있음 OR 의미적 위반 존재 |
| `ERROR` | Prowide 서비스 장애 (fail-safe HITL 발동) |

---

#### Generator (생성 에이전트)

**목적:** 자연어 요청을 받아 MT 또는 MX 전문 XML 초안을 생성한다.

```
입력: masked_message (자연어 요청)
  │
  ├─[1] RAG 검색 — 전문 구조·필수 필드 규칙 검색
  ├─[2] LLM XML 생성 — 가이드라인 기반 전문 초안 작성
  ├─[3] XML 문법 검증 — ET.fromstring()으로 유효성 확인
  └─[4] 실패 시 Jinja2 폴백 — pacs.008 템플릿으로 기본 구조 보장

출력: draft (XML 문자열), xml_valid, xml_error
※ 생성 결과는 항상 needs_hitl=True → 반드시 검수 승인 필요
```

---

#### Mapper (변환 에이전트)

**목적:** MT 전문을 MX로, 또는 MX를 MT로 필드 단위 변환 명세를 생성한다.

```
입력: raw_message, msg_type
  │
  ├─[1] msg_type 자동 감지 — MT 헤더 블록 또는 XML 네임스페이스 파싱
  ├─[2] Prowide 1차 변환 — 구조 변환 초안 생성 (best-effort)
  ├─[3] RAG 검색 — 필드 매핑 가이드라인 검색
  └─[4] LLM 구조화 매핑 명세 생성
          · FieldMapping: mt_tag ↔ mx_paths, 변환값, 가이드북 근거
          · 매핑 근거 없는 필드: is_unmapped=True

출력: mapper_output (FieldMapping 배열), unmapped_fields, enhancement_warnings
※ 변환 결과는 항상 needs_hitl=True → 반드시 검수 승인 필요
```

---

#### Explainer (설명 에이전트)

**목적:** 전문 유형의 목적·필드·사용 시나리오를 자연어로 설명한다.

- MT 전문: key_fields 테이블 형식으로 주요 필드 설명
- MX 전문: schema_explorer와 연동하여 전체 스키마 트리 병합 제공
- 결과 캐싱: 동일 msg_type 재요청 시 LLM 재호출 없이 즉시 반환
- **HITL 불필요** — 참조용 정보이므로 바로 응답

---

#### Schema Explorer (스키마 탐색 에이전트)

**목적:** ISO 20022 MX 전문의 전체 필드 구조(스키마 트리)를 섹션별로 생성한다.

- 가이드북 RAG + LLM으로 섹션별 XML 태그·다중도·필수여부 구조화
- 결과 JSON 캐싱 (`schema_cache/` 볼륨, TTL 없음)
- **HITL 불필요** — 스키마 정보이므로 바로 응답

---

### 4.3 PII(개인정보) 보안 흐름

SWIFT 전문에는 IBAN, BIC, 계좌번호, 금액 등 민감 정보가 포함됩니다. 본 시스템은 **LLM에 원본 정보를 절대 노출하지 않는** 구조를 취합니다.

```
원본 전문 (raw_message)
  "DE89370400440532013000"  ← IBAN

         │
         ▼ [pii_mask 노드]

마스킹본 (masked_message)
  "<<IBAN_1>>"              ← 플레이스홀더

         │                          │
         ▼                          ▼
   [LLM 분석·생성]           [Prowide 검증]
   (마스킹본만 처리)           (원본 전문 처리)

         │
         ▼ [unmask 노드]

복원된 출력
  "DE89370400440532013000"  ← 원본 복원
```

| 정보 유형 | 플레이스홀더 패턴 | 예시 원본값 |
|-----------|-----------------|------------|
| IBAN | `<<IBAN_N>>` | `DE89370400440532013000` |
| BIC | `<<BIC_N>>` | `DEUTDEDB` |
| 계좌번호 | `<<ACCT_N>>` | `/12345678901234` |
| 금액 | `<<AMT_N>>` | `5000,00` |
| 한국어 인명/기관 | `<<PS_N>>`, `<<OG_N>>` | 홍길동, 국민은행 |

---

### 4.4 HITL(인간 검수) 흐름

고위험 처리(분석 REJECT/WARNING, 생성, 변환)는 반드시 사람이 검수한 뒤 최종 처리됩니다.

```
[1] POST /convert
      └─ 처리 후 needs_hitl=True
      └─ 응답: { "status": "pending_hitl", "thread_id": "abc-123" }

[2] 검수자가 내용 확인 (hitl_payload 참조)

[3] POST /resume
      {
        "thread_id": "abc-123",
        "action": "approve" | "reject" | "modify",
        "comment": "선택적 코멘트"
      }

[4] 파이프라인 재개
      · approve / modify → unmask → audit → 완료
      · reject           → reject 노드 → audit → 완료 (output.status="rejected")
```

---

### 4.5 RAG(검색 증강 생성) 구조

LLM이 SWIFT 가이드라인 없이 혼자 판단하면 오류(환각)가 발생할 수 있습니다. 본 시스템은 Qdrant 벡터 DB에 가이드북을 사전 인덱싱하고, 요청마다 관련 규칙을 검색하여 LLM 프롬프트에 주입합니다.

```
[사전 인덱싱]
  data/MT/*.pdf, data/MX/*.pdf
        │
        ▼ [chunker.py]
  청크 분할 (섹션·필드·규칙 단위)
        │
        ▼ [BGE-M3 임베딩]
  Dense 벡터 (1024차원) + Sparse 벡터 (BM25 계열)
        │
        ▼ [indexer.py]
  Qdrant 저장 (메타데이터: msg_type, field_tag, doc_type, doc_category)

[검색 시]
  쿼리 → BGE-M3 임베딩
        │
        ├─ Dense Prefetch (코사인 유사도)
        └─ Sparse Prefetch (어휘 일치)
                │
                ▼ RRF 퓨전 (상위 30개)
                │
                ▼ BGE-Reranker-v2-m3 (교차 인코더 재정렬)
                │
                ▼ 상위 K개 규칙 청크 → LLM 프롬프트 주입
```

**CBPRPlus 우선 검색 전략:** 동일 필드에 대해 CBPRPlus 전용 가이드(`doc_subtype=cbpr_plus`)를 먼저 검색하고, 결과가 없으면 standard 가이드로 자동 폴백합니다.

---

## 5. API 엔드포인트 레퍼런스

전체 Swagger UI: `http://localhost:8000/docs`

### 5.1 주요 엔드포인트

| 메서드 | 경로 | 설명 |
|--------|------|------|
| `POST` | `/convert` | 새 SWIFT 전문 처리 시작 |
| `POST` | `/resume` | HITL 대기 스레드 재개 |
| `GET` | `/convert/{thread_id}` | 처리 상태 조회 |
| `GET` | `/health` | 서비스 헬스체크 (Prowide + Qdrant) |
| `GET` | `/msg-types` | 인덱싱된 전문 유형 목록 조회 |
| `GET` | `/threads` | 활성 스레드 목록 (HITL 대기 포함) |

### 5.2 `/convert` 요청 파라미터

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| `raw_message` | string | ✅ | 처리할 원본 전문 (최대 10MB) |
| `msg_type` | string | — | 전문 유형 (`MT103`, `pacs.008.001.08` 등). 생략 시 자동 감지 |
| `user_intent` | enum | — | `analyze` / `generate` / `map` / `explain` / `schema`. 생략 시 자동 분류 |
| `thread_id` | string | — | 멱등성 보장용 ID. 생략 시 UUID 자동 발급 |

### 5.3 응답 상태 코드

| `status` 값 | 의미 | 후속 조치 |
|-------------|------|-----------|
| `completed` | 처리 완료 | `output` 필드에서 결과 확인 |
| `pending_hitl` | 검수 대기 | `POST /resume`로 결정 전송 |
| `failed` | 처리 실패 | `error` 필드에서 원인 확인 |

---

## 6. 환경 설정 및 배포 가이드

### 6.1 최초 설치

```bash
# 1. 환경변수 파일 복사 및 설정
cp .env.example .env
# .env 편집: LLM API 키, Qdrant 연결 등 설정

# 2. 컨테이너 빌드 및 기동
docker compose up -d --build

# 3. 기동 상태 확인
docker compose ps
curl http://localhost:8000/health
```

### 6.2 주요 환경변수

| 변수명 | 설명 | 기본값 |
|--------|------|--------|
| `LLM_PROVIDER` | LLM 제공자 선택 | `ollama` |
| `VLLM_BASE_URL` | LLM 서비스 엔드포인트 | `http://host.docker.internal:11434/v1` |
| `VLLM_API_KEY` | LLM API 키 | (필수 설정) |
| `VLLM_MODEL` | 사용 모델명 | `meta-llama/Meta-Llama-3.1-70B-Instruct` |
| `ANTHROPIC_API_KEY` | Claude API 키 (`anthropic` 제공자 시) | — |
| `QDRANT_URL` | Qdrant 접속 URL | `http://localhost:6333` |
| `PROWIDE_URL` | Prowide 서비스 URL | `http://localhost:8080` |
| `PROWIDE_TIMEOUT` | Prowide 요청 타임아웃(초) | `10.0` |
| `GRAPH_TIMEOUT` | 전체 파이프라인 타임아웃(초) | `300` |
| `HITL_ENABLED` | HITL 활성화 여부 | `true` |
| `LOG_LEVEL` | 로그 레벨 | `info` |

전체 환경변수 목록 및 설명은 [`.env.example`](.env.example) 참조.

### 6.3 가이드북 인덱싱

SWIFT 가이드북 PDF를 Qdrant에 인덱싱해야 RAG 검색이 동작합니다.

```bash
# MT 가이드북 전체 인덱싱 (data/MT/ 하위 자동 탐색)
python scripts/ingest_mt_all.py

# MX 가이드북 전체 인덱싱 (data/MX/ 하위 자동 탐색)
python scripts/ingest_mx_all.py

# 인덱싱 결과 확인
curl http://localhost:8000/msg-types
```

**권장 디렉토리 구조:**

```
data/
  MT/
    Category1/          ← MT1xx 전문 가이드
      SR_2025_MT101.pdf
      SR_2025_MT103.pdf
    Category2/          ← MT2xx 전문 가이드
      SR_2025_MT202.pdf
  MX/
    pacs/               ← 결제 관련 MX 전문
      MX_pacs_008_001_14.pdf
    camt/               ← 계좌·잔액 관련 MX 전문
      MX_camt_053_001_08.pdf
```

---

## 7. 운영 모니터링 가이드

### 7.1 헬스체크

```bash
# 서비스 전체 상태 (Prowide + Qdrant 포함)
curl http://localhost:8000/health

# 정상 응답 예시
{
  "status": "ok",            # "degraded" 이면 하위 서비스 확인 필요
  "prowide_available": true,
  "qdrant_available": true,
  "version": "2.0.0"
}
```

### 7.2 로그 확인

```bash
# swift-agent 실시간 로그
docker compose logs -f swift-agent

# 주요 로그 이벤트 키
#  startup                    — 서버 기동 (prowide/qdrant 상태 포함)
#  missing_env_vars           — 필수 환경변수 미설정 경고
#  convert_start              — 새 요청 수신 (thread_id, msg_type)
#  convert_done               — 처리 완료 (status, elapsed_ms)
#  hitl_decision_received     — 검수자 결정 수신 (action)
#  prowide_unavailable        — Prowide 연결 실패 (degraded 모드)
#  graph_timeout              — 파이프라인 타임아웃 (300초 초과)
#  llm_reject_downgraded_*    — LLM REJECT → CBPR+ 권장으로 재분류
#  unmask_pii_placeholders_remaining — 복원 누락 플레이스홀더 감지
```

### 7.3 감사 로그

모든 처리 결과는 `audit.jsonl` 파일에 자동 기록됩니다.

```bash
# 컨테이너 내 감사 로그 확인
docker exec swift-agent tail -f /app/logs/audit.jsonl
```

### 7.4 HITL 대기 스레드 관리

```bash
# 현재 검수 대기 중인 스레드 목록
curl "http://localhost:8000/threads?pending_only=true"

# 특정 스레드 상태 조회
curl http://localhost:8000/convert/{thread_id}

# 검수 승인
curl -X POST http://localhost:8000/resume \
  -H "Content-Type: application/json" \
  -d '{"thread_id": "abc-123", "action": "approve"}'
```

### 7.5 Degraded 모드 동작

외부 서비스 장애 시 시스템이 무인 통과를 허용하지 않고 안전하게 동작합니다.

| 장애 서비스 | 동작 | 안전성 |
|-------------|------|--------|
| Prowide 장애 | 구문 검증 건너뜀, `degraded=True`, **HITL 강제 발동** | 무인 통과 차단 |
| Qdrant 장애 | RAG 없이 LLM만 처리, 가이드 근거 없음 경고 | 결과 신뢰도 저하 경고 |
| LLM 장애 | 구조화 출력 실패 → JSON 파싱 폴백 → `verdict=ERROR` | ERROR 상태로 반환 |

---

*본 문서는 코드베이스(`app/`, `docker-compose.yml`, `pyproject.toml`)를 직접 분석하여 작성되었습니다.*
