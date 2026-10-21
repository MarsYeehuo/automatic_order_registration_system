from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shipping_billing.config import load_app_config  # noqa: E402
from shipping_billing.pipeline import ShippingBillingPipeline  # noqa: E402
from shipping_billing.schema import validate_records  # noqa: E402

AUTO_OPERATIONS = {"CREATE", "UPDATE", "NO_CHANGE"}


def load_config():
    app = load_app_config(ROOT / "config" / "settings.yaml")
    entity = app.settings.accounting_entity
    entity.name = "Origin Agent Ltd"
    entity.display_name = "Origin Agent Ltd"
    entity.aliases = ["Origin Agent Ltd"]
    entity.role_hint = None
    return app


def record_key(record) -> tuple:
    amount = record.charge_details.amount
    return (
        record.charge_details.charge_item,
        round(amount, 2) if amount is not None else None,
        record.charge_details.currency,
        record.charge_details.payment_term,
    )


def expected_key(item: dict[str, Any]) -> tuple:
    amount = item.get("amount")
    return (
        item.get("charge_item"),
        round(amount, 2) if amount is not None else None,
        item.get("currency"),
        item.get("payment_term"),
    )


def ratio(correct: int, total: int) -> float:
    return round(correct / total, 4) if total else 1.0


def run_case(config, case: dict[str, Any]) -> dict[str, Any]:
    pipeline = ShippingBillingPipeline(
        config,
        use_llm=False,
        use_ocr=False,
        existing_bills_path=ROOT / "tests" / "fixtures" / "existing" / "bills.json",
    )
    try:
        result = pipeline.run([str(ROOT / path) for path in case["inputs"]])
    finally:
        pipeline.close()

    records = result.records
    payload = [record.model_dump(mode="json") for record in records]
    structure_errors = validate_records(payload)
    expected = case["expected"]

    expected_fields = Counter(expected_key(item) for item in expected)
    actual_fields = Counter(record_key(record) for record in records)
    expected_operations = Counter(item["operation"] for item in expected)
    actual_operations = Counter(record.operation.value for record in records)
    expected_postings = Counter(item.get("posting_type") for item in expected)
    actual_postings = Counter(
        record.financial_attributes.posting_type for record in records
    )

    auto_records = [r for r in records if r.operation.value in AUTO_OPERATIONS]
    with_evidence = sum(1 for r in auto_records if r.audit_trail.field_evidence)
    unsafe_auto = sum(
        1
        for r in auto_records
        if r.financial_attributes.posting_type is None
        or not r.audit_trail.field_evidence
    )

    field_correct = sum((expected_fields & actual_fields).values())
    operation_correct = sum((expected_operations & actual_operations).values())
    posting_correct = sum((expected_postings & actual_postings).values())
    field_total = sum(expected_fields.values())
    operation_total = sum(expected_operations.values())
    posting_total = sum(expected_postings.values())

    failures: list[str] = []
    if structure_errors:
        failures.extend(structure_errors)
    if field_correct != field_total:
        missing = expected_fields - actual_fields
        extra = actual_fields - expected_fields
        if missing:
            failures.append(f"缺少期望费用行: {dict(missing)}")
        if extra:
            failures.append(f"出现非期望费用行: {dict(extra)}")
    if operation_correct != operation_total:
        failures.append(
            f"操作期望 {dict(expected_operations)}，实际 {dict(actual_operations)}"
        )
    if posting_correct != posting_total:
        failures.append(
            f"AR/AP 期望 {dict(expected_postings)}，实际 {dict(actual_postings)}"
        )
    if unsafe_auto:
        failures.append(f"存在 {unsafe_auto} 条缺乏证据/AR-AP 的自动入账记录")

    return {
        "id": case["id"],
        "description": case.get("description"),
        "records": len(records),
        "expected_records": len(expected),
        "structure_valid": not structure_errors,
        "structure_errors": structure_errors,
        "field_correct": field_correct,
        "field_total": field_total,
        "operation_correct": operation_correct,
        "operation_total": operation_total,
        "posting_correct": posting_correct,
        "posting_total": posting_total,
        "evidence_completeness": ratio(with_evidence, len(auto_records)),
        "unsafe_auto_postings": unsafe_auto,
        "passed": not failures,
        "failures": failures,
    }


def summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        key: sum(case[key] for case in cases)
        for key in (
            "records",
            "field_correct",
            "field_total",
            "operation_correct",
            "operation_total",
            "posting_correct",
            "posting_total",
            "unsafe_auto_postings",
        )
    }
    auto_evidence = [
        case["evidence_completeness"] for case in cases if case["evidence_completeness"] is not None
    ]
    return {
        "cases_total": len(cases),
        "cases_passed": sum(1 for case in cases if case["passed"]),
        "records": totals["records"],
        "structure_valid_rate": ratio(
            sum(1 for case in cases if case["structure_valid"]), len(cases)
        ),
        "field_accuracy": ratio(totals["field_correct"], totals["field_total"]),
        "operation_accuracy": ratio(
            totals["operation_correct"], totals["operation_total"]
        ),
        "posting_accuracy": ratio(totals["posting_correct"], totals["posting_total"]),
        "evidence_completeness": round(sum(auto_evidence) / len(auto_evidence), 4)
        if auto_evidence
        else 1.0,
        "unsafe_auto_posting_rate": ratio(
            totals["unsafe_auto_postings"], totals["records"]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic golden set.")
    parser.add_argument(
        "--golden",
        default=str(ROOT / "evals" / "golden_set.json"),
        help="Path to golden_set.json",
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "evals" / "report.json"),
        help="Where to write the JSON report",
    )
    args = parser.parse_args()

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    config = load_config()
    cases = [run_case(config, case) for case in golden["cases"]]
    summary = summarize(cases)
    report = {
        "golden_version": golden.get("version"),
        "summary": summary,
        "cases": cases,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(
        f"cases {summary['cases_passed']}/{summary['cases_total']} | "
        f"structure {summary['structure_valid_rate']:.2%} | "
        f"field {summary['field_accuracy']:.2%} | "
        f"operation {summary['operation_accuracy']:.2%} | "
        f"posting {summary['posting_accuracy']:.2%} | "
        f"evidence {summary['evidence_completeness']:.2%} | "
        f"unsafe auto {summary['unsafe_auto_posting_rate']:.2%}"
    )
    for case in cases:
        status = "PASS" if case["passed"] else "FAIL"
        print(f"[{status}] {case['id']} ({case['records']} records)")
        for failure in case["failures"]:
            print(f"  - {failure}")
    print(f"report: {output}")
    return 0 if summary["cases_passed"] == summary["cases_total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
