from __future__ import annotations

import pytest

from shipping_billing.classification import rule_classify
from shipping_billing.extraction.rules import (
    extract_document_type,
    extract_parties,
    extract_transport_ids,
    parse_ocr_charges,
)
from shipping_billing.models import (
    ContentBlock,
    DocumentKind,
    NormalizedDocument,
    SourceRef,
    SourceType,
)
from shipping_billing.ocr import OCRClient
from shipping_billing.parsers.base import sniff_source_type
from shipping_billing.parsers.registry import DocumentParser
from shipping_billing.utils import is_valid_container_number
from tests.conftest import FIXTURES


@pytest.fixture
def parser(config, tmp_path):
    config.settings.ocr.raw_dir = str(tmp_path / "ocr" / "raw")
    ocr = OCRClient(config.settings.ocr, enabled=False)
    return DocumentParser(config, ocr)


def parse_one(parser: DocumentParser, path):
    documents = parser.parse_paths([path])
    assert len(documents) == 1
    return documents[0]


def test_text_parser_keeps_layout(parser):
    doc = parse_one(parser, FIXTURES / "bills" / "simple_invoice.txt")
    assert doc.source.source_type == SourceType.TXT
    assert "INV-3001" in doc.full_text
    assert "Handling Fee 40.00 USD" in doc.full_text
    assert doc.parse_error is None


def test_excel_parser_reads_table_rows(parser):
    doc = parse_one(parser, FIXTURES / "bills" / "table_bill.xlsx")
    assert doc.source.source_type == SourceType.XLSX
    assert "THC" in doc.full_text
    assert "Trucking Fee" in doc.full_text
    assert doc.parser_metadata.get("sheets")


def test_word_parser_reads_paragraphs_and_tables(parser):
    doc = parse_one(parser, FIXTURES / "bills" / "word_bill.docx")
    assert doc.source.source_type == SourceType.DOCX
    assert "INV-5001" in doc.full_text
    assert "Trucking Fee" in doc.full_text


def test_eml_parser_reads_body(parser):
    doc = parse_one(parser, FIXTURES / "eml" / "prealert.eml")
    assert doc.source.source_type == SourceType.EML
    assert "H/C USD 50" in doc.full_text
    assert "OOLU4055331281" in doc.full_text
    assert doc.parser_metadata.get("from", "").find("Carrier Agent Ltd") >= 0


def test_duplicate_file_is_skipped(parser):
    path = FIXTURES / "bills" / "simple_invoice.txt"
    documents = parser.parse_paths([path, path])
    assert len(documents) == 1
    assert parser.ctx.duplicates
    assert any(entry.get("duplicate_of") for entry in parser.manifest)


def test_sniff_by_magic_not_just_extension(tmp_path):
    disguised = tmp_path / "bill.txt"
    disguised.write_bytes(b"%PDF-1.7\nrest")
    assert sniff_source_type(disguised) == SourceType.PDF

    unknown = tmp_path / "bill.xyz"
    unknown.write_text("plain", encoding="utf-8")
    assert sniff_source_type(unknown) == SourceType.OTHER


def test_transport_document_is_other_even_with_arrival_words():
    doc = NormalizedDocument(
        document_id="doc_bl",
        source=SourceRef(
            source_file="HBL.pdf",
            source_type=SourceType.PDF,
            sha256="b" * 64,
            location="page:1",
        ),
        blocks=[
            ContentBlock(
                kind="text",
                location="page:1",
                text="FOR CARGO ARRIVAL AND INFORMATION APPLY TO: AGENT",
            )
        ],
        parser_metadata={"transport_document": True},
    )
    classification = rule_classify(doc)
    assert classification.kind == DocumentKind.OTHER
    assert classification.confidence >= 0.9


