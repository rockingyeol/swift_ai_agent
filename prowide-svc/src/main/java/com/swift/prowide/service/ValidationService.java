package com.swift.prowide.service;

import com.prowidesoftware.swift.model.SwiftBlock4;
import com.prowidesoftware.swift.model.SwiftMessage;
import com.prowidesoftware.swift.model.Tag;
import com.prowidesoftware.swift.model.mt.AbstractMT;
import org.springframework.stereotype.Service;
import org.xml.sax.InputSource;
import org.xml.sax.SAXException;

import javax.xml.parsers.DocumentBuilder;
import javax.xml.parsers.DocumentBuilderFactory;
import java.io.StringReader;
import java.util.*;
import java.util.regex.Pattern;

/**
 * MT 파싱·검증·변환 서비스.
 *
 * validateMt  — Prowide Core SwiftMessage 구조 파싱 + 필드 포맷/네트워크 규칙 검증
 * validateMx  — JAXP XML 정형성 검사 (전체 XSD 검증은 Prowide ISO 20022 SRU 라이브러리 필요)
 * parseMt     — 필드 구조화 추출 (Mapper Agent 전용)
 * translate   — Prowide ISO 20022 SRU 라이브러리 도입 후 구현 예정
 */
@Service
public class ValidationService {

    // ── 기본 포맷 패턴 ──────────────────────────────────────────────────────────
    private static final Pattern DATE_YYMMDD = Pattern.compile("^\\d{6}$");
    private static final Pattern CURRENCY    = Pattern.compile("^[A-Z]{3}$");
    private static final Pattern BIC_PATTERN = Pattern.compile("^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$");
    // SWIFT amount: 숫자 + 쉼표 소수점 (예: 1234,56)
    private static final Pattern AMOUNT_FMT  = Pattern.compile("^\\d+(,\\d+)?$");

    // ── 23B 유효 Bank Operation Code (4!c) ─────────────────────────────────────
    private static final Set<String> VALID_23B_CODES = Set.of("CRED", "SPAY", "SPRI", "SSTD");

    // ── 71A 유효 Details of Charges (3!a) ──────────────────────────────────────
    private static final Set<String> VALID_71A_CODES = Set.of("BEN", "OUR", "SHA");

    // ── MT 타입별 필수 필드 정의 ────────────────────────────────────────────────
    private static final Map<String, List<String>> MANDATORY_FIELDS = Map.of(
            // MT101 Request for Transfer — Seq A: 20/28D/30, Seq B(min 1): 21/32B
            "101", List.of("20", "28D", "30", "21", "32B"),
            "103", List.of("20", "23B", "32A", "50K", "59"),
            "202", List.of("20", "21", "32A", "58A"),
            "900", List.of("20", "21", "25", "32A"),
            "910", List.of("20", "21", "25", "32A")
    );

    // ═══════════════════════════════════════════════════════════════════════════
    // /validate/mt
    // ═══════════════════════════════════════════════════════════════════════════

    public Map<String, Object> validateMt(String content) {
        Map<String, Object> result = new LinkedHashMap<>();
        List<Map<String, Object>> problems = new ArrayList<>();

        if (content == null || content.isBlank()) {
            result.put("parseable", false);
            problems.add(problem("EMPTY_MSG", null, "Message content is empty or null"));
            result.put("problems", problems);
            return result;
        }

        try {
            AbstractMT mt = AbstractMT.parse(content);

            if (mt == null) {
                result.put("parseable", false);
                problems.add(problem("PARSE_FAILED", null, "Prowide could not parse the MT message"));
                result.put("problems", problems);
                return result;
            }

            String msgType = mt.getMessageType(); // "103", "202", …
            result.put("parseable", true);
            result.put("messageType", "MT" + msgType);

            SwiftMessage swiftMsg = mt.getSwiftMessage();
            if (swiftMsg == null) {
                problems.add(problem("INTERNAL", null, "SwiftMessage wrapper is null after parse"));
                result.put("problems", problems);
                return result;
            }

            SwiftBlock4 b4 = swiftMsg.getBlock4();
            if (b4 == null || b4.isEmpty()) {
                problems.add(problem("NO_BLOCK4", null, "Block 4 (message text) is missing or empty"));
                result.put("problems", problems);
                return result;
            }

            List<Tag> tags = b4.getTags();

            // 1. 중복 태그 검출 (기본 네트워크 규칙)
            detectDuplicateTags(tags, problems);

            // 2. 공통 필드 포맷 검증 (날짜·통화·금액·BIC·참조번호)
            validateFieldFormats(tags, problems);

            // 3. MT 타입별 필수 필드 존재 검증
            validateMandatoryFields(tags, msgType, problems);

        } catch (Exception e) {
            result.put("parseable", false);
            problems.add(problem("PARSE_ERROR", null,
                    e.getMessage() != null ? e.getMessage() : "Unexpected parse error"));
        }

        result.put("problems", problems);
        return result;
    }

