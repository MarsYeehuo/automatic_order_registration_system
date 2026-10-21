from __future__ import annotations

from shipping_billing.models import OperationType

from tests.conftest import FIXTURES


BILLS = FIXTURES / "bills"
EML = FIXTURES / "eml" / "prealert.eml"


def fixture_batch():
    return [
        BILLS / "arrival_notice.txt",
        BILLS / "simple_invoice.txt",
        BILLS / "update_bill.txt",
        BILLS / "table_bill.xlsx",
        BILLS / "word_bill.docx",
        EML,
    ]


def by_charge(records, item):
    return [record for record in records if record.charge_details.charge_item == item]


def test_fixture_batch_end_to_end(run_batch):
    result = run_batch(fixture_batch())
    records = result.records
    assert len(records) == 9
    operations = {}
    for record in records:
        operations[record.operation.value] = operations.get(record.operation.value, 0) + 1
    assert operations == {"CREATE": 6, "UPDATE": 2, "NO_CHANGE": 1}
    assert all(
        record.financial_attributes.posting_type == "AP" for record in records
    )
    assert all(record.audit_trail.field_evidence for record in records if record.operation != OperationType.PENDING_REVIEW)

    handling_updates = [
        record
        for record in records
        if record.operation == OperationType.UPDATE
        and record.charge_details.charge_item == "Handling Charge"
    ]
    assert len(handling_updates) == 1
    update = handling_updates[0]
    assert update.charge_details.amount == 45.0
    assert update.charge_details.payment_term == "PP"
    assert update.audit_trail.existing_bill_id == "BILL-9001"
    assert update.audit_trail.update_patch["charge"]["amount"] == 45.0

    thc = by_charge(records, "Terminal Handling Charge")[0]
    assert thc.operation == OperationType.UPDATE
    assert thc.charge_details.amount == 65.0
    assert thc.audit_trail.existing_bill_id == "BILL-9002"

    documentation = by_charge(records, "Documentation Fee")[0]
    assert documentation.operation == OperationType.NO_CHANGE

    assert not any(
        record.operation == OperationType.PENDING_REVIEW for record in records
    )
    assert result.summary["records_total"] == 9
    assert result.summary["documents_by_classification"].get("OTHER", 0) == 0


def test_entity_override_turns_ap_into_ar(config, run_batch):
    config.settings.accounting_entity.name = "Ocean Line Ltd"
    config.settings.accounting_entity.display_name = "Ocean Line Ltd"
    config.settings.accounting_entity.aliases = ["Ocean Line Ltd"]
    config.settings.accounting_entity.role_hint = None
    result = run_batch([BILLS / "simple_invoice.txt"])
    assert len(result.records) == 2
    assert all(
        record.financial_attributes.posting_type == "AR" for record in result.records
    )


def test_non_bill_can_be_forced_to_review(config, run_batch):
    config.settings.accounting_entity.name = "Third Party Ltd"
    config.settings.accounting_entity.display_name = "Third Party Ltd"
    config.settings.accounting_entity.aliases = ["Third Party Ltd"]
    config.settings.accounting_entity.role_hint = None
    config.settings.non_bill_operation = "PENDING_REVIEW"
    result = run_batch([EML])
    assert len(result.records) == 1
    record = result.records[0]
    assert record.operation == OperationType.PENDING_REVIEW
    assert record.audit_trail.review_reasons
    assert record.financial_attributes.posting_type is None


def test_batch_duplicate_create_keeps_one_and_reviews_others(config, run_batch, tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    body = (
        "COMMERCIAL INVOICE\n"
        "Invoice No: INV-DUP-1\n"
        "Invoice Date: 10/01/2026\n"
        "Issuer: Ocean Line Ltd\n"
        "Bill To: Origin Agent Ltd\n"
        "TOTAL AMOUNT: 10.00 USD\n"
        "\nHandling Fee 10.00 USD\n"
    )
    first.write_text(body, encoding="utf-8")
    second.write_text(body.replace("COMMERCIAL INVOICE", "INVOICE COPY"), encoding="utf-8")
    result = run_batch([first, second])
    operations = [record.operation for record in result.records]
    assert operations.count(OperationType.CREATE) == 1
    assert operations.count(OperationType.PENDING_REVIEW) == 1
    pending = next(
        record for record in result.records if record.operation == OperationType.PENDING_REVIEW
    )
    assert any("重复费用行" in reason for reason in pending.audit_trail.review_reasons)


def test_same_existing_target_from_two_inputs_goes_to_review(config, run_batch, tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    template = (
        "DEBIT NOTE\n"
        "Invoice No: DN-202609-001\n"
        "Invoice Date: 09/20/2026\n"
        "Issuer: Destination Agent Ltd\n"
        "Bill To: Origin Agent Ltd\n"
        "TOTAL AMOUNT: {total} USD\n"
        "\nHandling Fee {amount} USD\n"
    )
    first.write_text(template.format(total="45.00", amount="45.00"), encoding="utf-8")
    second.write_text(template.format(total="46.00", amount="46.00"), encoding="utf-8")
    result = run_batch([first, second])
    assert len(result.records) == 2
    assert all(
        record.operation == OperationType.PENDING_REVIEW for record in result.records
    )
    assert all(
        any("命中同一已有账单费用行" in reason for reason in record.audit_trail.review_reasons)
        for record in result.records
    )


def test_bill_without_charges_goes_to_review(config, run_batch, tmp_path):
    path = tmp_path / "empty_bill.txt"
    path.write_text(
        "COMMERCIAL INVOICE\n"
        "Invoice No: INV-EMPTY\n"
        "Invoice Date: 10/01/2026\n"
        "Issuer: Ocean Line Ltd\n"
        "Bill To: Origin Agent Ltd\n"
        "TOTAL AMOUNT: 0.00 USD\n",
        encoding="utf-8",
    )
    result = run_batch([path])
    assert len(result.records) == 1
    record = result.records[0]
    assert record.operation == OperationType.PENDING_REVIEW
    assert any("费用" in reason for reason in record.audit_trail.review_reasons)
