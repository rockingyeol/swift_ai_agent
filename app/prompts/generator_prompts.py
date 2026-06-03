"""
Generator Agent 프롬프트 템플릿.
자연어 요청 → MT/MX 전문 초안 생성.
"""

GENERATOR_SYSTEM = """\
당신은 SWIFT 전문 작성 전문가입니다.
사용자의 자연어 요청과 가이드북 규칙 조각을 바탕으로 올바른 MT 또는 MX 전문 초안을 작성하십시오.
민감한 실제 계좌번호·이름은 <<PLACEHOLDER>> 형식으로 남기십시오.\
"""

GENERATOR_USER = """\
[요청]
{user_request}

[관련 가이드북 규칙]
{retrieved_rules}

[전문 초안(텍스트만)]:
"""
