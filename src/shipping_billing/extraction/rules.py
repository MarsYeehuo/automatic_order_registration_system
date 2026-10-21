from __future__ import annotations

import email.utils
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from ..config import ChargeDictionary, ValidationSettings
from ..models import (
    BillCandidate,
    ChargeCandidate,
    ContentBlock,
    DocumentClassification,
    DocumentKind,
    ExtractionMethod,
    FieldEvidence,
    NormalizedDocument,
    PaymentTerm,
    SourceType,
)
from ..normalization import PaymentTermNormalizer, normalize_currency
from ..utils import (
    find_currencies,
    is_valid_container_number,
    normalize_charge_name,
    parse_date,
    parse_decimal,
)

DOCUMENT_TYPE_PATTERNS = [
    (r"\bDEBIT\s+NOTE\b", "Debit Note"),
    (r"\bCREDIT\s+NOTE\b", "Credit Note"),
    (r"\bTAX\s+INVOICE\b", "Tax Invoice"),
    (r"\bCOMMERCIAL\s+INVOICE\b", "Commercial Invoice"),
    (r"\bINVOICE\b", "Invoice"),
    (r"\bGENERAL\s+NOTICE\b", "General Notice"),
    (r"\bARRIVAL\s+NOTICE\b", "Arrival Notice"),
    (r"\bPRE[\s-]*ALERT\b", "PRE-ALERT"),
]

REFERENCE_PATTERNS = [
    r"(?im)\bD\s*/\s*C\s*(?:NO\.?|NUMBER|#)?\s*[:：]\s*([A-Z0-9][A-Z0-9._/-]{2,})",
    r"(?im)\b(?:DEBIT|CREDIT)\s+NOTE\s*(?:NO\.?|NUMBER|#)?\s*[:：]\s*([A-Z0-9][A-Z0-9._/-]{2,})",
    r"(?im)\b(?:INVOICE|INV)\s*(?:NO\.?|NUMBER|#)\s*[:：]\s*([A-Z0-9][A-Z0-9._/-]{2,})",
    r"(?im)\b(?:DOCUMENT|DOC|BILL)\s*(?:NO\.?|NUMBER|#)\s*[:：]\s*([A-Z0-9][A-Z0-9._/-]{2,})",
]

DATE_PATTERNS = [
    (r"(?im)\bD\s*/\s*C\s*DATE\b\s*[:：]?\s*([0-9]{1,4}[-/\.][0-9]{1,2}[-/\.][0-9]{1,4})", 1.0),
    (r"(?im)\bINVOICE\s*DATE\b\s*[:：]?\s*([0-9]{1,4}[-/\.][0-9]{1,2}[-/\.][0-9]{1,4})", 1.0),
    (r"(?im)\bDOCUMENT\s*DATE\b\s*[:：]?\s*([0-9]{1,4}[-/\.][0-9]{1,2}[-/\.][0-9]{1,4})", 1.0),
    (r"(?im)\bBILL\s*DATE\b\s*[:：]?\s*([0-9]{1,4}[-/\.][0-9]{1,2}[-/\.][0-9]{1,4})", 0.95),
    (r"(?im)\b(?:ISSUE|ISSUED)\s*DATE\b\s*[:：]?\s*([0-9]{1,4}[-/\.][0-9]{1,2}[-/\.][0-9]{1,4})", 0.9),
]

PDF_CHARGE_RE = re.compile(
    r"^\s*(?P<name>[A-Z0-9][A-Z0-9 '&/().,\-]*?)\s{2,}-\s+"
    r"(?P<rate>[\d,]+\.\d{2})\s+(?P<qty>[\d,.]+)\s+"
    r"(?P<pc>[PC])\s+(?P<amount>[\d,]+\.\d{2})\s*$"
)
PDF_CHARGE_RE_NO_PC = re.compile(
    r"^\s*(?P<name>[A-Z0-9][A-Z0-9 '&/().,\-]*?)\s{2,}"
    r"(?P<rate>[\d,]+\.\d{2})\s+(?P<qty>[\d,.]+)\s+"
    r"(?P<amount>[\d,]+\.\d{2})\s*$"
)
CONTAINER_RE = re.compile(r"\b([A-Z]{4}\d{7})\b")
EMAIL_MONEY_RE = re.compile(
    r"(?:(USD|US\$|CNY|RMB|EUR|GBP|HKD|SGD|AUD|\$|€|£|¥)\s*)?"
    r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s*(USD|CNY|RMB|EUR|GBP|HKD|SGD|AUD))?"
)
SKIP_CHARGE_NAMES = {
    "TOTAL",
    "SUBTOTAL",
    "SUB TOTAL",
    "GRAND TOTAL",
    "BALANCE",
    "BALANCE DUE",
    "PAID AMOUNT",
    "AMOUNT",
    "TOTAL AMOUNT",
    "TOTAL CHARGES",
    "DESCRIPTION",
    "CHARGE",
    "CHARGES",
    "AMOUNT DUE",
    "PAYMENT",
}


