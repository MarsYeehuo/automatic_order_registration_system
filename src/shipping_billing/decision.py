from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .config import AppConfig
from .matching import normalize_reference
from .models import (
    BillCandidate,
    ChargeCandidate,
    ExistingBill,
    ExistingCharge,
    MatchResult,
    OperationType,
    PostingDecision,
    PostingType,
    ValidationIssue,
)
from .utils import fuzzy_ratio, normalize_charge_name, normalize_party_name


def entity_matches(party: str | None, entity_names: list[str], threshold: float) -> tuple[bool, float]:
    if not party:
        return False, 0.0
    best = 0.0
    for name in entity_names:
        score = fuzzy_ratio(name, party)
        if score > best:
            best = score
        if score >= threshold:
            return True, score
    return False, best


def decide_posting(
    candidate: BillCandidate,
    entity_names: list[str],
    threshold: float,
) -> PostingDecision:
    issuer_match, issuer_score = entity_matches(candidate.issuer, entity_names, threshold)
    recipient_match, recipient_score = entity_matches(candidate.recipient, entity_names, threshold)
    if issuer_match and recipient_match:
        return PostingDecision(
            posting_type=None,
            rule_id="entity_is_both",
            reason="记账主体同时匹配签发方和接收方，无法判断应收应付",
            blocked=True,
        )
    if issuer_match:
        return PostingDecision(
            posting_type=PostingType.AR,
            rule_id="entity_is_issuer",
            reason=f"记账主体为签发方（匹配度 {issuer_score:.2f}），形成应收 AR",
        )
    if recipient_match:
        return PostingDecision(
            posting_type=PostingType.AP,
            rule_id="entity_is_recipient",
            reason=f"记账主体为接收方（匹配度 {recipient_score:.2f}），形成应付 AP",
        )
    return PostingDecision(
        posting_type=None,
        rule_id="entity_not_party",
        reason="记账主体未出现在签发方或接收方中，无法判断 AR/AP",
        blocked=True,
    )


@dataclass
class OperationDecision:
    operation: OperationType
    reasons: list[str] = field(default_factory=list)
    patch: dict = field(default_factory=dict)
    match: MatchResult = field(default_factory=MatchResult)
    posting: PostingDecision | None = None


def _field_evidence_present(candidate: BillCandidate, field: str) -> bool:
    return any(item.field == field for item in candidate.evidence)


def _charge_evidence_present(charge: ChargeCandidate, field: str) -> bool:
    return any(item.field == field for item in charge.evidence)


def _within(left: Decimal | None, right: Decimal | None, tolerance: float) -> bool:
    if left is None or right is None:
        return left is right
    return abs(left - right) <= Decimal(str(tolerance))


def build_update_patch(
    candidate: BillCandidate,
    charge: ChargeCandidate,
    existing_bill: ExistingBill,
    existing_charge: ExistingCharge,
    config: AppConfig,
) -> dict:
    patch: dict = {"charge": {}, "bill": {}}
    settings = config.settings
    matching = config.matching_rules
    amount_tolerance = max(
        matching.no_change_amount_tolerance,
        settings.matching.amount_tolerance,
    )
    if (
        charge.standardized_charge_name
        and _charge_evidence_present(charge, "standardized_charge_name")
        and normalize_charge_name(charge.standardized_charge_name)
        != normalize_charge_name(existing_charge.charge_item)
    ):
        patch["charge"]["charge_item"] = charge.standardized_charge_name
    if (
        charge.amount is not None
        and _charge_evidence_present(charge, "amount")
        and not _within(charge.amount, existing_charge.amount, amount_tolerance)
    ):
        patch["charge"]["amount"] = charge.amount
    if (
        charge.currency
        and _charge_evidence_present(charge, "currency")
        and (existing_charge.currency or "").upper() != charge.currency.upper()
    ):
        patch["charge"]["currency"] = charge.currency
    if (
        charge.payment_term is not None
        and _charge_evidence_present(charge, "payment_term")
        and charge.payment_term != existing_charge.payment_term
    ):
        patch["charge"]["payment_term"] = charge.payment_term
    if (
        charge.quantity is not None
        and _charge_evidence_present(charge, "quantity")
        and charge.quantity != existing_charge.quantity
    ):
        patch["charge"]["quantity"] = charge.quantity
    if (
        charge.unit_price is not None
        and _charge_evidence_present(charge, "unit_price")
        and not _within(charge.unit_price, existing_charge.unit_price, amount_tolerance)
    ):
        patch["charge"]["unit_price"] = charge.unit_price

    if (
        candidate.reference_no
        and _field_evidence_present(candidate, "reference_no")
        and normalize_reference(candidate.reference_no) != normalize_reference(existing_bill.reference_no)
    ):
        patch["bill"]["reference_no"] = candidate.reference_no
    if (
        candidate.document_date
        and _field_evidence_present(candidate, "document_date")
        and candidate.document_date != existing_bill.document_date
    ):
        patch["bill"]["document_date"] = candidate.document_date
    for field in ("master_bl_no", "house_bl_no", "booking_no", "issuer", "recipient", "document_type"):
        incoming = getattr(candidate, field)
        existing = getattr(existing_bill, field)
        if not incoming or not _field_evidence_present(candidate, field):
            continue
        if field in {"issuer", "recipient"}:
            if fuzzy_ratio(incoming, existing) >= settings.matching.party_fuzzy_threshold:
                continue
        elif normalize_charge_name(str(incoming)) == normalize_charge_name(str(existing)):
            continue
        patch["bill"][field] = incoming
    if candidate.container_nos and _field_evidence_present(candidate, "container_nos"):
        if {x.upper() for x in candidate.container_nos} != {x.upper() for x in existing_bill.container_nos}:
            patch["bill"]["container_nos"] = list(candidate.container_nos)
    patch["charge"] = {k: v for k, v in patch["charge"].items() if v is not None}
    patch["bill"] = {k: v for k, v in patch["bill"].items() if v is not None}
    return patch


