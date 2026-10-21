from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .config import MatchingRules, MatchingSettings
from .models import (
    BillCandidate,
    ChargeCandidate,
    ExistingBill,
    ExistingCharge,
    MatchCandidate,
    MatchResult,
    PaymentTerm,
)
from .normalization import ChargeNormalizer
from .utils import fuzzy_ratio, normalize_charge_name, read_json


def normalize_reference(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^A-Z0-9]", "", value.upper())


@dataclass
class ExistingRow:
    bill: ExistingBill
    charge_index: int
    charge: ExistingCharge
    standardized_name: str


class ExistingBillRepository:
    def __init__(
        self,
        bills: list[ExistingBill],
        normalizer: ChargeNormalizer,
    ) -> None:
        self.bills = bills
        self.normalizer = normalizer
        self.rows: list[ExistingRow] = []
        for bill in bills:
            for index, charge in enumerate(bill.charges):
                match = normalizer.match(charge.charge_item)
                standardized = match.canonical or normalize_charge_name(charge.charge_item)
                self.rows.append(
                    ExistingRow(
                        bill=bill,
                        charge_index=index,
                        charge=charge,
                        standardized_name=standardized,
                    )
                )

    @classmethod
    def from_json(
        cls,
        path: str | Path | None,
        normalizer: ChargeNormalizer,
    ) -> "ExistingBillRepository":
        if not path or not Path(path).exists():
            return cls([], normalizer)
        payload = read_json(path)
        raw_bills = payload.get("bills") if isinstance(payload, dict) else payload
        if not isinstance(raw_bills, list):
            return cls([], normalizer)
        bills: list[ExistingBill] = []
        for item in raw_bills:
            if not isinstance(item, dict):
                continue
            try:
                bills.append(ExistingBill(**item))
            except Exception:
                continue
        return cls(bills, normalizer)

    def get_bill(self, bill_id: str | None) -> ExistingBill | None:
        if not bill_id:
            return None
        return next((bill for bill in self.bills if bill.bill_id == bill_id), None)


def _charge_name_for_match(charge: ChargeCandidate) -> str:
    return charge.standardized_charge_name or normalize_charge_name(charge.raw_charge_name)


def _amount_equal(left: Decimal | None, right: Decimal | None, tolerance: Decimal) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= tolerance


def _candidate_row(
    row: ExistingRow,
    rule_id: str,
    charge: ChargeCandidate | None = None,
    score: float = 1.0,
) -> MatchCandidate:
    details = {
        "bill_reference": row.bill.reference_no,
        "existing_charge": row.charge.charge_item,
        "existing_amount": str(row.charge.amount) if row.charge.amount is not None else None,
        "existing_currency": row.charge.currency,
    }
    if charge and charge.amount is not None:
        details["incoming_amount"] = str(charge.amount)
    return MatchCandidate(
        bill_id=row.bill.bill_id,
        rule_id=rule_id,
        charge_index=row.charge_index,
        score=score,
        details=details,
    )


def _rule_reference_charge(
    candidate: BillCandidate,
    charge: ChargeCandidate,
    repo: ExistingBillRepository,
) -> list[MatchCandidate]:
    reference = normalize_reference(candidate.reference_no)
    name = _charge_name_for_match(charge)
    if not reference or not name:
        return []
    results: list[MatchCandidate] = []
    for row in repo.rows:
        if normalize_reference(row.bill.reference_no) != reference:
            continue
        if row.standardized_name != name:
            continue
        score = 1.0
        if _amount_equal(charge.amount, row.charge.amount, Decimal("0.01")):
            score += 0.1
        results.append(_candidate_row(row, "reference_charge", charge, score))
    return results


def _bl_matches(candidate: BillCandidate, bill: ExistingBill) -> bool:
    incoming = {
        normalize_reference(candidate.master_bl_no),
        normalize_reference(candidate.house_bl_no),
    } - {""}
    existing = {
        normalize_reference(bill.master_bl_no),
        normalize_reference(bill.house_bl_no),
    } - {""}
    return bool(incoming & existing)


def _rule_bl_charge_currency(
    candidate: BillCandidate,
    charge: ChargeCandidate,
    repo: ExistingBillRepository,
) -> list[MatchCandidate]:
    name = _charge_name_for_match(charge)
    if not name or not charge.currency:
        return []
    results: list[MatchCandidate] = []
    for row in repo.rows:
        if row.standardized_name != name:
            continue
        if (row.charge.currency or "").upper() != charge.currency.upper():
            continue
        if not _bl_matches(candidate, row.bill):
            continue
        score = 1.0
        if _amount_equal(charge.amount, row.charge.amount, Decimal("0.01")):
            score += 0.1
        results.append(_candidate_row(row, "bl_charge_currency", charge, score))
    return results


def _rule_booking_charge_currency(
    candidate: BillCandidate,
    charge: ChargeCandidate,
    repo: ExistingBillRepository,
) -> list[MatchCandidate]:
    booking = normalize_reference(candidate.booking_no)
    name = _charge_name_for_match(charge)
    if not booking or not name or not charge.currency:
        return []
    results: list[MatchCandidate] = []
    for row in repo.rows:
        if normalize_reference(row.bill.booking_no) != booking:
            continue
        if row.standardized_name != name:
            continue
        if (row.charge.currency or "").upper() != charge.currency.upper():
            continue
        results.append(_candidate_row(row, "booking_charge_currency", charge, 1.0))
    return results


