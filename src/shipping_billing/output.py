from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import AccountingEntitySettings, AppConfig
from .models import (
    AuditTrail,
    BillCandidate,
    ChargeCandidate,
    ChargeDetails,
    DocumentClassification,
    DocumentInfo,
    ExistingBill,
    ExistingCharge,
    FieldEvidence,
    FinancialAttributes,
    NormalizedDocument,
    OperationType,
    OutputRecord,
    SourceDocumentRef,
    TransactionParty,
    ValidationIssue,
)
from .decision import OperationDecision, final_values
from .utils import redact_sensitive_text, to_jsonable, truncate, write_json, write_jsonl

_METADATA_KEYS = {
    "parser",
    "page_count",
    "page_errors",
    "native_text_chars",
    "native_preferred",
    "ocr_used",
    "ocr_success",
    "ocr_cached",
    "ocr_error",
    "ocr_order_type",
    "subject",
    "from",
    "to",
    "cc",
    "date_header",
    "message_id",
    "depth",
    "sheets",
    "notes",
    "tables",
    "paragraphs",
    "attachment_name",
}


def entity_label(entity: AccountingEntitySettings | None) -> str | None:
    if entity is None:
        return None
    return entity.name or entity.display_name or entity.role_hint or None


def _safe_text(value: str | None, config: AppConfig, limit: int = 800) -> str | None:
    if value is None:
        return None
    if config.settings.output.redact_sensitive:
        value = redact_sensitive_text(value)
    return truncate(value, limit)


def _sanitize_evidence(
    items: list[FieldEvidence],
    config: AppConfig,
) -> list[FieldEvidence]:
    sanitized: list[FieldEvidence] = []
    seen: set[tuple[Any, ...]] = set()
    keep_raw = config.settings.output.include_raw_text_in_audit
    for item in items:
        key = (
            item.field,
            item.source_file,
            item.location,
            item.method.value,
            item.raw_text,
            item.note,
        )
        if key in seen:
            continue
        seen.add(key)
        copy = item.model_copy(deep=True)
        copy.raw_text = _safe_text(copy.raw_text, config) if keep_raw else None
        copy.note = _safe_text(copy.note, config)
        sanitized.append(copy)
    return sanitized


def _sanitize_issues(
    issues: list[ValidationIssue],
    config: AppConfig,
) -> list[ValidationIssue]:
    return [
        issue.model_copy(
            update={"evidence": _safe_text(issue.evidence, config)}
        )
        for issue in issues
    ]


def _sanitize_metadata(doc: NormalizedDocument, config: AppConfig) -> dict[str, Any]:
    metadata: dict[str, Any] = {"source_type": doc.source.source_type.value}
    for key in _METADATA_KEYS:
        if key not in doc.parser_metadata:
            continue
        value = doc.parser_metadata[key]
        if isinstance(value, str):
            metadata[key] = _safe_text(value, config, limit=400)
        else:
            metadata[key] = to_jsonable(value)
    return metadata


def _source_documents(documents: list[NormalizedDocument]) -> list[SourceDocumentRef]:
    refs: list[SourceDocumentRef] = []
    seen: set[str] = set()
    for doc in documents:
        if doc.document_id in seen:
            continue
        seen.add(doc.document_id)
        location = doc.source.location
        if not location or location == "file":
            location = doc.blocks[0].location if doc.blocks else "document"
        refs.append(
            SourceDocumentRef(
                file=doc.source.source_file,
                location=location,
                document_id=doc.document_id,
            )
        )
    return refs


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def _candidate_values(candidate: BillCandidate, charge: ChargeCandidate) -> dict[str, Any]:
    return {
        "document_type": candidate.document_type,
        "reference_no": candidate.reference_no,
        "document_date": candidate.document_date,
        "issuer": candidate.issuer,
        "recipient": candidate.recipient,
        "charge_item": charge.standardized_charge_name,
        "amount": charge.amount,
        "currency": charge.currency,
        "payment_term": charge.payment_term.value if charge.payment_term else None,
        "quantity": charge.quantity,
        "unit_price": charge.unit_price,
    }


