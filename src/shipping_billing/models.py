from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OperationType(str, Enum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    NO_CHANGE = "NO_CHANGE"
    PENDING_REVIEW = "PENDING_REVIEW"


class PostingType(str, Enum):
    AR = "AR"
    AP = "AP"


class PaymentTerm(str, Enum):
    PP = "PP"
    CC = "CC"


class DocumentKind(str, Enum):
    BILL = "BILL"
    BUSINESS_EMAIL = "BUSINESS_EMAIL"
    ARRIVAL_NOTICE = "ARRIVAL_NOTICE"
    OTHER = "OTHER"
    UNCERTAIN = "UNCERTAIN"


class ExtractionMethod(str, Enum):
    RULE = "RULE"
    TABLE = "TABLE"
    EML_BODY = "EML_BODY"
    OCR = "OCR"
    LLM = "LLM"
    INHERITED = "INHERITED"


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class SourceType(str, Enum):
    EML = "eml"
    PDF = "pdf"
    IMAGE = "image"
    XLSX = "xlsx"
    XLS = "xls"
    DOCX = "docx"
    DOC = "doc"
    TXT = "txt"
    JSON = "json"
    OTHER = "other"


class SourceRef(BaseModel):
    source_file: str
    source_type: SourceType
    sha256: str
    location: str
    raw_text: str | None = None


class ContentBlock(BaseModel):
    kind: Literal["text", "table"]
    location: str
    text: str | None = None
    rows: list[list[str]] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentClassification(BaseModel):
    kind: DocumentKind
    confidence: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class NormalizedDocument(BaseModel):
    document_id: str
    parent_document_id: str | None = None
    source: SourceRef
    blocks: list[ContentBlock] = Field(default_factory=list)
    parser_metadata: dict[str, Any] = Field(default_factory=dict)
    classification: DocumentClassification | None = None
    parse_error: str | None = None

    @property
    def full_text(self) -> str:
        parts: list[str] = []
        for block in self.blocks:
            if block.kind == "text" and block.text:
                parts.append(block.text)
            elif block.kind == "table" and block.rows:
                parts.extend(" | ".join(row) for row in block.rows)
        return "\n".join(parts)


class FieldEvidence(BaseModel):
    field: str
    source_file: str
    location: str
    raw_text: str | None = None
    method: ExtractionMethod
    confidence: float = 1.0
    note: str | None = None


class ChargeCandidate(BaseModel):
    raw_charge_name: str | None = None
    standardized_charge_name: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    currency: str | None = None
    payment_term: PaymentTerm | None = None
    line_no: int | None = None
    evidence: list[FieldEvidence] = Field(default_factory=list)
    confidence: float = 0.5
    issues: list[str] = Field(default_factory=list)

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class DirectionHint(BaseModel):
    direction: Literal["creditor", "debtor"]
    text: str
    location: str


class BillCandidate(BaseModel):
    document_type: str | None = None
    reference_no: str | None = None
    document_date: date | None = None
    due_date: date | None = None
    issuer: str | None = None
    recipient: str | None = None
    master_bl_no: str | None = None
    house_bl_no: str | None = None
    booking_no: str | None = None
    container_nos: list[str] = Field(default_factory=list)
    charges: list[ChargeCandidate] = Field(default_factory=list)
    document_total: Decimal | None = None
    document_currency: str | None = None
    terms: str | None = None
    source_document_ids: list[str] = Field(default_factory=list)
    evidence: list[FieldEvidence] = Field(default_factory=list)
    direction_hints: list[DirectionHint] = Field(default_factory=list)
    confidence: float = 0.0
    is_bill_document: bool = False
    notes: list[str] = Field(default_factory=list)
    llm_meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("container_nos")
    @classmethod
    def _dedupe_containers(cls, values: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for value in values:
            key = value.upper()
            if key and key not in seen:
                seen.add(key)
                out.append(value.upper())
        return out


class ExistingCharge(BaseModel):
    model_config = ConfigDict(extra="allow")

    charge_item: str
    raw_charge_name: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    payment_term: PaymentTerm | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None


class ExistingBill(BaseModel):
    model_config = ConfigDict(extra="allow")

    bill_id: str
    document_type: str | None = None
    reference_no: str | None = None
    document_date: date | None = None
    issuer: str | None = None
    recipient: str | None = None
    master_bl_no: str | None = None
    house_bl_no: str | None = None
    booking_no: str | None = None
    container_nos: list[str] = Field(default_factory=list)
    charges: list[ExistingCharge] = Field(default_factory=list)
    document_total: Decimal | None = None
    document_currency: str | None = None


class ValidationIssue(BaseModel):
    code: str
    severity: Severity
    message: str
    field: str | None = None
    evidence: str | None = None
    blocking: bool = False


class MatchCandidate(BaseModel):
    bill_id: str
    rule_id: str
    charge_index: int | None = None
    score: float = 0.0
    details: dict[str, Any] = Field(default_factory=dict)


class MatchResult(BaseModel):
    rule_id: str | None = None
    candidates: list[MatchCandidate] = Field(default_factory=list)
    selected_bill_id: str | None = None
    selected_charge_index: int | None = None
    ambiguous: bool = False
    conflict: bool = False
    reasons: list[str] = Field(default_factory=list)


class PostingDecision(BaseModel):
    posting_type: PostingType | None = None
    rule_id: str | None = None
    reason: str
    blocked: bool = False


class DocumentInfo(BaseModel):
    document_type: str | None = None
    reference_no: str | None = None
    document_date: str | None = None


class TransactionParty(BaseModel):
    issuer: str | None = None
    recipient: str | None = None


class ChargeDetails(BaseModel):
    charge_item: str | None = None
    amount: float | None = None
    currency: str | None = None
    payment_term: str | None = None


class FinancialAttributes(BaseModel):
    accounting_entity: str | None = None
    posting_type: str | None = None


class SourceDocumentRef(BaseModel):
    file: str
    location: str
    document_id: str | None = None


class AuditTrail(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: str
    record_id: str | None = None
    source_documents: list[SourceDocumentRef] = Field(default_factory=list)
    field_evidence: list[FieldEvidence] = Field(default_factory=list)
    validation_results: list[ValidationIssue] = Field(default_factory=list)
    match_candidates: list[MatchCandidate] = Field(default_factory=list)
    ai_reasoning: list[str] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    rules_version: str = ""
    prompt_version: str = ""
    document_kind: str | None = None
    classification_confidence: float | None = None
    operation_reason: list[str] = Field(default_factory=list)
    update_patch: dict[str, Any] = Field(default_factory=dict)
    inherited_fields: list[str] = Field(default_factory=list)
    existing_bill_id: str | None = None
    raw_charge_name: str | None = None
    charge_confidence: float | None = None
    source_sha256: str | None = None
    document_metadata: dict[str, Any] = Field(default_factory=dict)
    llm: dict[str, Any] = Field(default_factory=dict)


class OutputRecord(BaseModel):
    operation: OperationType
    document_info: DocumentInfo = Field(default_factory=DocumentInfo)
    transaction_party: TransactionParty = Field(default_factory=TransactionParty)
    charge_details: ChargeDetails = Field(default_factory=ChargeDetails)
    financial_attributes: FinancialAttributes = Field(default_factory=FinancialAttributes)
    audit_trail: AuditTrail


class BatchManifestEntry(BaseModel):
    path: str
    sha256: str
    source_type: SourceType
    parent_document_id: str | None = None
    duplicate_of: str | None = None
    error: str | None = None
