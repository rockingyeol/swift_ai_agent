CBPR+ 기준 MT/MX 공존기간(coexistence)은 2025년 11월 22일부로 공식 종료되어, 국경 간 지급지시 MT(MT103·202·9xx 등)는 ISO 20022 MX로 대체되는 것이 원칙입니다. <cite index="6-1">MT103, MT202 같은 지급지시 전문이 ISO 20022 등가 메시지로 공식 은퇴(retire)된 것</cite>이죠. 다만 현실은 <cite index="7-1">"끝났지만 끝나지 않은"</cite> 상태입니다. <cite index="7-1">상당수 레거시 메시지가 수년간 계속 사용되고, 일부 은행은 완전 전환 대신 임시 우회책(workaround)을 적용</cite>했으며, <cite index="8-1">SWIFT가 제공하는 contingency 변환 서비스는 데이터 손실 위험 때문에 장기 해법으로 의존해서는 안 되는</cite> 상황입니다. 또한 <cite index="8-1">2026년 11월에는 완전 비정형(unstructured) 주소가 폐기되는 더 까다로운 의무가 예정</cite>되어 있습니다.
설계 함의: 이 때문에 본 시스템의 MX/MT Mapper Agent는 단순 양방향 변환기가 아니라, "레거시 MT → MX 정밀 업리프트(uplift)"가 핵심 가치가 됩니다. 레거시 코어뱅킹이 여전히 MT를 생성하면 송신 직전 MX로 변환·보강해야 하고, 구조화 주소 강제 같은 신규 검증을 통과시켜야 NAK를 막을 수 있습니다. 아래 설계는 이 점을 반영합니다.