def _clean_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def find_location(doc: NormalizedDocument, needle: str | None) -> str:
    if not needle:
        return doc.source.location
    norm_needle = _clean_ws(needle).lower()
    for block in doc.blocks:
        if block.kind == "text" and block.text:
            if norm_needle in _clean_ws(block.text).lower():
                return block.location
        elif block.kind == "table" and block.rows:
            for index, row in enumerate(block.rows, start=1):
                if norm_needle in _clean_ws(" ".join(row)).lower():
                    return f"{block.location},row:{index}"
    return doc.source.location


def _evidence(
    doc: NormalizedDocument,
    field: str,
    raw_text: str | None,
    method: ExtractionMethod = ExtractionMethod.RULE,
    confidence: float = 0.9,
    note: str | None = None,
    location: str | None = None,
) -> FieldEvidence:
    return FieldEvidence(
        field=field,
        source_file=doc.source.source_file,
        location=location or find_location(doc, raw_text),
        raw_text=_clean_ws(raw_text or "") or None,
        method=method,
        confidence=confidence,
        note=note,
    )


def _add_field(
    candidate: BillCandidate,
    doc: NormalizedDocument,
    field: str,
    value,
    raw_text: str | None,
    method: ExtractionMethod = ExtractionMethod.RULE,
    confidence: float = 0.9,
    note: str | None = None,
) -> None:
    if value is None or value == "" or value == []:
        return
    setattr(candidate, field, value)
    candidate.evidence.append(
        _evidence(doc, field, raw_text or str(value), method, confidence, note)
    )


def _ocr_items(doc: NormalizedDocument) -> list[dict]:
    ocr = doc.parser_metadata.get("ocr") or {}
    if isinstance(ocr, dict):
        return list(ocr.get("items") or [])
    return []


def extract_document_type(doc: NormalizedDocument, classification: DocumentClassification) -> tuple[str | None, str | None]:
    text = doc.full_text or ""
    for pattern, label in DOCUMENT_TYPE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return label, match.group(0)
    if doc.source.source_type == SourceType.EML:
        subject = str(doc.parser_metadata.get("subject") or "")
        if re.search(r"(?i)PRE[\s-]*ALERT", subject):
            return "PRE-ALERT", subject[:80]
        return "Business Email", subject[:80] or None
    if _ocr_items(doc) and any(item.get("invoice_number") for item in _ocr_items(doc)):
        return "Invoice", "OCR invoiceNumber"
    if classification.kind == DocumentKind.ARRIVAL_NOTICE:
        return "Arrival Notice", None
    if classification.kind == DocumentKind.BILL:
        return "Bill", None
    return None, None


def extract_reference(doc: NormalizedDocument) -> tuple[str | None, str | None, ExtractionMethod | None]:
    text = doc.full_text or ""
    for pattern in REFERENCE_PATTERNS:
        match = re.search(pattern, text)
        if match:
            value = match.group(1).strip(".,;:")
            if value.upper() not in {"NO", "NUMBER", "N/A"}:
                return value, match.group(0), ExtractionMethod.RULE
    for item in _ocr_items(doc):
        invoice_number = item.get("invoice_number")
        if invoice_number:
            return str(invoice_number), f"OCR invoiceNumber={invoice_number}", ExtractionMethod.OCR
    if doc.source.source_type == SourceType.EML:
        subject = str(doc.parser_metadata.get("subject") or "")
        match = re.search(r"(?i)PRE[\s-]*ALERT\s+([A-Z0-9-]{6,})", subject)
        if match:
            return match.group(1), match.group(0), ExtractionMethod.EML_BODY
    return None, None, None


def extract_date(doc: NormalizedDocument, date_locale: str = "US") -> tuple[date | None, str | None, float]:
    text = doc.full_text or ""
    day_first = date_locale.upper() == "EU"
    for pattern, confidence in DATE_PATTERNS:
        match = re.search(pattern, text)
        if match:
            parsed = parse_date(match.group(1), day_first=day_first)
            if parsed:
                return parsed, match.group(0), confidence
    if doc.source.source_type == SourceType.EML:
        header = str(doc.parser_metadata.get("date_header") or "")
        parsed = parse_date(header, day_first=day_first)
        if parsed:
            return parsed, header, 0.9
    return None, None, 0.0


