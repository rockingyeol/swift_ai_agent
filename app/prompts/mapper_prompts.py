"""
Mapper Agent 프롬프트 템플릿.

설계 원칙:
  - 이 파일은 '역할 지시 + 출력 스키마 + 환각 방지 규칙'만 담는다.
  - MT↔MX 필드 매핑 규칙, 포맷 변환 기준, 경로 정보 등 모든 도메인 지식은
    Qdrant RAG에서 검색한 {rag_context}로 주입된다.
  - 새 전문 유형(MT202, camt.053 등)이 추가될 때 이 파일은 수정하지 않는다.
    매핑 가이드북 PDF를 Qdrant에 인제스트하면 자동 반영된다.
"""

MAPPER_SYSTEM = """\
당신은 SWIFT MT(ISO 15022) ↔ MX(ISO 20022 / CBPR+) 변환 전문가입니다.

[역할]
MT 필드 태그를 MX XML 경로로 매핑하여 완전한 매핑 명세를 반환하십시오.

[매핑 우선순위]
1순위: [가이드라인 문서]에 명시된 경로와 변환 규칙을 사용하십시오.
2순위: 가이드라인이 없거나 부족하면 CBPR+ SRU 표준 및 ISO 20022 일반 지식으로 매핑하십시오.
       (MT103 → pacs.008, MT202 → pacs.009, MT940 → camt.053 등 표준 매핑 적용)
3순위: 표준에도 없으면 is_unmapped=true 로 표시하십시오.

[주요 CBPR+ 기본 매핑 — 가이드라인 없을 때 참고]
MT103 → pacs.008.001.08 기준:
  :20: → GrpHdr/MsgId 및 CdtTrfTxInf/PmtId/EndToEndId
  :32A: → GrpHdr/IntrBkSttlmDt (날짜 YYMMDD → YYYY-MM-DD) + CdtTrfTxInf/IntrBkSttlmAmt (통화+금액)
  :50K:/50F: → CdtTrfTxInf/Dbtr (이름/주소) + CdtTrfTxInf/DbtrAcct/Id/IBAN
  :52A: → CdtTrfTxInf/DbtrAgt/FinInstnId/BICFI
  :57A: → CdtTrfTxInf/CdtrAgt/FinInstnId/BICFI
  :59:/59F: → CdtTrfTxInf/Cdtr (이름/주소) + CdtTrfTxInf/CdtrAcct/Id/IBAN
  :71A: SHA→SHAR, OUR→DEBT, BEN→CRED → CdtTrfTxInf/ChrgBr
  :70: → CdtTrfTxInf/RmtInf/Ustrd (슬래시 코드 없는 일반 송금 정보)
  :72: → CdtTrfTxInf/InstrForNxtAgt 또는 InstrForCdtrAgt
        MX→MT 역방향: Ustrd 값이 /ACC/, /REC/, /INS/, /INT/, /BNF/ 등 슬래시 코드로 시작하면 :70: 이 아닌 :72: 에 매핑

[준수 사항]
- <<PLACEHOLDER>> 형식의 마스킹 값은 그대로 유지하십시오.
- guidebook_ref: 가이드라인 출처 또는 "CBPR+ SRU standard"로 표시하십시오.
- mappings 배열을 반드시 채우십시오. 빈 배열은 허용되지 않습니다.
- mt_value / mx_value 값에 XML 태그(<Tag>, </Tag>)를 절대 포함하지 마십시오. 태그를 제거한 순수 텍스트 값만 기재하십시오.
- MX→MT 변환 시 Ustrd 내용이 /ACC/, /REC/, /INS/, /INT/, /BNF/, /PHONEBEN/ 등 슬래시 코드(/CODE/)로 시작하면 반드시 :72: 필드로 매핑하십시오. :70: 에 포함하지 마십시오.\
"""

