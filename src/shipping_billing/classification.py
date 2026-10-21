from __future__ import annotations

import re
from dataclasses import dataclass

from .llm import LLMClient
from .models import DocumentClassification, DocumentKind, NormalizedDocument, SourceType

BILL_PATTERNS = [
    r"\bDEBIT\s*NOTE\b",
    r"\bCREDIT\s*NOTE\b",
    r"\bTAX\s+INVOICE\b",
    r"\bCOMMERCIAL\s+INVOICE\b",
    r"\bINVOICE\b",
    r"\bSTATEMENT\s+OF\s+ACCOUNT\b",
    r"账单",
    r"发票",
    r"借记单",
    r"贷记单",
]
ARRIVAL_PATTERNS = [
    r"\bGENERAL\s+NOTICE\b",
    r"\bARRIVAL\s+NOTICE\b",
    r"\bNOTICE\s+OF\s+ARRIVAL\b",
    r"\bPRE[\s-]*ALERT\b",
    r"到港通知",
    r"预到港",
]
OTHER_PATTERNS = [
    r"\bBILL\s+OF\s+LADING\b",
    r"\bSEA\s+WAYBILL\b",
    r"\bWAYBILL\b",
    r"\bBOOKING\s+CONFIRMATION\b",
    r"\bPACKING\s+LIST\b",
    r"\bCOMMERCIAL\s+DOCUMENT\b",
    r"\bCERTIFICATE\b",
]


@dataclass
class ClassificationContext:
    has_charges: bool = False
    has_currency: bool = False
    has_total: bool = False
    invoice_number: str | None = None


def _match_patterns(text: str, patterns: list[str]) -> list[str]:
    found: list[str] = []
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            found.append(match.group(0))
    return found


def _ocr_context(doc: NormalizedDocument) -> ClassificationContext:
    ocr = doc.parser_metadata.get("ocr") or {}
    items = ocr.get("items") or [] if isinstance(ocr, dict) else []
    has_charges = any(item.get("charges") for item in items)
    invoice_number = next(
        (item.get("invoice_number") for item in items if item.get("invoice_number")), None
    )
    return ClassificationContext(
        has_charges=has_charges,
        invoice_number=invoice_number,
    )