def test_ocr_charge_rows_keep_currency():
    doc = NormalizedDocument(
        document_id="doc_ocr",
        source=SourceRef(
            source_file="bill.png",
            source_type=SourceType.IMAGE,
            sha256="c" * 64,
            location="ocr:structured",
        ),
        blocks=[
            ContentBlock(
                kind="table",
                location="ocr:charges",
                rows=[
                    [
                        "charge_item",
                        "amount",
                        "currency",
                        "chargeCtr",
                        "amount_raw",
                        "correction",
                    ],
                    ["WHARFAGE", "5.00", "USD", "", "5.00", ""],
                ],
            )
        ],
    )
    charges = parse_ocr_charges(doc)
    assert len(charges) == 1
    assert charges[0].currency == "USD"


def test_unreadable_file_becomes_error_document(parser, tmp_path):
    path = tmp_path / "bill.txt"
    path.write_text("   ", encoding="utf-8")
    doc = parse_one(parser, path)
    assert doc.parse_error == "文本文件为空"
    assert doc.blocks == []


def test_container_check_digit_rejects_invoice_numbers():
    assert is_valid_container_number("JXLU4504463")
    assert is_valid_container_number("SAPU2040689")
    assert not is_valid_container_number("ORFR1618731")
    assert not is_valid_container_number("NOTACONTAINER")

    doc = NormalizedDocument(
        document_id="doc_container",
        source=SourceRef(
            source_file="bill.txt",
            source_type=SourceType.TXT,
            sha256="d" * 64,
            location="file",
        ),
        blocks=[
            ContentBlock(
                kind="text",
                location="file",
                text="Invoice ORFR1618731\nContainer JXLU4504463",
            )
        ],
    )
    result = extract_transport_ids(doc)
    assert result["values"]["container_nos"] == ["JXLU4504463"]


def test_creditor_excludes_bank_beneficiary():
    doc = NormalizedDocument(
        document_id="doc_party",
        source=SourceRef(
            source_file="notice.txt",
            source_type=SourceType.TXT,
            sha256="e" * 64,
            location="file",
        ),
        blocks=[
            ContentBlock(
                kind="text",
                location="file",
                text=(
                    "NEPTUNE SHIPPING LIMITED\n"
                    "GENERAL NOTICE\n"
                    "DBA CMA-CGM Beneficiary: CMA CGM & ANL Securities BV\n"
                    "BALANCE DUE TO NEPTUNE SHIPPING LIMITED (LAX)\n"
                ),
            )
        ],
    )
    issuer, _, _ = extract_parties(doc, ["Origin Agent Ltd"])
    assert issuer == "NEPTUNE SHIPPING LIMITED (LAX)"


def test_letterhead_is_used_when_only_bank_beneficiary_exists():
    doc = NormalizedDocument(
        document_id="doc_party_bank",
        source=SourceRef(
            source_file="notice.txt",
            source_type=SourceType.TXT,
            sha256="f" * 64,
            location="file",
        ),
        blocks=[
            ContentBlock(
                kind="text",
                location="file",
                text=(
                    "NEPTUNE SHIPPING LIMITED17800 CASTLETON STREET, SUITE 300\n"
                    "GENERAL NOTICE\n"
                    "Beneficiary: CMA CGM & ANL Securities BV\n"
                    "EQUIPMENT CHARGES ARE PAYABLE TO CMA CGM and do not include rail storage.\n"
                ),
            )
        ],
    )
    issuer, _, _ = extract_parties(doc, ["Origin Agent Ltd"])
    assert issuer == "NEPTUNE SHIPPING LIMITED"


def test_general_notice_type_wins_over_body_arrival_notice():
    doc = NormalizedDocument(
        document_id="doc_type",
        source=SourceRef(
            source_file="notice.txt",
            source_type=SourceType.TXT,
            sha256="1" * 64,
            location="file",
        ),
        blocks=[
            ContentBlock(
                kind="text",
                location="file",
                text="GENERAL NOTICE\nARRIVAL NOTICE\n",
            )
        ],
    )
    classification = rule_classify(doc)
    document_type, _ = extract_document_type(doc, classification)
    assert document_type == "General Notice"
