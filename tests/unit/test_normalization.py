from __future__ import annotations

from decimal import Decimal

import pytest

from shipping_billing.config import (
    load_charge_dictionary,
    load_payment_terms,
)
from shipping_billing.models import (
    BillCandidate,
    ChargeCandidate,
    ExtractionMethod,
    FieldEvidence,
    PaymentTerm,
)
from shipping_billing.normalization import (
    ChargeNormalizer,
    NormalizationContext,
    PaymentTermNormalizer,
    normalize_bill_candidate,
)
from shipping_billing.utils import parse_decimal
from tests.conftest import ROOT


@pytest.fixture
def charge_normalizer():
    dictionary = load_charge_dictionary(ROOT / "config" / "charge_aliases.yaml")
    return ChargeNormalizer(dictionary, fuzzy_threshold=0.86)


@pytest.fixture
def term_normalizer():
    dictionary = load_payment_terms(ROOT / "config" / "payment_terms.yaml")
    return PaymentTermNormalizer(dictionary)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Handling Fee", "Handling Charge"),
        ("H/D Fee", "Handling Charge"),
        ("Handling Charge", "Handling Charge"),
        ("Doc Fee", "Documentation Fee"),
        ("THC", "Terminal Handling Charge"),
        ("Trucking Fee", "Trucking Charge"),
        ("Customs Fee", "Customs Clearance Fee"),
        ("Storage Fee", "Storage Charge"),
        ("wharfage administration fee", "Wharfage Administration Fee"),
        ("H/C", "Handling Charge"),
    ],
)
def test_required_charge_aliases(charge_normalizer, raw, expected):
    match = charge_normalizer.match(raw)
    assert match.canonical == expected
    assert match.method in {"exact", "fuzzy"}


def test_unknown_charge_has_no_standard_name(charge_normalizer):
    match = charge_normalizer.match("Mystery Surcharge XYZ")
    assert match.canonical is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PP", PaymentTerm.PP),
        ("Prepaid", PaymentTerm.PP),
        ("FREIGHT PREPAID", PaymentTerm.PP),
        ("CC", PaymentTerm.CC),
        ("Collect", PaymentTerm.CC),
        ("COD", PaymentTerm.CC),
        ("P", PaymentTerm.PP),
        ("C", PaymentTerm.CC),
    ],
)
def test_payment_terms(term_normalizer, raw, expected):
    assert term_normalizer.normalize(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,310.40", Decimal("1310.40")),
        ("1O.5O", Decimal("10.50")),
        ("USD 40.00", Decimal("40.00")),
        ("(25.00)", Decimal("-25.00")),
        ("not-a-number", None),
    ],
)
def test_parse_decimal(raw, expected):
    assert parse_decimal(raw, correct_suspicious=True) == expected


def test_normalization_adds_field_evidence_for_updates(charge_normalizer, term_normalizer):
    charge = ChargeCandidate(
        raw_charge_name="4 ADD'L DETENTION FREE TIME CHARGE",
        amount=Decimal("1310.40"),
        currency="USD",
        quantity=Decimal("1.00"),
        unit_price=Decimal("1310.40"),
        payment_term=PaymentTerm.CC,
        evidence=[
            FieldEvidence(
                field="charge",
                source_file="bill.pdf",
                location="page:1",
                raw_text=(
                    "4 ADD'L DETENTION FREE TIME CHARGE - 1,310.40 1.00 C 1,310.40"
                ),
                method=ExtractionMethod.TABLE,
            )
        ],
        confidence=0.95,
    )
    candidate = BillCandidate(
        document_currency="USD",
        terms="COD",
        charges=[charge],
    )
    context = NormalizationContext(
        normalizer=charge_normalizer,
        payment_terms=term_normalizer,
        document_text="TERMS: COD\nCURRENCY: USD\n",
        document_currency="USD",
        document_payment_term=PaymentTerm.CC,
    )
    normalized = normalize_bill_candidate(candidate, context)

    fields = {item.field for item in normalized.charges[0].evidence}
    assert {
        "standardized_charge_name",
        "amount",
        "quantity",
        "unit_price",
        "currency",
        "payment_term",
    } <= fields
