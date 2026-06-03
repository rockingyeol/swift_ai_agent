package com.swift.prowide.controller;

import com.swift.prowide.service.ValidationService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

/**
 * /validate/mt  — MT 구문/네트워크 검증
 * /validate/mx  — MX (ISO 20022) XML 정형성 검사 (XSD는 Prowide ISO 20022 SRU 필요)
 * /parse/mt     — MT 필드 구조화 추출 (Mapper Agent 전용)
 * /translate    — MT ↔ MX 변환 (Prowide ISO 20022 SRU 도입 후 구현 예정)
 */
@RestController
public class ValidationController {

    private final ValidationService service;

    public ValidationController(ValidationService service) {
        this.service = service;
    }

    @PostMapping("/validate/mt")
    public ResponseEntity<Map<String, Object>> validateMt(@RequestBody Map<String, String> body) {
        return ResponseEntity.ok(service.validateMt(body.get("content")));
    }

    @PostMapping("/validate/mx")
    public ResponseEntity<Map<String, Object>> validateMx(@RequestBody Map<String, String> body) {
        return ResponseEntity.ok(service.validateMx(body.get("content")));
    }

    @PostMapping("/parse/mt")
    public ResponseEntity<Map<String, Object>> parseMt(@RequestBody Map<String, String> body) {
        return ResponseEntity.ok(service.parseMt(body.get("content")));
    }

    @PostMapping("/translate")
    public ResponseEntity<Map<String, Object>> translate(@RequestBody Map<String, String> body) {
        return ResponseEntity.ok(
            service.translate(body.get("content"), body.get("direction"))
        );
    }
}
