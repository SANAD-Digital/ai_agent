"""files.read — read a file: one the user attached in this chat, or one attached
to an ERP document the user can read (e.g. the PDF on a journal entry).

Without `file`: lists every attachment of the conversation (name, type,
size, which message). With `file`: returns its readable content — text for
PDF/Word/Excel/CSV, OCR for images and scanned pages (RapidOCR locally,
Gemini when configured). A document's file follows that document's read
permission; another conversation's attachments are never readable.
"""

import frappe
from frappe.utils import cint

MAX_CHARS = 40_000


def _session(permission_context):
    run = getattr(permission_context, "run_name", None)
    if not run:
        return None
    return frappe.db.get_value("AI Run", run, "chat_session")


def _attachments(session):
    messages = frappe.get_all("AI Chat Message", filters={"session": session, "role": "user"}, fields=["name", "seq"], order_by="seq asc")
    seq_of = {m.name: m.seq for m in messages}
    if not seq_of:
        return []
    rows = frappe.get_all(
        "File",
        filters={"attached_to_doctype": "AI Chat Message", "attached_to_name": ("in", list(seq_of)), "is_folder": 0},
        fields=["name", "file_name", "file_size", "attached_to_name", "creation"],
        order_by="creation asc",
    )
    return [
        {"file": r.name, "file_name": r.file_name, "size_kb": round(cint(r.file_size) / 1024), "message_seq": seq_of.get(r.attached_to_name), "uploaded": str(r.creation)[:16]}
        for r in rows
    ]


def _document_file(wanted, arguments, user):
    """A File attached to an ERP document the user may read (or an unattached file the user
    uploaded). Never another conversation's attachment. Returns (file name, error)."""
    filters = {"is_folder": 0}
    doctype, docname = arguments.get("doctype"), arguments.get("document_name")
    if doctype and docname:
        filters.update({"attached_to_doctype": doctype, "attached_to_name": docname})
    name = frappe.db.get_value("File", {**filters, "name": wanted}, "name") or frappe.db.get_value("File", {**filters, "file_name": wanted}, "name")
    if not name:
        return None, None
    f = frappe.db.get_value("File", name, ["attached_to_doctype", "attached_to_name", "owner"], as_dict=True)
    if f.attached_to_doctype == "AI Chat Message":
        return None, "That file belongs to another conversation."
    if f.attached_to_doctype and f.attached_to_name:
        if not frappe.db.exists(f.attached_to_doctype, f.attached_to_name) or not frappe.has_permission(f.attached_to_doctype, "read", doc=f.attached_to_name, user=user):
            return None, f"No read permission on {f.attached_to_doctype} {f.attached_to_name}."
        return name, None
    if f.owner != user:
        return None, "That file is not attached to a document you can read."
    return name, None


def _read_pages(doc, spec, vision=None):
    """Some pages of a PDF: a long scanned file is read in passes, each within a time budget."""
    from ai_agent.runtime.file_context import FileContextService, _ranges, parse_pages

    if not (doc.file_name or "").lower().endswith(".pdf"):
        return {"error": "`pages` is for PDF files; read other files without it."}
    pages = parse_pages(spec)
    if not pages:
        return {"error": f"`pages` must be page numbers and ranges such as \"11-21\" or \"1,4,7-9\", not {spec!r}."}
    texts, read, total = FileContextService(vision).read_pdf_pages(doc, pages)
    content, shown = "", []
    for n in read:
        part = f"[page {n}]\n{texts.get(n) or '(no text found on this page)'}\n\n"
        if shown and len(content) + len(part) > MAX_CHARS:
            break
        content += part
        shown.append(n)
    out = {"file": doc.name, "file_name": doc.file_name, "page_count": total, "pages_read": _ranges(shown), "content": content.strip()}
    missing = [n for n in pages if n <= total and n not in shown]
    if missing:
        out["pages_not_read"] = _ranges(missing)
        out["note"] = f"Not read yet (time or size limit of one call): call files__read again with pages=\"{out['pages_not_read']}\"."
    return out


def handle_read(arguments, permission_context):
    user = permission_context.user
    session = _session(permission_context)
    if session and frappe.db.get_value("AI Chat Session", session, "user") != user:
        return {"error": "Not your conversation."}
    files = _attachments(session) if session else []
    wanted = (arguments.get("file") or "").strip()
    if not wanted:
        if not session:
            return {"error": "Give `file` (and the document's doctype and name) to read a document's file."}
        return {"attachments": files, "count": len(files), "note": "Call files__read with `file` (the id or the file name) to read one."}
    match = next((f for f in files if wanted in (f["file"], f["file_name"])), None)
    if match:
        file_name = match["file"]
    else:
        file_name, error = _document_file(wanted, arguments, user)
        if not file_name:
            return {"error": error or f"No file '{wanted}' in this conversation or on a document you can read.", "attachments": files}

    from ai_agent.engine.context.files import extracted_text

    doc = frappe.get_doc("File", file_name)
    match = {"file": doc.name, "file_name": doc.file_name}
    from ai_agent.runtime import vision_ocr

    vision = vision_ocr.reader_of_run(getattr(permission_context, "run_name", None))
    if arguments.get("pages"):
        return _read_pages(doc, arguments["pages"], vision)
    if arguments.get("refresh"):
        frappe.cache.delete_value(f"ai_agent:file_summary:{doc.name}:{doc.modified}:{vision_ocr.tag(vision)}")
    text = extracted_text(doc, vision) or ""
    truncated = len(text) > MAX_CHARS
    return {"file": match["file"], "file_name": match["file_name"], "content": text[:MAX_CHARS], "truncated": truncated}
