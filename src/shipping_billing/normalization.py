from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from .config import ChargeDictionary, PaymentTermDictionary
from .models import (
    BillCandidate,
    ChargeCandidate,
    ExtractionMethod,
    FieldEvidence,
    PaymentTerm,
)
from .utils import (
    CURRENCY_SYMBOLS,
    fuzzy_ratio,
    is_iso_currency,
    normalize_charge_name,
    parse_decimal,
)


@dataclass
class ChargeMatch:
    canonical: str | None
    method: str | None
    score: float


class ChargeNormalizer:
    def __init__(self, dictionary: ChargeDictionary, fuzzy_threshold: float = 0.86) -> None:
        self.dictionary = dictionary
        self.fuzzy_threshold = fuzzy_threshold
        self._exact: dict[str, str] = {}
        for entry in dictionary.entries:
            self._exact[normalize_charge_name(entry.canonical)] = entry.canonical
            for alias in entry.aliases:
                self._exact[normalize_charge_name(alias)] = entry.canonical
        self._candidates: list[tuple[str, str]] = [
            (normalize_charge_name(canonical), canonical) for canonical in dictionary.canonical_names()
        ] + [(normalize_charge_name(alias), canonical) for alias, canonical in dictionary.alias_pairs()]

    def match(self, raw: str | None, llm_candidate: str | None = None) -> ChargeMatch:
        key = normalize_charge_name(raw)
        if not key:
            return ChargeMatch(None, None, 0.0)
        if key in self._exact:
            return ChargeMatch(self._exact[key], "exact", 1.0)
        best_canonical = None
        best_score = 0.0
        for candidate_key, canonical in self._candidates:
            score = fuzzy_ratio(key, candidate_key)
            if score > best_score:
                best_score = score
                best_canonical = canonical
        if best_canonical and best_score >= self.fuzzy_threshold:
            return ChargeMatch(best_canonical, "fuzzy", best_score)
        if llm_candidate:
            llm_key = normalize_charge_name(llm_candidate)
            if llm_key in self._exact:
                return ChargeMatch(self._exact[llm_key], "llm_candidate", 0.8)
        return ChargeMatch(None, None, best_score)


def _normalize_term_token(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^A-Z0-9]+", " ", value.upper()).strip()


class PaymentTermNormalizer:
    def __init__(self, dictionary: PaymentTermDictionary) -> None:
        self.dictionary = dictionary
        self._exact: dict[str, str] = {}
        for code, aliases in dictionary.terms.items():
            for alias in aliases:
                self._exact[_normalize_term_token(alias)] = code
            self._exact[_normalize_term_token(code)] = code
        self._column = {k.upper(): v for k, v in dictionary.column_tokens.items()}
        self._phrases = sorted(
            ((_normalize_term_token(alias), code) for code, aliases in dictionary.terms.items()
             for alias in aliases if len(_normalize_term_token(alias)) >= 4),
            key=lambda item: len(item[0]),
            reverse=True,
        )

    def normalize(self, value: str | None, allow_column_token: bool = True) -> PaymentTerm | None:
        token = _normalize_term_token(value)
        if not token:
            return None
        if token in self._exact:
            return PaymentTerm(self._exact[token])
        if allow_column_token and token in self._column:
            return PaymentTerm(self._column[token])
        padded = f" {token} "
        for phrase, code in self._phrases:
            if f" {phrase} " in padded:
                return PaymentTerm(code)
        return None

    def from_column(self, value: str | None) -> PaymentTerm | None:
        token = _normalize_term_token(value)
        if token in self._column:
            return PaymentTerm(self._column[token])
        return None

    def find_in_text(self, text: str) -> PaymentTerm | None:
        normalized = _normalize_term_token(text)
        padded = f" {normalized} "
        for phrase, code in self._phrases:
            if f" {phrase} " in padded:
                return PaymentTerm(code)
        return None


def normalize_currency(
    raw: str | None,
    document_currency: str | None = None,
    fallback_text: str | None = None,
) -> str | None:
    if raw:
        upper = raw.upper().strip()
        code_match = re.search(r"\b([A-Z]{3})\b", upper)
        if code_match and is_iso_currency(code_match.group(1)):
            return code_match.group(1)
        if upper in CURRENCY_SYMBOLS:
            return CURRENCY_SYMBOLS[upper]
        if upper in ("US$", "USD$"):
            return "USD"
    if document_currency and is_iso_currency(document_currency):
        return document_currency.upper()
    if fallback_text:
        symbols = [symbol for symbol in CURRENCY_SYMBOLS if symbol in fallback_text]
        if symbols:
            return CURRENCY_SYMBOLS[symbols[0]]
        codes = re.findall(r"\b(USD|EUR|CNY|GBP|HKD|SGD|AUD|JPY)\b", fallback_text.upper())
        if codes:
            return codes[0]
    return None


@dataclass
class NormalizationContext:
    normalizer: ChargeNormalizer
    payment_terms: PaymentTermNormalizer
    document_text: str
    document_currency: str | None
    document_payment_term: PaymentTerm | None


