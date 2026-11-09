"""Attaching files to documents, always through an approval.

- `actions.request_attach_files`: files on an existing document. An "Approved Method" request (`attach_files`):
  a person always decides, auto mode never runs it.
- `attach_files` on actions.request_create_draft / actions.request_update: the files go on the document when that
  request runs, in the same transaction. The usual case: a document made from data extracted from a file the user
  attached (a scanned invoice), which keeps its source.

A file is one of this conversation's attachments, or a file on a document the requester can read (chat_files). It
is attached as a new File record pointing at the same stored file: Frappe removes the stored file only when no File
record uses it any more.
"""

from __future__ import annotations

import frappe
from frappe import _

from ai_agent.runtime.exceptions import PermissionDenied

METHOD = "attach_files"
KEY = "_attach_files"  # in a Create / Update payload: never a document field, and the cards skip "_" keys
WITH_FILES = ("Create", "Update")
MAX_FILES = 10


def resolve(refs, user, run_name=None):
    """[{file, file_name}] for the ids or names the model gave. Raises PermissionDenied for a file the user may not use."""
    from ai_agent.tools import chat_files

    if isinstance(refs, str):
        refs = [refs]
    refs = [str(r).strip() for r in refs or [] if str(r or "").strip()]
    if len(refs) > MAX_FILES:
        raise PermissionDenied(_("At most {0} files per request.").format(MAX_FILES))
    session = frappe.db.get_value("AI Run", run_name, "chat_session") if run_name else None
    if session and frappe.db.get_value("AI Chat Session", session, "user") != user:
        session = None  # another user's conversation: its attachments are not this user's
    mine = chat_files._attachments(session) if session else []
    out = []
    for ref in refs:
        match = next((f for f in mine if ref in (f["file"], f["file_name"])), None)
        name = match["file"] if match else None
        if not name:
            name, error = chat_files._document_file(ref, {}, user)
            if not name:
                raise PermissionDenied(error or _("No file {0} in this conversation or on a document you can read.").format(ref))
        if all(o["file"] != name for o in out):
            out.append({"file": name, "file_name": frappe.db.get_value("File", name, "file_name")})
    return out


def bind(action_type, payload, user, run_name=None):
    """The request's files, resolved and checked, in a Create / Update payload; other actions never carry them."""
    if not isinstance(payload, dict) or KEY not in payload:
        return payload
    payload = dict(payload)
    refs = payload.pop(KEY)
    if action_type in WITH_FILES:
        files = resolve([f.get("file") if isinstance(f, dict) else f for f in (refs or [])], user, run_name)
        if files:
            payload[KEY] = files
    return payload


def with_files(payload, arguments):
    """The tool's `attach_files` argument, carried in the payload until the request is created."""
    files = (arguments or {}).get("attach_files")
    if files:
        payload = dict(payload)
        payload[KEY] = files
    return payload


def split(payload):
    """(the document's own payload, the files to attach after the write)."""
    payload = dict(payload or {})
    return payload, payload.pop(KEY, None) or []


def names(payload):
    """The file names a request attaches, for the cards."""
    if not isinstance(payload, dict):
        return []
    files = payload.get("files") if payload.get("method") == METHOD else payload.get(KEY)
    return [f.get("file_name") or f.get("file") for f in files or [] if isinstance(f, dict)]


def attach(files, doctype, name):
    """The files on the document (skipping one it already has). Raises when a file was deleted since the request."""
    attached = []
    for f in files or []:
        src = frappe.db.get_value("File", f.get("file"), ["file_url", "file_name", "is_private"], as_dict=True)
        if not src or not src.file_url:
            raise frappe.ValidationError(_("The file {0} no longer exists.").format(f.get("file_name") or f.get("file")))
        existing = frappe.db.get_value("File", {"file_url": src.file_url, "attached_to_doctype": doctype, "attached_to_name": name}, "name")
        if not existing:
            doc = frappe.get_doc({"doctype": "File", "file_url": src.file_url, "file_name": src.file_name, "is_private": src.is_private,
                "attached_to_doctype": doctype, "attached_to_name": name})
            doc.insert(ignore_permissions=True)  # the write permission on the document is checked by the caller
            existing = doc.name
        attached.append({"file": existing, "file_name": src.file_name})
    return attached


# -- actions.request_attach_files ------------------------------------------------------------------------------------


def build_payload(arguments, user, run_name=None):
    doctype, name = arguments.get("doctype"), arguments.get("document_name")
    if not doctype or not name:
        frappe.throw(_("doctype and document_name are required."))
    files = resolve(arguments.get("files"), user, run_name)
    if not files:
        frappe.throw(_("Name at least one file to attach."))
    return {"method": METHOD, "doctype": doctype, "name": name, "files": files}


def assert_attach_allowed(payload, user):
    """Raise unless `user` may attach files to the document: its write permission, on a document the agent may change."""
    from ai_agent.runtime import policy

    doctype, name = payload.get("doctype"), payload.get("name")
    if not (doctype and name and frappe.db.exists(doctype, name)):
        raise frappe.PermissionError(_("The document was not found."))
    try:
        policy.assert_write_target_allowed(doctype, {})
    except policy.PolicyViolation as exc:
        raise frappe.PermissionError(str(exc))
    if not frappe.has_permission(doctype, "write", doc=name, user=user):
        raise frappe.PermissionError(_("You do not have permission to change {0} {1}.").format(doctype, name))
    if not payload.get("files"):
        raise frappe.ValidationError(_("Name at least one file to attach."))


def run(payload, approval):
    """Executed by the approval engine after the decision (the approver was checked by the engine; the requester here)."""
    assert_attach_allowed(payload, approval.requested_by)
    attached = attach(payload["files"], payload["doctype"], payload["name"])
    return {"doctype": payload["doctype"], "document_name": payload["name"], "attached": attached}


def summary(payload):
    return _("Attach {0} to {1} {2}").format("، ".join(names(payload)), _(payload["doctype"]), payload["name"])
