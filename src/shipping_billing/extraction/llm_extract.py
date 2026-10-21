from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from typing import Any

from ..llm import LLMClient
from ..models import (
    BillCandidate,
    ChargeCandidate,
    ExtractionMethod,
    FieldEvidence,
    NormalizedDocument,
)
from ..normalization import ChargeNormalizer, normalize_currency, PaymentTermNormalizer
from ..utils import is_valid_container_number, parse_date, parse_decimal

EXTRACT_SYSTEM = """You are a shipping billing extraction engine.
Extract only facts explicitly present in the provided input. Return exactly one JSON object.
Schema:
{
  "document_type": string|null,
  "reference_no": string|null,
  "document_date": "YYYY-MM-DD"|null,
  "issuer": string|null,
  "recipient": string|null,
  "master_bl_no": string|null,
  "house_bl_no": string|null,
  "booking_no": string|null,
  "container_nos": [string],
  "document_total": number|null,
  "document_currency": string|null,
  "charges": [
    {
      "raw_charge_name": string,
      "amount": number|null,
      "currency": string|null,
      "quantity": number|null,
      "unit_price": number|null,
      "payment_term": "PP"|"CC"|null,
      "evidence": "exact snippet from input"
    }
  ],
  "field_evidence": {"field_name": "exact snippet from input"},
  "notes": [string]
}
Rules:
- Never invent, guess, translate, or normalize values. If a field is absent, use null or [].
- Copy evidence verbatim from the input. A field or charge without verifiable evidence must be omitted/null.
- Do not merge multiple charge lines into one. One row = one charge.
- Amounts must be JSON numbers, not strings. Dates must be YYYY-MM-DD.
- Do not decide CREATE/UPDATE/AR/AP.
- OCR JSON is evidence only for fields explicitly present in it.
- If text and OCR disagree, prefer the PDF/table text and mention the conflict in notes."""