1. 전체 시스템 아키텍처 (E2E Pipeline)
   핵심 의사결정 두 가지를 먼저 못 박겠습니다.
   (A) 오케스트레이션은 LangChain이 아니라 LangGraph를 권장합니다. 본 워크플로우는 라우팅 → 검색 → 검증 → 사람 검수(HITL)로 이어지는 상태(state)를 가진 순환 그래프입니다. 단순 체인(LangChain LCEL)으로는 HITL 중단·재개와 검증 실패 시 재시도 루프를 깔끔하게 표현하기 어렵습니다. LangGraph의 interrupt 기반 체크포인트가 HITL에 최적입니다.
   (B) Prowide는 Java 라이브러리이므로, Python 메인 스택과는 별도 마이크로서비스로 분리합니다. JPype/Py4J 인프로세스 브리지는 PoC엔 편하지만 금융망 운영 환경에서 JVM과 Python 프로세스를 한 컨테이너에 묶으면 장애 격리·GC·스레드 안정성 문제가 생깁니다. Spring Boot 기반 Prowide REST 마이크로서비스(/validate/mt, /parse/mt, /translate)로 분리하는 것이 운영상 정답입니다. 참고로 MT는 Prowide Core(LGPL 오픈소스)지만, MX/ISO 20022는 별도의 Prowide ISO 20022(SRU) 라이브러리가 필요하고 라이선스 정책이 다르니 도입 전 확인이 필요합니다.
   전체 처리 흐름:
   ┌──────────────────────────────────────────────────────────────────────┐
   │ [1] 사용자 입력 (운영자/개발자/신입) │
   │ · 분석 요청(MT/MX 원문) · 생성 요청(자연어) · 변환 요청(MT↔MX) │
   └───────────────┬──────────────────────────────────────────────────────┘
   ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ [2] PII 마스킹 게이트 (LLM 입력 前 필수) │
   │ · 정형: 계좌/IBAN/BIC/금액 → 정규식 │
   │ · 비정형: 이름/주소 → 로컬 NER(Presidio + ko 모델) │
   │ · 원본↔플레이스홀더 매핑 테이블은 메모리(보안 vault)에만 보관 │
   └───────────────┬──────────────────────────────────────────────────────┘
   ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ [3] Supervisor / Router 노드 (LangGraph) │
   │ · 의도 분류 → Analyzer / Generator / Mapper 중 라우팅 │
   └──────┬────────────────────┬───────────────────────┬───────────────────┘
   ▼ ▼ ▼
   ┌───────────┐ ┌───────────┐ ┌───────────┐
   │ Analyzer │ │ Generator │ │ Mapper │
   │ Agent │ │ Agent │ │ Agent │
   └─────┬─────┘ └─────┬─────┘ └─────┬─────┘
   │ │ │
   ▼ ▼ ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ [4] Hierarchical RAG (공통 검색 백본) │
   │ Query → Hybrid Search(Dense BGE-m3 + Sparse BM25) │
   │ → 메타데이터 필터(msg_type/field/rule) │
   │ → Cross-Encoder Re-ranker(bge-reranker-v2-m3) │
   │ → 상위 N개 규칙 조각 + 가이드북 page 번호 반환 │
   └───────────────┬──────────────────────────────────────────────────────┘
   ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ [5] 하이브리드 검증 루프 │
   │ ┌─ (5a) Rule Engine: Prowide REST → 구문/네트워크 규칙(C1~Cn) 1차 │
   │ └─ (5b) LLM(Llama 3.1 70B): 의미론적·조건부 필수 규칙 해석 │
   │ 두 결과를 Reconciler가 병합 → 불일치 시 재시도 / 에스컬레이션 │
   └───────────────┬──────────────────────────────────────────────────────┘
   ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ [6] HITL 체크포인트 (LangGraph interrupt) │
   │ · NAK 위험도 high이거나 LLM↔Rule 불일치 시 사람 검수로 중단 │
   │ · 검수자 승인/수정 → 그래프 재개 │
   └───────────────┬──────────────────────────────────────────────────────┘
   ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ [7] 언마스킹 + 최종 산출물 + 감사로그(audit trail) 저장 │
   │ · 근거 page·규칙 ID·LLM 추론·검수자 ID를 전건 기록(규제 대응) │
   └──────────────────────────────────────────────────────────────────────┘
   온프레미스 모델 서빙은 vLLM(높은 처리량, 연간 수십만 건에 적합)으로 Llama 3.1 70B를 AWQ/GPTQ 4bit 양자화하여 서빙(대략 A100 80GB ×2 또는 H100 ×2 권장), 임베딩·리랭커는 동일 GPU 클러스터에 별도 텐서 서버로 올리는 구성이 현실적입니다.

2. Hierarchical RAG 구축 전략
   방법론
   SWIFT 가이드북(User Handbook, MT/MX Message Reference Guide)은 "메시지 타입 → 필드(태그) → 규칙" 의 반복적 구조를 가집니다. 이 구조를 그대로 메타데이터로 보존하는 것이 핵심입니다. 단순 고정 크기 청킹(예: 512 토큰)을 쓰면 "필드 50K의 조건부 필수 규칙 C1" 같은 의미 단위가 청크 경계에서 잘려 검색 정확도가 무너집니다.
   전략 3원칙:

구조 인지 청킹(structure-aware chunking): 페이지가 아니라 의미 단위(필드 정의, 개별 규칙)로 자릅니다.
메타데이터 부착: 모든 청크에 message_type, field_tag, rule_id, rule_type(presence/format/network), page 를 부착하여 검색 시 필터링과 정확한 출처 인용을 가능하게 합니다.
부모-자식(hierarchical) 인덱싱: 작은 규칙 청크로 검색하되, LLM에는 부모(필드 전체 정의) 컨텍스트를 함께 제공합니다(small-to-big retrieval).

전처리 코드
python"""
SWIFT 가이드북 → Hierarchical Chunks 전처리 파이프라인
의존성: pymupdf(fitz), pydantic, (인덱싱) qdrant-client + FlagEmbedding(BGE-m3)
"""
import re
import fitz # PyMuPDF
from enum import Enum
from typing import Optional
from pydantic import BaseModel

