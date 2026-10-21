from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from shipping_billing.decision import (
    build_update_patch,
    decide_operation,
    decide_posting,
    final_values,
)
from shipping_billing.models import (
    BillCandidate,
    ChargeCandidate,
    ExistingBill,
    ExistingCharge,
    ExtractionMethod,
    FieldEvidence,
    MatchCandidate,
    MatchResult,
    OperationType,
    PostingType,
)


ENTITY_NAMES = ["Origin Agent Ltd"]


def make_charge(
    *,
    amount: str = "10.00",
    currency: str = "USD",
    evidence_fields: set[str] | None = None,
) -> ChargeCandidate:
    fields = evidence_fields if evidence_fields is not None else {"charge", "amount", "currency"}
    evidence = [
        FieldEvidence(
            field=field,
            source_file="bill.pdf",
            location="page:1",
            raw_text=f"{field} evidence",
            method=ExtractionMethod.RULE,
        )
        for field in fields
    ]
    return ChargeCandidate(
        raw_charge_name="HANDLING FEE",
        standardized_charge_name="Handling Charge",
        amount=Decimal(amount),
        currency=currency,
        payment_term="PP",
        quantity=Decimal("1"),
        unit_price=Decimal(amount),
        evidence=evidence,
        confidence=0.9,
    )


def make_candidate(
    issuer: str | None = "Issuer Ltd",
    recipient: str | None = "Origin Agent Ltd",
    charge: ChargeCandidate | None = None,
) -> BillCandidate:
    return BillCandidate(
        document_type="Debit Note",
        reference_no="REF-1",
        document_date=date(2026, 9, 20),
        issuer=issuer,
        recipient=recipient,
        charges=[charge or make_charge()],
        document_total=Decimal("10.00"),
        document_currency="USD",
        confidence=0.9,
    )


def make_existing() -> tuple[ExistingBill, ExistingCharge]:
    charge = ExistingCharge(
        charge_item="Handling Charge",
        raw_charge_name="HANDLING CHARGE",
        amount=Decimal("8.00"),
        currency="USD",
        payment_term="PP",
        quantity=Decimal("1"),
        unit_price=Decimal("8.00"),
    )
    bill = ExistingBill(
        bill_id="BILL-1",
        document_type="Debit Note",
        reference_no="REF-1",
        document_date=date(2026, 9, 20),
        issuer="Issuer Ltd",
        recipient="Origin Agent Ltd",
        charges=[charge],
    )
    return bill, charge


@pytest.mark.parametrize(
    ("issuer", "recipient", "expected", "rule_id"),
    [
        ("Origin Agent Ltd", "Other Ltd", PostingType.AR, "entity_is_issuer"),
        ("Other Ltd", "Origin Agent Ltd", PostingType.AP, "entity_is_recipient"),
        ("Origin Agent Ltd", "origin agent ltd", None, "entity_is_both"),
        ("Other Ltd", "Third Ltd", None, "entity_not_party"),
    ],
)
def test_ar_ap_matrix(issuer, recipient, expected, rule_id):
    decision = decide_posting(make_candidate(issuer=issuer, recipient=recipient), ENTITY_NAMES, 0.82)
    assert decision.posting_type == expected
    assert decision.rule_id == rule_id
    assert decision.blocked is (expected is None)


def make_match(existing: bool = True) -> MatchResult:
    if not existing:
        return MatchResult(reasons=["未匹配到已有账单"])
    return MatchResult(
        rule_id="reference_charge",
        candidates=[MatchCandidate(bill_id="BILL-1", rule_id="reference_charge", charge_index=0)],
        selected_bill_id="BILL-1",
        selected_charge_index=0,
    )


def test_update_patch_whitelist_requires_evidence(config):
    bill, existing_charge = make_existing()
    candidate = make_candidate(charge=make_charge(amount="12.00", evidence_fields={"amount"}))
    patch = build_update_patch(candidate, candidate.charges[0], bill, existing_charge, config)
    assert patch["charge"] == {"amount": Decimal("12.00")}
    assert patch["bill"] == {}


def test_amount_without_evidence_cannot_update(config):
    bill, existing_charge = make_existing()
    candidate = make_candidate(charge=make_charge(amount="12.00", evidence_fields=set()))
    patch = build_update_patch(candidate, candidate.charges[0], bill, existing_charge, config)
    assert patch["charge"] == {}
    assert patch["bill"] == {}