def decide_operation(
    candidate: BillCandidate,
    charge: ChargeCandidate,
    issues: list[ValidationIssue],
    match: MatchResult,
    posting: PostingDecision,
    existing_bill: ExistingBill | None,
    existing_charge: ExistingCharge | None,
    config: AppConfig,
    non_bill: bool,
) -> OperationDecision:
    reasons: list[str] = []
    blocking = [issue for issue in issues if issue.blocking or issue.severity.value == "ERROR"]
    if blocking:
        reasons.append("存在阻断性校验问题，转人工审核")
        reasons.extend(issue.message for issue in blocking[:5])
        return OperationDecision(
            operation=OperationType.PENDING_REVIEW,
            reasons=reasons,
            match=match,
            posting=posting,
        )
    if match.ambiguous:
        reasons.append("匹配到多个已有账单费用行，转人工审核")
        reasons.extend(match.reasons)
        return OperationDecision(
            operation=OperationType.PENDING_REVIEW,
            reasons=reasons,
            match=match,
            posting=posting,
        )
    if match.conflict:
        reasons.append("多个输入费用行命中同一已有账单费用行，转人工审核")
        reasons.extend(match.reasons)
        return OperationDecision(
            operation=OperationType.PENDING_REVIEW,
            reasons=reasons,
            match=match,
            posting=posting,
        )
    if posting.blocked:
        if non_bill and config.settings.non_bill_operation.upper() == "CREATE":
            reasons.append(posting.reason)
            reasons.append("非账单文档包含有效费用，按配置直接 CREATE（AR/AP 待人工确认）")
            return OperationDecision(
                operation=OperationType.CREATE,
                reasons=reasons,
                match=match,
                posting=posting,
            )
        reasons.append(posting.reason)
        return OperationDecision(
            operation=OperationType.PENDING_REVIEW,
            reasons=reasons,
            match=match,
            posting=posting,
        )

    if existing_bill and existing_charge:
        patch = build_update_patch(candidate, charge, existing_bill, existing_charge, config)
        if patch["charge"] or patch["bill"]:
            reasons.append(f"匹配已有账单 {existing_bill.bill_id}，存在明确差异，生成字段白名单补丁")
            reasons.extend(f"更新 {key}={value}" for key, value in {**patch['charge'], **patch['bill']}.items())
            return OperationDecision(
                operation=OperationType.UPDATE,
                reasons=reasons,
                patch=patch,
                match=match,
                posting=posting,
            )
        reasons.append(f"匹配已有账单 {existing_bill.bill_id}，金额与关键字段无实质变化")
        return OperationDecision(
            operation=OperationType.NO_CHANGE,
            reasons=reasons,
            match=match,
            posting=posting,
        )

    if not non_bill and candidate.confidence < config.settings.validation.min_auto_operation_confidence:
        reasons.append(
            f"账单候选整体置信度 {candidate.confidence:.2f} 低于自动建账阈值 "
            f"{config.settings.validation.min_auto_operation_confidence:.2f}"
        )
        return OperationDecision(
            operation=OperationType.PENDING_REVIEW,
            reasons=reasons,
            match=match,
            posting=posting,
        )
    reasons.append("未匹配到已有账单，费用行有效，生成新增账单")
    if non_bill:
        reasons.append("非账单文档按配置直接 CREATE")
    return OperationDecision(
        operation=OperationType.CREATE,
        reasons=reasons,
        match=match,
        posting=posting,
    )


def final_values(
    candidate: BillCandidate,
    charge: ChargeCandidate,
    existing_bill: ExistingBill | None,
    existing_charge: ExistingCharge | None,
    patch: dict,
    operation: OperationType,
) -> dict:
    if existing_bill and existing_charge and operation in (OperationType.UPDATE, OperationType.NO_CHANGE):
        charge_patch = patch.get("charge", {})
        bill_patch = patch.get("bill", {})
        values = {
            "document_type": bill_patch.get("document_type", existing_bill.document_type),
            "reference_no": bill_patch.get("reference_no", existing_bill.reference_no),
            "document_date": bill_patch.get("document_date", existing_bill.document_date),
            "issuer": bill_patch.get("issuer", existing_bill.issuer),
            "recipient": bill_patch.get("recipient", existing_bill.recipient),
            "charge_item": charge_patch.get("charge_item", existing_charge.charge_item),
            "amount": charge_patch.get("amount", existing_charge.amount),
            "currency": charge_patch.get("currency", existing_charge.currency),
            "payment_term": charge_patch.get("payment_term", existing_charge.payment_term),
            "quantity": charge_patch.get("quantity", existing_charge.quantity),
            "unit_price": charge_patch.get("unit_price", existing_charge.unit_price),
        }
        return values
    return {
        "document_type": candidate.document_type,
        "reference_no": candidate.reference_no,
        "document_date": candidate.document_date,
        "issuer": candidate.issuer,
        "recipient": candidate.recipient,
        "charge_item": charge.standardized_charge_name or charge.raw_charge_name,
        "amount": charge.amount,
        "currency": charge.currency,
        "payment_term": charge.payment_term,
        "quantity": charge.quantity,
        "unit_price": charge.unit_price,
    }
