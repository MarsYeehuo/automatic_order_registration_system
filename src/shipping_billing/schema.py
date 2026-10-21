from __future__ import annotations

from typing import Any

from .models import OperationType, OutputRecord


def build_schema() -> dict[str, Any]:
    record = OutputRecord.model_json_schema()
    definitions = record.pop("$defs", {})
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ShippingBillingResults",
        "type": "array",
        "items": {"$ref": "#/$defs/OutputRecord"},
        "$defs": {**definitions, "OutputRecord": record},
    }


def validate_records(payload: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, list):
        return ["结果文件顶层必须是 JSON 数组"]

    try:
        import jsonschema

        schema = build_schema()
        validator = jsonschema.Draft202012Validator(schema)
        for error in validator.iter_errors(payload):
            location = "/".join(str(part) for part in error.absolute_path)
            errors.append(f"schema[{location}]: {error.message}")
    except ImportError:
        pass

    records: list[OutputRecord] = []
    for index, item in enumerate(payload):
        try:
            record = OutputRecord.model_validate(item)
        except Exception as exc:
            errors.append(f"[{index}] 无法解析为 OutputRecord: {exc}")
            continue
        records.append(record)

    for index, record in enumerate(records):
        label = record.audit_trail.record_id or f"index {index}"
        if record.operation in (
            OperationType.CREATE,
            OperationType.UPDATE,
            OperationType.NO_CHANGE,
        ):
            if not record.charge_details.charge_item:
                errors.append(f"{label}: {record.operation.value} 缺少 charge_item")
            if record.charge_details.amount is None:
                errors.append(f"{label}: {record.operation.value} 缺少 amount")
            if not record.charge_details.currency:
                errors.append(f"{label}: {record.operation.value} 缺少 currency")
        if record.operation == OperationType.UPDATE:
            if not record.audit_trail.update_patch:
                errors.append(f"{label}: UPDATE 缺少 update_patch")
        if record.operation == OperationType.NO_CHANGE:
            if record.audit_trail.update_patch:
                errors.append(f"{label}: NO_CHANGE 不应包含 update_patch")
        if record.operation == OperationType.PENDING_REVIEW:
            if not record.audit_trail.review_reasons:
                errors.append(f"{label}: PENDING_REVIEW 缺少 review_reasons")
        if not record.audit_trail.run_id:
            errors.append(f"{label}: audit_trail.run_id 为空")
        if not record.audit_trail.source_documents:
            errors.append(f"{label}: audit_trail.source_documents 为空")
    return errors
