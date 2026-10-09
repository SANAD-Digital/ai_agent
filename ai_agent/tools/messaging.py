"""Email plugin: the agent may only *request* a send; it goes out after the user approves.

The request becomes an AI Approval Request of type "Approved Method" with
payload {"method": "send_email", ...}. The approval engine checks it again
with `assert_email_allowed` before executing `send_email`, so a payload cannot
be widened between request and approval (the payload hash is also checked).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import escape_html, validate_email_address

METHOD = "send_email"
MAX_RECIPIENTS = 10
MAX_ATTACHMENTS = 5


def _emails(value):
    if isinstance(value, str):
        value = [v for v in value.replace(";", ",").split(",")]
    out = []
    for item in value or []:
        email = validate_email_address(str(item or "").strip(), throw=False)
        if email and email not in out:
            out.append(email)
    return out


def outgoing_account_ready():
    return bool(frappe.db.exists("Email Account", {"enable_outgoing": 1}))


def build_payload(arguments):
    """Normalise the model's arguments into the approval payload (or raise ValidationError)."""
    recipients = _emails(arguments.get("recipients"))
    cc = [e for e in _emails(arguments.get("cc")) if e not in recipients]
    subject = str(arguments.get("subject") or "").strip()[:200]
    message = str(arguments.get("message") or "").strip()
    if not recipients:
        frappe.throw(_("لا يوجد بريد صحيح للمستلم."))
    if len(recipients) + len(cc) > MAX_RECIPIENTS:
        frappe.throw(_("عدد المستلمين أكثر من {0}.").format(MAX_RECIPIENTS))
    if not subject or not message:
        frappe.throw(_("الموضوع ونص الرسالة مطلوبان."))
    attachments = [str(a).strip() for a in (arguments.get("attachments") or []) if str(a or "").strip()][:MAX_ATTACHMENTS]
    return {
        "method": METHOD,
        "recipients": recipients,
        "cc": cc,
        "subject": subject,
        "message": message[:20000],
        "attachments": attachments,
        "reference_doctype": arguments.get("reference_doctype") or None,
        "reference_name": arguments.get("reference_name") or None,
    }


def _attachment_files(urls, user):
    """File names for attachment URLs the user owns (their exports, uploads)."""
    names = []
    for url in urls or []:
        row = frappe.db.get_value("File", {"file_url": url}, ["name", "owner", "attached_to_doctype", "attached_to_name"], as_dict=True)
        if not row:
            raise frappe.PermissionError(_("المرفق {0} غير موجود.").format(url))
        # the requester's own export, or a file the checking user (requester or approver) may read
        owned = row.owner == user
        if not owned and row.attached_to_doctype == "AI Run":
            owned = frappe.db.get_value("AI Run", row.attached_to_name, "user") == user
        if not owned and not frappe.has_permission("File", "read", doc=row.name, user=user):
            raise frappe.PermissionError(_("لا يمكنك إرفاق الملف {0}.").format(url))
        names.append(row.name)
    return names


def assert_email_allowed(payload, user):
    """Raise unless `user` may send this email."""
    if not outgoing_account_ready():
        raise frappe.ValidationError(_("لا يوجد حساب بريد صادر مُعدّ في النظام. أضِف Email Account بالإرسال مفعّل أولاً."))
    if not frappe.has_permission("Communication", "create", user=user):
        raise frappe.PermissionError(_("ليست لديك صلاحية إرسال البريد."))
    ref_dt, ref_name = payload.get("reference_doctype"), payload.get("reference_name")
    if ref_dt or ref_name:
        if not (ref_dt and ref_name and frappe.db.exists(ref_dt, ref_name)):
            raise frappe.PermissionError(_("المستند المرتبط غير موجود."))
        if not frappe.has_permission(ref_dt, "read", doc=ref_name, user=user):
            raise frappe.PermissionError(_("لا تملك صلاحية على المستند المرتبط."))
    _attachment_files(payload.get("attachments"), user)


def summary(payload):
    to = "، ".join(payload["recipients"])
    files = len(payload.get("attachments") or [])
    text = _("إرسال بريد إلى {0}: «{1}»").format(to, payload["subject"])
    return text + (_(" مع {0} مرفق").format(files) if files else "")


def send_email(payload, approval):
    """Executed by the approval engine after the decision."""
    user = approval.requested_by
    assert_email_allowed(payload, user)
    from frappe.core.doctype.communication.email import make

    content = "<br>".join(escape_html(line) for line in payload["message"].splitlines())
    result = make(
        recipients=", ".join(payload["recipients"]),
        cc=", ".join(payload.get("cc") or []) or None,
        subject=payload["subject"],
        content=content,
        doctype=payload.get("reference_doctype"),
        name=payload.get("reference_name"),
        send_email=True,
        attachments=_attachment_files(payload.get("attachments"), user),
    )
    return {"communication": result.get("name"), "recipients": payload["recipients"], "status": "queued"}
