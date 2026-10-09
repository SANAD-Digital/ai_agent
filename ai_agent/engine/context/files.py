"""Attachments of a user message as native blocks.

Images go as image blocks when the model can see; PDFs as document blocks
when the provider takes them natively (Anthropic), otherwise as extracted
text through the existing FileContextService (CSV, XLSX, DOCX, OCR).
"""

from __future__ import annotations

import base64
import os

import frappe

from ai_agent.engine.types import DOCUMENT, IMAGE, TEXT, Block, clip

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 40_000


def message_files(message_name):
    names = frappe.get_all(
        "File",
        filters={"attached_to_doctype": "AI Chat Message", "attached_to_name": message_name, "is_folder": 0},
        pluck="name",
        order_by="creation asc",
    )
    return [frappe.get_doc("File", n) for n in names]


def blocks_for(message_name, caps):
    """[Block] for every attachment of a user message, according to provider caps."""
    blocks = []
    images = 0
    for f in message_files(message_name):
        filename = f.file_name or f.name
        ext = os.path.splitext(filename)[1].lower()
        try:
            if ext in IMAGE_TYPES and caps.supports_vision and images < 4:
                content = f.get_content()
                if isinstance(content, str):
                    content = content.encode()
                if len(content) <= MAX_IMAGE_BYTES:
                    blocks.append(Block(kind=IMAGE, name=filename, data={"media_type": IMAGE_TYPES[ext], "data": base64.b64encode(content).decode()}))
                    images += 1
                    continue
            if ext == ".pdf" and caps.supports_pdf:
                content = f.get_content()
                if isinstance(content, str):
                    content = content.encode()
                if len(content) <= MAX_PDF_BYTES:
                    blocks.append(Block(kind=DOCUMENT, name=filename, data={"media_type": "application/pdf", "data": base64.b64encode(content).decode()}))
                    continue
            text = extracted_text(f)
            blocks.append(Block(kind=DOCUMENT, name=filename, text=f"<<<ATTACHED_FILE name=\"{filename}\">>>\n{text}\n<<<END_ATTACHED_FILE>>>"))
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"AI chat attachment {f.name}")
            blocks.append(Block(kind=TEXT, text=f"[Attachment {filename} could not be read]"))
    return blocks


def extracted_text(file_doc):
    from ai_agent.runtime.file_context import FileContextService

    summary = FileContextService().summarize_file(file_doc) or ""
    return clip(summary, MAX_TEXT_CHARS) or "(no readable text)"


