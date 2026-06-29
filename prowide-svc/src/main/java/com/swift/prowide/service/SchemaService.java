package com.swift.prowide.service;

import javax.xml.bind.annotation.XmlElement;
import org.springframework.stereotype.Service;

import java.lang.reflect.Field;
import java.lang.reflect.ParameterizedType;
import java.lang.reflect.Type;
import java.util.*;

/**
 * ISO 20022 MX 전문 유형의 필드 스키마를 pw-iso20022 JAXB 모델 클래스에서
 * 리플렉션으로 추출한다.
 *
 * msgType  "admi.024.001.01"  →  클래스  MxAdmi02400101
 * 최대 재귀 깊이 3까지 섹션(그룹) 구조를 펼쳐 반환한다.
 */
@Service
public class SchemaService {

    private static final String MX_PKG = "com.prowidesoftware.swift.model.mx.";
    private static final int    MAX_DEPTH = 3;

    // ISO 20022 기본 타입 — 이 타입들은 리프 노드로 취급
    private static final Set<String> LEAF_TYPES = Set.of(
        "String", "Boolean", "boolean",
        "BigDecimal", "Long", "long", "Integer", "int",
        "XMLGregorianCalendar", "byte[]",
        "ActiveCurrencyAndAmount", "ActiveOrHistoricCurrencyAndAmount",
        "Max35Text", "Max140Text", "Max350Text", "Max500Text",
        "ISODate", "ISODateTime", "Exact4AlphaNumericText",
        "BICFIDec2014Identifier", "IBANIdentifier",
        "DecimalNumber", "PercentageRate", "ExternalOrganisationIdentification1Code",
        "TrueFalseIndicator", "YesNoIndicator"
    );

    /**
     * MX 전문 유형의 섹션 구조를 반환한다.
     *
     * @param msgType "admi.024.001.01" 형태
     * @return 섹션 목록 또는 오류 정보
     */
    public Map<String, Object> getMxSchema(String msgType) {
        String normalized = msgType.replace("_", ".").toLowerCase();
        String className  = toMxClassName(normalized);
        String fullName   = MX_PKG + className;

        try {
            Class<?> mxClass = Class.forName(fullName);
            List<Map<String, Object>> sections = extractSections(mxClass);
            return Map.of(
                "msg_type", normalized,
                "class",    fullName,
                "sections", sections,
                "source",   "prowide-xsd"
            );
        } catch (ClassNotFoundException e) {
            return Map.of(
                "msg_type", normalized,
                "error",    "Class not found: " + fullName,
                "sections", List.of()
            );
        }
    }

    /** 디버그: MxType enum, XSD 리소스, 클래스 가용 여부 확인 */
    public Map<String, Object> debugAvailableClasses() {
        Map<String, Object> result = new LinkedHashMap<>();

        // 1) MxType enum 존재 여부
        List<String> mxTypeNames = new ArrayList<>();
        try {
            Class<?> mxTypeClass = Class.forName("com.prowidesoftware.swift.model.mx.MxType");
            Object[] constants = mxTypeClass.getEnumConstants();
            for (Object c : constants) mxTypeNames.add(c.toString());
            result.put("mxType_count", constants.length);
            result.put("mxType_sample", mxTypeNames.subList(0, Math.min(10, mxTypeNames.size())));
        } catch (ClassNotFoundException e) {
            result.put("mxType_error", e.getMessage());
        }

        // 2) XSD 리소스 경로 탐색 (admi.024 기준)
        List<String> xsdPaths = List.of(
            "/xsd/admi.024.001.01.xsd",
            "/iso20022/admi.024.001.01.xsd",
            "/admi.024.001.01.xsd",
            "/schema/admi.024.001.01.xsd",
            "/META-INF/xsd/admi.024.001.01.xsd"
        );
        List<String> xsdFound = new ArrayList<>();
        for (String p : xsdPaths) {
            boolean exists = getClass().getResourceAsStream(p) != null;
            xsdFound.add((exists ? "FOUND" : "NOT_FOUND") + ": " + p);
        }
        result.put("xsd_probe", xsdFound);

        // 3) 기존 클래스명 테스트
        String[] testTypes = {"admi.024.001.01", "pacs.008.001.10", "camt.053.001.11"};
        List<String> classCheck = new ArrayList<>();
        for (String t : testTypes) {
            String cls = toMxClassName(t);
            try { Class.forName(MX_PKG + cls); classCheck.add("OK: " + cls); }
            catch (ClassNotFoundException e) { classCheck.add("MISS: " + cls); }
        }
        result.put("class_check", classCheck);
        return result;
    }

    // ── private ──────────────────────────────────────────────────────────────

    /**
     * MX 클래스의 최상위 필드들을 섹션으로 분류한다.
     * AppHdr(헤더)와 Document 바디를 각각 섹션으로 구성한다.
     */
    private List<Map<String, Object>> extractSections(Class<?> mxClass) {
        List<Map<String, Object>> sections = new ArrayList<>();

        for (Field field : getAllFields(mxClass)) {
            XmlElement xmlEl = field.getAnnotation(XmlElement.class);
            if (xmlEl == null) continue;

            String xmlName = xmlEl.name().equals("##default") ? field.getName() : xmlEl.name();
            Class<?> fieldType = resolveType(field);
            boolean  mandatory = xmlEl.required();

            Map<String, Object> section = new LinkedHashMap<>();
            section.put("section",     xmlName);
            section.put("xml_tag",     xmlName);
            section.put("mandatory",   mandatory ? "M" : "O");
            section.put("multiplicity", getMultiplicity(field, mandatory));
            section.put("description", toReadableName(xmlName));

            // 바디 타입은 하위 필드 재귀 추출
            List<Map<String, Object>> children = extractFields(fieldType, 1);
            section.put("fields", children);
            sections.add(section);
        }
        return sections;
    }

