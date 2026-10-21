from __future__ import annotations

from pathlib import Path

from ..config import AppConfig
from ..models import ContentBlock, NormalizedDocument, SourceType
from ..ocr import OCRClient
from ..utils import sha256_file
from .base import ParseContext, build_document, sniff_source_type
from .eml import parse_eml
from .excel import parse_excel
from .image import parse_image
from .pdf import parse_pdf
from .text import parse_text
from .word import parse_word

PARSER_BY_TYPE = {
    SourceType.PDF: parse_pdf,
    SourceType.IMAGE: parse_image,
    SourceType.XLSX: parse_excel,
    SourceType.XLS: parse_excel,
    SourceType.DOCX: parse_word,
    SourceType.DOC: parse_word,
    SourceType.TXT: parse_text,
}


class DocumentParser:
    def __init__(self, config: AppConfig, ocr: OCRClient) -> None:
        self.config = config
        self.ocr = ocr
        self.ctx = ParseContext(
            config=config,
            ocr=ocr,
            raw_dir=config.settings.resolve_path(
                self.config.settings.ocr.raw_dir
            ).parent / "raw_files",
        )
        self.manifest: list[dict] = []

    def parse_paths(self, paths: list[str | Path]) -> list[NormalizedDocument]:
        files: list[Path] = []
        for raw in paths:
            path = Path(raw)
            if path.is_dir():
                files.extend(
                    item
                    for item in sorted(path.rglob("*"))
                    if item.is_file() and not item.name.startswith(".")
                )
            elif path.is_file():
                files.append(path)
        documents: list[NormalizedDocument] = []
        for path in files:
            documents.extend(self.parse_file(path, parent_document_id=None, depth=0))
        return documents

    def parse_file(
        self,
        path: Path,
        parent_document_id: str | None,
        depth: int,
        display_name: str | None = None,
        declared_type: SourceType | None = None,
    ) -> list[NormalizedDocument]:
        try:
            digest = sha256_file(path)
        except OSError as exc:
            digest = ""
            document = self._error_document(
                path,
                declared_type or SourceType.OTHER,
                digest,
                f"文件读取失败: {exc}",
                parent_document_id,
                display_name,
            )
            self.manifest.append(
                {"path": str(path), "sha256": digest, "error": document.parse_error}
            )
            return [document]

        if digest in self.ctx.seen_hashes:
            duplicate_of = self.ctx.seen_hashes[digest]
            self.ctx.duplicates.append((str(path), duplicate_of))
            self.manifest.append(
                {
                    "path": str(path),
                    "sha256": digest,
                    "duplicate_of": duplicate_of,
                    "parent_document_id": parent_document_id,
                }
            )
            return []
        self.ctx.seen_hashes[digest] = f"doc_{digest[:16]}"

        source_type = declared_type or sniff_source_type(path, display_name)
        max_bytes = self.config.settings.parsing.max_attachment_mb * 1024 * 1024
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > max_bytes:
            document = self._error_document(
                path,
                source_type,
                digest,
                f"文件超过解析上限 {self.config.settings.parsing.max_attachment_mb}MB",
                parent_document_id,
                display_name,
            )
            self.manifest.append(
                {"path": str(path), "sha256": digest, "source_type": source_type.value,
                 "error": document.parse_error}
            )
            return [document]

        try:
            if source_type == SourceType.EML:
                result = parse_eml(
                    path,
                    self.ctx,
                    parent_document_id,
                    depth,
                    display_name=display_name,
                )
                documents = [result.document]
                for attachment_path, attachment_name in result.attachments:
                    documents.extend(
                        self.parse_file(
                            attachment_path,
                            parent_document_id=result.document.document_id,
                            depth=depth + 1,
                            display_name=attachment_name,
                        )
                    )
                self.manifest.append(
                    {
                        "path": str(path),
                        "sha256": digest,
                        "source_type": source_type.value,
                        "documents": len(documents),
                        "attachments": len(result.attachments),
                        "parent_document_id": parent_document_id,
                    }
                )
                return documents

            parser = PARSER_BY_TYPE.get(source_type)
            if parser is None:
                document = self._error_document(
                    path,
                    source_type,
                    digest,
                    f"不支持的文件类型: {source_type.value}",
                    parent_document_id,
                    display_name,
                )
                self.manifest.append(
                    {"path": str(path), "sha256": digest, "source_type": source_type.value,
                     "error": document.parse_error}
                )
                return [document]
            document = parser(path, self.ctx, parent_document_id)
            if display_name:
                document.source.source_file = display_name
                document.parser_metadata.setdefault("attachment_name", display_name)
            self.manifest.append(
                {
                    "path": str(path),
                    "sha256": digest,
                    "source_type": source_type.value,
                    "parent_document_id": parent_document_id,
                    "parse_error": document.parse_error,
                    "ocr_used": document.parser_metadata.get("ocr_used"),
                    "ocr_success": document.parser_metadata.get("ocr_success"),
                }
            )
            return [document]
        except Exception as exc:  # pragma: no cover - defensive boundary
            document = self._error_document(
                path,
                source_type,
                digest,
                f"解析异常: {type(exc).__name__}: {exc}",
                parent_document_id,
                display_name,
            )
            self.manifest.append(
                {"path": str(path), "sha256": digest, "source_type": source_type.value,
                 "error": document.parse_error}
            )
            return [document]

    def _error_document(
        self,
        path: Path,
        source_type: SourceType,
        digest: str,
        error: str,
        parent_document_id: str | None,
        display_name: str | None,
    ) -> NormalizedDocument:
        digest = digest or sha256_file(path) if path.exists() else digest or "0" * 64
        blocks: list[ContentBlock] = []
        return build_document(
            path=path,
            source_type=source_type,
            sha256=digest,
            blocks=blocks,
            parent_document_id=parent_document_id,
            metadata={"parser": "error"},
            parse_error=error,
            source_file=display_name or path.name,
        )
