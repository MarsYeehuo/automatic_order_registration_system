from __future__ import annotations

import json
import re
from pathlib import Path

from pypdf import PdfReader

from ..models import ContentBlock, NormalizedDocument, SourceType
from ..utils import sha256_file
from .base import ParseContext, build_document


def parse_pdf(path: Path, ctx: ParseContext, parent_document_id: str | None) -> NormalizedDocument:
    digest = sha256_file(path)
    blocks: list[ContentBlock] = []
    page_texts: list[str] = []
    metadata: dict = {"parser": "pypdf"}
    parse_error: str | None = None
    reader = None
    try:
        reader = PdfReader(str(path))
        metadata["page_count"] = len(reader.pages)
        for index, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text(extraction_mode="layout") or ""
            except TypeError:
                text = page.extract_text() or ""
            except Exception as exc:  # pragma: no cover - malformed PDFs
                text = ""
                metadata.setdefault("page_errors", []).append(f"page {index}: {exc}")
            page_texts.append(text)
            if text.strip():
                blocks.append(
                    ContentBlock(
                        kind="text",
                        location=f"page:{index}",
                        text=text,
                        metadata={"source": "native_pdf"},
                    )
                )
    except Exception as exc:
        parse_error = f"PDF 解析失败: {type(exc).__name__}: {exc}"

    native_text = "\n".join(page_texts).strip()
    native_ok = len(native_text) >= ctx.config.settings.parsing.min_pdf_text_chars
    metadata["native_text_chars"] = len(native_text)
    metadata["native_preferred"] = native_ok
    metadata["native_text"] = native_text[:20000]

    ocr_result = None
    transport_pattern = re.compile(
        r"(?i)BILL\s+OF\s+LADING|SEA\s+WAYBILL|\bWAYBILL\b"
    )
    detection_text = native_text
    if reader is not None and not transport_pattern.search(detection_text):
        try:
            detection_text += "\n" + "\n".join(
                page.extract_text() or "" for page in reader.pages
            )
        except Exception:  # pragma: no cover - defensive boundary
            pass
    looks_like_transport_doc = bool(transport_pattern.search(detection_text))
    metadata["transport_document"] = looks_like_transport_doc
    should_ocr = (
        ctx.config.settings.parsing.ocr_all_pdfs and not looks_like_transport_doc
    ) or not native_ok
    if should_ocr:
        ocr_result = ctx.ocr.recognize(path, digest)
        metadata["ocr_used"] = True
        metadata["ocr_success"] = ocr_result.success
        metadata["ocr_cached"] = ocr_result.cached
        metadata["ocr_error"] = ocr_result.error
        metadata["ocr_order_type"] = ocr_result.order_type
        if ocr_result.success:
            metadata["ocr"] = ocr_result.prompt_payload()
            ocr_lines = ["OCR 结构化结果：", json.dumps(ocr_result.prompt_payload(), ensure_ascii=False)]
            blocks.append(
                ContentBlock(
                    kind="text",
                    location="ocr:structured",
                    text="\n".join(ocr_lines),
                    metadata={"source": "ocr"},
                )
            )
            charge_rows: list[list[str]] = [
                ["charge_item", "amount", "currency", "chargeCtr", "amount_raw", "correction"]
            ]
            for charge in ocr_result.all_charges():
                charge_rows.append(
                    [
                        charge.charge_item or "",
                        str(charge.amount) if charge.amount is not None else (charge.amount_raw or ""),
                        charge.currency or "",
                        charge.charge_ctr or "",
                        charge.amount_raw or "",
                        charge.correction_note or "",
                    ]
                )
            if len(charge_rows) > 1:
                blocks.append(
                    ContentBlock(
                        kind="table",
                        location="ocr:charges",
                        rows=charge_rows,
                        metadata={"source": "ocr", "confidence": 0.7},
                    )
                )
        elif not native_ok:
            parse_error = parse_error or f"PDF 无可提取文本且 OCR 失败: {ocr_result.error}"
    else:
        metadata["ocr_used"] = False

    return build_document(
        path=path,
        source_type=SourceType.PDF,
        sha256=digest,
        blocks=blocks,
        parent_document_id=parent_document_id,
        metadata=metadata,
        parse_error=parse_error,
    )