MAPPER_FEWSHOT = """\
━━ 아래 예시는 MT103→pacs.008 및 MT202→pacs.009 변환 참고용입니다 ━━
실제 분석 대상은 예시 다음 [실제 변환 대상] 섹션을 사용하십시오.

[예시 1 — MT103 → pacs.008 정상 매핑]
변환 방향: MT103 → pacs.008.001.08
원본 전문(마스킹):
  :20:TXN20240115001
  :23B:CRED
  :32A:240115EUR10000,00
  :50K:/<<IBAN_1>>
  <<NAME_1>>
  :52A:DEUTDEDB
  :57A:BNPAFRPP
  :59:/<<IBAN_2>>
  <<NAME_2>>
  :71A:SHA
  :70:INVOICE 2024-001
가이드북: (p.14) :20: → GrpHdr/MsgId 및 PmtId/EndToEndId. (p.21) :32A: → IntrBkSttlmDt (YYMMDD→YYYY-MM-DD) + IntrBkSttlmAmt. (p.28) :50K: → Dbtr/Nm + DbtrAcct/Id/IBAN. (p.30) :52A: → DbtrAgt/FinInstnId/BICFI. (p.33) :57A: → CdtrAgt/FinInstnId/BICFI. (p.35) :59: → Cdtr/Nm + CdtrAcct/Id/IBAN. (p.40) :71A: SHA → ChrgBr=SHAR. (p.42) :70: → RmtInf/Ustrd.
결과: {{"direction":"mt_to_mx","source_type":"MT103","target_type":"pacs.008.001.08","mappings":[{{"mt_tag":":20:","mt_value":"TXN20240115001","mx_paths":["Document/pacs.008.001.08/FIToFICstmrCdtTrf/GrpHdr/MsgId","Document/pacs.008.001.08/FIToFICstmrCdtTrf/CdtTrfTxInf/PmtId/EndToEndId"],"mx_value":"TXN20240115001","is_unmapped":false,"notes":"MsgId와 EndToEndId 동일값 사용","guidebook_ref":"p.14"}},{{"mt_tag":":32A:","mt_value":"240115EUR10000,00","mx_paths":["Document/pacs.008.001.08/FIToFICstmrCdtTrf/GrpHdr/IntrBkSttlmDt","Document/pacs.008.001.08/FIToFICstmrCdtTrf/CdtTrfTxInf/IntrBkSttlmAmt"],"mx_value":"2024-01-15 / EUR 10000.00","is_unmapped":false,"notes":"날짜 YYMMDD→YYYY-MM-DD, 쉼표→마침표 소수점","guidebook_ref":"p.21"}},{{"mt_tag":":50K:","mt_value":"/<<IBAN_1>> <<NAME_1>>","mx_paths":["Document/pacs.008.001.08/FIToFICstmrCdtTrf/CdtTrfTxInf/Dbtr/Nm","Document/pacs.008.001.08/FIToFICstmrCdtTrf/CdtTrfTxInf/DbtrAcct/Id/IBAN"],"mx_value":"<<NAME_1>> / <<IBAN_1>>","is_unmapped":false,"notes":"비구조화 주소 → CBPR+ 이행 시 Dbtr/PstlAdr/AdrLine 분리 권장","guidebook_ref":"p.28"}},{{"mt_tag":":52A:","mt_value":"DEUTDEDB","mx_paths":["Document/pacs.008.001.08/FIToFICstmrCdtTrf/CdtTrfTxInf/DbtrAgt/FinInstnId/BICFI"],"mx_value":"DEUTDEDB","is_unmapped":false,"notes":null,"guidebook_ref":"p.30"}},{{"mt_tag":":57A:","mt_value":"BNPAFRPP","mx_paths":["Document/pacs.008.001.08/FIToFICstmrCdtTrf/CdtTrfTxInf/CdtrAgt/FinInstnId/BICFI"],"mx_value":"BNPAFRPP","is_unmapped":false,"notes":null,"guidebook_ref":"p.33"}},{{"mt_tag":":59:","mt_value":"/<<IBAN_2>> <<NAME_2>>","mx_paths":["Document/pacs.008.001.08/FIToFICstmrCdtTrf/CdtTrfTxInf/Cdtr/Nm","Document/pacs.008.001.08/FIToFICstmrCdtTrf/CdtTrfTxInf/CdtrAcct/Id/IBAN"],"mx_value":"<<NAME_2>> / <<IBAN_2>>","is_unmapped":false,"notes":null,"guidebook_ref":"p.35"}},{{"mt_tag":":71A:","mt_value":"SHA","mx_paths":["Document/pacs.008.001.08/FIToFICstmrCdtTrf/CdtTrfTxInf/ChrgBr"],"mx_value":"SHAR","is_unmapped":false,"notes":"SHA → SHAR (CBPR+ 코드값 변환)","guidebook_ref":"p.40"}},{{"mt_tag":":70:","mt_value":"INVOICE 2024-001","mx_paths":["Document/pacs.008.001.08/FIToFICstmrCdtTrf/CdtTrfTxInf/RmtInf/Ustrd"],"mx_value":"INVOICE 2024-001","is_unmapped":false,"notes":"140자 초과 시 잘림 위험","guidebook_ref":"p.42"}}],"unmapped_fields":[],"enhancement_warnings":[]}}

[예시 2 — MT202 → pacs.009 매핑 (경고 포함)]
변환 방향: MT202 → pacs.009.001.08
원본 전문(마스킹):
  :20:FI20240115002
  :21:TXN20240115001
  :32A:240115USD250000,00
  :52A:CHASUS33
  :58A:DEUTDEDB
가이드북: (p.52) :20: → GrpHdr/MsgId. (p.53) :21: → CdtTrfTxInf/PmtId/InstrId (원거래 참조). (p.55) :32A: → IntrBkSttlmDt + IntrBkSttlmAmt. (p.58) :52A: → InstgAgt/FinInstnId/BICFI. (p.60) :58A: → CdtrAgt/FinInstnId/BICFI. (p.61) Cdtr 필수이나 :58A:만으로는 법인명 미확인.
결과: {{"direction":"mt_to_mx","source_type":"MT202","target_type":"pacs.009.001.08","mappings":[{{"mt_tag":":20:","mt_value":"FI20240115002","mx_paths":["Document/pacs.009.001.08/FICdtTrf/GrpHdr/MsgId"],"mx_value":"FI20240115002","is_unmapped":false,"notes":null,"guidebook_ref":"p.52"}},{{"mt_tag":":21:","mt_value":"TXN20240115001","mx_paths":["Document/pacs.009.001.08/FICdtTrf/CdtTrfTxInf/PmtId/InstrId"],"mx_value":"TXN20240115001","is_unmapped":false,"notes":"원거래(MT103) 참조번호 연결","guidebook_ref":"p.53"}},{{"mt_tag":":32A:","mt_value":"240115USD250000,00","mx_paths":["Document/pacs.009.001.08/FICdtTrf/GrpHdr/IntrBkSttlmDt","Document/pacs.009.001.08/FICdtTrf/CdtTrfTxInf/IntrBkSttlmAmt"],"mx_value":"2024-01-15 / USD 250000.00","is_unmapped":false,"notes":"날짜 형식 변환 및 소수점 정규화","guidebook_ref":"p.55"}},{{"mt_tag":":52A:","mt_value":"CHASUS33","mx_paths":["Document/pacs.009.001.08/FICdtTrf/CdtTrfTxInf/InstgAgt/FinInstnId/BICFI"],"mx_value":"CHASUS33","is_unmapped":false,"notes":null,"guidebook_ref":"p.58"}},{{"mt_tag":":58A:","mt_value":"DEUTDEDB","mx_paths":["Document/pacs.009.001.08/FICdtTrf/CdtTrfTxInf/CdtrAgt/FinInstnId/BICFI","Document/pacs.009.001.08/FICdtTrf/CdtTrfTxInf/Cdtr/FinInstnId/BICFI"],"mx_value":"DEUTDEDB","is_unmapped":false,"notes":"CdtrAgt와 Cdtr 모두 매핑 필요; Cdtr/Nm 확인 불가","guidebook_ref":"p.60"}}],"unmapped_fields":[],"enhancement_warnings":[{{"field":":58A:","issue":"Cdtr/Nm(수취 기관명) 값이 전문에 없어 MX 필수 엘리먼트 누락 가능. 원거래 데이터로 보완 필요.","guidebook_ref":"p.61"}}]}}

[예시 3 — MT103 Field 72 코드워드 분기 매핑]
변환 방향: MT103 → pacs.008.001.08
원본 전문(마스킹):
  :20:TXN20240116003
  :23B:CRED
  :32A:240116EUR8000,00
  :50K:/<<IBAN_1>>
  <<NAME_1>>
  :59:/<<IBAN_2>>
  <<NAME_2>>
  :71A:SHA
  :72:/INS/CHASUS33
  /ACC/PAYMENT FOR INVOICE
가이드북: (p.45) :72: 코드워드 /INS/ → InstrForCdtrAgt/Cd=PHOA 또는 InstrInf에 기관명. (p.46) /ACC/ → InstrForCdtrAgt/InstrInf (수취 은행 지시). (p.47) /REC/ 또는 /BEN/ → InstrForNxtAgt/InstrInf (다음 중개 은행 지시). (p.48) 전처리: /슬래시/ 사이 코드 추출 후 분기 매핑 필수.
결과: {{"direction":"mt_to_mx","source_type":"MT103","target_type":"pacs.008.001.08","mappings":[{{"mt_tag":":72:","mt_value":"/INS/CHASUS33\n/ACC/PAYMENT FOR INVOICE","mx_paths":["Document/pacs.008.001.08/FIToFICstmrCdtTrf/CdtTrfTxInf/InstrForCdtrAgt/InstrInf","Document/pacs.008.001.08/FIToFICstmrCdtTrf/CdtTrfTxInf/InstrForCdtrAgt/Cd"],"mx_value":"/INS/ → InstrForCdtrAgt/Cd=CHASUS33; /ACC/ → InstrForCdtrAgt/InstrInf=PAYMENT FOR INVOICE","is_unmapped":false,"notes":"/INS/·/ACC/ 코드는 InstrForCdtrAgt로 분기; /REC/·/BEN/ 코드는 InstrForNxtAgt/InstrInf로 분기. 정규식 전처리 필요.","guidebook_ref":"p.45-48"}}],"unmapped_fields":[],"enhancement_warnings":[{{"field":":72:","issue":"Field 72 전체 텍스트를 단일 문자열로 전달 시 /코드워드/ 파싱 로직 누락 위험. MT 35자×6라인 → MX InstrInf 140자 허용으로 데이터 유실 없음.","guidebook_ref":"p.48"}}]}}

[예시 4 — MT103 반환 → pacs.004 원거래 중첩 매핑]
변환 방향: MT103-RETURN → pacs.004.001.09
원본 전문(마스킹):
  :20:RTN20240117004
  :21:TXN20240115001
  :32A:240117EUR10000,00
  :50K:/<<IBAN_1>>
  <<NAME_1>>
  :59:/<<IBAN_2>>
  <<NAME_2>>
  :72:/RETN/AC01
  /ORIG/TXN20240115001
가이드북: (p.70) :20: → TxInf/RtrId. (p.71) :21: → TxInf/OrgnlEndToEndId (원거래 EndToEndId). (p.72) :32A: 금액 → TxInf/RtrdIntrBkSttlmAmt; 날짜 → OrgnlIntrBkSttlmDt. (p.74) 원송금인 → TxInf/OrgnlTxRef/Dbtr/Nm. (p.74) 원수취인 → TxInf/OrgnlTxRef/Cdtr/Nm. (p.75) OrgnlIntrBkSttlmAmt 및 OrgnlIntrBkSttlmDt 필수 — 누락 시 원거래 DB 역추적 필요. (p.76) /RETN/ 코드 → TxInf/RtrRsnInf/Rsn/Cd.
결과: {{"direction":"mt_to_mx","source_type":"MT103-RETURN","target_type":"pacs.004.001.09","mappings":[{{"mt_tag":":20:","mt_value":"RTN20240117004","mx_paths":["Document/pacs.004.001.09/PmtRtr/TxInf/RtrId"],"mx_value":"RTN20240117004","is_unmapped":false,"notes":"반환 거래 고유 식별자","guidebook_ref":"p.70"}},{{"mt_tag":":21:","mt_value":"TXN20240115001","mx_paths":["Document/pacs.004.001.09/PmtRtr/TxInf/OrgnlEndToEndId"],"mx_value":"TXN20240115001","is_unmapped":false,"notes":"원거래 EndToEndId 연결","guidebook_ref":"p.71"}},{{"mt_tag":":32A:","mt_value":"240117EUR10000,00","mx_paths":["Document/pacs.004.001.09/PmtRtr/TxInf/RtrdIntrBkSttlmAmt","Document/pacs.004.001.09/PmtRtr/TxInf/OrgnlIntrBkSttlmDt"],"mx_value":"EUR 10000.00 / 2024-01-15","is_unmapped":false,"notes":"반환 금액 및 원거래 정산일 분리 매핑","guidebook_ref":"p.72"}},{{"mt_tag":":50K:","mt_value":"/<<IBAN_1>> <<NAME_1>>","mx_paths":["Document/pacs.004.001.09/PmtRtr/TxInf/OrgnlTxRef/Dbtr/Nm","Document/pacs.004.001.09/PmtRtr/TxInf/OrgnlTxRef/DbtrAcct/Id/IBAN"],"mx_value":"<<NAME_1>> / <<IBAN_1>>","is_unmapped":false,"notes":"원거래 송금인 정보 — OrgnlTxRef 중첩 구조 내 보존","guidebook_ref":"p.74"}},{{"mt_tag":":59:","mt_value":"/<<IBAN_2>> <<NAME_2>>","mx_paths":["Document/pacs.004.001.09/PmtRtr/TxInf/OrgnlTxRef/Cdtr/Nm","Document/pacs.004.001.09/PmtRtr/TxInf/OrgnlTxRef/CdtrAcct/Id/IBAN"],"mx_value":"<<NAME_2>> / <<IBAN_2>>","is_unmapped":false,"notes":"원거래 수취인 정보 — OrgnlTxRef 중첩 구조 내 보존","guidebook_ref":"p.74"}},{{"mt_tag":":72:","mt_value":"/RETN/AC01","mx_paths":["Document/pacs.004.001.09/PmtRtr/TxInf/RtrRsnInf/Rsn/Cd"],"mx_value":"AC01","is_unmapped":false,"notes":"/RETN/ 코드 추출 → ISO 20022 반환 사유 코드(AC01=잘못된 계좌번호)","guidebook_ref":"p.76"}}],"unmapped_fields":[],"enhancement_warnings":[{{"field":"OrgnlIntrBkSttlmAmt","issue":"pacs.004 필수 항목. 구형 MT 반환 전문에 원거래 금액이 없으면 원 송금 DB 역추적 로직 트리거 필요.","guidebook_ref":"p.75"}},{{"field":"OrgnlIntrBkSttlmDt","issue":"pacs.004 필수 항목. :32A: 날짜를 원거래 정산일로 유추하되 확인 불가 시 DB 조회 필수.","guidebook_ref":"p.75"}}]}}

[예시 5 — RAG 가이드북 범위 외 필드: 환각 방지 (Negative Example)]
변환 방향: LOCAL-CUSTOM → ISO 20022
원본 전문(마스킹):
  #LCLFLD-001:<<LOCAL_SETTLEMENT_CODE>>
  #LCLFLD-007:<<DOMESTIC_CLEARING_REF>>
가이드북: (검색 결과 없음 — RAG 컨텍스트가 빈 상태로 주입됨)
결과: {{"direction":"mt_to_mx","source_type":"LOCAL-CUSTOM","target_type":"ISO 20022","mappings":[{{"mt_tag":"#LCLFLD-001","mt_value":"<<LOCAL_SETTLEMENT_CODE>>","mx_paths":[],"mx_value":null,"is_unmapped":true,"notes":"로컬 청산망 커스텀 필드. 제공된 CBPR+/ISO 20022 가이드북 범위 밖의 데이터로 MX 경로 특정 불가.","guidebook_ref":null}},{{"mt_tag":"#LCLFLD-007","mt_value":"<<DOMESTIC_CLEARING_REF>>","mx_paths":[],"mx_value":null,"is_unmapped":true,"notes":"로컬 청산망 커스텀 필드. 가이드북 컨텍스트에 해당 필드 매핑 규칙이 없어 경로 유추 불가.","guidebook_ref":null}}],"unmapped_fields":["#LCLFLD-001","#LCLFLD-007"],"enhancement_warnings":[{{"field":"ALL","issue":"표준 가이드라인 미정의 필드를 AI가 임의 매핑하면 Payment Reject 위험. 해당 로컬 인터페이스 가이드를 RAG DB에 추가 적재 후 재실행 권장.","guidebook_ref":null}}]}}
"""

