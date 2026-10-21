from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from shipping_billing.models import (
    BillCandidate,
    ChargeCandidate,
    ContentBlock,
    ExistingBill,
    ExtractionMethod,
    FieldEvidence,
    MatchCandidate,
    MatchResult,
    NormalizedDocument,
    SourceRef,
    SourceType,
)
from shipping_billing.validation import validate_candidate, validate_match_consistency


def make_doc() -> NormalizedDocument:
    return NormalizedDocument(
        document_id="doc_test",
        source=SourceRef(
            source_file="bill.pdf",
            source_type=SourceType.PDF,
            sha256="0" * 64,
            location="page:1",
        ),
        blocks=[ContentBlock(kind="text", location="page:1", text="test")],
    )


def make_charge(**overrides) -> ChargeCandidate:
    values = {
        "raw_charge_name": "HANDLING FEE",
        "standardized_charge_name": "Handling Charge",
        "amount": Decimal("10.00"),
        "currency": "USD",
        "quantity": Decimal("1"),
        "unit_price": Decimal("10.00"),
        "evidence": [
            FieldEvidence(
                field="charge",
                source_file="bill.pdf",
                location="page:1",
                raw_text="HANDLING FEE 10.00 USD",
                method=ExtractionMethod.RULE,
            )
        ],
        "confidence": 0.9,
    }
    values.update(overrides)
    return ChargeCandidate(**values)


def make_candidate(charges: list[ChargeCandidate], **overrides) -> BillCandidate:
    values = {
        "document_type": "Debit Note",
        "reference_no": "DN-1",
        "document_date": date(2026, 9, 20),
        "issuer": "Issuer Ltd",
        "recipient": "Recipient Ltd",
        "charges": charges,
        "document_total": sum((charge.amount or Decimal("0")) for charge in charges),
        "document_currency": "USD",
        "confidence": 0.9,
    }
    values.update(overrides)
    return BillCandidate(**values)


def codes(issues):
    return {issue.code for issue in issues}


def test_missing_currency_blocks(config):
    issues = validate_candidate(
        make_candidate([make_charge(currency=None)]),
        make_doc(),
        config.settings.validation,
    )
    assert "MISSING_CURRENCY" in codes(issues)
    assert any(issue.blocking for issue in issues if issue.code == "MISSING_CURRENCY")


def test_invalid_iso_currency(config):
    issues = validate_candidate(
        make_candidate([make_charge(currency="US")]),
        make_doc(),
        config.settings.validation,
    )
    assert "INVALID_CURRENCY" in codes(issues)


def test_currency_conflict(config):
    issues = validate_candidate(
        make_candidate([make_charge(currency="EUR")]),
        make_doc(),
        config.settings.validation,
    )
    assert "CURRENCY_CONFLICT" in codes(issues)


def test_total_mismatch_blocks(config):
    issues = validate_candidate(
        make_candidate([make_charge()], document_total=Decimal("99.00")),
        make_doc(),
        config.settings.validation,
    )
    assert "TOTAL_MISMATCH" in codes(issues)
    assert any(issue.blocking for issue in issues if issue.code == "TOTAL_MISMATCH")


def test_negative_amount_requires_credit_note(config):
    issues = validate_candidate(
        make_candidate(
            [make_charge(amount=Decimal("-10.00"), unit_price=Decimal("-10.00"))],
            document_total=Decimal("-10.00"),
        ),
        make_doc(),
        config.settings.validation,
    )
    assert "NEGATIVE_AMOUNT" in codes(issues)

    credit_issues = validate_candidate(
        make_candidate(
            [make_charge(amount=Decimal("-10.00"), unit_price=Decimal("-10.00"))],
            document_type="Credit Note",
            document_total=Decimal("-10.00"),
        ),
        make_doc(),
        config.settings.validation,
    )
    assert "NEGATIVE_AMOUNT" not in codes(credit_issues)


def test_unit_price_mismatch_is_blocking(config):
    issues = validate_candidate(
        make_candidate(
            [make_charge(amount=Decimal("20.00"), quantity=Decimal("2"), unit_price=Decimal("5.00"))]
        ),
        make_doc(),
        config.settings.validation,
    )
    assert "UNIT_PRICE_MISMATCH" in codes(issues)
    issue = next(issue for issue in issues if issue.code == "UNIT_PRICE_MISMATCH")
    assert issue.blocking


