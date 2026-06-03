완료된 작업
app/pii/masker.py
PiiMasker 클래스 (단일 메시지 세션):

\_orig_to_ph: 세션 내 중복제거 (same value → same placeholder)
\_ph_to_orig: 복원용 역방향 맵 → mapping 프로퍼티로 state에 직렬화
\_mask_structured(): 4가지 정규식 패턴을 IBAN → BIC → ACCT → AMT 순 적용
AMT 패턴을 \d{1,15}[,.]\d{2}(?!\d)로 설계해 USD5000,00 처럼 단어경계 없는 SWIFT 포맷 커버
ACCT 패턴은 (?<=/)\d{10,34} — / 뒤 숫자만 매칭해 오탐 최소화
\_mask_unstructured(): Presidio의 analyze()는 자체 등록 엔터티만 반환하므로, 한국어 레이블(PS/LC/OG)을 spaCy를 직접 사용해 처리. 모델 미설치 시 no-op.
LangGraph 노드:

mask_pii(): raw_message → masked_message + pii_mapping (직렬화 가능 dict)
unmask_pii(): output dict의 모든 string 필드에서 플레이스홀더 복원
tests/test_pii.py — 40개 테스트 전부 통과
클래스 테스트 수 내용
TestPiiVault 7 put/get/restore/clear 단위
TestPiiMaskerStructured 13 IBAN/BIC/ACCT/AMT 각 패턴 + 오탐 방지
TestPiiMaskerDeduplication 4 동일값 동일 플레이스홀더, 카테고리별 독립 카운터
TestPiiMaskerRoundtrip 6 MT103 스니펫 완전 왕복 포함
TestLangGraphNodes 7 mask_pii/unmask_pii 노드 엣지케이스
TestKoreanNerMasking 3 spaCy NER (모델 미설치 시 자동 skip)