    private List<Map<String, Object>> extractFields(Class<?> cls, int depth) {
        if (depth > MAX_DEPTH || cls == null || isLeafType(cls)) return List.of();

        List<Map<String, Object>> result = new ArrayList<>();
        for (Field field : getAllFields(cls)) {
            XmlElement xmlEl = field.getAnnotation(XmlElement.class);
            if (xmlEl == null) continue;

            String   xmlName  = xmlEl.name().equals("##default") ? field.getName() : xmlEl.name();
            Class<?> fType    = resolveType(field);
            boolean  required = xmlEl.required();
            boolean  isList   = List.class.isAssignableFrom(field.getType());

            Map<String, Object> f = new LinkedHashMap<>();
            f.put("xml_tag",     xmlName);
            f.put("name",        toReadableName(xmlName));
            f.put("mandatory",   required ? "M" : "O");
            f.put("multiplicity", getMultiplicity(field, required));
            f.put("type",        fType != null ? fType.getSimpleName() : "?");

            if (fType != null && !isLeafType(fType) && depth < MAX_DEPTH) {
                List<Map<String, Object>> children = extractFields(fType, depth + 1);
                if (!children.isEmpty()) f.put("children", children);
            }
            result.add(f);
        }
        return result;
    }

    /** 클래스 및 부모 클래스의 모든 선언 필드 수집 */
    private List<Field> getAllFields(Class<?> cls) {
        List<Field> fields = new ArrayList<>();
        while (cls != null && cls != Object.class) {
            fields.addAll(Arrays.asList(cls.getDeclaredFields()));
            cls = cls.getSuperclass();
        }
        return fields;
    }

    /** List<T> 이면 T를, 그 외엔 field 자체 타입 반환 */
    private Class<?> resolveType(Field field) {
        if (List.class.isAssignableFrom(field.getType())) {
            Type generic = field.getGenericType();
            if (generic instanceof ParameterizedType pt) {
                Type arg = pt.getActualTypeArguments()[0];
                if (arg instanceof Class<?> c) return c;
            }
        }
        return field.getType();
    }

    private boolean isLeafType(Class<?> cls) {
        if (cls.isPrimitive()) return true;
        if (cls.getName().startsWith("java.")) return true;
        if (cls.isEnum()) return true;
        return LEAF_TYPES.contains(cls.getSimpleName());
    }

    private String getMultiplicity(Field field, boolean required) {
        boolean isList = List.class.isAssignableFrom(field.getType());
        if (isList) return required ? "[1..*]" : "[0..*]";
        return required ? "[1..1]" : "[0..1]";
    }

    /**
     * "admi.024.001.01" → "MxAdmi02400101"
     *   - 첫 파트 첫 글자 대문자
     *   - 이후 파트는 숫자 그대로 연결
     */
    static String toMxClassName(String msgType) {
        String[] parts = msgType.split("\\.");
        if (parts.length < 2) return "Mx" + capitalize(msgType);
        StringBuilder sb = new StringBuilder("Mx");
        sb.append(capitalize(parts[0]));
        for (int i = 1; i < parts.length; i++) sb.append(parts[i]);
        return sb.toString();
    }

    private static String capitalize(String s) {
        if (s == null || s.isEmpty()) return s;
        return Character.toUpperCase(s.charAt(0)) + s.substring(1);
    }

    /** CamelCase XML 태그 → 읽기 쉬운 이름 (GrpHdr → Group Header) */
    private String toReadableName(String xmlTag) {
        // 약어 확장 맵
        Map<String, String> abbr = Map.ofEntries(
            Map.entry("GrpHdr",   "Group Header"),
            Map.entry("MsgId",    "Message Identification"),
            Map.entry("CreDtTm",  "Creation Date Time"),
            Map.entry("NbOfTxs",  "Number of Transactions"),
            Map.entry("TtlIntrBkSttlmAmt", "Total Interbank Settlement Amount"),
            Map.entry("IntrBkSttlmDt",     "Interbank Settlement Date"),
            Map.entry("SttlmInf",  "Settlement Information"),
            Map.entry("PmtTpInf",  "Payment Type Information"),
            Map.entry("CdtTrfTxInf","Credit Transfer Transaction Information"),
            Map.entry("Dbtr",     "Debtor"),
            Map.entry("DbtrAcct", "Debtor Account"),
            Map.entry("DbtrAgt",  "Debtor Agent"),
            Map.entry("Cdtr",     "Creditor"),
            Map.entry("CdtrAcct", "Creditor Account"),
            Map.entry("CdtrAgt",  "Creditor Agent"),
            Map.entry("RmtInf",   "Remittance Information"),
            Map.entry("AppHdr",   "Application Header"),
            Map.entry("Document", "Document"),
            Map.entry("Fr",       "From"),
            Map.entry("To",       "To")
        );
        if (abbr.containsKey(xmlTag)) return abbr.get(xmlTag);
        // CamelCase → space separated
        return xmlTag.replaceAll("([A-Z])", " $1").trim();
    }
}