def test_missing_evidence_blocks(config):
    issues = validate_candidate(
        make_candidate([make_charge(evidence=[])]),
        make_doc(),
        config.settings.validation,
    )
    assert "NO_EVIDENCE" in codes(issues)


def test_ocr_note_is_not_treated_as_ocr_correction(config):
    evidence = FieldEvidence(
        field="standardized_charge_name",
        source_file="bill.pdf",
        location="page:1",
        raw_text="HANDLING FEE",
        method=ExtractionMethod.RULE,
        note="费用别名映射: HANDLING FEE -> Handling Charge (exact)",
    )
    issues = validate_candidate(
        make_candidate(
            [
                make_charge(
                    evidence=[
                        FieldEvidence(
                            field="charge",
                            source_file="bill.pdf",
                            location="page:1",
                            raw_text="HANDLING FEE 10.00 USD",
                            method=ExtractionMethod.RULE,
                        ),
                        evidence,
                    ]
                )
            ]
        ),
        make_doc(),
        config.settings.validation,
    )
    assert "OCR_CORRECTION" not in codes(issues)


def test_ocr_note_is_flagged_when_ocr_correction(config):
    issues = validate_candidate(
        make_candidate(
            [
                make_charge(
                    evidence=[
                        FieldEvidence(
                            field="amount",
                            source_file="bill.pdf",
                            location="ocr:charges",
                            raw_text="1O.00",
                            method=ExtractionMethod.OCR,
                            note="OCR 金额修正: 1O.00 -> 10.00",
                        )
                    ]
                )
            ]
        ),
        make_doc(),
        config.settings.validation,
    )
    assert "OCR_CORRECTION" in codes(issues)


def test_duplicate_charge_warns(config):
    charge = make_charge()
    duplicate = make_charge()
    issues = validate_candidate(
        make_candidate([charge, duplicate], document_total=Decimal("20.00")),
        make_doc(),
        config.settings.validation,
    )
    assert "DUPLICATE_CHARGE" in codes(issues)


def test_missing_standard_name_blocks(config):
    issues = validate_candidate(
        make_candidate([make_charge(standardized_charge_name=None)]),
        make_doc(),
        config.settings.validation,
    )
    assert "MISSING_STANDARD_CHARGE" in codes(issues)


def make_match(rule_id: str = "reference_charge") -> MatchResult:
    return MatchResult(
        rule_id=rule_id,
        candidates=[
            MatchCandidate(bill_id="BILL-1", rule_id=rule_id, charge_index=0)
        ],
        selected_bill_id="BILL-1",
        selected_charge_index=0,
    )


def test_reference_match_conflicts_are_blocking(config):
    bill = ExistingBill(
        bill_id="BILL-1",
        reference_no="ORFR1618731",
        document_date=date(2026, 9, 30),
        master_bl_no="ZIMUNGB1312144",
        container_nos=["JXLU4594463"],
    )
    candidate = make_candidate(
        [make_charge()],
        reference_no="ORFR1618731",
        document_date=date(2025, 10, 3),
        master_bl_no="ZIMUNGB9999999",
        container_nos=["JXLU4504463"],
    )
    issues = validate_match_consistency(
        candidate, bill, make_match(), config.settings.matching
    )
    assert {issue.code for issue in issues} == {
        "MATCH_BL_CONFLICT",
        "MATCH_CONTAINER_CONFLICT",
        "MATCH_DATE_CONFLICT",
    }
    assert all(issue.blocking for issue in issues)


def test_reference_match_with_consistent_identifiers_has_no_issues(config):
    bill = ExistingBill(
        bill_id="BILL-1",
        reference_no="ORFR1618731",
        document_date=date(2026, 9, 30),
        master_bl_no="ZIMUNGB1312144",
        container_nos=["JXLU4504463"],
    )
    candidate = make_candidate(
        [make_charge()],
        reference_no="ORFR1618731",
        document_date=date(2026, 9, 30),
        master_bl_no="ZIMUNGB1312144",
        container_nos=["JXLU4504463"],
    )
    assert (
        validate_match_consistency(
            candidate, bill, make_match(), config.settings.matching
        )
        == []
    )


def test_other_matching_rules_skip_reference_consistency(config):
    bill = ExistingBill(
        bill_id="BILL-1",
        reference_no="ORFR1618731",
        container_nos=["JXLU4594463"],
    )
    candidate = make_candidate(
        [make_charge()],
        container_nos=["JXLU4504463"],
    )
    assert (
        validate_match_consistency(
            candidate, bill, make_match("bl_charge_currency"), config.settings.matching
        )
        == []
    )
