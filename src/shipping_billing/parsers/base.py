from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..models import ContentBlock, NormalizedDocument, SourceRef, SourceType
from ..ocr import OCRClient
from ..utils import sha256_file


@dataclass
class ParseContext:
    config: AppConfig
    ocr: OCRClient
    raw_dir: Path
    seen_hashes: dict[str, str] = field(default_factory=dict)
    duplicates: list[tuple[str, str]] = field(default_factory=list)

    def document_id(self, sha256: str) -> str:
        return f"doc_{sha256[:16]}"


_EXT_MAP = {
    ".eml": SourceType.EML,
    ".pdf": SourceType.PDF,
    ".png": SourceType.IMAGE,
    ".jpg": SourceType.IMAGE,
    ".jpeg": SourceType.IMAGE,
    ".tif": SourceType.IMAGE,
    ".tiff": SourceType.IMAGE,
    ".bmp": SourceType.IMAGE,
    ".xlsx": SourceType.XLSX,
    ".xls": SourceType.XLS,
    ".docx": SourceType.DOCX,
    ".doc": SourceType.DOC,
    ".txt": SourceType.TXT,
    ".json": SourceType.JSON,
}

_MIME_EXT = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/tiff": ".tiff",
    "image/bmp": ".bmp",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "message/rfc822": ".eml",
    "text/plain": ".txt",
}


def sniff_source_type(path: str | Path, declared_name: str | None = None) -> SourceType:
    source = Path(path)
    extension = source.suffix.lower()
    if declared_name:
        declared_ext = Path(declared_name).suffix.lower()
        if declared_ext in _EXT_MAP:
            extension = declared_ext
    try:
        head = source.open("rb").read(16)
    except OSError:
        return _EXT_MAP.get(extension, SourceType.OTHER)

    if head.startswith(b"%PDF"):
        return SourceType.PDF
    if head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8\xff"):
        return SourceType.IMAGE
    if head.startswith((b"II*\x00", b"MM\x00*", b"BM")):
        return SourceType.IMAGE
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(source) as archive:
                names = archive.namelist()
            if any(name.startswith("word/") for name in names):
                return SourceType.DOCX
            if any(name.startswith("xl/") for name in names):
                return SourceType.XLSX
        except zipfile.BadZipFile:
            pass
        return _EXT_MAP.get(extension, SourceType.OTHER)
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        if extension == ".xls":
            return SourceType.XLS
        return SourceType.DOC
    if head.lstrip().lower().startswith((b"from:", b"received:", b"return-path:")):
        return SourceType.EML
    return _EXT_MAP.get(extension, SourceType.OTHER)


def extension_for_mime(mime_type: str | None) -> str | None:
    if not mime_type:
        return None
    return _MIME_EXT.get(mime_type.split(";")[0].strip().lower())


def safe_filename(name: str, fallback: str = "attachment.bin") -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKC", name).replace("\xa0", " ")
    candidate = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", normalized)
    candidate = re.sub(r"\s+", " ", candidate).strip(" .")
    return candidate or fallback


def build_document(
    *,
    path: Path,
    source_type: SourceType,
    sha256: str,
    blocks: list[ContentBlock],
    parent_document_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    parse_error: str | None = None,
    source_file: str | None = None,
    location: str = "file",
) -> NormalizedDocument:
    return NormalizedDocument(
        document_id=f"doc_{sha256[:16]}",
        parent_document_id=parent_document_id,
        source=SourceRef(
            source_file=source_file or path.name,
            source_type=source_type,
            sha256=sha256,
            location=location,
        ),
        blocks=blocks,
        parser_metadata=metadata or {},
        parse_error=parse_error,
    )


def read_text_auto(path: str | Path) -> str:
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def stored_attachment_path(ctx: ParseContext, sha256: str, filename: str) -> Path:
    folder = ctx.raw_dir / sha256[:16]
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / safe_filename(filename)
    if not target.exists() or target.stat().st_size == 0:
        return target
    return target


def file_sha(path: Path) -> str:
    return sha256_file(path)
