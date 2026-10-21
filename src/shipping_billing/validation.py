from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

from .config import MatchingSettings, ValidationSettings
from .matching import normalize_reference
from .models import (
    BillCandidate,
    ChargeCandidate,
    ExistingBill,
    ExtractionMethod,
    MatchResult,
    NormalizedDocument,
    Severity,
    ValidationIssue,
)
from .utils import is_iso_currency


def _issue(
    code: str,
    severity: Severity,
    message: str,
    charge: ChargeCandidate | None = None,
    field: str | None = None,
    blocking: bool = False,
) -> ValidationIssue:
    evidence = None
    if charge and charge.evidence:
        evidence = charge.evidence[0].raw_text
    return ValidationIssue(
        code=code,
        severity=severity,
        message=message,
        field=field,
        evidence=evidence,
        blocking=blocking,
    )


def validate_candidate(
    candidate: BillCandidate,
    doc: NormalizedDocument,
    settings: ValidationSettings,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    is_credit = bool(candidate.document_type and "credit" in candidate.document_type.lower())
    charge_amounts: list[Decimal] = []

    for index, charge in enumerate(candidate.charges, start=1):
        label = f"费用行 {index} ({charge.raw_charge_name or '未命名'})"
        if not charge.raw_charge_name:
            issues.append(
                _issue("MISSING_CHARGE_NAME", Severity.ERROR, f"{label} 缺少费用名称", charge, "charge_item", True)
            )
        if not charge.standardized_charge_name:
            issues.append(
                _issue(
                    "MISSING_STANDARD_CHARGE",
                    Severity.ERROR,
                    f"{label} 无法映射到标准费用名称",
                    charge,
                    "charge_item",
                    True,
                )
            )
        if charge.amount is None:
            issues.append(
                _issue("MISSING_AMOUNT", Severity.ERROR, f"{label} 金额缺失或不是有效数字", charge, "amount", True)
            )
        else:
            charge_amounts.append(charge.amount)
            if charge.amount == 0 and not settings.allow_zero_amount:
                issues.append(
                    _issue("ZERO_AMOUNT", Severity.ERROR, f"{label} 金额为 0", charge, "amount", True)
                )
            if charge.amount < 0 and not is_credit:
                issues.append(
                    _issue(
                        "NEGATIVE_AMOUNT",
                        Severity.ERROR,
                        f"{label} 金额为负数，但单据不是 Credit Note",
                        charge,
                        "amount",
                        True,
                    )
                )
            if (
                charge.quantity is not None
                and charge.unit_price is not None
                and charge.quantity != 0
            ):
                expected = charge.quantity * charge.unit_price
                difference = abs(expected - charge.amount)
                tolerance = max(
                    Decimal(str(settings.unit_price_tolerance)),
                    abs(charge.amount) * Decimal("0.01"),
                )
                if difference > tolerance:
                    issues.append(
                        _issue(
                            "UNIT_PRICE_MISMATCH",
                            Severity.ERROR if difference > abs(charge.amount) * Decimal("0.05") else Severity.WARNING,
                            f"{label} 数量×单价={expected} 与金额 {charge.amount} 不一致",
                            charge,
                            "amount",
                            blocking=True,
                        )
                    )

        if not charge.currency:
            issues.append(
                _issue("MISSING_CURRENCY", Severity.ERROR, f"{label} 缺少币种", charge, "currency", True)
            )
        elif not is_iso_currency(charge.currency):
            issues.append(
                _issue(
                    "INVALID_CURRENCY",
                    Severity.ERROR,
                    f"{label} 币种 {charge.currency} 不是 ISO 4217 三位代码",
                    charge,
                    "currency",
                    True,
                )
            )
        elif candidate.document_currency and charge.currency != candidate.document_currency:
            issues.append(
                _issue(
                    "CURRENCY_CONFLICT",
                    Severity.ERROR,
                    f"{label} 币种 {charge.currency} 与单据币种 {candidate.document_currency} 不一致",
                    charge,
                    "currency",
                    True,
                )
            )

        if not charge.evidence:
            issues.append(
                _issue("NO_EVIDENCE", Severity.ERROR, f"{label} 缺少来源证据", charge, None, True)
            )
        elif any(
            item.method == ExtractionMethod.OCR
            and item.note
            and "OCR" in item.note
            and "->" in item.note
            for item in charge.evidence
        ):
            issues.append(
                _issue(
                    "OCR_CORRECTION",
                    Severity.WARNING,
                    f"{label} 对 OCR 易混淆字符进行了修正，需结合合计复核",
                    charge,
                    "amount",
                    False,
                )
            )
        if charge.confidence < settings.min_charge_confidence:
            issues.append(
                _issue(
                    "LOW_CONFIDENCE",
                    Severity.ERROR,
                    f"{label} 置信度 {charge.confidence:.2f} 低于阈值 {settings.min_charge_confidence:.2f}",
                    charge,
                    None,
                    True,
                )
            )

    if candidate.document_total is not None:
        computed = sum(charge_amounts, Decimal("0"))
        difference = abs(computed - candidate.document_total)
        tolerance = max(
            Decimal(str(settings.total_tolerance)),
            abs(candidate.document_total) * Decimal("0.001"),
        )
        if difference > tolerance:
            issues.append(
                ValidationIssue(
                    code="TOTAL_MISMATCH",
                    severity=Severity.ERROR,
                    message=(
                        f"费用行合计 {computed} 与单据总计 {candidate.document_total} 不一致"
                    ),
                    field="document_total",
                    blocking=True,
                )
            )
        else:
            issues.append(
                ValidationIssue(
                    code="TOTAL_MATCH",
                    severity=Severity.INFO,
                    message=f"费用行合计 {computed} 与单据总计 {candidate.document_total} 一致",
                    field="document_total",
                )
            )
    elif candidate.charges:
        issues.append(
            ValidationIssue(
                code="NO_TOTAL",
                severity=Severity.WARNING,
                message="单据没有可提取的合计金额，费用行未能与合计勾稽",
                field="document_total",
            )
        )

    if not candidate.reference_no:
        issues.append(
            ValidationIssue(
                code="MISSING_REFERENCE",
                severity=Severity.WARNING,
                message="未提取到单据编号，将依赖提单号/订舱号匹配",
                field="reference_no",
            )
        )
    if not candidate.document_date:
        issues.append(
            ValidationIssue(
                code="MISSING_DATE",
                severity=Severity.WARNING,
                message="未提取到单据日期",
                field="document_date",
            )
        )
    elif candidate.document_date > date.today() + timedelta(days=settings.max_future_days):
        issues.append(
            ValidationIssue(
                code="DATE_TOO_FAR_FUTURE",
                severity=Severity.ERROR,
                message=f"单据日期 {candidate.document_date} 超出允许的未来范围",
                field="document_date",
                blocking=True,
            )
        )
    if not candidate.issuer and not candidate.recipient:
        issues.append(
            ValidationIssue(
                code="MISSING_PARTIES",
                severity=Severity.WARNING,
                message="签发方与接收方均未提取，无法判断 AR/AP",
                field="issuer",
            )
        )

    seen: dict[tuple[str, str, str], int] = {}
    for index, charge in enumerate(candidate.charges, start=1):
        key = (
            charge.standardized_charge_name or charge.raw_charge_name or "",
            str(charge.amount),
            charge.currency or "",
        )
        if key in seen:
            issues.append(
                _issue(
                    "DUPLICATE_CHARGE",
                    Severity.WARNING,
                    f"费用行 {index} 与费用行 {seen[key]} 的标准化名称、金额、币种相同，疑似重复提取",
                    charge,
                    None,
                    False,
                )
            )
        else:
            seen[key] = index

    for note in candidate.notes:
        if "OCR" in note and "不一致" in note:
            issues.append(
                ValidationIssue(
                    code="OCR_TEXT_CONFLICT",
                    severity=Severity.WARNING,
                    message=note,
                    field=None,
                )
            )
    return issues


def validate_match_consistency(
    candidate: BillCandidate,
    bill: ExistingBill | None,
    match: MatchResult,
    settings: MatchingSettings,
) -> list[ValidationIssue]:
    if bill is None or match.rule_id != "reference_charge":
        return []
    issues: list[ValidationIssue] = []
    for field, label, code in (
        ("master_bl_no", "主提单号", "MATCH_BL_CONFLICT"),
        ("house_bl_no", "分提单号", "MATCH_HBL_CONFLICT"),
    ):
        incoming = getattr(candidate, field, None)
        existing = getattr(bill, field, None)
        if (
            incoming
            and existing
            and normalize_reference(incoming) != normalize_reference(existing)
        ):
            issues.append(
                ValidationIssue(
                    code=code,
                    severity=Severity.ERROR,
                    message=f"单据编号匹配到已有账单，但{label}不一致：{incoming} vs {existing}",
                    field=field,
                    blocking=True,
                )
            )
    incoming_containers = {value.upper() for value in candidate.container_nos}
    existing_containers = {value.upper() for value in bill.container_nos}
    if (
        incoming_containers
        and existing_containers
        and not (incoming_containers & existing_containers)
    ):
        issues.append(
            ValidationIssue(
                code="MATCH_CONTAINER_CONFLICT",
                severity=Severity.ERROR,
                message=(
                    "单据编号匹配到已有账单，但箱号无交集："
                    f"{', '.join(sorted(incoming_containers))} vs "
                    f"{', '.join(sorted(existing_containers))}"
                ),
                field="container_nos",
                blocking=True,
            )
        )
    if candidate.document_date and bill.document_date:
        difference = abs((candidate.document_date - bill.document_date).days)
        if difference > settings.date_tolerance_days:
            issues.append(
                ValidationIssue(
                    code="MATCH_DATE_CONFLICT",
                    severity=Severity.ERROR,
                    message=(
                        "单据编号匹配到已有账单，但单据日期相差 "
                        f"{difference} 天：{candidate.document_date.isoformat()} vs "
                        f"{bill.document_date.isoformat()}"
                    ),
                    field="document_date",
                    blocking=True,
                )
            )
    return issues


def has_blocking(issues: list[ValidationIssue]) -> bool:
    return any(issue.blocking or issue.severity == Severity.ERROR for issue in issues)


def review_reasons(issues: list[ValidationIssue]) -> list[str]:
    return [issue.message for issue in issues if issue.blocking or issue.severity == Severity.ERROR]
