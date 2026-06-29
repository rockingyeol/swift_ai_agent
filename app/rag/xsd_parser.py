"""
ISO 20022 XSD 파일 파서.

data/XSD/ 하위 폴더를 재귀 스캔하여 {msg_type: xsd_path} 인덱스를 구축하고,
XSD를 파싱해 schema_explorer 가 사용하는 sections 형식으로 반환한다.

섹션 구조:
  sections = [
    {
      "section":      "GrpHdr",
      "xml_tag":      "GrpHdr",
      "mandatory":    "M",
      "multiplicity": "[1..1]",
      "description":  "Group Header",
      "fields": [
        {
          "xml_tag":     "MsgId",
          "name":        "Message Identification",
          "mandatory":   "M",
          "multiplicity":"[1..1]",
          "type":        "Max35Text",
          "description": "Message Identification",
          "example":     ""
        }, ...
      ]
    }, ...
  ]
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

# XSD 루트 디렉토리 (컨테이너 내 마운트 경로)
_XSD_ROOT = Path("/app/data/XSD")
_XS = "http://www.w3.org/2001/XMLSchema"

# msg_type → XSD 경로 인덱스 (최초 접근 시 빌드)
_INDEX: dict[str, Path] | None = None


# ── 인덱스 ────────────────────────────────────────────────────────────────────

def _build_index() -> dict[str, Path]:
    """XSD 루트 하위를 재귀 스캔해 {msg_type: path} 맵 반환."""
    index: dict[str, Path] = {}
    if not _XSD_ROOT.exists():
        log.warning("xsd_root_not_found", path=str(_XSD_ROOT))
        return index
    for xsd_path in _XSD_ROOT.rglob("*.xsd"):
        # 파일명에서 msg_type 추출: pacs.008.001.14.xsd → pacs.008.001.14
        name = xsd_path.stem  # 확장자 제거
        # 점 구분 패턴에 맞는 것만 (예: pacs.008.001.14)
        if re.match(r'^[a-z]{2,6}\.\d{3}\.\d{3}\.\d{1,3}$', name):
            index[name] = xsd_path
    log.info("xsd_index_built", count=len(index))
    return index


def _get_index() -> dict[str, Path]:
    global _INDEX
    if _INDEX is None:
        _INDEX = _build_index()
    return _INDEX


def get_xsd_path(msg_type: str) -> Optional[Path]:
    """msg_type(예: pacs.008.001.14)에 해당하는 XSD 경로 반환. 없으면 None."""
    normalized = msg_type.lower().replace("_", ".")
    index = _get_index()
    # 정확히 일치
    if normalized in index:
        return index[normalized]
    # 버전 앞 부분으로 최신 버전 찾기 (예: pacs.008.001 → 가장 높은 버전)
    prefix = ".".join(normalized.split(".")[:3]) + "."
    candidates = {k: v for k, v in index.items() if k.startswith(prefix)}
    if candidates:
        latest = sorted(candidates.keys())[-1]
        log.info("xsd_version_fallback", requested=normalized, using=latest)
        return candidates[latest]
    return None


def list_available_types() -> list[str]:
    """사용 가능한 모든 msg_type 목록 반환."""
    return sorted(_get_index().keys())


# ── XSD 파싱 ──────────────────────────────────────────────────────────────────

def parse_xsd(msg_type: str) -> Optional[list[dict]]:
    """
    XSD를 파싱해 sections 목록을 반환한다.
    XSD가 없거나 파싱 실패 시 None 반환.
    """
    path = get_xsd_path(msg_type)
    if path is None:
        return None
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        type_map = _build_type_map(root)
        sections = _extract_sections(root, type_map)
        log.info("xsd_parsed", msg_type=msg_type, path=str(path),
                 sections=len(sections))
        return sections
    except Exception as e:
        log.error("xsd_parse_error", msg_type=msg_type, error=str(e))
        return None


def _build_type_map(root: ET.Element) -> dict[str, ET.Element]:
    """complexType name → element 맵."""
    return {
        el.get("name"): el
        for el in root.findall(f"{{{_XS}}}complexType")
        if el.get("name")
    }


def _extract_sections(root: ET.Element, type_map: dict) -> list[dict]:
    """
    Document complexType 파싱:
    - Document의 직접 자식이 1개이고 그 자식의 fields가 여러 개면 한 단계 언랩
      (예: Document → FIToFICstmrCdtTrf → {GrpHdr, CdtTrfTxInf, ...})
    - 직접 자식이 여러 개면 각 자식을 섹션으로 사용
    """
    doc_type = type_map.get("Document")
    if doc_type is None:
        return []

    doc_children = list(_iter_sequence_elements(doc_type))

    # 자식이 1개이고, 그 자식 타입의 필드가 여러 개면 → 언랩
    if len(doc_children) == 1:
        only = doc_children[0]
        type_ref = only.get("type", "")
        if type_ref and type_ref in type_map:
            sub_fields = list(_iter_sequence_elements(type_map[type_ref]))
            if len(sub_fields) > 1:
                return _build_sections_from_elements(sub_fields, type_map)

    return _build_sections_from_elements(doc_children, type_map)


def _build_sections_from_elements(elements, type_map: dict) -> list[dict]:
    sections = []
    for child_el in elements:
        xml_tag  = child_el.get("name", "")
        min_occ  = child_el.get("minOccurs", "1")
        max_occ  = child_el.get("maxOccurs", "1")
        type_ref = child_el.get("type", "")
        mandatory = "M" if min_occ != "0" else "O"
        mult      = _multiplicity(min_occ, max_occ)

        fields: list[dict] = []
        if type_ref and type_ref in type_map:
            fields = _parse_type_fields(type_map[type_ref], type_map, depth=0)

        sections.append({
            "section":      xml_tag,
            "xml_tag":      xml_tag,
            "mandatory":    mandatory,
            "multiplicity": mult,
            "description":  _readable(xml_tag),
            "fields":       fields,
        })
    return sections


def _parse_type_fields(
    complex_type: ET.Element,
    type_map: dict,
    depth: int,
    max_depth: int = 6,
) -> list[dict]:
    """complexType 내 sequence/all 요소들을 재귀적으로 필드 목록으로 변환."""
    if depth >= max_depth:
        return []

    fields = []
    for el in _iter_sequence_elements(complex_type):
        xml_tag  = el.get("name", "")
        min_occ  = el.get("minOccurs", "1")
        max_occ  = el.get("maxOccurs", "1")
        type_ref = el.get("type", "")
        mandatory = "M" if min_occ != "0" else "O"
        mult      = _multiplicity(min_occ, max_occ)

        # choice 그룹 내 요소인지 확인 (부모가 xs:choice)
        is_choice = _is_in_choice(complex_type, xml_tag)

        field: dict = {
            "xml_tag":      xml_tag,
            "name":         _readable(xml_tag),
            "mandatory":    "O" if is_choice else mandatory,
            "multiplicity": mult,
            "type":         _short_type(type_ref),
            "description":  _readable(xml_tag),
            "example":      "",
        }

        # 하위 complexType 재귀 (리프 타입이 아닌 경우)
        if type_ref and type_ref in type_map and not _is_leaf_type(type_ref):
            children = _parse_type_fields(
                type_map[type_ref], type_map, depth + 1, max_depth
            )
            if children:
                field["children"] = children

        fields.append(field)

    # choice 처리: xs:choice 직접 자식도 수집
    for choice in complex_type.findall(f".//{{{_XS}}}choice"):
        for el in choice.findall(f"{{{_XS}}}element"):
            xml_tag  = el.get("name", "")
            type_ref = el.get("type", "")
            if not xml_tag or any(f["xml_tag"] == xml_tag for f in fields):
                continue
            field = {
                "xml_tag":      xml_tag,
                "name":         _readable(xml_tag),
                "mandatory":    "O",
                "multiplicity": "[0..1]",
                "type":         _short_type(type_ref),
                "description":  _readable(xml_tag),
                "example":      "",
            }
            if type_ref and type_ref in type_map and not _is_leaf_type(type_ref):
                children = _parse_type_fields(
                    type_map[type_ref], type_map, depth + 1, max_depth
                )
                if children:
                    field["children"] = children
            fields.append(field)

    return fields


def _iter_sequence_elements(complex_type: ET.Element):
    """complexType 내 xs:sequence / xs:all 의 직접 xs:element 자식만 반환."""
    for container in ("sequence", "all"):
        seq = complex_type.find(f"{{{_XS}}}{container}")
        if seq is not None:
            yield from seq.findall(f"{{{_XS}}}element")
            return
    # complexContent > extension > sequence
    cc = complex_type.find(f"{{{_XS}}}complexContent")
    if cc is not None:
        ext = cc.find(f"{{{_XS}}}extension")
        if ext is not None:
            seq = ext.find(f"{{{_XS}}}sequence")
            if seq is not None:
                yield from seq.findall(f"{{{_XS}}}element")


def _is_in_choice(complex_type: ET.Element, xml_tag: str) -> bool:
    for choice in complex_type.findall(f".//{{{_XS}}}choice"):
        for el in choice.findall(f"{{{_XS}}}element"):
            if el.get("name") == xml_tag:
                return True
    return False


_LEAF_SUFFIXES = (
    "Text", "Identifier", "Code", "Indicator", "Amount", "Rate",
    "Number", "Date", "DateTime", "Time", "Id", "Name",
)
_LEAF_EXACT = {
    "ISODate", "ISODateTime", "ISOTime", "Max35Text", "Max140Text",
    "Max350Text", "Max500Text", "Max10000Text", "DecimalNumber",
    "PercentageRate", "ActiveCurrencyCode", "CurrencyCode",
    "BICFIDec2014Identifier", "LEIIdentifier", "CountryCode",
    "TrueFalseIndicator", "YesNoIndicator", "ExternalOrganisationIdentification1Code",
}


def _is_leaf_type(type_name: str) -> bool:
    if type_name in _LEAF_EXACT:
        return True
    return any(type_name.endswith(s) for s in _LEAF_SUFFIXES)


def _multiplicity(min_occ: str, max_occ: str) -> str:
    mn = min_occ if min_occ else "1"
    mx = max_occ if max_occ else "1"
    if mx == "unbounded":
        return f"[{mn}..*]"
    return f"[{mn}..{mx}]"


def _short_type(type_ref: str) -> str:
    """네임스페이스 제거."""
    return type_ref.split(":")[-1] if type_ref else ""


# CamelCase → 사람이 읽기 쉬운 이름
_ABBR = {
    "GrpHdr": "Group Header", "MsgId": "Message Identification",
    "CreDtTm": "Creation Date Time", "NbOfTxs": "Number of Transactions",
    "TtlIntrBkSttlmAmt": "Total Interbank Settlement Amount",
    "IntrBkSttlmDt": "Interbank Settlement Date",
    "SttlmInf": "Settlement Information", "PmtTpInf": "Payment Type Information",
    "CdtTrfTxInf": "Credit Transfer Transaction Information",
    "Dbtr": "Debtor", "DbtrAcct": "Debtor Account", "DbtrAgt": "Debtor Agent",
    "Cdtr": "Creditor", "CdtrAcct": "Creditor Account", "CdtrAgt": "Creditor Agent",
    "RmtInf": "Remittance Information", "Ustrd": "Unstructured",
    "InstdAmt": "Instructed Amount", "XchgRate": "Exchange Rate",
    "ChrgBr": "Charge Bearer", "ChrgsInf": "Charges Information",
    "PrvsInstgAgt1": "Previous Instructing Agent 1",
    "IntrmyAgt1": "Intermediary Agent 1",
    "IntrBkSttlmAmt": "Interbank Settlement Amount",
    "Purp": "Purpose", "RgltryRptg": "Regulatory Reporting",
    "SplmtryData": "Supplementary Data", "AppHdr": "Application Header",
    "Document": "Document", "Fr": "From", "To": "To",
    "BizMsgIdr": "Business Message Identifier",
    "MsgDefIdr": "Message Definition Identifier",
    "BizSvc": "Business Service", "CreDt": "Creation Date",
}


def _readable(xml_tag: str) -> str:
    if xml_tag in _ABBR:
        return _ABBR[xml_tag]
    # CamelCase를 공백 분리
    s = re.sub(r'([A-Z][a-z])', r' \1', xml_tag).strip()
    s = re.sub(r'([a-z])([A-Z])', r'\1 \2', s)
    return s.strip()
