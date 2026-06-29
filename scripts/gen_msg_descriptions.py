"""
전문 유형별 설명 사전 생성 스크립트.

동작:
  1. Qdrant에서 모든 msg_type 수집
  2. prefix 단위(예: camt.003)로 그룹핑
  3. 각 prefix별 대표 청크 추출 → LLM 호출 → {name_en, name_ko} 생성
  4. data/msg_descriptions.json 저장

사용법:
  python scripts/gen_msg_descriptions.py            # 전체 도메인
  python scripts/gen_msg_descriptions.py --domain camt
  python scripts/gen_msg_descriptions.py --domain camt pacs pain
  python scripts/gen_msg_descriptions.py --force    # 기존 캐시 무시하고 전체 재생성
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# 프로젝트 루트를 sys.path에 추가
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

CACHE_FILE = ROOT / "data" / "msg_descriptions.json"

# ── LLM 설정 ─────────────────────────────────────────────────────────────────
LLM_PROVIDER     = os.getenv("LLM_PROVIDER", "anthropic").lower()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL  = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
VLLM_BASE_URL    = os.getenv("VLLM_BASE_URL", "http://localhost:11434/v1")
VLLM_API_KEY     = os.getenv("VLLM_API_KEY", "ollama")
VLLM_MODEL       = os.getenv("VLLM_MODEL", "qwen2.5:32b")

# ── 프롬프트 ─────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = (
    "You are a SWIFT ISO 20022 messaging expert. "
    "Given document excerpts from an ISO 20022 message guide, "
    "provide a concise name for the message type in English and Korean. "
    "Respond ONLY with valid JSON, no explanation."
)

USER_PROMPT_TMPL = """\
Message type: {msg_type}

Document excerpts:
{text}

Provide the official short name for this ISO 20022 message type.
Respond with JSON only:
{{"name_en": "<5-10 word English name>", "name_ko": "<Korean translation>"}}"""

DOMAIN_PROMPT_TMPL = """\
The ISO 20022 message domain code is "{domain}".
Examples: pacs = Payments Clearing & Settlement, camt = Cash Management, pain = Payments Initiation

