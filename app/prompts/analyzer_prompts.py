"""
Analyzer Agent 프롬프트 템플릿.
plan.md §3 Few-shot 프롬프트 템플릿 참조.
"""

ANALYZER_SYSTEM = """\
당신은 SWIFT 전문 검증 전문가입니다.
주어진 [전문]을 [가이드북 규칙 조각]에 근거해서만 분석하십시오.
규칙 조각에 없는 내용은 추측하지 말고 'insufficient_context'로 표시하십시오.
반드시 아래 JSON 스키마로만 응답하십시오. 모든 위반 사항에는 근거 page와 rule_id를 명시하십시오.\
"""

FEWSHOT = """\
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
{{
  "verdict": "WARNING",
  "violations": [],
  "warnings": [
    {{
      "field": "59",
      "issue": "수취인이 비정형 이름/계좌 형식으로 작성됨. CBPR+ 이행 환경에서는 구조화 정보 권장.",
      "rule_id": null,
      "page": 118,
      "reasoning": "33B 부재로 C1(p.142)은 미적용. 단 59 옵션 미사용으로 BIC 미포함은 규칙상 허용."
    }}
  ],
  "applied_conditional_rules": [
    {{"rule_id": "C1", "page": 142, "triggered": false,
      "why": "필드 33B가 전문에 부재하여 36(환율) 필수 조건이 발동되지 않음"}}
  ]
}}
[예시 끝]
"""

ANALYZER_USER = """\
{fewshot}
[실제 분석 대상]
전문(마스킹됨):
{masked_message}

가이드북 규칙 조각:
{retrieved_rules}

분석 결과(JSON만):\
"""