def test_update_patch_updates_unit_price_with_evidence(config):
    bill, existing_charge = make_existing()
    candidate = make_candidate(
        charge=make_charge(
            amount="12.00",
            evidence_fields={"amount", "unit_price", "quantity"},
        )
    )
    patch = build_update_patch(candidate, candidate.charges[0], bill, existing_charge, config)
    assert patch["charge"] == {
        "unit_price": Decimal("12.00"),
        "amount": Decimal("12.00"),
    }


def test_update_patch_can_change_standard_charge_name(config):
    bill, existing_charge = make_existing()
    charge = make_charge(amount="8.00", evidence_fields={"standardized_charge_name"})
    charge.standardized_charge_name = "Terminal Handling Charge"
    candidate = make_candidate(charge=charge)
    patch = build_update_patch(candidate, charge, bill, existing_charge, config)
    assert patch["charge"] == {"charge_item": "Terminal Handling Charge"}
    values = final_values(
        candidate, charge, bill, existing_charge, patch, OperationType.UPDATE
    )
    assert values["charge_item"] == "Terminal Handling Charge"


def test_decision_update_inherits_other_fields(config):
    bill, existing_charge = make_existing()
    charge = make_charge(amount="12.00", evidence_fields={"amount"})
    candidate = make_candidate(charge=charge)
    decision = decide_operation(
        candidate,
        charge,
        [],
        make_match(),
        decide_posting(candidate, ENTITY_NAMES, 0.82),
        bill,
        existing_charge,
        config,
        non_bill=False,
    )
    assert decision.operation == OperationType.UPDATE
    assert decision.patch["charge"]["amount"] == Decimal("12.00")

    values = final_values(candidate, charge, bill, existing_charge, decision.patch, decision.operation)
    assert values["amount"] == Decimal("12.00")
    assert values["currency"] == "USD"
    assert values["payment_term"] == "PP"
    assert values["reference_no"] == "REF-1"
    assert values["charge_item"] == "Handling Charge"


def test_decision_no_change_when_amount_matches(config):
    bill, existing_charge = make_existing()
    charge = make_charge(amount="8.00")
    candidate = make_candidate(charge=charge)
    decision = decide_operation(
        candidate,
        charge,
        [],
        make_match(),
        decide_posting(candidate, ENTITY_NAMES, 0.82),
        bill,
        existing_charge,
        config,
        non_bill=False,
    )
    assert decision.operation == OperationType.NO_CHANGE


def test_blocked_posting_sends_bill_to_review(config):
    charge = make_charge()
    candidate = make_candidate(issuer="Other Ltd", recipient="Third Ltd", charge=charge)
    posting = decide_posting(candidate, ENTITY_NAMES, 0.82)
    decision = decide_operation(
        candidate,
        charge,
        [],
        make_match(existing=False),
        posting,
        None,
        None,
        config,
        non_bill=False,
    )
    assert decision.operation == OperationType.PENDING_REVIEW
    assert decision.posting is not None and decision.posting.blocked


def test_non_bill_with_blocked_posting_creates_by_config(config):
    charge = make_charge()
    candidate = make_candidate(issuer="Other Ltd", recipient="Third Ltd", charge=charge)
    posting = decide_posting(candidate, ENTITY_NAMES, 0.82)
    config.settings.non_bill_operation = "CREATE"
    decision = decide_operation(
        candidate,
        charge,
        [],
        make_match(existing=False),
        posting,
        None,
        None,
        config,
        non_bill=True,
    )
    assert decision.operation == OperationType.CREATE
    assert decision.posting is not None and decision.posting.posting_type is None


def test_non_bill_review_when_config_says_review(config):
    charge = make_charge()
    candidate = make_candidate(issuer="Other Ltd", recipient="Third Ltd", charge=charge)
    posting = decide_posting(candidate, ENTITY_NAMES, 0.82)
    config.settings.non_bill_operation = "PENDING_REVIEW"
    decision = decide_operation(
        candidate,
        charge,
        [],
        make_match(existing=False),
        posting,
        None,
        None,
        config,
        non_bill=True,
    )
    assert decision.operation == OperationType.PENDING_REVIEW


def test_ambiguous_match_sends_to_review(config):
    charge = make_charge()
    candidate = make_candidate(charge=charge)
    match = MatchResult(
        candidates=[
            MatchCandidate(bill_id="BILL-1", rule_id="reference_charge", charge_index=0),
            MatchCandidate(bill_id="BILL-2", rule_id="reference_charge", charge_index=0),
        ],
        ambiguous=True,
        reasons=["命中 2 条"],
    )
    decision = decide_operation(
        candidate,
        charge,
        [],
        match,
        decide_posting(candidate, ENTITY_NAMES, 0.82),
        None,
        None,
        config,
        non_bill=False,
    )
    assert decision.operation == OperationType.PENDING_REVIEW
