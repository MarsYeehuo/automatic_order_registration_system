from __future__ import annotations

import json
from pathlib import Path

from ..models import ContentBlock, NormalizedDocument, SourceType
from ..utils import sha256_file
from .base import ParseContext, build_document


def parse_image(path: Path, ctx: ParseContext, parent_document_id: str | None) -> NormalizedDocument:
    digest = sha256_file(path)
    blocks: list[ContentBlock] = []
    metadata: dict = {"parser": "ocr"}
    parse_error: str | None = None
    result = ctx.ocr.recognize(path, digest)
    metadata["ocr_used"] = True
    metadata["ocr_success"] = result.success
    metadata["ocr_cached"] = result.cached
    metadata["ocr_error"] = result.error
    metadata["ocr_order_type"] = result.order_type
    if result.success:
        payload = result.prompt_payload()
        metadata["ocr"] = payload
        blocks.append(
            ContentBlock(
                kind="text",
                location="ocr:structured",
                text=json.dumps(payload, ensure_ascii=False, indent=2),
                metadata={"source": "ocr"},
            )
        )
        charge_rows: list[list[str]] = [
            ["charge_item", "amount", "currency", "chargeCtr", "amount_raw", "correction"]
        ]
        for charge in result.all_charges():
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
    else:
        parse_error = f"图片 OCR 失败: {result.error}"

    return build_document(
        path=path,
        source_type=SourceType.IMAGE,
        sha256=digest,
        blocks=blocks,
        parent_document_id=parent_document_id,
        metadata=metadata,
        parse_error=parse_error,
    )