def _inherited_fields(existing_bill: ExistingBill, existing_charge: ExistingCharge, patch: dict) -> list[str]:
    bill_patch = patch.get("bill", {}) if isinstance(patch, dict) else {}
    charge_patch = patch.get("charge", {}) if isinstance(patch, dict) else {}
    inherited: list[str] = []
    for field in ("document_type", "reference_no", "document_date", "issuer", "recipient"):
        if field not in bill_patch:
            inherited.append(f"bill.{field}")
    for field in ("charge_item", "amount", "currency", "payment_term", "quantity", "unit_price"):
        if field not in charge_patch:
            inherited.append(f"charge.{field}")
    return inherited


def _reasoning(
    classification: DocumentClassification | None,
    candidate: BillCandidate | None,
    decision: OperationDecision,
    inherited: list[str],
) -> list[str]:
    reasons: list[str] = []
    if classification:
        reasons.extend(classification.reasons)
    if candidate:
        reasons.extend(candidate.notes)
    reasons.extend(decision.reasons)
    if decision.operation in (OperationType.UPDATE, OperationType.NO_CHANGE) and inherited:
        reasons.append("未被明确要求修改的字段继承已有账单值")
    return _dedupe(reasons)


def render_charge_record(
    *,
    run_id: str,
    record_id: str,
    config: AppConfig,
    documents: list[NormalizedDocument],
    classification: DocumentClassification | None,
    candidate: BillCandidate,
    charge: ChargeCandidate,
    decision: OperationDecision,
    issues: list[ValidationIssue],
    existing_bill: ExistingBill | None,
    existing_charge: ExistingCharge | None,
    entity: AccountingEntitySettings,
    non_bill: bool,
) -> OutputRecord:
    operation = decision.operation
    patch = decision.patch if isinstance(decision.patch, dict) else {}
    if (
        operation in (OperationType.UPDATE, OperationType.NO_CHANGE)
        and existing_bill
        and existing_charge
    ):
        values = final_values(
            candidate, charge, existing_bill, existing_charge, patch, operation
        )
        inherited = _inherited_fields(existing_bill, existing_charge, patch)
    else:
        values = _candidate_values(candidate, charge)
        inherited = []

    document_info = DocumentInfo(
        document_type=values.get("document_type"),
        reference_no=values.get("reference_no"),
        document_date=(
            values.get("document_date").isoformat()
            if hasattr(values.get("document_date"), "isoformat")
            else values.get("document_date")
        ),
    )
    transaction_party = TransactionParty(
        issuer=values.get("issuer"),
        recipient=values.get("recipient"),
    )
    amount = values.get("amount")
    charge_details = ChargeDetails(
        charge_item=values.get("charge_item"),
        amount=float(amount) if amount is not None else None,
        currency=values.get("currency"),
        payment_term=values.get("payment_term"),
    )
    financial_attributes = FinancialAttributes(
        accounting_entity=entity_label(entity),
        posting_type=(
            decision.posting.posting_type.value
            if operation != OperationType.PENDING_REVIEW
            and decision.posting
            and decision.posting.posting_type
            else None
        ),
    )

    review_reasons: list[str] = []
    if operation == OperationType.PENDING_REVIEW:
        review_reasons = _dedupe(decision.reasons) or [
            issue.message
            for issue in issues
            if issue.blocking or issue.severity.value == "ERROR"
        ] or ["需要人工复核"]

    evidence = _sanitize_evidence(
        [*candidate.evidence, *charge.evidence],
        config,
    )
    audit = AuditTrail(
        run_id=run_id,
        record_id=record_id,
        source_documents=_source_documents(documents),
        field_evidence=evidence,
        validation_results=_sanitize_issues(issues, config),
        match_candidates=decision.match.candidates,
        ai_reasoning=_reasoning(classification, candidate, decision, inherited),
        review_reasons=review_reasons,
        rules_version=config.settings.project.rule_version,
        prompt_version=config.settings.project.prompt_version,
        document_kind=classification.kind.value if classification else None,
        classification_confidence=classification.confidence if classification else None,
        operation_reason=_dedupe(decision.reasons),
        update_patch=to_jsonable(patch) if operation == OperationType.UPDATE else {},
        inherited_fields=inherited,
        existing_bill_id=existing_bill.bill_id if existing_bill else None,
        raw_charge_name=_safe_text(charge.raw_charge_name, config),
        charge_confidence=charge.confidence,
        source_sha256=documents[-1].source.sha256 if documents else None,
        document_metadata=_sanitize_metadata(documents[-1], config) if documents else {},
        llm=to_jsonable(candidate.llm_meta),
    )
    return OutputRecord(
        operation=operation,
        document_info=document_info,
        transaction_party=transaction_party,
        charge_details=charge_details,
        financial_attributes=financial_attributes,
        audit_trail=audit,
    )