def _normalize_for_search(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _snippet_present(snippet: Any, source: str) -> bool:
    if not isinstance(snippet, str) or len(snippet.strip()) < 3:
        return False
    return _normalize_for_search(snippet) in _normalize_for_search(source)


def _value_in_source(value: str, source: str) -> bool:
    normalized_value = _normalize_for_search(value)
    normalized_source = _normalize_for_search(source)
    if not normalized_value:
        return False
    if normalized_value in normalized_source:
        return True
    compact_value = re.sub(r"[^a-z0-9]+", "", normalized_value)
    compact_source = re.sub(r"[^a-z0-9]+", "", normalized_source)
    return bool(compact_value) and compact_value in compact_source


def _amount_variants(value: Decimal) -> list[str]:
    return [
        format(value, "f"),
        f"{value:,.2f}",
        f"{value:,.4f}",
        f"{value:,.0f}",
    ]


def _evidence_for(payload: dict[str, Any], field: str) -> str | None:
    evidence = payload.get("field_evidence")
    if isinstance(evidence, dict):
        value = evidence.get(field)
        if isinstance(value, str):
            return value
    value = payload.get(f"{field}_evidence")
    return value if isinstance(value, str) else None


def _accept_string_field(
    candidate: BillCandidate,
    doc: NormalizedDocument,
    payload: dict[str, Any],
    field: str,
    source_text: str,
) -> None:
    if getattr(candidate, field):
        return
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        return
    value = value.strip()
    evidence = _evidence_for(payload, field)
    if not _snippet_present(evidence, source_text):
        candidate.notes.append(f"LLM 字段 {field} 缺少可核验原文证据，已拒绝: {value}")
        return
    if not (_value_in_source(value, evidence or "") or _value_in_source(value, source_text)):
        candidate.notes.append(f"LLM 字段 {field} 的值未在证据中出现，已拒绝: {value}")
        return
    setattr(candidate, field, value)
    candidate.evidence.append(
        FieldEvidence(
            field=field,
            source_file=doc.source.source_file,
            location=_locate(doc, evidence or value),
            raw_text=evidence,
            method=ExtractionMethod.LLM,
            confidence=0.65,
            note="LLM 补全，已通过原文证据核验",
        )
    )


def _locate(doc: NormalizedDocument, snippet: str | None) -> str:
    from .rules import find_location

    return find_location(doc, snippet)


def _accept_date(
    candidate: BillCandidate,
    doc: NormalizedDocument,
    payload: dict[str, Any],
    source_text: str,
) -> None:
    if candidate.document_date:
        return
    raw = payload.get("document_date")
    parsed = parse_date(raw)
    evidence = _evidence_for(payload, "document_date")
    if not parsed or not _snippet_present(evidence, source_text):
        if raw:
            candidate.notes.append(f"LLM 日期缺少可核验证据，已拒绝: {raw}")
        return
    candidate.document_date = parsed
    candidate.evidence.append(
        FieldEvidence(
            field="document_date",
            source_file=doc.source.source_file,
            location=_locate(doc, evidence),
            raw_text=evidence,
            method=ExtractionMethod.LLM,
            confidence=0.65,
            note="LLM 补全日期，已通过原文证据核验",
        )
    )


def _accept_total(candidate: BillCandidate, doc: NormalizedDocument, payload: dict[str, Any], source_text: str) -> None:
    if candidate.document_total is not None:
        return
    total = parse_decimal(payload.get("document_total"))
    evidence = _evidence_for(payload, "document_total")
    if total is None or not _snippet_present(evidence, source_text):
        if payload.get("document_total") is not None:
            candidate.notes.append("LLM 单据总额缺少可核验证据，已拒绝")
        return
    if not any(variant in (evidence or "") for variant in _amount_variants(total)):
        candidate.notes.append("LLM 单据总额与证据片段不一致，已拒绝")
        return
    candidate.document_total = total
    candidate.evidence.append(
        FieldEvidence(
            field="document_total",
            source_file=doc.source.source_file,
            location=_locate(doc, evidence),
            raw_text=evidence,
            method=ExtractionMethod.LLM,
            confidence=0.65,
            note="LLM 补全总额，已通过原文证据核验",
        )
    )


def _accept_currency(
    candidate: BillCandidate,
    doc: NormalizedDocument,
    payload: dict[str, Any],
    source_text: str,
) -> None:
    if candidate.document_currency:
        return
    raw = payload.get("document_currency")
    if not isinstance(raw, str) or not raw.strip():
        return
    evidence = _evidence_for(payload, "document_currency")
    if not _snippet_present(evidence, source_text):
        candidate.notes.append("LLM 币种缺少可核验证据，已拒绝")
        return
    currency = normalize_currency(raw, fallback_text=evidence)
    if not currency or currency.upper() not in (evidence or "").upper():
        candidate.notes.append(f"LLM 币种未在证据中出现，已拒绝: {raw}")
        return
    candidate.document_currency = currency
    candidate.evidence.append(
        FieldEvidence(
            field="document_currency",
            source_file=doc.source.source_file,
            location=_locate(doc, evidence),
            raw_text=evidence,
            method=ExtractionMethod.LLM,
            confidence=0.65,
            note="LLM 补全币种，已通过原文证据核验",
        )
    )


def _accept_containers(
    candidate: BillCandidate,
    doc: NormalizedDocument,
    payload: dict[str, Any],
    source_text: str,
) -> None:
    values = payload.get("container_nos")
    if not isinstance(values, list):
        return
    evidence = _evidence_for(payload, "container_nos")
    if values and not _snippet_present(evidence, source_text):
        candidate.notes.append("LLM 箱号缺少可核验证据，已拒绝")
        return
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = re.sub(r"[^A-Z0-9]", "", value.upper())
        if not is_valid_container_number(normalized):
            continue
        if normalized in candidate.container_nos:
            continue
        if normalized not in (evidence or "").upper() and normalized not in source_text.upper():
            continue
        candidate.container_nos.append(normalized)
        candidate.evidence.append(
            FieldEvidence(
                field="container_nos",
                source_file=doc.source.source_file,
                location=_locate(doc, evidence or normalized),
                raw_text=evidence or normalized,
                method=ExtractionMethod.LLM,
                confidence=0.65,
                note="LLM 补全箱号，已通过原文证据核验",
            )
        )


def _llm_charge_acceptable(charge: dict[str, Any], source_text: str) -> tuple[bool, str | None]:
    evidence = charge.get("evidence")
    if not _snippet_present(evidence, source_text):
        return False, "费用行缺少可核验证据"
    name = charge.get("raw_charge_name")
    if not isinstance(name, str) or not name.strip():
        return False, "费用行缺少名称"
    if not _value_in_source(name, evidence or "") and not _value_in_source(name, source_text):
        return False, "费用名称未在证据中出现"
    amount = parse_decimal(charge.get("amount"))
    if amount is None:
        return False, "费用金额无效"
    if not any(variant in (evidence or "") for variant in _amount_variants(amount)):
        if not any(variant in source_text for variant in _amount_variants(amount)):
            return False, "费用金额未在证据中出现"
    return True, None


def _accept_llm_charges(
    candidate: BillCandidate,
    doc: NormalizedDocument,
    payload: dict[str, Any],
    source_text: str,
    normalizer: ChargeNormalizer,
    payment_terms: PaymentTermNormalizer,
) -> None:
    raw_charges = payload.get("charges")
    if not isinstance(raw_charges, list) or not raw_charges:
        return
    if candidate.charges:
        # Fill only empty per-charge fields when a verified LLM charge row matches.
        for raw_charge in raw_charges:
            if not isinstance(raw_charge, dict):
                continue
            ok, reason = _llm_charge_acceptable(raw_charge, source_text)
            if not ok:
                continue
            llm_amount = parse_decimal(raw_charge.get("amount"))
            llm_name = str(raw_charge.get("raw_charge_name") or "")
            for charge in candidate.charges:
                same_name = (
                    charge.raw_charge_name
                    and normalize_charge_name_simple(charge.raw_charge_name)
                    == normalize_charge_name_simple(llm_name)
                )
                if not same_name or charge.amount != llm_amount:
                    continue
                if not charge.currency:
                    currency = normalize_currency(
                        raw_charge.get("currency"), fallback_text=str(raw_charge.get("evidence") or "")
                    )
                    if currency and str(currency) in str(raw_charge.get("evidence") or "").upper():
                        charge.currency = currency
                if not charge.payment_term:
                    term_raw = raw_charge.get("payment_term")
                    charge.payment_term = payment_terms.normalize(str(term_raw)) if term_raw else None
        return

    for raw_charge in raw_charges:
        if not isinstance(raw_charge, dict):
            continue
        ok, reason = _llm_charge_acceptable(raw_charge, source_text)
        if not ok:
            candidate.notes.append(f"LLM 费用行被拒绝: {reason}")
            continue
        evidence = str(raw_charge.get("evidence"))
        raw_name = str(raw_charge.get("raw_charge_name")).strip()
        match = normalizer.match(raw_name)
        charge = ChargeCandidate(
            raw_charge_name=raw_name,
            standardized_charge_name=match.canonical,
            amount=parse_decimal(raw_charge.get("amount")),
            currency=normalize_currency(raw_charge.get("currency")),
            quantity=parse_decimal(raw_charge.get("quantity")),
            unit_price=parse_decimal(raw_charge.get("unit_price")),
            payment_term=payment_terms.normalize(str(raw_charge.get("payment_term")))
            if raw_charge.get("payment_term")
            else None,
            evidence=[
                FieldEvidence(
                    field="charge",
                    source_file=doc.source.source_file,
                    location=_locate(doc, evidence),
                    raw_text=evidence,
                    method=ExtractionMethod.LLM,
                    confidence=0.65,
                    note="LLM 抽取费用行，已通过原文证据核验",
                )
            ],
            confidence=0.65,
        )
        candidate.charges.append(charge)


def normalize_charge_name_simple(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", value.upper()).strip()


def merge_llm_payload(
    candidate: BillCandidate,
    doc: NormalizedDocument,
    payload: dict[str, Any] | list[Any] | None,
    normalizer: ChargeNormalizer,
    payment_terms: PaymentTermNormalizer,
) -> BillCandidate:
    if not isinstance(payload, dict):
        return candidate
    source_text = doc.full_text or ""
    for field in (
        "document_type",
        "reference_no",
        "issuer",
        "recipient",
        "master_bl_no",
        "house_bl_no",
        "booking_no",
    ):
        _accept_string_field(candidate, doc, payload, field, source_text)
    if (
        candidate.issuer
        and candidate.recipient
        and normalize_charge_name_simple(candidate.issuer)
        == normalize_charge_name_simple(candidate.recipient)
    ):
        candidate.recipient = None
        candidate.evidence = [
            item for item in candidate.evidence if item.field != "recipient"
        ]
        candidate.notes.append("签发方与接收方相同，已忽略接收方")
    _accept_date(candidate, doc, payload, source_text)
    _accept_total(candidate, doc, payload, source_text)
    _accept_currency(candidate, doc, payload, source_text)
    _accept_containers(candidate, doc, payload, source_text)
    _accept_llm_charges(candidate, doc, payload, source_text, normalizer, payment_terms)
    if candidate.document_currency:
        for charge in candidate.charges:
            if not charge.currency:
                charge.currency = candidate.document_currency
    notes = payload.get("notes")
    if isinstance(notes, list):
        for note in notes[:5]:
            if isinstance(note, str) and note.strip():
                candidate.notes.append(f"LLM note: {note.strip()}")
    return candidate


def extract_with_llm(
    doc: NormalizedDocument,
    candidate: BillCandidate,
    llm: LLMClient,
    normalizer: ChargeNormalizer,
    payment_terms: PaymentTermNormalizer,
    max_input_chars: int = 14000,
) -> BillCandidate:
    if not llm.available:
        return candidate
    text = (doc.full_text or "")[:max_input_chars]
    if not text.strip():
        return candidate
    ocr_payload = doc.parser_metadata.get("ocr")
    user = (
        f"SOURCE_FILE: {doc.source.source_file}\n"
        f"DOCUMENT_KIND: {doc.classification.kind.value if doc.classification else 'UNCERTAIN'}\n"
        f"OCR_JSON: {ocr_payload if ocr_payload else 'none'}\n\n"
        "SOURCE_TEXT:\n" + text
    )
    response = llm.complete_json(EXTRACT_SYSTEM, user, cache_tag="extract")
    candidate.llm_meta = {
        "model": response.model,
        "cached": response.cached,
        "latency_seconds": round(response.latency_seconds, 3)
        if response.latency_seconds is not None
        else None,
        "error": response.error,
    }
    if response.error:
        candidate.notes.append(f"LLM 抽取不可用: {response.error}")
        return candidate
    return merge_llm_payload(candidate, doc, response.data, normalizer, payment_terms)