    // ═══════════════════════════════════════════════════════════════════════════
    // /parse/mt  — 구조화 필드 추출 (Mapper Agent 전용)
    // ═══════════════════════════════════════════════════════════════════════════

    public Map<String, Object> parseMt(String content) {
        Map<String, Object> result = new LinkedHashMap<>();

        if (content == null || content.isBlank()) {
            result.put("parseable", false);
            result.put("error", "Empty content");
            return result;
        }

        try {
            AbstractMT mt = AbstractMT.parse(content);
            if (mt == null) {
                result.put("parseable", false);
                result.put("error", "Prowide parse returned null");
                return result;
            }

            result.put("parseable", true);
            result.put("messageType", "MT" + mt.getMessageType());

            SwiftMessage swiftMsg = mt.getSwiftMessage();
            if (swiftMsg != null) {
                if (swiftMsg.getBlock1() != null) {
                    result.put("block1", swiftMsg.getBlock1().getValue());
                }
                if (swiftMsg.getBlock2() != null) {
                    result.put("block2", swiftMsg.getBlock2().getValue());
                }

                SwiftBlock4 b4 = swiftMsg.getBlock4();
                if (b4 != null) {
                    List<Map<String, String>> fields = new ArrayList<>();
                    for (Tag tag : b4.getTags()) {
                        Map<String, String> f = new LinkedHashMap<>();
                        f.put("tag", tag.getName());
                        f.put("value", tag.getValue() != null ? tag.getValue() : "");
                        fields.add(f);
                    }
                    result.put("fields", fields);
                }
            }

        } catch (Exception e) {
            result.put("parseable", false);
            result.put("error", e.getMessage() != null ? e.getMessage() : "Unknown error");
        }

        return result;
    }

    // ═══════════════════════════════════════════════════════════════════════════
    // /validate/mx  — XML 정형성 검사
    // ═══════════════════════════════════════════════════════════════════════════

    public Map<String, Object> validateMx(String content) {
        Map<String, Object> result = new LinkedHashMap<>();
        List<Map<String, Object>> problems = new ArrayList<>();

        if (content == null || content.isBlank()) {
            result.put("parseable", false);
            problems.add(problem("EMPTY_MSG", null, "MX content is empty"));
            result.put("problems", problems);
            return result;
        }

        try {
            DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
            factory.setNamespaceAware(true);
            // XXE 방어 (OWASP)
            factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
            factory.setFeature("http://xml.org/sax/features/external-general-entities", false);
            factory.setFeature("http://xml.org/sax/features/external-parameter-entities", false);
            factory.setExpandEntityReferences(false);

            DocumentBuilder builder = factory.newDocumentBuilder();
            builder.parse(new InputSource(new StringReader(content)));

            result.put("parseable", true);
            // XSD 스키마 검증은 Prowide ISO 20022 SRU 라이브러리 필요 (plan.md §1(B))
            result.put("note", "XML well-formed check passed. "
                    + "Full XSD schema validation requires Prowide ISO 20022 SRU library.");
        } catch (SAXException e) {
            result.put("parseable", false);
            problems.add(problem("XML_ERROR", null, e.getMessage()));
        } catch (Exception e) {
            result.put("parseable", false);
            problems.add(problem("MX_ERROR", null,
                    e.getMessage() != null ? e.getMessage() : "MX parse error"));
        }

        result.put("problems", problems);
        return result;
    }

    // ═══════════════════════════════════════════════════════════════════════════
    // /translate  — MT↔MX 업리프트 (Prowide ISO 20022 SRU 라이브러리 도입 후 구현)
    // ═══════════════════════════════════════════════════════════════════════════

    public Map<String, Object> translate(String content, String direction) {
        return Map.of(
                "ok", false,
                "error", "Translation requires the Prowide ISO 20022 SRU library "
                        + "(commercial license). See plan.md §1(B).",
                "direction", direction != null ? direction : "unknown"
        );
    }

    // ═══════════════════════════════════════════════════════════════════════════
    // Private helpers
    // ═══════════════════════════════════════════════════════════════════════════

    private void detectDuplicateTags(List<Tag> tags, List<Map<String, Object>> problems) {
        Set<String> seen = new LinkedHashSet<>();
        for (Tag tag : tags) {
            if (!seen.add(tag.getName())) {
                problems.add(problem("DUP_TAG", tag.getName(),
                        "Duplicate tag :" + tag.getName() + ": detected in Block 4"));
            }
        }
    }