def _party_score(candidate: BillCandidate, bill: ExistingBill, threshold: float) -> tuple[bool, float]:
    pairs: list[float] = []
    if candidate.issuer and bill.issuer:
        pairs.append(fuzzy_ratio(candidate.issuer, bill.issuer))
    if candidate.recipient and bill.recipient:
        pairs.append(fuzzy_ratio(candidate.recipient, bill.recipient))
    if not pairs:
        return False, 0.0
    best = max(pairs)
    if len(pairs) == 2:
        return min(pairs) >= threshold, best
    return best >= threshold, best


def _rule_container_parties_date_amount(
    candidate: BillCandidate,
    charge: ChargeCandidate,
    repo: ExistingBillRepository,
    settings: MatchingSettings,
) -> list[MatchCandidate]:
    incoming_containers = {value.upper() for value in candidate.container_nos}
    if not incoming_containers or charge.amount is None:
        return []
    results: list[MatchCandidate] = []
    tolerance = Decimal(str(settings.amount_tolerance))
    for row in repo.rows:
        bill = row.bill
        existing_containers = {value.upper() for value in bill.container_nos}
        if not incoming_containers & existing_containers:
            continue
        parties_ok, party_score = _party_score(
            candidate, bill, settings.party_fuzzy_threshold
        )
        if not parties_ok:
            continue
        if candidate.document_date and bill.document_date:
            if abs((candidate.document_date - bill.document_date).days) > settings.date_tolerance_days:
                continue
        elif not candidate.document_date:
            continue
        if not _amount_equal(charge.amount, row.charge.amount, tolerance):
            continue
        results.append(
            _candidate_row(
                row,
                "container_parties_date_amount",
                charge,
                0.8 + 0.1 * party_score,
            )
        )
    return results


def _disambiguate(
    candidates: list[MatchCandidate],
    charge: ChargeCandidate,
    repo: ExistingBillRepository,
    rules: MatchingRules,
    settings: MatchingSettings,
) -> list[MatchCandidate]:
    if len(candidates) <= 1:
        return candidates
    remaining = list(candidates)
    amount_tolerance = Decimal(str(settings.amount_tolerance))
    for field in rules.charge_row_disambiguation:
        if len(remaining) <= 1:
            break
        filtered: list[MatchCandidate] = []
        for candidate in remaining:
            row = next(
                (
                    row
                    for row in repo.rows
                    if row.bill.bill_id == candidate.bill_id
                    and row.charge_index == candidate.charge_index
                ),
                None,
            )
            if row is None:
                continue
            if field == "amount":
                if charge.amount is not None and _amount_equal(
                    charge.amount, row.charge.amount, amount_tolerance
                ):
                    filtered.append(candidate)
            elif field == "unit_price":
                if charge.unit_price is not None and _amount_equal(
                    charge.unit_price, row.charge.unit_price, amount_tolerance
                ):
                    filtered.append(candidate)
            elif field == "quantity":
                if charge.quantity is not None and charge.quantity == row.charge.quantity:
                    filtered.append(candidate)
            elif field == "payment_term":
                if charge.payment_term is not None and charge.payment_term == row.charge.payment_term:
                    filtered.append(candidate)
        if filtered:
            remaining = filtered
    return remaining


class BillMatcher:
    def __init__(
        self,
        repo: ExistingBillRepository,
        rules: MatchingRules,
        settings: MatchingSettings,
    ) -> None:
        self.repo = repo
        self.rules = rules
        self.settings = settings
        self.rule_order = settings.rule_order or [rule["id"] for rule in rules.rules]

    def match_charge(self, candidate: BillCandidate, charge: ChargeCandidate) -> MatchResult:
        if not charge.standardized_charge_name and not charge.raw_charge_name:
            return MatchResult(reasons=["费用名称缺失，无法匹配已有账单"])
        for rule_id in self.rule_order:
            if rule_id == "reference_charge":
                matches = _rule_reference_charge(candidate, charge, self.repo)
            elif rule_id == "bl_charge_currency":
                matches = _rule_bl_charge_currency(candidate, charge, self.repo)
            elif rule_id == "booking_charge_currency":
                matches = _rule_booking_charge_currency(candidate, charge, self.repo)
            elif rule_id == "container_parties_date_amount":
                matches = _rule_container_parties_date_amount(
                    candidate, charge, self.repo, self.settings
                )
            else:
                continue
            if not matches:
                continue
            matches = _disambiguate(matches, charge, self.repo, self.rules, self.settings)
            ambiguous = len(matches) > 1
            result = MatchResult(
                rule_id=rule_id,
                candidates=matches,
                ambiguous=ambiguous,
            )
            if ambiguous:
                result.reasons.append(
                    f"规则 {rule_id} 命中 {len(matches)} 条已有账单费用行，无法唯一确定"
                )
            else:
                result.selected_bill_id = matches[0].bill_id
                result.selected_charge_index = matches[0].charge_index
                result.reasons.append(f"通过规则 {rule_id} 唯一匹配已有账单")
            return result
        return MatchResult(reasons=["未匹配到已有账单"])
