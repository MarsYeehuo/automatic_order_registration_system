from __future__ import annotations

from ..config import AppConfig
from ..llm import LLMClient
from ..models import BillCandidate, DocumentClassification, DocumentKind, NormalizedDocument
from ..normalization import (
    ChargeNormalizer,
    NormalizationContext,
    PaymentTermNormalizer,
    normalize_bill_candidate,
)
from ..utils import find_currencies
from .llm_extract import extract_with_llm
from .rules import extract_rules_candidate


class Extractor:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.normalizer = ChargeNormalizer(
            config.charge_dictionary,
            config.settings.matching.charge_name_fuzzy_threshold,
        )
        self.payment_terms = PaymentTermNormalizer(config.payment_terms)

    def extract(
        self,
        doc: NormalizedDocument,
        classification: DocumentClassification,
        llm: LLMClient | None = None,
    ) -> BillCandidate:
        candidate = extract_rules_candidate(
            doc,
            classification,
            self.config.charge_dictionary,
            self.payment_terms,
            self.config.settings.validation,
            self.config.settings.accounting_entity.all_names(),
            date_locale=self.config.settings.parsing.date_locale,
        )
        doc.classification = classification

        should_call_llm = bool(
            llm
            and llm.available
            and doc.full_text.strip()
            and (
                candidate.charges
                or doc.parse_error
                or classification.kind
                in {
                    DocumentKind.BILL,
                    DocumentKind.BUSINESS_EMAIL,
                    DocumentKind.ARRIVAL_NOTICE,
                }
            )
        )
        if should_call_llm:
            candidate = extract_with_llm(
                doc,
                candidate,
                llm,
                self.normalizer,
                self.payment_terms,
                max_input_chars=self.config.settings.llm.max_input_chars,
            )

        if not candidate.document_currency:
            currencies = list(dict.fromkeys(find_currencies(doc.full_text or "")))
            if len(currencies) == 1:
                candidate.document_currency = currencies[0]

        document_term = (
            self.payment_terms.normalize(candidate.terms) if candidate.terms else None
        )
        context = NormalizationContext(
            normalizer=self.normalizer,
            payment_terms=self.payment_terms,
            document_text=doc.full_text or "",
            document_currency=candidate.document_currency,
            document_payment_term=document_term,
        )
        candidate = normalize_bill_candidate(candidate, context)
        candidate.charges = self._dedupe_charges(candidate)
        return candidate

    @staticmethod
    def _dedupe_charges(candidate: BillCandidate) -> list:
        seen: set[tuple] = set()
        unique = []
        for charge in candidate.charges:
            key = (
                charge.raw_charge_name or "",
                str(charge.amount) if charge.amount is not None else "",
                charge.currency or "",
                charge.evidence[0].raw_text if charge.evidence else "",
            )
            if key in seen:
                candidate.notes.append(
                    f"同文档重复费用行已去重: {charge.raw_charge_name} {charge.amount}"
                )
                continue
            seen.add(key)
            unique.append(charge)
        return unique