def rule_classify(doc: NormalizedDocument) -> DocumentClassification:
    text = doc.full_text or ""
    upper = text.upper()
    reasons: list[str] = []
    evidence: list[str] = []
    if doc.source.source_type == SourceType.EML:
        return DocumentClassification(
            kind=DocumentKind.BUSINESS_EMAIL,
            confidence=0.95,
            reasons=["EML 邮件按业务邮件处理，正文作为费用上下文"],
            evidence=[doc.parser_metadata.get("subject") or doc.source.source_file],
        )
    if doc.parser_metadata.get("transport_document"):
        return DocumentClassification(
            kind=DocumentKind.OTHER,
            confidence=0.95,
            reasons=["解析器识别为提单/海运单，不作为独立账单"],
            evidence=[doc.source.source_file],
        )

    arrival_hits = _match_patterns(upper, ARRIVAL_PATTERNS)
    bill_hits = _match_patterns(upper, BILL_PATTERNS)
    other_hits = _match_patterns(upper, OTHER_PATTERNS)
    ocr_ctx = _ocr_context(doc)
    has_charge_rows = any(
        block.kind == "table" and block.location == "ocr:charges" and block.rows
        for block in doc.blocks
    )
    charge_like = ocr_ctx.has_charges or has_charge_rows or bool(
        re.search(r"(?i)\b(handling|freight|charge|fee)\b", upper)
    )
    currency_like = bool(re.search(r"\b(USD|EUR|CNY|GBP|HKD|SGD|JPY)\b", upper))
    total_like = bool(re.search(r"(?i)\b(total|grand\s+total|balance\s+due)\b", upper))

    if arrival_hits:
        reasons.append("命中到港/通知类关键词")
        evidence.extend(arrival_hits[:3])
        return DocumentClassification(
            kind=DocumentKind.ARRIVAL_NOTICE,
            confidence=0.9,
            reasons=reasons,
            evidence=evidence,
        )
    if bill_hits:
        reasons.append("命中账单/发票类关键词")
        evidence.extend(bill_hits[:3])
        if charge_like:
            reasons.append("存在费用行信号")
        return DocumentClassification(
            kind=DocumentKind.BILL,
            confidence=0.92 if charge_like else 0.8,
            reasons=reasons,
            evidence=evidence,
        )
    if ocr_ctx.invoice_number and ocr_ctx.has_charges:
        reasons.append("OCR 返回发票号与费用明细")
        evidence.append(ocr_ctx.invoice_number)
        return DocumentClassification(
            kind=DocumentKind.BILL,
            confidence=0.7,
            reasons=reasons,
            evidence=evidence,
        )
    if other_hits:
        return DocumentClassification(
            kind=DocumentKind.OTHER,
            confidence=0.8,
            reasons=["命中其他业务文档关键词（如提单/装箱单），不作为独立账单"],
            evidence=other_hits[:3],
        )
    if charge_like and (currency_like or total_like):
        reasons.append("存在费用、金额或合计信号，按账单候选处理")
        return DocumentClassification(
            kind=DocumentKind.BILL,
            confidence=0.62,
            reasons=reasons,
            evidence=[],
        )
    if doc.parse_error:
        return DocumentClassification(
            kind=DocumentKind.UNCERTAIN,
            confidence=0.2,
            reasons=[f"解析异常: {doc.parse_error}"],
            evidence=[],
        )
    if not text.strip():
        return DocumentClassification(
            kind=DocumentKind.UNCERTAIN,
            confidence=0.1,
            reasons=["文档没有可用文本"],
            evidence=[],
        )
    return DocumentClassification(
        kind=DocumentKind.OTHER,
        confidence=0.4,
        reasons=["未命中账单、发票或到港通知信号"],
        evidence=[],
    )


CLASSIFY_SYSTEM = """You classify shipping/logistics documents.
Return only one JSON object with keys: document_kind, confidence, reason, evidence.
document_kind must be one of: BILL, BUSINESS_EMAIL, ARRIVAL_NOTICE, OTHER, UNCERTAIN.
BILL includes debit note, credit note, invoice, billing statement with charge amounts.
ARRIVAL_NOTICE includes arrival notice and general notice documents.
Never infer facts that are not in the provided text. Keep reason under 40 Chinese words."""


def llm_classify(doc: NormalizedDocument, llm: LLMClient) -> DocumentClassification | None:
    if not llm.available:
        return None
    text = (doc.full_text or "")[:6000]
    if not text.strip():
        return None
    response = llm.complete_json(
        CLASSIFY_SYSTEM,
        "请分类以下文档：\n\n" + text,
        cache_tag="classify",
    )
    if not isinstance(response.data, dict):
        return None
    raw_kind = str(response.data.get("document_kind") or "").upper()
    try:
        kind = DocumentKind(raw_kind)
    except ValueError:
        return None
    confidence = response.data.get("confidence")
    try:
        confidence_value = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence_value = 0.5
    reason = str(response.data.get("reason") or "")
    evidence = response.data.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    return DocumentClassification(
        kind=kind,
        confidence=confidence_value,
        reasons=["LLM 分类" + (f": {reason}" if reason else "")],
        evidence=[str(x) for x in evidence if str(x).strip()][:5],
    )


def classify_document(doc: NormalizedDocument, llm: LLMClient | None = None) -> DocumentClassification:
    result = rule_classify(doc)
    if doc.parser_metadata.get("transport_document"):
        return result
    if result.kind in (DocumentKind.UNCERTAIN, DocumentKind.OTHER) and llm and llm.available:
        llm_result = llm_classify(doc, llm)
        if llm_result and llm_result.kind != DocumentKind.UNCERTAIN:
            return llm_result
    return result
