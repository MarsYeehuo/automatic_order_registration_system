from __future__ import annotations

import email
import html
import re
from dataclasses import dataclass, field
from email import policy
from email.message import Message
from pathlib import Path

from ..models import ContentBlock, NormalizedDocument, SourceType
from ..utils import sha256_bytes
from .base import ParseContext, build_document, extension_for_mime, safe_filename


@dataclass
class EmlParseResult:
    document: NormalizedDocument
    attachments: list[tuple[Path, str]] = field(default_factory=list)


def _html_to_text(value: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", text)
    text = re.sub(r"(?i)<td[^>]*>", "\t", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _collect_bodies(message: Message) -> tuple[str, str | None]:
    plain: list[str] = []
    html_parts: list[str] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        disposition = part.get_content_disposition()
        if disposition == "attachment":
            continue
        if content_type == "text/plain":
            try:
                plain.append(part.get_content())
            except (LookupError, UnicodeDecodeError):
                payload = part.get_payload(decode=True) or b""
                plain.append(payload.decode("utf-8", errors="replace"))
        elif content_type == "text/html":
            try:
                html_parts.append(part.get_content())
            except (LookupError, UnicodeDecodeError):
                payload = part.get_payload(decode=True) or b""
                html_parts.append(payload.decode("utf-8", errors="replace"))
    html_text = _html_to_text("\n".join(html_parts)) if html_parts else None
    body = "\n".join(x.strip() for x in plain if x and x.strip())
    return body, html_text


def parse_eml(
    path: Path,
    ctx: ParseContext,
    parent_document_id: str | None,
    depth: int,
    display_name: str | None = None,
) -> EmlParseResult:
    raw = path.read_bytes()
    digest = sha256_bytes(raw)
    message = email.message_from_bytes(raw, policy=policy.default)
    subject = str(message.get("subject") or "")
    sender = str(message.get("from") or "")
    recipient = str(message.get("to") or "")
    cc = str(message.get("cc") or "")
    date_header = str(message.get("date") or "")
    message_id = str(message.get("message-id") or "")
    body, html_text = _collect_bodies(message)
    if not body and html_text:
        body = html_text

    header_text = "\n".join(
        line
        for line in [
            f"Subject: {subject}" if subject else "",
            f"From: {sender}" if sender else "",
            f"To: {recipient}" if recipient else "",
            f"Cc: {cc}" if cc else "",
            f"Date: {date_header}" if date_header else "",
            f"Message-ID: {message_id}" if message_id else "",
        ]
        if line
    )
    blocks = [
        ContentBlock(
            kind="text",
            location="email:headers",
            text=header_text,
            metadata={"source": "eml"},
        )
    ]
    if body.strip():
        blocks.append(
            ContentBlock(
                kind="text",
                location="email:body",
                text=body.strip(),
                metadata={"source": "eml"},
            )
        )
    if html_text and html_text != body:
        blocks.append(
            ContentBlock(
                kind="text",
                location="email:body:html",
                text=html_text,
                metadata={"source": "eml_html"},
            )
        )

    attachments: list[tuple[Path, str]] = []
    attachment_meta: list[dict] = []
    skipped: list[dict] = []
    supported = {ext.lower() for ext in ctx.config.settings.parsing.supported_extensions}
    max_bytes = ctx.config.settings.parsing.max_attachment_mb * 1024 * 1024
    inline_mode = ctx.config.settings.parsing.inline_images
    for index, part in enumerate(message.walk(), start=1):
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        content_id = part.get("Content-ID")
        is_inline = bool(content_id) and disposition != "attachment"
        if is_inline and inline_mode != "process":
            continue
        if not filename and disposition != "attachment":
            continue
        if not filename:
            filename = f"inline_{index}{extension_for_mime(part.get_content_type()) or '.bin'}"
        filename = safe_filename(str(filename))
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        if len(payload) > max_bytes:
            skipped.append({"filename": filename, "reason": "attachment exceeds size limit"})
            continue
        extension = Path(filename).suffix.lower()
        if not extension:
            extension = extension_for_mime(part.get_content_type()) or ""
            if extension:
                filename += extension
                extension = extension.lower()
        if extension and extension not in supported:
            skipped.append({"filename": filename, "reason": f"unsupported extension {extension}"})
            continue
        if depth >= ctx.config.settings.parsing.max_eml_depth:
            skipped.append({"filename": filename, "reason": "max_eml_depth reached"})
            continue
        attachment_sha = sha256_bytes(payload)
        folder = ctx.raw_dir / attachment_sha[:16]
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / safe_filename(filename)
        if not target.exists() or target.stat().st_size != len(payload):
            target.write_bytes(payload)
        attachments.append((target, filename))
        attachment_meta.append(
            {
                "filename": filename,
                "sha256": attachment_sha,
                "size": len(payload),
                "saved_to": str(target),
                "content_type": part.get_content_type(),
                "inline": is_inline,
            }
        )

    metadata = {
        "parser": "stdlib_email",
        "subject": subject,
        "from": sender,
        "to": recipient,
        "cc": cc,
        "date_header": date_header,
        "message_id": message_id,
        "attachments": attachment_meta,
        "skipped_attachments": skipped,
        "depth": depth,
    }
    document = build_document(
        path=path,
        source_type=SourceType.EML,
        sha256=digest,
        blocks=blocks,
        parent_document_id=parent_document_id,
        metadata=metadata,
        source_file=display_name or path.name,
    )
    return EmlParseResult(document=document, attachments=attachments)
