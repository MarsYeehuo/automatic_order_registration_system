from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .classification import classify_document
from .config import AccountingEntitySettings, AppConfig
from .decision import OperationDecision, decide_operation, decide_posting
from .extraction.engine import Extractor
from .llm import LLMClient
from .matching import BillMatcher, ExistingBillRepository, normalize_reference
from .models import (
    BillCandidate,
    ChargeCandidate,
    DocumentClassification,
    DocumentKind,
    ExistingBill,
    ExistingCharge,
    MatchResult,
    NormalizedDocument,
    OperationType,
    OutputRecord,
    PostingDecision,
    Severity,
    ValidationIssue,
)
from .ocr import OCRClient
from .output import (
    entity_label,
    render_charge_record,
    render_review_record,
)
from .parsers.registry import DocumentParser
from .utils import dedupe, normalize_charge_name
from .validation import validate_candidate, validate_match_consistency


@dataclass
class BatchResult:
    run_id: str
    records: list[OutputRecord]
    summary: dict[str, Any]
    documents: list[NormalizedDocument] = field(default_factory=list)


@dataclass
class _Plan:
    doc: NormalizedDocument
    documents: list[NormalizedDocument]
    classification: DocumentClassification
    candidate: BillCandidate
    charge: ChargeCandidate
    issues: list[ValidationIssue]
    decision: OperationDecision
    existing_bill: ExistingBill | None
    existing_charge: ExistingCharge | None
    entity: AccountingEntitySettings
    non_bill: bool


@dataclass
class _ReviewTask:
    doc: NormalizedDocument
    documents: list[NormalizedDocument]
    classification: DocumentClassification | None
    issues: list[ValidationIssue]
    reasons: list[str]
    candidate: BillCandidate | None
    entity: AccountingEntitySettings