class RuleType(str, Enum):
PRESENCE = "presence" # 필수/선택/조건부 필수 (예: C1, C2)
FORMAT = "format" # 길이/문자셋/포맷 (예: 16x, 4!c)
NETWORK = "network" # 네트워크 검증 규칙 (예: T26, D49)
USAGE = "usage" # 사용 지침/의미론

class SwiftChunk(BaseModel):
chunk_id: str
level: str # "message" | "field" | "rule"
message_type: str # 예: "MT103", "pacs.008"
field_tag: Optional[str] # 예: "50K", "59"
rule_id: Optional[str] # 예: "C1", "T26"
rule_type: Optional[RuleType]
page: int
parent_id: Optional[str] # small-to-big retrieval용
text: str # 임베딩 대상 본문

# --- 가이드북 구조 패턴 (실제 PDF에 맞춰 정규식 튜닝 필요) ---

RE_MSG_TYPE = re.compile(r"\b(MT\s?\d{3}|pacs\.\d{3}\.\d{3}\.\d{2}|camt\.\d{3})", re.I)
RE_FIELD = re.compile(r"^\s*(?:Field\s+)?(\d{1,2}[A-Z]?)\b[:\s]", re.M) # 50K, 59 등
RE_RULE = re.compile(r"\b([CDT]\d{1,3})\b") # C1, D49, T26
RE_FORMAT = re.compile(r"\b(\d+[!*]?[anxcde](?:/\d+[!*]?[anxcde])?)\b") # 4!c, 16x, 35x

def classify_rule(text: str) -> RuleType:
if "conditional" in text.lower() or RE_RULE.search(text):
return RuleType.NETWORK if re.search(r"\b[T]\d", text) else RuleType.PRESENCE
if RE_FORMAT.search(text):
return RuleType.FORMAT
return RuleType.USAGE

def chunk_guidebook(pdf_path: str) -> list[SwiftChunk]:
doc = fitz.open(pdf_path)
chunks: list[SwiftChunk] = []
current_mt: Optional[str] = None
current_field: Optional[str] = None
field_parent_id: Optional[str] = None

    for page_no in range(len(doc)):
        page = doc[page_no]
        # "blocks" 단위 추출로 표/단락 경계를 어느 정도 보존
        blocks = page.get_text("blocks")  # (x0,y0,x1,y1,text,block_no,block_type)
        for blk in sorted(blocks, key=lambda b: (b[1], b[0])):
            text = blk[4].strip()
            if not text:
                continue

            # 1) 메시지 타입 헤더 감지 → 컨텍스트 갱신
            if m := RE_MSG_TYPE.search(text):
                if len(text) < 120:  # 제목성 블록만 헤더로 인정
                    current_mt = m.group(1).replace(" ", "").upper()
                    msg_id = f"{current_mt}::msg::p{page_no}"
                    chunks.append(SwiftChunk(
                        chunk_id=msg_id, level="message",
                        message_type=current_mt, field_tag=None,
                        rule_id=None, rule_type=None, page=page_no + 1,
                        parent_id=None, text=text,
                    ))
                    continue

            if current_mt is None:
                continue  # 메시지 타입 컨텍스트 확정 전 본문은 보류

            # 2) 필드 정의 블록 감지 → 부모 청크 생성
            if fm := RE_FIELD.match(text):
                current_field = fm.group(1)
                field_parent_id = f"{current_mt}::field::{current_field}::p{page_no}"
                chunks.append(SwiftChunk(
                    chunk_id=field_parent_id, level="field",
                    message_type=current_mt, field_tag=current_field,
                    rule_id=None, rule_type=RuleType.FORMAT, page=page_no + 1,
                    parent_id=None, text=text,
                ))
                continue

            # 3) 규칙 청크: 검증 규칙 ID 단위로 분할 (조건부 필수가 핵심 검색 타깃)
            rule_ids = RE_RULE.findall(text)
            if rule_ids:
                for rid in set(rule_ids):
                    # 해당 규칙 ID가 언급된 문장만 발췌하여 의미 단위 보존
                    sentences = [s for s in re.split(r"(?<=[.\n])", text) if rid in s]
                    rule_text = " ".join(sentences).strip() or text
                    chunks.append(SwiftChunk(
                        chunk_id=f"{current_mt}::{current_field}::{rid}::p{page_no}",
                        level="rule", message_type=current_mt,
                        field_tag=current_field, rule_id=rid,
                        rule_type=classify_rule(rule_text), page=page_no + 1,
                        parent_id=field_parent_id, text=rule_text,
                    ))
            else:
                # 규칙 ID 없는 사용 지침도 필드 자식으로 보관
                chunks.append(SwiftChunk(
                    chunk_id=f"{current_mt}::{current_field}::usage::p{page_no}::{blk[5]}",
                    level="rule", message_type=current_mt,
                    field_tag=current_field, rule_id=None,
                    rule_type=RuleType.USAGE, page=page_no + 1,
                    parent_id=field_parent_id, text=text,
                ))
    return chunks