def _valid_party(value: str) -> bool:
    if not value or len(value) < 3:
        return False
    if re.search(r"[#@]|\b(ABA|ACH|ACCOUNT|ROUTING|SWIFT|TEL|FAX|E_?MAIL|PHONE|ATTN)\b", value, re.IGNORECASE):
        return False
    if re.search(
        r"(?i)\b(NOTIFY\s+PARTY|CONSIGNEE|SHIPPER|BILL\s+TO|SOLD\s+TO|PLEASE\s+NOTE|"
        r"WIRE\s+TRANSFERS?|YOU\s+MAY|BANK|SEND|CONTACT|CUSTOMER\s+SERVICE|TELEPHONE|"
        r"HERE|BELOW|BENEFICIARY)\b",
        value,
    ):
        return False
    if re.search(
        r"(?i)\b(DO|DOES|DID|WILL|SHALL|WOULD|SHOULD|INCLUDE|INCLUDES|INCLUDING|"
        r"RESPONSIBLE|INCURRED|MERCHANT)\b",
        value,
    ):
        return False
    if not re.search(r"[A-Za-z一-鿿]", value):
        return False
    digits = len(re.findall(r"\d", value))
    return digits / max(1, len(value)) <= 0.25


def _clean_party(value: str | None) -> str | None:
    if not value:
        return None
    value = _clean_ws(value)
    value = re.split(r"\s{2,}", value)[0]
    value = re.sub(
        r"(?i)\s+(?:TEL|FAX|E_?MAIL|EMAIL|PHONE|ATTN)\b.*$", "", value
    )
    value = re.sub(r"\s+(?:USD|EUR|CNY|GBP|HKD|SGD|AUD|JPY)\b.*$", "", value, flags=re.IGNORECASE)
    value = value.strip(" ,;:.-")
    if not _valid_party(value):
        return None
    return value[:160]


def _labeled_party(text: str, labels: list[str]) -> tuple[str | None, str | None]:
    for label in labels:
        match = re.search(
            rf"(?im)^\s*{label}\s*[:：]\s*([^\n]{{3,160}})",
            text,
        )
        if match:
            value = _clean_party(match.group(1))
            if value:
                return value, match.group(0)
    return None, None


def _letterhead(text: str) -> tuple[str | None, str | None]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    legal = re.compile(
        r"(?i)\b(LTD|LIMITED|LLC|INC|INCORPORATED|CORP|CORPORATION|GMBH|COMPANY)(?![A-Za-z])|"
        r"CO\.?\s*,?\s*LTD|有限公司"
    )
    operational = re.compile(r"(?i)\b(SHIPPING|LOGISTICS)(?![A-Za-z])|物流|航运")
    for line in lines[:12]:
        if re.search(
            r"(?i)\b(DEBIT|CREDIT|INVOICE|NOTICE|BILL TO|DESCRIPTION|NOTIFY|CONSIGNEE|SHIPPER|PARTY)\b",
            line,
        ):
            continue
        legal_hits = list(legal.finditer(line))
        if legal_hits:
            candidate = line[: legal_hits[-1].end()].strip(" ,;:-")
        elif operational.search(line):
            hits = list(operational.finditer(line))
            candidate = line[: hits[-1].end()].strip(" ,;:-") if re.search(r"\d", line) else line
        else:
            continue
        candidate = candidate.strip(" ,;:-")
        if len(candidate) <= 160 and _valid_party(candidate):
            return candidate, line
    return None, None


