from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from shipping_billing.matching import BillMatcher, ExistingBillRepository
from shipping_billing.models import (
    BillCandidate,
    ChargeCandidate,
    ExistingBill,
    ExistingCharge,
    ExtractionMethod,
    FieldEvidence,
)
from shipping_billing.normalization import ChargeNormalizer, PaymentTermNormalizer
from shipping_billing.config import load_charge_dictionary, load_payment_terms
from tests.conftest import EXISTING_BILLS, ROOT


@pytest.fixture
def normalizer():
    return ChargeNormalizer(
        load_charge_dictionary(ROOT / "config" / "charge_aliases.yaml"), 0.86
    )


@pytest.fixture
def repo(normalizer):
    return ExistingBillRepository.from_json(EXISTING_BILLS, normalizer)


@pytest.fixture
def matcher(config, repo):
    return BillMatcher(repo, config.matching_rules, config.settings.matching)


def make_candidate(**overrides) -> BillCandidate:
    values = {
        "document_type": "Debit Note",
        "reference_no": None,
        "document_date": date(2026, 9, 20),
        "issuer": "Destination Agent Ltd",
        "recipient": "Origin Agent Ltd",
        "master_bl_no": None,
        "house_bl_no": None,
        "booking_no": None,
        "container_nos": [],
        "confidence": 0.9,
    }
    values.update(overrides)
    return BillCandidate(**values)


def make_charge(name: str, amount: str, currency: str = "USD") -> ChargeCandidate:
    return ChargeCandidate(
        raw_charge_name=name,
        standardized_charge_name=name,
        amount=Decimal(amount),
        currency=currency,
        evidence=[
            FieldEvidence(
                field="charge",
                source_file="input.pdf",
                location="page:1",
                raw_text=f"{name} {amount} {currency}",
                method=ExtractionMethod.RULE,
            )
        ],
    )


def test_match_by_reference_and_charge(matcher):
    result = matcher.match_charge(
        make_candidate(reference_no="DN-202609-001"),
        make_charge("Handling Charge", "40.00"),
    )
    assert result.selected_bill_id == "BILL-9001"
    assert result.selected_charge_index == 0
    assert result.rule_id == "reference_charge"
    assert not result.ambiguous


def test_match_by_master_bl_and_currency(matcher):
    result = matcher.match_charge(
        make_candidate(master_bl_no="TEST123456789"),
        make_charge("Handling Charge", "40.00"),
    )
    assert result.selected_bill_id == "BILL-9001"
    assert result.rule_id == "bl_charge_currency"


def test_match_by_container_parties_date_amount(matcher):
    result = matcher.match_charge(
        make_candidate(container_nos=["TEST1234567"], document_date=date(2026, 9, 21)),
        make_charge("Terminal Handling Charge", "60.00"),
    )
    assert result.selected_bill_id == "BILL-9002"
    assert result.selected_charge_index == 0
    assert result.rule_id == "container_parties_date_amount"


def test_no_match_returns_reason(matcher):
    result = matcher.match_charge(
        make_candidate(reference_no="UNKNOWN-1"),
        make_charge("Handling Charge", "999.00"),
    )
    assert result.selected_bill_id is None
    assert any("未匹配" in reason for reason in result.reasons)


def test_ambiguity_and_amount_disambiguation(config, normalizer):
    bills = [
        ExistingBill(
            bill_id="BILL-A",
            reference_no="DUP-1",
            charges=[
                ExistingCharge(
                    charge_item="Handling Charge", amount=Decimal("40.00"), currency="USD"
                )
            ],
        ),
        ExistingBill(
            bill_id="BILL-B",
            reference_no="DUP-1",
            charges=[
                ExistingCharge(
                    charge_item="Handling Charge", amount=Decimal("50.00"), currency="USD"
                )
            ],
        ),
    ]
    repo = ExistingBillRepository(bills, normalizer)
    matcher = BillMatcher(repo, config.matching_rules, config.settings.matching)

    unique = matcher.match_charge(
        make_candidate(reference_no="DUP-1"),
        make_charge("Handling Charge", "50.00"),
    )
    assert not unique.ambiguous
    assert unique.selected_bill_id == "BILL-B"

    ambiguous = matcher.match_charge(
        make_candidate(reference_no="DUP-1"),
        make_charge("Handling Charge", "45.00"),
    )
    assert ambiguous.ambiguous
    assert {candidate.bill_id for candidate in ambiguous.candidates} == {"BILL-A", "BILL-B"}