벡터 DB 인덱싱(Qdrant + BGE-m3 하이브리드)은 다음과 같이 구성합니다. BGE-m3는 dense·sparse를 한 모델에서 동시 산출해 별도 BM25 인프라 없이 하이브리드 검색이 가능하고, 다국어(한국어 운영자 쿼리) 대응도 됩니다.
pythonfrom FlagEmbedding import BGEM3FlagModel
from qdrant_client import QdrantClient, models

embed = BGEM3FlagModel("BAAI/bge-m3", use_fp16=True) # 온프레미스 로컬 로드
client = QdrantClient(url="http://localhost:6333") # 금융망 내부

client.recreate_collection(
collection_name="swift_guidebook",
vectors_config={"dense": models.VectorParams(size=1024, distance=models.Distance.COSINE)},
sparse_vectors_config={"sparse": models.SparseVectorParams()},
)

def index_chunks(chunks: list[SwiftChunk], batch: int = 64):
for i in range(0, len(chunks), batch):
part = chunks[i:i + batch]
out = embed.encode([c.text for c in part], return_dense=True, return_sparse=True)
points = []
for j, c in enumerate(part):
lex = out["lexical_weights"][j]
points.append(models.PointStruct(
id=abs(hash(c.chunk_id)) % (2\*\*63),
vector={
"dense": out["dense_vecs"][j].tolist(),
"sparse": models.SparseVector(
indices=[int(k) for k in lex.keys()],
values=[float(v) for v in lex.values()],
),
},
payload=c.model_dump(), # message_type/field_tag/rule_id/page 등 필터·인용용
))
client.upsert("swift_guidebook", points=points)
검색 시에는 (1) 쿼리에서 추출한 message_type/field_tag로 메타데이터 사전 필터, (2) dense+sparse 하이브리드 점수로 후보 30개 회수, (3) bge-reranker-v2-m3 크로스인코더로 재정렬해 상위 5개를 남기고, (4) 각 rule 청크의 parent_id로 부모 필드 정의를 함께 끌어와 LLM에 넘깁니다(small-to-big).

실무 주의: 위 정규식은 일반 구조 가정입니다. SWIFT 핸드북은 버전(SRG)마다 레이아웃이 다르고 표가 많아, 표 영역은 page.find_tables()(PyMuPDF) 또는 camelot으로 별도 추출해 규칙 매트릭스를 구조화하는 보강이 거의 항상 필요합니다.

3. Analyzer Agent 코어 로직
   Few-shot 프롬프트 템플릿
   SWIFT 특화 포인트는 ① 출력을 구조화 JSON으로 강제, ② 반드시 가이드북 page·rule_id를 인용, ③ 조건부 필수(C-rule)를 "전건 추론" 형태로 보여주는 few-shot 예시를 넣는 것입니다. 이렇게 하면 Llama 3.1 70B가 추론 패턴을 모방합니다.
   pythonANALYZER_SYSTEM = """당신은 SWIFT 전문 검증 전문가입니다.
   주어진 [전문]을 [가이드북 규칙 조각]에 근거해서만 분석하십시오.
   규칙 조각에 없는 내용은 추측하지 말고 'insufficient_context'로 표시하십시오.
   반드시 아래 JSON 스키마로만 응답하십시오. 모든 위반 사항에는 근거 page와 rule_id를 명시하십시오."""