Provide the official full name for this ISO 20022 domain in English and Korean.
Respond with JSON only:
{{"name_en": "<official English domain name>", "name_ko": "<Korean translation>"}}"""


# ── Qdrant 헬퍼 ──────────────────────────────────────────────────────────────

def get_qdrant_client():
    from qdrant_client import QdrantClient
    url = os.getenv("QDRANT_URL", "http://localhost:6333")
    api_key = os.getenv("QDRANT_API_KEY") or None
    return QdrantClient(url=url, api_key=api_key)


def get_collection_name() -> str:
    return os.getenv("QDRANT_COLLECTION", "swift_guidebook")


def fetch_all_mx_versions(client, collection: str) -> dict[str, list[str]]:
    """Qdrant에서 MX msg_type을 수집해 domain → [full_versions] 로 그룹핑."""
    domain_map: dict[str, list[str]] = {}
    offset = None
    while True:
        result, next_offset = client.scroll(
            collection_name=collection,
            limit=1000,
            offset=offset,
            with_payload=["msg_type", "message_type"],
            with_vectors=False,
        )
        for point in result:
            p = point.payload or {}
            raw = (p.get("msg_type") or p.get("message_type") or "").strip().lower()
            if not raw or raw.startswith("mt") or "." not in raw:
                continue
            domain = raw.split(".")[0]
            domain_map.setdefault(domain, [])
            if raw not in domain_map[domain]:
                domain_map[domain].append(raw)
        if next_offset is None:
            break
        offset = next_offset
    return domain_map


def fetch_sample_chunks(client, collection: str, msg_type: str, n: int = 5) -> list[str]:
    """특정 msg_type의 대표 청크 텍스트를 반환 (짧은 것 제외)."""
    from qdrant_client.models import Filter, FieldCondition, MatchValue
    flt = Filter(must=[FieldCondition(key="msg_type", match=MatchValue(value=msg_type))])
    result, _ = client.scroll(
        collection_name=collection,
        limit=20,
        scroll_filter=flt,
        with_payload=["text", "section"],
        with_vectors=False,
    )
    texts = []
    for point in result:
        p = point.payload or {}
        t = (p.get("text") or "").strip()
        if len(t) >= 80:
            texts.append(t[:400])
        if len(texts) >= n:
            break
    return texts


# ── LLM 호출 ─────────────────────────────────────────────────────────────────

def call_llm(prefix: str, sample_texts: list[str]) -> dict[str, str]:
    """LLM에 전문명 생성 요청 후 {name_en, name_ko} 반환."""
    text_block = "\n\n---\n\n".join(sample_texts) if sample_texts else "(no content available)"
    user_msg = USER_PROMPT_TMPL.format(msg_type=prefix, text=text_block[:2000])

    if LLM_PROVIDER == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=128,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
    else:
        from openai import OpenAI
        client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)
        resp = client.chat.completions.create(
            model=VLLM_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=128,
            temperature=0.0,
        )
        raw = resp.choices[0].message.content.strip()

    # JSON 파싱
    if raw.startswith("```"):
        raw = "\n".join(l for l in raw.splitlines() if not l.startswith("```")).strip()
    try:
        data = json.loads(raw)
        return {"name_en": data.get("name_en", ""), "name_ko": data.get("name_ko", "")}
    except Exception:
        s, e = raw.find("{"), raw.rfind("}") + 1
        if s >= 0 and e > s:
            try:
                data = json.loads(raw[s:e])
                return {"name_en": data.get("name_en", ""), "name_ko": data.get("name_ko", "")}
            except Exception:
                pass
    return {"name_en": "", "name_ko": ""}


def call_llm_domain(domain: str) -> dict[str, str]:
    """도메인 코드로 LLM에 공식 도메인명 생성 요청."""
    user_msg = DOMAIN_PROMPT_TMPL.format(domain=domain)

    if LLM_PROVIDER == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=128,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
    else:
        from openai import OpenAI
        client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)
        resp = client.chat.completions.create(
            model=VLLM_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=128,
            temperature=0.0,
        )
        raw = resp.choices[0].message.content.strip()

    if raw.startswith("```"):
        raw = "\n".join(l for l in raw.splitlines() if not l.startswith("```")).strip()
    try:
        data = json.loads(raw)
        return {"name_en": data.get("name_en", ""), "name_ko": data.get("name_ko", "")}
    except Exception:
        s, e = raw.find("{"), raw.rfind("}") + 1
        if s >= 0 and e > s:
            try:
                data = json.loads(raw[s:e])
                return {"name_en": data.get("name_en", ""), "name_ko": data.get("name_ko", "")}
            except Exception:
                pass
    return {"name_en": "", "name_ko": ""}


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="전문 유형 설명 캐시 생성")
    parser.add_argument("--domain", nargs="+", help="처리할 도메인 (예: camt pacs). 생략 시 전체")
    parser.add_argument("--force", action="store_true", help="기존 캐시 무시하고 전체 재생성")
    args = parser.parse_args()

    # 기존 캐시 로드
    cache: dict[str, Any] = {}
    if CACHE_FILE.exists() and not args.force:
        with open(CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
        print(f"기존 캐시 로드: {len(cache)}개 항목")

    client = get_qdrant_client()
    collection = get_collection_name()

    print("Qdrant에서 전문 목록 수집 중...")
    domain_map = fetch_all_mx_versions(client, collection)

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

    # ── Step 1: 도메인 레이블 생성 ────────────────────────────────────────────
    domains_cache: dict[str, Any] = cache.get("_domains", {})
    target_domains = sorted(domain_map.keys())
    if args.domain:
        target_domains = [d for d in target_domains if d in set(x.lower() for x in args.domain)]

    new_domains = [d for d in target_domains if d not in domains_cache or args.force]
    if new_domains:
        print(f"\n[도메인 레이블] {len(new_domains)}개 생성 중...")
        for domain in new_domains:
            print(f"  {domain} ...", end=" ", flush=True)
            try:
                result = call_llm_domain(domain)
                domains_cache[domain] = result
                print(f"OK  {result['name_en']} / {result['name_ko']}")
            except Exception as exc:
                print(f"ERR {exc}")
                domains_cache[domain] = {"name_en": domain, "name_ko": ""}
            time.sleep(0.3)
        cache["_domains"] = domains_cache
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        print(f"  >> 도메인 레이블 저장 완료\n")

    # ── Step 2: 버전별 전문명 생성 ────────────────────────────────────────────
    all_versions: list[str] = []
    for domain, versions in sorted(domain_map.items()):
        if args.domain and domain not in set(d.lower() for d in args.domain):
            continue
        all_versions.extend(sorted(versions))

    targets = [v for v in all_versions if v not in cache or args.force]
    print(f"[전문 버전] 처리 대상: {len(targets)}개 버전 (전체 {len(all_versions)}개 중 신규/갱신 대상)")

    if not targets:
        print("모두 캐시 완료. 종료합니다.")
        return

    for i, version in enumerate(targets, 1):
        print(f"[{i}/{len(targets)}] {version} ...", end=" ", flush=True)

        try:
            texts = fetch_sample_chunks(client, collection, version)
            result = call_llm(version, texts)
            cache[version] = result
            print(f"OK  {result['name_en']} / {result['name_ko']}")
        except Exception as exc:
            print(f"ERR {exc}")
            cache[version] = {"name_en": "", "name_ko": ""}

        # 20개마다 중간 저장
        if i % 20 == 0:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
            print(f"  >> 중간 저장 ({i}/{len(targets)})")

        # API 레이트 리밋 대응
        time.sleep(0.3)

    # 최종 저장
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    print(f"\n완료! {CACHE_FILE} 에 {len(cache)}개 항목 저장됨")


if __name__ == "__main__":
    main()
