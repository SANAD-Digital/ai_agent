"""files.read — the files the user attached in this chat conversation.

Without `file`: lists every attachment of the conversation (name, type,
size, which message). With `file`: returns its readable content — text for
PDF/Word/Excel/CSV, OCR for images and scanned pages (RapidOCR locally,
Gemini when configured). Only the conversation of the running message,
only files the user attached there.
"""

import frappe
from frappe.utils import cint

MAX_CHARS = 40_000


def _session(permission_context):
    run = getattr(permission_context, "run_name", None)
    if not run:
        return None
    return frappe.db.get_value("AI Run", run, "session")


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


def handle_read(arguments, permission_context):
    session = _session(permission_context)
    if not session:
        return {"error": "files.read works only inside a chat conversation."}
    owner = frappe.db.get_value("AI Chat Session", session, "user")
    if owner != permission_context.user:
        return {"error": "Not your conversation."}
    files = _attachments(session)
    wanted = (arguments.get("file") or "").strip()
    if not wanted:
        return {"attachments": files, "count": len(files), "note": "Call files__read with `file` (the id or the file name) to read one."}
    match = next((f for f in files if wanted in (f["file"], f["file_name"])), None)
    if not match:
        return {"error": f"No attachment '{wanted}' in this conversation.", "attachments": files}

    from ai_agent.engine.context.files import extracted_text

    doc = frappe.get_doc("File", match["file"])
    if arguments.get("refresh"):
        frappe.cache().delete_value(f"ai_agent:file_summary:{doc.name}:{doc.modified}")
    text = extracted_text(doc) or ""
    truncated = len(text) > MAX_CHARS
    return {"file": match["file"], "file_name": match["file_name"], "content": text[:MAX_CHARS], "truncated": truncated}