# --- Few-shot: 조건부 필수 규칙(C1) 추론을 시연 ---

FEWSHOT = """
[예시]
전문(마스킹됨):
:23B:CRED
:32A:240115USD5000,00
:59:/<<ACCT_1>>
<<NAME_1>>

가이드북 규칙 조각:

- (p.142, MT103, rule C1) 필드 33B가 존재하면 필드 32A의 통화와 다를 경우 필드 36(환율)이 필수이다.
- (p.118, MT103, field 59) 수취인. 옵션 미사용 시 계좌+이름. 옵션 A 사용 시 BIC 필수.

분석 결과:
{
"verdict": "WARNING",
"violations": [],
"warnings": [
{
"field": "59",
"issue": "수취인이 비정형 이름/계좌 형식으로 작성됨. CBPR+ 이행 환경에서는 구조화 정보 권장.",
"rule_id": null,
"page": 118,
"reasoning": "33B 부재로 C1(p.142)은 미적용. 단 59 옵션 미사용으로 BIC 미포함은 규칙상 허용."
}
],
"applied_conditional_rules": [
{"rule_id": "C1", "page": 142, "triggered": false,
"why": "필드 33B가 전문에 부재하여 36(환율) 필수 조건이 발동되지 않음"}
]
}
[예시 끝]
"""

ANALYZER_USER = """{fewshot}
[실제 분석 대상]
전문(마스킹됨):
{masked_message}

가이드북 규칙 조각:
{retrieved_rules}

분석 결과(JSON만):"""
Prowide 1차 구문 검증 + LLM 결합 핵심 함수
pythonimport json
import httpx
from typing import Any

PROWIDE_URL = "http://prowide-svc.internal:8080" # 금융망 내부 Java 마이크로서비스

def prowide_syntax_verify(raw_message: str, msg_type: str) -> dict[str, Any]:
"""1차 구문/네트워크 검증 (Prowide Core). 결정론적이고 빠르며 PII 노출 없이 동작.
Prowide 측에서 SwiftMessage 파싱 + validate() 결과를 표준 형식으로 반환한다고 가정."""
endpoint = "/validate/mt" if msg_type.upper().startswith("MT") else "/validate/mx"
try:
resp = httpx.post(f"{PROWIDE_URL}{endpoint}",
json={"content": raw_message}, timeout=10.0)
resp.raise_for_status()
data = resp.json() # 예: {"parseable": true, "problems": [{"field":"32A","code":"T26","desc":"..."}]}
return {
"syntax_ok": data.get("parseable", False) and not data.get("problems"),
"problems": data.get("problems", []),
"source": "prowide",
}
except httpx.HTTPError as e:
return {"syntax_ok": False, "problems": [{"code": "SVC_ERR", "desc": str(e)}],
"source": "prowide", "degraded": True}