    private void validateFieldFormats(List<Tag> tags, List<Map<String, Object>> problems) {
        for (Tag tag : tags) {
            String name  = tag.getName();
            String value = tag.getValue();
            if (value == null) continue;
            String v = value.trim();

            switch (name) {
                case "32A" -> {
                    // 6!n 3!a 15d  예: 240115USD5000,00
                    if (v.length() < 10) {
                        problems.add(problem("FMT_32A", "32A",
                                "Tag 32A is too short (expected 6+3+amount): " + v));
                    } else {
                        String date = v.substring(0, 6);
                        String ccy  = v.substring(6, 9);
                        String amt  = v.substring(9);
                        if (!DATE_YYMMDD.matcher(date).matches())
                            problems.add(problem("FMT_DATE", "32A",
                                    "32A date portion invalid (YYMMDD): " + date));
                        if (!CURRENCY.matcher(ccy).matches())
                            problems.add(problem("FMT_CCY", "32A",
                                    "32A currency code invalid (3!a): " + ccy));
                        if (!AMOUNT_FMT.matcher(amt).matches())
                            problems.add(problem("FMT_AMT", "32A",
                                    "32A amount format invalid (comma decimal required): " + amt));
                    }
                }
                case "52A", "57A", "58A" -> {
                    // 옵션 A: BIC (선행 /계좌 행은 허용)
                    String[] lines = v.split("\n");
                    String bicLine = lines[lines.length - 1].trim();
                    if (!BIC_PATTERN.matcher(bicLine).matches()) {
                        problems.add(problem("FMT_BIC", name,
                                "Tag " + name + " BIC portion format invalid: " + bicLine));
                    }
                }
                case "32B" -> {
                    // 3!a 15d  예: EUR10000,00  (날짜 없는 통화+금액)
                    if (v.length() < 4) {
                        problems.add(problem("FMT_32B", "32B",
                                "Tag 32B is too short (expected 3+amount): " + v));
                    } else {
                        String ccy = v.substring(0, 3);
                        String amt = v.substring(3);
                        if (!CURRENCY.matcher(ccy).matches())
                            problems.add(problem("FMT_CCY", "32B",
                                    "32B currency code invalid (3!a): " + ccy));
                        if (!AMOUNT_FMT.matcher(amt).matches())
                            problems.add(problem("FMT_AMT", "32B",
                                    "32B amount format invalid (comma decimal required): " + amt));
                    }
                }
                case "20" -> {
                    // 16x 참조 번호: 슬래시 시작/끝 금지, 최대 16자
                    if (v.length() > 16)
                        problems.add(problem("FMT_20", "20",
                                "Tag 20 exceeds 16 characters (length=" + v.length() + ")"));
                    if (v.startsWith("/") || v.endsWith("/"))
                        problems.add(problem("FMT_20", "20",
                                "Tag 20 must not start or end with '/'"));
                    if (v.contains("//"))
                        problems.add(problem("FMT_20", "20",
                                "Tag 20 must not contain double slash (//)"));
                }
                case "23B" -> {
                    // 4!c — 정확히 4자 대문자, 유효 코드: CRED/SPAY/SPRI/SSTD
                    if (v.length() != 4 || !v.equals(v.toUpperCase())) {
                        problems.add(problem("FMT_23B", "23B",
                                "Tag 23B must be exactly 4 uppercase characters (4!c), got: " + v));
                    } else if (!VALID_23B_CODES.contains(v)) {
                        problems.add(problem("FMT_23B", "23B",
                                "Tag 23B invalid code '" + v + "'. Valid codes: CRED, SPAY, SPRI, SSTD"));
                    }
                }
                case "71A" -> {
                    // 3!a — 정확히 3자 대문자, 유효 코드: BEN/OUR/SHA
                    if (v.length() != 3 || !v.equals(v.toUpperCase())) {
                        problems.add(problem("FMT_71A", "71A",
                                "Tag 71A must be exactly 3 uppercase characters (3!a), got: " + v));
                    } else if (!VALID_71A_CODES.contains(v)) {
                        problems.add(problem("FMT_71A", "71A",
                                "Tag 71A invalid code '" + v + "'. Valid codes: BEN, OUR, SHA"));
                    }
                }
                default -> { /* 그 외 필드는 Prowide 상세 검증 또는 LLM에 위임 */ }
            }
        }
    }

    private void validateMandatoryFields(List<Tag> tags, String msgType,
                                          List<Map<String, Object>> problems) {
        List<String> required = MANDATORY_FIELDS.getOrDefault(msgType, Collections.emptyList());
        if (required.isEmpty()) return;

        Set<String> present = new HashSet<>();
        for (Tag tag : tags) present.add(tag.getName());

        for (String req : required) {
            if (!present.contains(req)) {
                problems.add(problem("MISSING_FIELD", req,
                        "Mandatory field :" + req + ": missing for MT" + msgType));
            }
        }
    }

    private Map<String, Object> problem(String code, String field, String desc) {
        Map<String, Object> p = new LinkedHashMap<>();
        p.put("code", code);
        if (field != null && !field.isBlank()) p.put("field", field);
        p.put("desc", desc != null ? desc : "");
        return p;
    }
}
