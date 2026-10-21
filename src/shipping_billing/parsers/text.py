from __future__ import annotations

from pathlib import Path

from ..models import ContentBlock, NormalizedDocument, SourceType
from ..utils import sha256_file
from .base import ParseContext, build_document, read_text_auto


def parse_text(path: Path, ctx: ParseContext, parent_document_id: str | None) -> NormalizedDocument:
    digest = sha256_file(path)
    parse_error: str | None = None
    blocks: list[ContentBlock] = []
    try:
        text = read_text_auto(path)
        if text.strip():
            blocks.append(
                ContentBlock(kind="text", location="text:1", text=text, metadata={"source": "txt"})
            )
        else:
            parse_error = "文本文件为空"
    except Exception as exc:
        parse_error = f"文本读取失败: {type(exc).__name__}: {exc}"
    return build_document(
        path=path,
        source_type=SourceType.TXT,
        sha256=digest,
        blocks=blocks,
        parent_document_id=parent_document_id,
        metadata={"parser": "text"},
        parse_error=parse_error,
    )