def _creditor(text: str) -> tuple[str | None, str | None]:
    patterns = [
        r"(?im)BALANCE\s+DUE\s+TO\s*[:：]?\s*([A-Z][^\n]{2,140})",
        r"(?im)AMOUNT\s+DUE\s+TO\s*[:：]?\s*([A-Z][^\n]{2,140})",
        r"(?im)PAYABLE\s+TO\s*[:：]?\s*([A-Z][^\n]{2,140})",
        r"(?im)REMIT\s+TO\s*[:：]?\s*([A-Z][^\n]{2,140})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            value = _clean_party(match.group(1))
            if value:
                return value, match.group(0)
    return None, None


def _email_parties(doc: NormalizedDocument, accounting_entity_names: list[str]) -> tuple[str | None, str | None, list[tuple[str, str]]]:
    sender = str(doc.parser_metadata.get("from") or "")
    recipient = str(doc.parser_metadata.get("to") or "")
    sender_name, sender_addr = email.utils.parseaddr(sender)
    recipient_name, recipient_addr = email.utils.parseaddr(recipient)
    sender_party = _clean_party(sender_name or sender_addr)
    recipient_party = _clean_party(recipient_name or recipient_addr)
    evidence: list[tuple[str, str]] = []
    if sender_party:
        evidence.append(("issuer", f"From: {sender}"))
    if recipient_party:
        evidence.append(("recipient", f"To: {recipient}"))

    body = ""
    for block in doc.blocks:
        if block.location == "email:body" and block.text:
            body = block.text
            break
    pay_match = re.search(
        r"(?im)^\s*([A-Z][A-Z0-9]{1,8})\s+pay\s+([A-Z][A-Z0-9]{1,12})\s*[:：]",
        body,
    )
    if pay_match:
        payer, payee = pay_match.group(1), pay_match.group(2)
        entity_hint = any(
            name and name.upper() in {payer.upper(), "SHA", "PLT"}
            for name in accounting_entity_names
        )
        if entity_hint or re.search(r"(?i)@pltplt\.com\b", sender):
            entity_name = accounting_entity_names[0] if accounting_entity_names else payer
            return (
                payee,
                entity_name,
                [
                    ("issuer", pay_match.group(0)),
                    ("recipient", f"To: {recipient}" if recipient else pay_match.group(0)),
                ],
            )
    return sender_party, recipient_party, evidence


def extract_parties(
    doc: NormalizedDocument,
    accounting_entity_names: list[str],
) -> tuple[str | None, str | None, list[tuple[str, str]]]:
    text = doc.full_text or ""
    if doc.source.source_type == SourceType.EML:
        return _email_parties(doc, accounting_entity_names)
    explicit_issuer, issuer_raw = _labeled_party(
        text, [r"DOCUMENT\s+ISSUER", r"ISSUER", r"ISSUED\s+BY", r"BILLER", r"SELLER"]
    )
    explicit_recipient, recipient_raw = _labeled_party(
        text, [r"BILL\s+TO", r"SOLD\s+TO", r"INVOICE\s+TO", r"CUSTOMER", r"RECIPIENT", r"MESSRS"]
    )
    letterhead, letterhead_raw = _letterhead(text)
    creditor, creditor_raw = _creditor(text)
    issuer = explicit_issuer or creditor or letterhead
    counterpart = explicit_recipient
    if not counterpart and letterhead and letterhead != issuer:
        counterpart = letterhead
    if counterpart and issuer and normalize_charge_name(counterpart) == normalize_charge_name(issuer):
        counterpart = None
    evidence: list[tuple[str, str]] = []
    if issuer:
        evidence.append(("issuer", creditor_raw or issuer_raw or letterhead_raw or issuer))
    if counterpart:
        evidence.append(("recipient", recipient_raw or letterhead_raw or counterpart))
    return issuer, counterpart, evidence


def extract_transport_ids(doc: NormalizedDocument) -> dict:
    text = doc.full_text or ""
    result: dict = {"master_bl_no": None, "house_bl_no": None, "booking_no": None, "container_nos": []}
    patterns = {
        "master_bl_no": [
            r"(?im)\bMASTER\s+B/?L\s*(?:NO\.?|NUMBER|#)?\s*[:：]\s*([A-Z0-9][A-Z0-9-]{5,})",
            r"(?im)\bM/?BL\s*(?:NO\.?|NUMBER|#)?\s*[:：]\s*([A-Z0-9][A-Z0-9-]{5,})",
        ],
        "house_bl_no": [
            r"(?im)\bHOUSE\s+B/?L\s*(?:NO\.?|NUMBER|#)?\s*[:：]\s*([A-Z0-9][A-Z0-9-]{5,})",
            r"(?im)\bHBL\s*(?:NO\.?|NUMBER|#)?\s*[:：]\s*([A-Z0-9][A-Z0-9-]{5,})",
        ],
        "booking_no": [
            r"(?im)\bBOOKING\s*(?:NO\.?|NUMBER|REF(?:ERENCE)?|#)\s*[:：]\s*([A-Z0-9][A-Z0-9-]{3,})",
            r"(?im)\bBKG\s*(?:NO\.?|#)?\s*[:：]\s*([A-Z0-9][A-Z0-9-]{3,})",
        ],
    }
    evidence: dict[str, str] = {}
    for field, field_patterns in patterns.items():
        for pattern in field_patterns:
            match = re.search(pattern, text)
            if match:
                result[field] = match.group(1).strip(".,;:")
                evidence[field] = match.group(0)
                break
    containers = [
        match.group(1).upper()
        for match in CONTAINER_RE.finditer(text.upper())
        if is_valid_container_number(match.group(1))
    ]
    ocr_containers: list[str] = []
    ocr_mbl = None
    for item in _ocr_items(doc):
        for value in item.get("containers") or []:
            token = re.sub(r"[^A-Z0-9]", "", str(value).upper())
            if token and is_valid_container_number(token):
                ocr_containers.append(token)
        if not ocr_mbl and item.get("mbl_number"):
            ocr_mbl = str(item["mbl_number"])
    if ocr_containers:
        containers.extend(ocr_containers)
    seen: list[str] = []
    for number in containers:
        if number not in seen:
            seen.append(number)
    result["container_nos"] = seen
    if evidence.get("container_nos") is None and seen:
        evidence["container_nos"] = ", ".join(seen)
    if not result["master_bl_no"] and ocr_mbl:
        result["master_bl_no"] = ocr_mbl
        evidence["master_bl_no"] = f"OCR mblNumber={ocr_mbl}"
    return {"values": result, "evidence": evidence}


def extract_total_and_currency(doc: NormalizedDocument) -> dict:
    text = doc.full_text or ""
    total = None
    total_raw = None
    currency = None
    patterns = [
        r"(?im)\bTOTAL\s+CHARGES\s+TO\s+BE\s+PAID\b\s*[:：]?\s*([\d,.]+)\s*([A-Z]{3})?",
        r"(?im)\bTOTAL\s+AMOUNT\b\s*[:：]?\s*([\d,.]+)\s*(?:CURRENCY\s*)?([A-Z]{3})?",
        r"(?im)\bGRAND\s+TOTAL[^\n]{0,80}?\b([A-Z]{3})\b\s*([\d,.]+)",
        r"(?im)\bAMOUNT\s+DUE\b\s*[:：]?\s*([A-Z]{3})?\s*([\d,.]+)",
        r"(?im)\bBALANCE\s+DUE\b\s*[:：]?\s*([A-Z]{3})?\s*([\d,.]+)",
    ]
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text)
        if not match:
            continue
        if index in (2, 3, 4):
            groups = match.groups()
            code = groups[0] if index == 2 else groups[0]
            amount_raw = groups[1]
            total = parse_decimal(amount_raw, correct_suspicious=True)
            currency = code
        else:
            amount_raw = match.group(1)
            total = parse_decimal(amount_raw, correct_suspicious=True)
            currency = match.group(2) if len(match.groups()) >= 2 else None
        if total is not None:
            total_raw = match.group(0)
            break
    currency_match = re.search(r"(?im)\bCURRENCY\s*[:：]?\s*([A-Z]{3})\b", text)
    if currency_match:
        currency = currency_match.group(1)
    if not currency:
        codes = find_currencies(text)
        unique = list(dict.fromkeys(codes))
        if len(unique) == 1:
            currency = unique[0]
    if total is None:
        return {"total": None, "currency": normalize_currency(currency), "evidence": {}}
    return {
        "total": total,
        "currency": normalize_currency(currency, fallback_text=text),
        "evidence": {"document_total": total_raw, "currency": total_raw},
    }