class ShippingBillingPipeline:
    def __init__(
        self,
        config: AppConfig,
        *,
        use_llm: bool = True,
        use_ocr: bool = True,
        existing_bills_path: str | Path | None = None,
    ) -> None:
        self.config = config
        settings = config.settings
        self.use_llm = use_llm
        ocr_settings = settings.ocr.model_copy(
            update={
                "serve_dir": str(settings.resolve_path(settings.ocr.serve_dir)),
                "public_dir": str(settings.resolve_path(settings.ocr.public_dir)),
                "cache_dir": str(settings.resolve_path(settings.ocr.cache_dir)),
                "raw_dir": str(settings.resolve_path(settings.ocr.raw_dir)),
            }
        )
        self.ocr = OCRClient(ocr_settings, enabled=use_ocr)
        self.llm: LLMClient | None = None
        if use_llm:
            llm_settings = settings.llm.model_copy(
                update={"cache_dir": str(settings.resolve_path(settings.llm.cache_dir))}
            )
            self.llm = LLMClient(
                llm_settings,
                config.llm_credentials,
                prompt_version=settings.project.prompt_version,
            )
        self.extractor = Extractor(config)
        if existing_bills_path is None:
            existing_bills_path = settings.resolve_path("data/existing_bills.json")
        self.existing_bills_path = Path(existing_bills_path)
        self.repo = ExistingBillRepository.from_json(
            self.existing_bills_path, self.extractor.normalizer
        )
        self.matcher = BillMatcher(
            self.repo, config.matching_rules, settings.matching
        )

    def close(self) -> None:
        self.ocr.close()

    def run(self, inputs: list[str | Path]) -> BatchResult:
        started = datetime.now(timezone.utc)
        run_id = "run_" + started.strftime("%Y%m%dT%H%M%S%f") + "_" + uuid4().hex[:6]
        parser = DocumentParser(self.config, self.ocr)
        documents = parser.parse_paths(inputs)
        docs_by_id = {doc.document_id: doc for doc in documents}
        tasks: list[tuple[str, _Plan | _ReviewTask]] = []
        record_counts: dict[str, int] = {}
        llm_calls = 0
        llm_errors: list[str] = []
        ocr_errors: list[str] = []

        for doc in documents:
            chain = self._documents_chain(doc, docs_by_id)
            classification = classify_document(doc, self.llm)
            doc.classification = classification
            entity = self.config.settings.entity_for(doc.source.source_file)
            metadata = doc.parser_metadata or {}
            if self.ocr.enabled and metadata.get("ocr_used"):
                if metadata.get("ocr_success") is False and metadata.get("ocr_error"):
                    ocr_errors.append(
                        f"{doc.source.source_file}: {metadata.get('ocr_error')}"
                    )
            review_reasons = self._precheck_reasons(doc)
            if review_reasons:
                issues = [
                    ValidationIssue(
                        code="PARSE_ERROR",
                        severity=Severity.ERROR,
                        message=review_reasons[0],
                        blocking=True,
                    )
                ]
                tasks.append(
                    (
                        "review",
                        _ReviewTask(
                            doc=doc,
                            documents=chain,
                            classification=classification,
                            issues=issues,
                            reasons=review_reasons,
                            candidate=None,
                            entity=entity,
                        ),
                    )
                )
                continue

            if doc.parser_metadata.get("transport_document"):
                continue
            if not self._should_extract(doc, classification):
                continue

            try:
                candidate = self.extractor.extract(doc, classification, self.llm)
                issues = validate_candidate(
                    candidate, doc, self.config.settings.validation
                )
            except Exception as exc:  # pragma: no cover - defensive boundary
                tasks.append(
                    (
                        "review",
                        _ReviewTask(
                            doc=doc,
                            documents=chain,
                            classification=classification,
                            issues=[
                                ValidationIssue(
                                    code="EXTRACTION_ERROR",
                                    severity=Severity.ERROR,
                                    message=f"抽取异常: {type(exc).__name__}: {exc}",
                                    blocking=True,
                                )
                            ],
                            reasons=[f"抽取异常，转人工审核: {type(exc).__name__}"],
                            candidate=None,
                            entity=entity,
                        ),
                    )
                )
                continue

            if candidate.llm_meta:
                llm_calls += 1
                if candidate.llm_meta.get("error"):
                    llm_errors.append(
                        f"{doc.source.source_file}: {candidate.llm_meta['error']}"
                    )

            if not candidate.charges:
                if doc.parse_error or classification.kind in (
                    DocumentKind.BILL,
                    DocumentKind.UNCERTAIN,
                ):
                    reason = "未提取到有效费用行，无法自动建账"
                    if doc.parse_error:
                        reason = f"文档可读但费用抽取不完整: {doc.parse_error}"
                    issue = ValidationIssue(
                        code="NO_VALID_CHARGES",
                        severity=Severity.ERROR,
                        message=reason,
                        blocking=True,
                    )
                    tasks.append(
                        (
                            "review",
                            _ReviewTask(
                                doc=doc,
                                documents=chain,
                                classification=classification,
                                issues=[*issues, issue],
                                reasons=[reason],
                                candidate=candidate,
                                entity=entity,
                            ),
                        )
                    )
                continue

            non_bill = classification.kind != DocumentKind.BILL
            for charge in candidate.charges:
                tasks.append(
                    (
                        "plan",
                        self._build_plan(
                            doc=doc,
                            documents=chain,
                            classification=classification,
                            candidate=candidate,
                            charge=charge,
                            issues=issues,
                            entity=entity,
                            non_bill=non_bill,
                        ),
                    )
                )

        plans = [task for kind, task in tasks if kind == "plan"]
        self._apply_batch_rules(plans)

        records: list[OutputRecord] = []
        for index, (kind, task) in enumerate(tasks, start=1):
            record_id = f"{run_id}-R{index:04d}"
            if kind == "plan":
                assert isinstance(task, _Plan)
                record = render_charge_record(
                    run_id=run_id,
                    record_id=record_id,
                    config=self.config,
                    documents=task.documents,
                    classification=task.classification,
                    candidate=task.candidate,
                    charge=task.charge,
                    decision=task.decision,
                    issues=task.issues,
                    existing_bill=task.existing_bill,
                    existing_charge=task.existing_charge,
                    entity=task.entity,
                    non_bill=task.non_bill,
                )
            else:
                assert isinstance(task, _ReviewTask)
                record = render_review_record(
                    run_id=run_id,
                    record_id=record_id,
                    config=self.config,
                    doc=task.doc,
                    documents=task.documents,
                    classification=task.classification,
                    issues=task.issues,
                    reasons=task.reasons,
                    candidate=task.candidate,
                    entity=task.entity,
                )
            records.append(record)
            record_counts[task.doc.document_id] = (
                record_counts.get(task.doc.document_id, 0) + 1
            )

        finished = datetime.now(timezone.utc)
        summary = self._build_summary(
            run_id=run_id,
            inputs=inputs,
            started=started,
            finished=finished,
            documents=documents,
            records=records,
            parser=parser,
            record_counts=record_counts,
            llm_calls=llm_calls,
            llm_errors=llm_errors,
            ocr_errors=ocr_errors,
        )
        return BatchResult(
            run_id=run_id,
            records=records,
            summary=summary,
            documents=documents,
        )

    @staticmethod
    def _documents_chain(
        doc: NormalizedDocument, docs_by_id: dict[str, NormalizedDocument]
    ) -> list[NormalizedDocument]:
        chain: list[NormalizedDocument] = []
        current: NormalizedDocument | None = doc
        seen: set[str] = set()
        while current is not None and current.document_id not in seen:
            seen.add(current.document_id)
            chain.append(current)
            parent_id = current.parent_document_id
            current = docs_by_id.get(parent_id) if parent_id else None
        chain.reverse()
        return chain

    def _precheck_reasons(self, doc: NormalizedDocument) -> list[str]:
        if doc.parse_error and not (doc.full_text or "").strip():
            return [f"解析失败，转人工审核: {doc.parse_error}"]
        if not (doc.full_text or "").strip():
            return ["文档没有可提取文本，转人工审核"]
        return []

    @staticmethod
    def _should_extract(
        doc: NormalizedDocument, classification: DocumentClassification
    ) -> bool:
        if not (doc.full_text or "").strip():
            return False
        return classification.kind in {
            DocumentKind.BILL,
            DocumentKind.BUSINESS_EMAIL,
            DocumentKind.ARRIVAL_NOTICE,
            DocumentKind.OTHER,
            DocumentKind.UNCERTAIN,
        }

    def _build_plan(
        self,
        *,
        doc: NormalizedDocument,
        documents: list[NormalizedDocument],
        classification: DocumentClassification,
        candidate: BillCandidate,
        charge: ChargeCandidate,
        issues: list[ValidationIssue],
        entity: AccountingEntitySettings,
        non_bill: bool,
    ) -> _Plan:
        match = self.matcher.match_charge(candidate, charge)
        existing_bill: ExistingBill | None = None
        existing_charge: ExistingCharge | None = None
        if match.selected_bill_id:
            bill = self.repo.get_bill(match.selected_bill_id)
            index = match.selected_charge_index
            if bill is not None and index is not None and 0 <= index < len(bill.charges):
                existing_bill = bill
                existing_charge = bill.charges[index]
            else:
                match = match.model_copy(
                    update={
                        "ambiguous": True,
                        "reasons": [
                            *match.reasons,
                            "匹配结果指向的已有账单费用行不存在",
                        ],
                    }
                )
        consistency_issues = validate_match_consistency(
            candidate,
            existing_bill,
            match,
            self.config.settings.matching,
        )
        charge_issues = [*issues, *consistency_issues]
        posting = decide_posting(
            candidate,
            entity.all_names(),
            self.config.settings.matching.party_fuzzy_threshold,
        )
        decision = decide_operation(
            candidate,
            charge,
            charge_issues,
            match,
            posting,
            existing_bill,
            existing_charge,
            self.config,
            non_bill,
        )
        return _Plan(
            doc=doc,
            documents=documents,
            classification=classification,
            candidate=candidate,
            charge=charge,
            issues=charge_issues,
            decision=decision,
            existing_bill=existing_bill,
            existing_charge=existing_charge,
            entity=entity,
            non_bill=non_bill,
        )

    @staticmethod
    def _apply_batch_rules(plans: list[_Plan]) -> None:
        targets: dict[tuple[str, int], list[_Plan]] = {}
        for plan in plans:
            if plan.decision.operation not in (
                OperationType.UPDATE,
                OperationType.NO_CHANGE,
            ):
                continue
            if plan.existing_bill is None or plan.existing_charge is None:
                continue
            index = plan.decision.match.selected_charge_index
            if index is None:
                continue
            key = (plan.existing_bill.bill_id, index)
            targets.setdefault(key, []).append(plan)

        for (bill_id, index), group in targets.items():
            if len(group) <= 1:
                continue
            for plan in group:
                plan.decision = OperationDecision(
                    operation=OperationType.PENDING_REVIEW,
                    reasons=[
                        f"多个输入费用行命中同一已有账单费用行 {bill_id}#{index}，无法判断以哪一条为准"
                    ],
                    match=plan.decision.match,
                    posting=plan.decision.posting,
                )

        creates: dict[tuple, list[_Plan]] = {}
        for plan in plans:
            if plan.decision.operation != OperationType.CREATE:
                continue
            creates.setdefault(_create_key(plan), []).append(plan)
        for group in creates.values():
            if len({plan.doc.document_id for plan in group}) <= 1:
                continue
            group.sort(
                key=lambda plan: (
                    plan.candidate.is_bill_document,
                    plan.candidate.confidence,
                    len(plan.charge.evidence),
                ),
                reverse=True,
            )
            preferred_document = group[0].doc.document_id
            for plan in group[1:]:
                if plan.doc.document_id == preferred_document:
                    continue
                plan.decision = OperationDecision(
                    operation=OperationType.PENDING_REVIEW,
                    reasons=[
                        "输入批次内存在重复费用行，已保留一条 CREATE，其余转人工审核以防重复建账"
                    ],
                    match=plan.decision.match,
                    posting=plan.decision.posting,
                )

    def _build_summary(
        self,
        *,
        run_id: str,
        inputs: list[str | Path],
        started: datetime,
        finished: datetime,
        documents: list[NormalizedDocument],
        records: list[OutputRecord],
        parser: DocumentParser,
        record_counts: dict[str, int],
        llm_calls: int,
        llm_errors: list[str],
        ocr_errors: list[str],
    ) -> dict[str, Any]:
        operations: dict[str, int] = {}
        postings: dict[str, int] = {}
        for record in records:
            operations[record.operation.value] = operations.get(record.operation.value, 0) + 1
            posting = record.financial_attributes.posting_type or "UNKNOWN"
            postings[posting] = postings.get(posting, 0) + 1

        classification_counts: dict[str, int] = {}
        document_details: list[dict[str, Any]] = []
        for doc in documents:
            classification = doc.classification
            kind = classification.kind.value if classification else None
            if kind:
                classification_counts[kind] = classification_counts.get(kind, 0) + 1
            document_details.append(
                {
                    "document_id": doc.document_id,
                    "file": doc.source.source_file,
                    "source_type": doc.source.source_type.value,
                    "parent_document_id": doc.parent_document_id,
                    "classification": kind,
                    "classification_confidence": (
                        classification.confidence if classification else None
                    ),
                    "parse_error": doc.parse_error,
                    "ocr_used": bool(doc.parser_metadata.get("ocr_used")),
                    "ocr_success": doc.parser_metadata.get("ocr_success"),
                    "records": record_counts.get(doc.document_id, 0),
                }
            )

        duplicate_files = [
            {"path": path, "duplicate_of": duplicate_of}
            for path, duplicate_of in parser.ctx.duplicates
        ]
        parse_errors = [
            {"file": doc.source.source_file, "error": doc.parse_error}
            for doc in documents
            if doc.parse_error
        ]
        settings = self.config.settings
        return {
            "run_id": run_id,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_seconds": round((finished - started).total_seconds(), 3),
            "inputs": [str(item) for item in inputs],
            "accounting_entity": entity_label(settings.accounting_entity),
            "documents_total": len(documents),
            "documents_by_classification": classification_counts,
            "documents": document_details,
            "records_total": len(records),
            "operations": operations,
            "postings": postings,
            "duplicate_files": duplicate_files,
            "parse_errors": parse_errors,
            "manifest": parser.manifest,
            "existing_bills": {
                "path": str(self.existing_bills_path),
                "bills": len(self.repo.bills),
                "rows": len(self.repo.rows),
            },
            "ocr": {
                "enabled": self.ocr.enabled,
                "url_mode": settings.ocr.url_mode,
                "used": sum(
                    1 for doc in documents if doc.parser_metadata.get("ocr_used")
                ),
                "errors": dedupe(ocr_errors),
            },
            "llm": {
                "requested": self.use_llm,
                "available": bool(self.llm and self.llm.available),
                "model": self.llm.model_name if self.llm else None,
                "provider": self.llm.provider if self.llm else None,
                "disabled_reason": self.llm.disabled_reason if self.llm else "disabled by command line",
                "calls": llm_calls,
                "errors": dedupe(llm_errors),
            },
            "config_versions": {
                "rules": settings.project.rule_version,
                "prompt": settings.project.prompt_version,
                "charge_dictionary": self.config.charge_dictionary.version,
                "payment_terms": self.config.payment_terms.version,
                "matching_rules": self.config.matching_rules.version,
                "ar_ap_rules": self.config.ar_ap_rules.version,
            },
        }


def _create_key(plan: _Plan) -> tuple:
    candidate = plan.candidate
    charge = plan.charge
    name = charge.standardized_charge_name or normalize_charge_name(charge.raw_charge_name)
    base: tuple
    reference = normalize_reference(candidate.reference_no)
    if reference:
        base = ("reference", reference)
    else:
        identifiers = {
            normalize_reference(candidate.master_bl_no),
            normalize_reference(candidate.house_bl_no),
            normalize_reference(candidate.booking_no),
        } - {""}
        if identifiers:
            base = ("transport", tuple(sorted(identifiers)))
        else:
            base = (
                "document",
                normalize_reference(candidate.document_type),
                normalize_reference(candidate.issuer),
                normalize_reference(candidate.recipient),
                candidate.document_date.isoformat() if candidate.document_date else "",
            )
    return (
        *base,
        name,
        str(charge.amount) if charge.amount is not None else "",
        (charge.currency or "").upper(),
    )