def analyze_message(raw_message: str, msg_type: str, masked_message: str,
retriever, llm) -> dict[str, Any]:
"""하이브리드 검증: Prowide(구문) + RAG+LLM(의미/조건부) 결합 후 Reconcile. - raw_message: 원본(Prowide 전용, LLM에는 절대 미전달) - masked_message: PII 마스킹본(LLM 전용)
""" # 1차: 결정론적 구문 검증 (Rule Engine)
syntax = prowide_syntax_verify(raw_message, msg_type)

    # 구문 자체가 파싱 불가면 LLM 의미 분석은 의미 없음 → 조기 반환
    if not syntax["syntax_ok"] and not syntax.get("degraded"):
        # 단, 구문 오류 자체에 대한 가이드북 근거는 RAG로 보강하여 NAK 사유 설명 제공
        rule_chunks = retriever.search(
            query=f"{msg_type} " + " ".join(p.get("code", "") for p in syntax["problems"]),
            filters={"message_type": msg_type.upper()}, top_k=5,
        )
        return {
            "verdict": "REJECT",
            "stage": "syntax",
            "syntax_problems": syntax["problems"],
            "guidebook_basis": [
                {"page": c.page, "rule_id": c.rule_id, "text": c.text} for c in rule_chunks
            ],
            "needs_hitl": True,
        }

    # 2차: 의미론적/조건부 필수 규칙 — RAG 검색 후 LLM 추론
    fields = [p for p in raw_message.split(":") if p]  # 실제론 Prowide 파싱 결과 활용
    rule_chunks = retriever.search(
        query=masked_message,
        filters={"message_type": msg_type.upper(),
                 "rule_type": ["presence", "network", "usage"]},
        top_k=5, rerank=True, include_parents=True,  # small-to-big
    )
    retrieved_rules = "\n".join(
        f"- (p.{c.page}, {c.message_type}, rule {c.rule_id or 'usage'}) {c.text}"
        for c in rule_chunks
    )

    prompt = ANALYZER_USER.format(
        fewshot=FEWSHOT, masked_message=masked_message, retrieved_rules=retrieved_rules,
    )
    llm_raw = llm.invoke(ANALYZER_SYSTEM, prompt)  # vLLM JSON 모드 권장
    try:
        llm_result = json.loads(llm_raw)
    except json.JSONDecodeError:
        llm_result = {"verdict": "ERROR", "violations": [],
                      "note": "LLM JSON 파싱 실패 → HITL 필요"}

    # 3차: Reconcile — 두 엔진 결과 병합 및 신뢰도 판정
    return reconcile(syntax, llm_result, rule_chunks)

def reconcile(syntax: dict, llm_result: dict, rule_chunks) -> dict[str, Any]:
"""Rule Engine과 LLM 결과를 병합하고 HITL 필요 여부를 결정.
원칙: 구문 위반은 Prowide를 신뢰(결정론적), 의미/조건부는 LLM을 신뢰하되
둘 중 하나라도 위험 신호가 있으면 보수적으로 사람에게 에스컬레이션."""
syntax_problems = syntax.get("problems", [])
llm_violations = llm_result.get("violations", [])
llm_verdict = llm_result.get("verdict", "ERROR")

    # 불일치/저신뢰 → HITL
    needs_hitl = (
        bool(syntax_problems)                       # 구문 문제 존재
        or llm_verdict in ("REJECT", "WARNING", "ERROR")
        or syntax.get("degraded", False)            # 룰엔진 장애(degraded) → 무인 통과 금지
    )

    final_verdict = "REJECT" if (syntax_problems or llm_verdict == "REJECT") \
        else ("WARNING" if llm_verdict == "WARNING" else "PASS")

    return {
        "verdict": final_verdict,
        "needs_hitl": needs_hitl,
        "rule_engine": {"problems": syntax_problems, "degraded": syntax.get("degraded")},
        "semantic": {
            "violations": llm_violations,
            "warnings": llm_result.get("warnings", []),
            "conditional_rules": llm_result.get("applied_conditional_rules", []),
        },
        "guidebook_basis": [
            {"page": c.page, "rule_id": c.rule_id, "field": c.field_tag} for c in rule_chunks
        ],
    }

이 설계의 핵심 안전장치 두 가지를 강조하면:
원본은 Prowide에만, 마스킹본은 LLM에만 전달됩니다. Prowide는 결정론적 파서라 PII를 외부로 보내거나 학습하지 않으므로 원본 처리가 안전하고, LLM은 마스킹본으로 의미 분석만 합니다. 둘의 책임이 깔끔하게 분리됩니다.
degraded(룰엔진 장애) 시 무인 통과를 금지합니다. 금융 인프라에서 검증기 일부 장애가 곧 "검증 통과"로 둔갑하면 사고로 직결되므로, 의심스러울 때는 항상 HITL로 보내는 fail-safe 기조를 reconcile에 박아두었습니다.