def extract_terms(text: str, normalizer: PaymentTermNormalizer) -> tuple[str | None, PaymentTerm | None, str | None]:
    match = re.search(r"(?im)\b(?:PAYMENT\s+)?TERMS?\b\s*[:：]?\s*([A-Z][A-Z /-]{1,30})", text)
    if not match:
        return None, None, None
    raw = _clean_ws(match.group(1))
    if re.search(r"(?i)\b(AND\s+CONDITIONS?|OF\s+(SALE|SERVICE)|DOCUMENTATION)\b", raw):
        return None, None, None
    normalized = normalizer.normalize(raw)
    if normalized is None and not re.search(
        r"(?i)\b(PP|CC|PREPAID|COLLECT|COD|PAYABLE|FREIGHT)\b", raw
    ):
        return None, None, None
    return raw, normalized, match.group(0)


def parse_pdf_native_charges(doc: NormalizedDocument) -> list[ChargeCandidate]:
    charges: list[ChargeCandidate] = []
    for block in doc.blocks:
        if block.kind != "text" or block.metadata.get("source") != "native_pdf" or not block.text:
            continue
        for line in block.text.splitlines():
            match = PDF_CHARGE_RE.match(line) or PDF_CHARGE_RE_NO_PC.match(line)
            if not match:
                continue
            name = _clean_ws(match.group("name"))
            if name.upper().strip() in SKIP_CHARGE_NAMES:
                continue
            if not re.search(r"[A-Z]", name):
                continue
            amount = parse_decimal(match.group("amount"))
            rate = parse_decimal(match.group("rate"))
            qty = parse_decimal(match.group("qty"))
            term = None
            pc = match.groupdict().get("pc")
            if pc:
                term = PaymentTerm.PP if pc.upper() == "P" else PaymentTerm.CC
            evidence = _evidence(
                doc,
                "charge",
                line.strip(),
                ExtractionMethod.TABLE,
                0.95,
                location=block.location,
            )
            charges.append(
                ChargeCandidate(
                    raw_charge_name=name,
                    quantity=qty,
                    unit_price=rate,
                    amount=amount,
                    payment_term=term,
                    evidence=[evidence],
                    confidence=0.95,
                )
            )
    return charges