def _literal_amount(text: str | None, value: Decimal | None) -> str | None:
    if not text or value is None:
        return None
    variants = {
        format(value, "f"),
        f"{value:,.2f}",
        f"{value:,.4f}",
        str(value),
    }
    for variant in variants:
        if re.search(rf"(?<![\d.,]){re.escape(variant)}(?![\d.,])", text):
            return variant
    return None


def _add_charge_evidence(
    charge: ChargeCandidate,
    field: str,
    raw_text: str | None,
    note: str,
    confidence: float = 0.9,
    method: ExtractionMethod = ExtractionMethod.RULE,
) -> None:
    if any(item.field == field for item in charge.evidence):
        return
    first = charge.evidence[0] if charge.evidence else None
    charge.evidence.append(
        FieldEvidence(
            field=field,
            source_file=first.source_file if first else "",
            location=first.location if first else "",
            raw_text=raw_text,
            method=method,
            confidence=confidence,
            note=note,
        )
    )


def normalize_bill_candidate(candidate: BillCandidate, ctx: NormalizationContext) -> BillCandidate:
    if candidate.document_currency:
        candidate.document_currency = normalize_currency(
            candidate.document_currency, fallback_text=ctx.document_text
        )
    for charge in candidate.charges:
        raw_name = charge.raw_charge_name
        raw_currency = charge.currency
        raw_term = charge.payment_term
        match = ctx.normalizer.match(raw_name)
        charge.standardized_charge_name = match.canonical
        if match.method and match.canonical:
            charge.evidence.append(
                FieldEvidence(
                    field="standardized_charge_name",
                    source_file=charge.evidence[0].source_file if charge.evidence else "",
                    location=charge.evidence[0].location if charge.evidence else "",
                    raw_text=raw_name,
                    method=ExtractionMethod.LLM if match.method == "llm_candidate" else ExtractionMethod.RULE,
                    confidence=match.score,
                    note=f"费用别名映射: {raw_name} -> {match.canonical} ({match.method})",
                )
            )
        elif raw_name:
            charge.issues.append(f"费用名称未匹配词典: {raw_name}")

        row_text = " ".join(
            evidence.raw_text or "" for evidence in charge.evidence if evidence.raw_text
        )
        charge.currency = normalize_currency(
            charge.currency,
            document_currency=candidate.document_currency or ctx.document_currency,
            fallback_text=row_text or None,
        )
        if charge.payment_term is None:
            charge.payment_term = ctx.document_payment_term
        if charge.amount is None and charge.quantity is not None and charge.unit_price is not None:
            charge.amount = charge.quantity * charge.unit_price
            _add_charge_evidence(
                charge, "amount", None, "由数量 × 单价计算", confidence=0.8
            )
        if (
            charge.unit_price is None
            and charge.quantity
            and charge.quantity != 0
            and charge.amount is not None
        ):
            charge.unit_price = (charge.amount / charge.quantity).quantize(Decimal("0.0001"))

        if charge.amount is not None and charge.currency:
            literal = _literal_amount(row_text, charge.amount)
            if literal:
                _add_charge_evidence(
                    charge, "amount", literal, "金额在原始文本中出现", confidence=0.95
                )
        if charge.quantity is not None:
            literal = _literal_amount(row_text, charge.quantity)
            if literal:
                _add_charge_evidence(
                    charge, "quantity", literal, "数量在原始文本中出现", confidence=0.9
                )
        if charge.unit_price is not None:
            literal = _literal_amount(row_text, charge.unit_price)
            if literal:
                _add_charge_evidence(
                    charge, "unit_price", literal, "单价在原始文本中出现", confidence=0.9
                )
            elif charge.amount is not None and charge.quantity:
                _add_charge_evidence(
                    charge, "unit_price", None, "由金额 ÷ 数量计算", confidence=0.75
                )
        if charge.currency:
            if raw_currency:
                _add_charge_evidence(
                    charge,
                    "currency",
                    raw_currency,
                    "币种来自费用行或单据级字段",
                    confidence=0.95,
                )
            elif charge.currency.upper() in row_text.upper():
                _add_charge_evidence(
                    charge, "currency", charge.currency, "币种在原始文本中出现", confidence=0.9
                )
            else:
                source_currency = candidate.document_currency or ctx.document_currency
                if source_currency:
                    _add_charge_evidence(
                        charge,
                        "currency",
                        source_currency,
                        "币种来自单据级字段",
                        confidence=0.85,
                    )
        if charge.payment_term is not None:
            if raw_term is not None:
                _add_charge_evidence(
                    charge,
                    "payment_term",
                    raw_term.value,
                    "付款方式来自费用行",
                    confidence=0.9,
                )
            else:
                _add_charge_evidence(
                    charge,
                    "payment_term",
                    candidate.terms,
                    "付款方式来自单据级条款",
                    confidence=0.8,
                )
    return candidate


def parse_term_from_text(normalizer: PaymentTermNormalizer, text: str | None) -> PaymentTerm | None:
    if not text:
        return None
    return normalizer.find_in_text(text)


def parse_amount_and_quantity(raw_amount: str, raw_qty: str | None, raw_price: str | None):
    amount = parse_decimal(raw_amount, correct_suspicious=True)
    quantity = parse_decimal(raw_qty) if raw_qty else None
    price = parse_decimal(raw_price) if raw_price else None
    return amount, quantity, price
