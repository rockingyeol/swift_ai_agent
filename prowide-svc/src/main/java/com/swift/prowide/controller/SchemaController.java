package com.swift.prowide.controller;

import com.swift.prowide.service.SchemaService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

/**
 * /schema/mx/{msgType}  — ISO 20022 MX 전문 필드 스키마 반환
 * pw-iso20022 JAXB 모델 클래스에서 리플렉션으로 추출한 XSD 기반 정확한 구조.
 */
@RestController
public class SchemaController {

    private final SchemaService schemaService;

    public SchemaController(SchemaService schemaService) {
        this.schemaService = schemaService;
    }

    /**
     * @param msgType 점 구분 또는 밑줄 구분 모두 허용 (예: admi.024.001.01, admi_024_001_01)
     */
    @GetMapping("/schema/mx/{msgType}")
    public ResponseEntity<Map<String, Object>> getMxSchema(
            @PathVariable String msgType) {
        Map<String, Object> result = schemaService.getMxSchema(msgType);
        if (result.containsKey("error") &&
                ((String) result.get("error")).contains("not found")) {
            return ResponseEntity.status(404).body(result);
        }
        return ResponseEntity.ok(result);
    }

    /** 디버그: pw-iso20022 에서 사용 가능한 MX 클래스 목록 샘플 반환 */
    @GetMapping("/schema/debug/classes")
    public ResponseEntity<Map<String, Object>> debugClasses() {
        return ResponseEntity.ok(schemaService.debugAvailableClasses());
    }
}