def parse_ocr_charges(
    doc: NormalizedDocument, default_currency: str | None = None
) -> list[ChargeCandidate]:
    charges: list[ChargeCandidate] = []
    for block in doc.blocks:
        if block.kind != "table" or block.location != "ocr:charges" or not block.rows:
            continue
        headers = [cell.strip().lower() for cell in block.rows[0]]

        def cell(row: list[str], name: str, fallback: int) -> str:
            index = headers.index(name) if name in headers else fallback
            if 0 <= index < len(row):
                return row[index].strip()
            return ""

        correction_fallback = 5 if "currency" in headers else 4
        for row_index, row in enumerate(block.rows[1:], start=2):
            if not row:
                continue
            name = cell(row, "charge_item", 0)
            amount_raw = cell(row, "amount", 1)
            if not name or not amount_raw:
                continue
            amount = parse_decimal(amount_raw, correct_suspicious=True)
            correction = cell(row, "correction", correction_fallback)
            note = f"OCR 金额修正: {correction}" if correction else None
            evidence = _evidence(
                doc,
                "charge",
                " | ".join(row),
                ExtractionMethod.OCR,
                0.7,
                note=note,
                location=f"{block.location},row:{row_index}",
            )
            charges.append(
                ChargeCandidate(
                    raw_charge_name=name,
                    amount=amount,
                    currency=normalize_currency(
                        cell(row, "currency", -1) or None,
                        document_currency=default_currency,
                    ),
                    evidence=[evidence],
                    confidence=0.7,
                )
            )
    return charges


def _is_skip_row(joined: str) -> bool:
    upper = joined.upper().strip()
    if not upper:
        return True
    if re.search(r"(?i)\b(TOTAL|SUBTOTAL|SUB TOTAL|GRAND TOTAL|BALANCE|PAID|REMARK)\b", upper):
        return True
    return False


