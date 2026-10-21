from __future__ import annotations

import copy
import json

import pytest

from shipping_billing.schema import build_schema, validate_records


def base_record(operation: str = "CREATE") -> dict:
    return {
        "operation": operation,
        "document_info": {"reference_no": "REF-1"},
        "transaction_party": {"issuer": "Issuer Ltd", "recipient": "Origin Agent Ltd"},
        "charge_details": {
            "charge_item": "Handling Charge",
            "amount": 10.0,
            "currency": "USD",
        },
        "financial_attributes": {"posting_type": "AP"},
        "audit_trail": {
            "run_id": "run_1",
            "record_id": "run_1-R0001",
            "source_documents": [{"file": "bill.pdf", "location": "page:1"}],
        },
    }


def test_schema_is_array_of_output_record():
    schema = build_schema()
    assert schema["type"] == "array"
    assert schema["items"] == {"$ref": "#/$defs/OutputRecord"}
    assert "OutputRecord" in schema["$defs"]
    json.dumps(schema)


def test_valid_create_passes():
    assert validate_records([base_record()]) == []


def test_top_level_must_be_array():
    assert validate_records({"operation": "CREATE"}) == ["结果文件顶层必须是 JSON 数组"]


@pytest.mark.parametrize("operation", ["CREATE", "UPDATE", "NO_CHANGE"])
def test_charge_fields_required_for_posting_operations(operation):
    record = base_record(operation)
    record["charge_details"]["amount"] = None
    if operation == "UPDATE":
        record["audit_trail"]["update_patch"] = {"charge": {"amount": 1}}
    if operation == "NO_CHANGE":
        record["audit_trail"].pop("update_patch", None)
    errors = validate_records([record])
    assert any("缺少 amount" in error for error in errors)


def test_update_requires_patch():
    record = base_record("UPDATE")
    errors = validate_records([record])
    assert any("UPDATE 缺少 update_patch" in error for error in errors)


def test_no_change_must_not_have_patch():
    record = base_record("NO_CHANGE")
    record["audit_trail"]["update_patch"] = {"charge": {"amount": 10}}
    errors = validate_records([record])
    assert any("NO_CHANGE 不应包含 update_patch" in error for error in errors)


def test_pending_review_requires_reasons_and_charge_can_be_empty():
    record = base_record("PENDING_REVIEW")
    record["charge_details"] = {}
    errors = validate_records([record])
    assert any("PENDING_REVIEW 缺少 review_reasons" in error for error in errors)
    record["audit_trail"]["review_reasons"] = ["需要人工复核"]
    assert validate_records([record]) == []


def test_audit_identity_required():
    record = base_record()
    record["audit_trail"]["run_id"] = ""
    record["audit_trail"]["source_documents"] = []
    errors = validate_records([copy.deepcopy(record)])
    assert any("run_id 为空" in error for error in errors)
    assert any("source_documents 为空" in error for error in errors)


def test_invalid_operation_is_reported():
    record = base_record()
    record["operation"] = "DELETE"
    errors = validate_records([record])
    assert errors
