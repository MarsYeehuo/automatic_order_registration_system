from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from shipping_billing.decision import decide_operation, decide_posting
from shipping_billing.models import (
    BillCandidate,
    ChargeCandidate,
    ContentBlock,
    ExistingBill,
    ExistingCharge,
    ExtractionMethod,
    FieldEvidence,
    MatchCandidate,
    MatchResult,
    NormalizedDocument,
    OperationType,
    SourceRef,
    SourceType,
)
from shipping_billing.output import (
    render_charge_record,
    render_review_record,
    write_outputs,
)


ENTITY_NAMES = ["Origin Agent Ltd"]


def make_document(redact_probe: bool = False) -> NormalizedDocument:
    text = "Handling Charge 12.00 USD"
    if redact_probe:
        text += " contact ops@example.com"
    return NormalizedDocument(
        document_id="doc_1",
        source=SourceRef(
            source_file="bill.pdf",
            source_type=SourceType.PDF,
            sha256="a" * 64,
            location="page:1",
        ),
        blocks=[ContentBlock(kind="text", location="page:1", text=text)],
        parser_metadata={"parser": "pdf", "page_count": 1},
    )


def make_candidate(amount: str = "12.00") -> tuple[BillCandidate, ChargeCandidate]:
    evidence = [
        FieldEvidence(
            field="amount",
            source_file="bill.pdf",
            location="page:1",
            raw_text="12.00",
            method=ExtractionMethod.RULE,
        )
    ]
    charge = ChargeCandidate(
        raw_charge_name="HANDLING FEE",
        standardized_charge_name="Handling Charge",
        amount=Decimal(amount),
        currency="USD",
        payment_term=None,
        quantity=Decimal("1"),
        unit_price=Decimal(amount),
        evidence=evidence,
        confidence=0.9,
    )
    candidate = BillCandidate(
        document_type="Debit Note",
        reference_no="REF-1",
        document_date=date(2026, 9, 20),
        issuer="Issuer Ltd",
        recipient="Origin Agent Ltd",
        charges=[charge],
        document_total=Decimal(amount),
        document_currency="USD",
        confidence=0.9,
    )
    return candidate, charge


def make_existing() -> tuple[ExistingBill, ExistingCharge]:
    charge = ExistingCharge(
        charge_item="Handling Charge",
        amount=Decimal("8.00"),
        currency="USD",
        payment_term=None,
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


def build_update_record(config, redact_probe: bool = False):
    candidate, charge = make_candidate()
    bill, existing_charge = make_existing()
    match = MatchResult(
        rule_id="reference_charge",
        candidates=[MatchCandidate(bill_id="BILL-1", rule_id="reference_charge", charge_index=0)],
        selected_bill_id="BILL-1",
        selected_charge_index=0,
    )
    decision = decide_operation(
        candidate,
        charge,
        [],
        match,
        decide_posting(candidate, ENTITY_NAMES, 0.82),
        bill,
        existing_charge,
        config,
        non_bill=False,
    )
    assert decision.operation == OperationType.UPDATE
    return render_charge_record(
        run_id="run_test",
        record_id="run_test-R0001",
        config=config,
        documents=[make_document(redact_probe=redact_probe)],
        classification=None,
        candidate=candidate,
        charge=charge,
        decision=decision,
        issues=[],
        existing_bill=bill,
        existing_charge=existing_charge,
        entity=config.settings.accounting_entity,
        non_bill=False,
    )


def test_update_record_uses_final_values_and_audit(config):
    record = build_update_record(config)
    assert record.operation == OperationType.UPDATE
    assert record.charge_details.amount == 12.0
    assert record.charge_details.currency == "USD"
    assert record.financial_attributes.posting_type == "AP"
    assert record.audit_trail.update_patch["charge"]["amount"] == Decimal("12.00")
    assert record.audit_trail.existing_bill_id == "BILL-1"
    assert "charge.payment_term" in record.audit_trail.inherited_fields
    assert record.audit_trail.source_sha256 == "a" * 64
    assert record.audit_trail.document_metadata["page_count"] == 1


def test_audit_redacts_email_and_keeps_evidence(config):
    record = build_update_record(config, redact_probe=True)
    metadata_text = json.dumps(
        [item.raw_text for item in record.audit_trail.field_evidence],
        ensure_ascii=False,
    )
    assert "@" not in metadata_text or "***@" in metadata_text
    assert record.audit_trail.field_evidence


def test_review_record_has_reasons_and_empty_charge(config):
    record = render_review_record(
        run_id="run_test",
        record_id="run_test-R0001",
        config=config,
        doc=make_document(),
        documents=[make_document()],
        classification=None,
        issues=[],
        reasons=["未提取到有效费用行，无法自动建账"],
        candidate=None,
        entity=config.settings.accounting_entity,
    )
    assert record.operation == OperationType.PENDING_REVIEW
    assert record.audit_trail.review_reasons == ["未提取到有效费用行，无法自动建账"]
    assert record.charge_details.charge_item is None
    assert record.financial_attributes.posting_type is None


def test_write_outputs_creates_three_files(config, tmp_path):
    record = build_update_record(config)
    summary = {"run_id": "run_test", "records_total": 1}
    paths = write_outputs([record], summary, tmp_path, config)
    results = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    audit_lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    summary_data = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert results[0]["operation"] == "UPDATE"
    assert len(audit_lines) == 1
    assert summary_data["records_total"] == 1
    assert set(paths) == {"results", "audit", "summary"}