def _header_map(headers: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, header in enumerate(headers):
        key = header.strip().lower().replace("_", " ")
        if not key:
            continue
        if any(token in key for token in ("charge", "description", "item", "fee", "particular", "费用", "项目", "名称")):
            mapping.setdefault("name", index)
        if any(token in key for token in ("amount", "debit", "credit", "金额")):
            mapping.setdefault("amount", index)
        if "currency" in key or key in {"cur", "ccy"} or "币种" in key:
            mapping.setdefault("currency", index)
        if any(token in key for token in ("qty", "quantity", "数量")):
            mapping.setdefault("quantity", index)
        if any(token in key for token in ("unit price", "rate", "price", "单价")):
            mapping.setdefault("unit_price", index)
        if any(token in key for token in ("payment", "term", "pp/cc", "付款")):
            mapping.setdefault("payment_term", index)
    return mapping


def parse_table_charges(doc: NormalizedDocument, default_currency: str | None, normalizer: PaymentTermNormalizer) -> list[ChargeCandidate]:
    charges: list[ChargeCandidate] = []
    for block in doc.blocks:
        if block.kind != "table" or not block.rows or block.location == "ocr:charges":
            continue
        rows = block.rows
        header_index = block.metadata.get("header_row")
        if header_index is None:
            from ..parsers.excel import _detect_header

            detected = _detect_header(rows)
            header_index = detected + 1 if detected is not None else None
        headers = rows[header_index - 1] if header_index and header_index <= len(rows) else []
        mapping = _header_map(headers)
        start = header_index if header_index else 0
        for row_index in range(start, len(rows)):
            row = rows[row_index]
            joined = " | ".join(cell for cell in row if cell)
            if _is_skip_row(joined):
                continue
            if not any(cell.strip() for cell in row):
                continue
            name = ""
            amount_raw = ""
            currency_raw = ""
            quantity_raw = ""
            price_raw = ""
            term_raw = ""
            if mapping:
                name = row[mapping["name"]] if "name" in mapping and mapping["name"] < len(row) else ""
                amount_raw = row[mapping["amount"]] if "amount" in mapping and mapping["amount"] < len(row) else ""
                currency_raw = row[mapping["currency"]] if "currency" in mapping and mapping["currency"] < len(row) else ""
                quantity_raw = row[mapping["quantity"]] if "quantity" in mapping and mapping["quantity"] < len(row) else ""
                price_raw = row[mapping["unit_price"]] if "unit_price" in mapping and mapping["unit_price"] < len(row) else ""
                term_raw = row[mapping["payment_term"]] if "payment_term" in mapping and mapping["payment_term"] < len(row) else ""
            name_index = row.index(name) if name and name in row else -1
            if not name:
                text_cells = [
                    (cell_index, cell) for cell_index, cell in enumerate(row)
                    if cell.strip() and re.search(r"[A-Za-z一-鿿]", cell) and len(cell.strip()) >= 3
                ]
                if not text_cells:
                    continue
                name_index, name = text_cells[0]
            numbers = []
            for cell_index, cell in enumerate(row):
                if cell_index == name_index:
                    continue
                value = parse_decimal(cell)
                if value is not None and not re.fullmatch(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", cell.strip()):
                    numbers.append((cell_index, cell, value))
            if not amount_raw and numbers:
                amount_raw = numbers[-1][1]
            if not amount_raw:
                continue
            name = _clean_ws(name)
            if name.upper() in SKIP_CHARGE_NAMES:
                continue
            if re.search(r"(?i)\b(TOTAL|SUBTOTAL|BALANCE|REMARK)\b", name):
                continue
            amount = parse_decimal(amount_raw)
            if amount is None:
                continue
            currency = normalize_currency(currency_raw, document_currency=default_currency)
            term = normalizer.normalize(term_raw) if term_raw else None
            row_location = f"{block.location},row:{row_index + 1}"
            charges.append(
                ChargeCandidate(
                    raw_charge_name=name,
                    amount=amount,
                    currency=currency,
                    quantity=parse_decimal(quantity_raw) if quantity_raw else None,
                    unit_price=parse_decimal(price_raw) if price_raw else None,
                    payment_term=term,
                    evidence=[
                        _evidence(
                            doc,
                            "charge",
                            joined,
                            ExtractionMethod.TABLE,
                            0.85,
                            location=row_location,
                        )
                    ],
                    confidence=0.85,
                )
            )
    return charges


def parse_email_charges(
    doc: NormalizedDocument,
    dictionary: ChargeDictionary,
    normalizer: PaymentTermNormalizer,
) -> list[ChargeCandidate]:
    body = ""
    for block in doc.blocks:
        if block.location == "email:body" and block.text:
            body = block.text
            break
    if not body:
        return []
    aliases: list[str] = []
    for entry in dictionary.entries:
        aliases.append(entry.canonical)
        aliases.extend(entry.aliases)
    aliases = sorted({alias for alias in aliases if len(alias) >= 2}, key=len, reverse=True)
    charges: list[ChargeCandidate] = []
    seen: set[tuple[str, str]] = set()
    for line in body.splitlines():
        if not line.strip():
            continue
        upper = line.upper()
        money_matches = list(EMAIL_MONEY_RE.finditer(line))
        if not money_matches:
            continue
        matched_alias = None
        for alias in aliases:
            if re.search(rf"(?i)(?<![A-Z0-9]){re.escape(alias)}(?![A-Z0-9])", line):
                matched_alias = alias
                break
        if not matched_alias:
            continue
        for money in money_matches:
            symbol = money.group(1) or money.group(3)
            amount_raw = money.group(2)
            start = line.rfind(matched_alias, 0, money.start())
            if start == -1:
                continue
            # Avoid amounts far from the charge token on the same line.
            if money.start() - (start + len(matched_alias)) > 30:
                continue
            if re.fullmatch(r"\d{4}", amount_raw.strip()):
                continue
            amount = parse_decimal(amount_raw)
            if amount is None:
                continue
            key = (matched_alias.upper(), str(amount))
            if key in seen:
                continue
            seen.add(key)
            currency = normalize_currency(symbol)
            term = normalizer.find_in_text(line[start:])
            charges.append(
                ChargeCandidate(
                    raw_charge_name=matched_alias,
                    amount=amount,
                    currency=currency,
                    payment_term=term,
                    evidence=[
                        _evidence(
                            doc,
                            "charge",
                            line.strip(),
                            ExtractionMethod.EML_BODY,
                            0.8,
                            location="email:body",
                        )
                    ],
                    confidence=0.8,
                )
            )
            break
    return charges


def parse_loose_text_charges(doc: NormalizedDocument, normalizer: PaymentTermNormalizer) -> list[ChargeCandidate]:
    if doc.source.source_type not in {SourceType.TXT, SourceType.DOC, SourceType.DOCX}:
        return []
    charges: list[ChargeCandidate] = []
    pattern = re.compile(
        r"(?i)^\s*(?P<name>[A-Za-z一-鿿][A-Za-z0-9一-鿿 /&'\-]{2,}?)"
        r"\s+(?:(?P<cur1>USD|EUR|CNY|GBP|HKD|SGD|AUD|\$|€|£|¥)\s*)?"
        r"(?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(?P<cur2>USD|EUR|CNY|GBP|HKD|SGD|AUD)?\s*$"
    )
    for block in doc.blocks:
        if block.kind != "text" or not block.text:
            continue
        for line in block.text.splitlines():
            if not re.search(r"(?i)(charge|fee|handling|freight|thc|wharfage|detention|storage)", line):
                continue
            match = pattern.match(line)
            if not match:
                continue
            amount = parse_decimal(match.group("amount"))
            if amount is None:
                continue
            name = _clean_ws(match.group("name"))
            if name.upper() in SKIP_CHARGE_NAMES:
                continue
            charges.append(
                ChargeCandidate(
                    raw_charge_name=name,
                    amount=amount,
                    currency=normalize_currency(match.group("cur1") or match.group("cur2")),
                    payment_term=normalizer.find_in_text(line),
                    evidence=[
                        _evidence(
                            doc,
                            "charge",
                            line.strip(),
                            ExtractionMethod.RULE,
                            0.7,
                            location=block.location,
                        )
                    ],
                    confidence=0.7,
                )
            )
    return charges


def extract_rules_candidate(
    doc: NormalizedDocument,
    classification: DocumentClassification,
    dictionary: ChargeDictionary,
    normalizer: PaymentTermNormalizer,
    validation: ValidationSettings,
    accounting_entity_names: list[str],
    date_locale: str = "US",
) -> BillCandidate:
    text = doc.full_text or ""
    candidate = BillCandidate(source_document_ids=[doc.document_id])
    candidate.is_bill_document = classification.kind == DocumentKind.BILL

    document_type, document_type_raw = extract_document_type(doc, classification)
    _add_field(
        candidate,
        doc,
        "document_type",
        document_type,
        document_type_raw or document_type,
        confidence=0.9 if document_type_raw else 0.6,
    )

    reference, reference_raw, reference_method = extract_reference(doc)
    if reference:
        _add_field(
            candidate,
            doc,
            "reference_no",
            reference,
            reference_raw or reference,
            method=reference_method or ExtractionMethod.RULE,
            confidence=0.9 if reference_method == ExtractionMethod.RULE else 0.7,
        )

    document_date, date_raw, date_confidence = extract_date(doc, date_locale)
    if document_date:
        _add_field(
            candidate,
            doc,
            "document_date",
            document_date,
            date_raw or document_date.isoformat(),
            confidence=date_confidence,
        )

    issuer, recipient, party_evidence = extract_parties(doc, accounting_entity_names)
    if issuer:
        _add_field(candidate, doc, "issuer", issuer, dict(party_evidence).get("issuer", issuer), confidence=0.75)
    if recipient:
        _add_field(candidate, doc, "recipient", recipient, dict(party_evidence).get("recipient", recipient), confidence=0.75)

    transport = extract_transport_ids(doc)
    for field in ("master_bl_no", "house_bl_no", "booking_no", "container_nos"):
        value = transport["values"].get(field)
        if value:
            raw = transport["evidence"].get(field)
            method = (
                ExtractionMethod.OCR
                if raw and raw.startswith("OCR ")
                else ExtractionMethod.RULE
            )
            _add_field(
                candidate,
                doc,
                field,
                value,
                raw or (", ".join(value) if isinstance(value, list) else str(value)),
                method=method,
                confidence=0.85,
            )

    total_info = extract_total_and_currency(doc)
    if total_info["total"] is not None:
        _add_field(
            candidate,
            doc,
            "document_total",
            total_info["total"],
            total_info["evidence"].get("document_total") or str(total_info["total"]),
            confidence=0.9,
        )
    if total_info["currency"]:
        _add_field(
            candidate,
            doc,
            "document_currency",
            total_info["currency"],
            total_info["evidence"].get("currency") or total_info["currency"],
            confidence=0.85,
        )

    terms_raw, document_term, terms_line = extract_terms(text, normalizer)
    if terms_raw:
        _add_field(candidate, doc, "terms", terms_raw, terms_line or terms_raw, confidence=0.85)
    candidate.document_currency = candidate.document_currency or total_info["currency"]

    native_charges = parse_pdf_native_charges(doc)
    ocr_charges = parse_ocr_charges(doc, candidate.document_currency)
    if native_charges and ocr_charges:
        native_sum = sum((charge.amount or Decimal("0")) for charge in native_charges)
        ocr_sum = sum((charge.amount or Decimal("0")) for charge in ocr_charges)
        if len(native_charges) != len(ocr_charges) or native_sum != ocr_sum:
            candidate.notes.append(
                "OCR 费用行与 PDF 原生文本不一致，采用原生文本费用行，OCR 结果仅保留作审计"
            )
    charges = native_charges or ocr_charges
    if not charges:
        charges = parse_table_charges(doc, candidate.document_currency, normalizer)
    if not charges and doc.source.source_type == SourceType.EML:
        charges = parse_email_charges(doc, dictionary, normalizer)
    if not charges:
        charges = parse_loose_text_charges(doc, normalizer)
    if not charges and classification.kind == DocumentKind.BUSINESS_EMAIL:
        charges = parse_email_charges(doc, dictionary, normalizer)

    candidate.charges = charges
    if candidate.document_currency:
        for charge in candidate.charges:
            if not charge.currency:
                charge.currency = candidate.document_currency
    candidate.terms = candidate.terms or terms_raw
    candidate.confidence = _candidate_confidence(candidate, classification)
    return candidate


def _candidate_confidence(candidate: BillCandidate, classification: DocumentClassification) -> float:
    score = 0.35
    if candidate.document_type:
        score += 0.1
    if candidate.reference_no:
        score += 0.1
    if candidate.document_date:
        score += 0.05
    if candidate.issuer or candidate.recipient:
        score += 0.1
    if candidate.master_bl_no or candidate.house_bl_no or candidate.booking_no:
        score += 0.05
    if candidate.document_total is not None:
        score += 0.05
    if candidate.charges:
        score += 0.15
        score += min(0.1, 0.02 * len(candidate.charges))
    score *= max(0.5, classification.confidence)
    return round(min(0.99, score), 3)