def render_review_record(
    *,
    run_id: str,
    record_id: str,
    config: AppConfig,
    doc: NormalizedDocument,
    documents: list[NormalizedDocument],
    classification: DocumentClassification | None,
    issues: list[ValidationIssue],
    reasons: list[str],
    candidate: BillCandidate | None = None,
    entity: AccountingEntitySettings | None = None,
) -> OutputRecord:
    reasoning = list(reasons)
    if classification:
        reasoning = [*classification.reasons, *reasoning]
    if candidate:
        reasoning = [*reasoning, *candidate.notes]
    reasoning = _dedupe(reasoning) or ["需要人工复核"]
    review_reasons = _dedupe(reasons) or ["需要人工复核"]

    audit = AuditTrail(
        run_id=run_id,
        record_id=record_id,
        source_documents=_source_documents(documents),
        field_evidence=_sanitize_evidence(
            [*(candidate.evidence if candidate else [])],
            config,
        ),
        validation_results=_sanitize_issues(issues, config),
        match_candidates=[],
        ai_reasoning=reasoning,
        review_reasons=review_reasons,
        rules_version=config.settings.project.rule_version,
        prompt_version=config.settings.project.prompt_version,
        document_kind=classification.kind.value if classification else None,
        classification_confidence=classification.confidence if classification else None,
        operation_reason=list(reasons),
        inherited_fields=[],
        existing_bill_id=None,
        raw_charge_name=None,
        charge_confidence=candidate.confidence if candidate else None,
        source_sha256=doc.source.sha256,
        document_metadata=_sanitize_metadata(doc, config),
        llm=to_jsonable(candidate.llm_meta) if candidate else {},
    )
    return OutputRecord(
        operation=OperationType.PENDING_REVIEW,
        document_info=DocumentInfo(
            document_type=candidate.document_type if candidate else None,
            reference_no=candidate.reference_no if candidate else None,
            document_date=(
                candidate.document_date.isoformat()
                if candidate and candidate.document_date
                else None
            ),
        ),
        transaction_party=TransactionParty(
            issuer=candidate.issuer if candidate else None,
            recipient=candidate.recipient if candidate else None,
        ),
        charge_details=ChargeDetails(),
        financial_attributes=FinancialAttributes(
            accounting_entity=entity_label(entity),
            posting_type=None,
        ),
        audit_trail=audit,
    )


def audit_row(record: OutputRecord) -> dict[str, Any]:
    payload = record.audit_trail.model_dump(mode="json")
    payload["operation"] = record.operation.value
    payload["document_info"] = record.document_info.model_dump(mode="json")
    payload["charge_details"] = record.charge_details.model_dump(mode="json")
    payload["financial_attributes"] = record.financial_attributes.model_dump(mode="json")
    return payload


def write_outputs(
    records: list[OutputRecord],
    summary: dict[str, Any],
    output_dir: str | Path,
    config: AppConfig,
) -> dict[str, str]:
    settings = config.settings.output
    out_dir = Path(output_dir)
    results_path = out_dir / settings.results_file
    audit_path = out_dir / settings.audit_file
    summary_path = out_dir / settings.summary_file
    write_json(
        results_path,
        [record.model_dump(mode="json") for record in records],
        ensure_ascii=settings.ensure_ascii,
    )
    write_jsonl(
        audit_path,
        [audit_row(record) for record in records],
        ensure_ascii=settings.ensure_ascii,
    )
    summary = dict(summary)
    summary["outputs"] = {
        "results": str(results_path),
        "audit": str(audit_path),
        "summary": str(summary_path),
    }
    write_json(summary_path, summary, ensure_ascii=settings.ensure_ascii)
    return summary["outputs"]