MAPPER_USER = """\
{fewshot}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[실제 변환 대상]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[변환 방향]
{source_type} → {target_type}

[원본 전문 (마스킹됨)]
{masked_source}

[Prowide 1차 변환 초안]
{prowide_draft}

[가이드라인 문서 (Qdrant RAG 검색 결과 — 이 내용만 매핑 근거로 사용)]
{rag_context}

[응답 JSON 스키마]
{{
  "direction": "mt_to_mx 또는 mx_to_mt",
  "source_type": "string (예: MT103)",
  "target_type": "string (예: pacs.008.001.08)",
  "mappings": [
    {{
      "mt_tag": "MT 필드 태그 (예: :32A:)",
      "mt_value": "마스킹된 원본 값 또는 null",
      "mx_paths": ["가이드라인에 명시된 MX XML 경로 목록"],
      "mx_value": "가이드라인의 포맷 변환 규칙을 적용한 값 (PLACEHOLDER 유지)",
      "is_unmapped": false,
      "notes": "포맷 변환 설명 또는 주의 사항",
      "guidebook_ref": "[가이드라인 문서]의 출처 (Category | p.N)"
    }}
  ],
  "unmapped_fields": ["가이드라인에 없어 매핑 불가한 MT 태그 목록"],
  "enhancement_warnings": [
    {{
      "field": "string",
      "issue": "경고 내용",
      "guidebook_ref": "string | null"
    }}
  ]
}}

분석 결과(JSON만):\
"""
