"""Ownership rules for the agent's conversation records.

Conversations (runs, chat sessions, chatbot threads, memories, attachment
sessions) are private to the user who owns them. System Managers may read
another user's conversation only when ``AI Settings.allow_admin_conversation_audit``
is enabled, and every such read is written to the standard Access Log.

Used both by the whitelisted endpoints and by the DocType permission hooks
(``has_permission`` / ``permission_query_conditions`` in hooks.py), so desk and
REST access follow the same rule as the agent UI.
"""

import frappe
from frappe import _
from frappe.utils import cint

AUDIT_SETTING = "allow_admin_conversation_audit"
AUDITOR_ROLE = "System Manager"
AUDIT_METHOD = "ai_agent.conversation_audit"

# DocType -> owner field, for records owned directly by a user.
OWNED_DOCTYPES = {
	"AI Run": "user",
	"AI Chat Session": "user",
	"AI Chat Message": "user",
	"AI Memory": "user",
}
# DocTypes owned through their AI Run.
RUN_LINKED_DOCTYPES = ("AI Tool Call",)


def admin_audit_enabled():
	try:
		return bool(cint(frappe.db.get_single_value("AI Settings", AUDIT_SETTING)))
	except Exception:
		return False


def can_audit_conversations(user=None):
	user = user or frappe.session.user
	return admin_audit_enabled() and AUDITOR_ROLE in frappe.get_roles(user)


def log_conversation_audit(doctype, name, owner, user=None):
	"""Record an auditor's read of someone else's conversation in Access Log."""
	user = user or frappe.session.user
	# has_permission runs several times per request; log each read once.
	seen = getattr(frappe.local, "ai_agent_audit_logged", None)
	if seen is None:
		seen = frappe.local.ai_agent_audit_logged = set()
	key = (user, doctype, name)
	if key in seen:
		return
	seen.add(key)
	try:
		frappe.get_doc(
			{
				"doctype": "Access Log",
				"user": user,
				"export_from": doctype,
				"reference_document": name,
				"method": AUDIT_METHOD,
				"filters": frappe.as_json({"conversation_owner": owner}),
			}
		).db_insert()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "AI Conversation Audit Log Failed")


def can_access_owned(owner, user=None, *, doctype=None, name=None, log=True):
	"""Owner always; an auditor only when auditing is enabled (and logged)."""
	user = user or frappe.session.user
	if owner and owner == user:
		return True
	if can_audit_conversations(user):
		if log:
			log_conversation_audit(doctype, name, owner, user)
		return True
	return False


def assert_owned_access(owner, doctype=None, name=None, message=None):
	if not can_access_owned(owner, doctype=doctype, name=name):
		frappe.throw(message or _("Not authorized to view this conversation."), frappe.PermissionError)


def owner_filters(user=None, field="user"):
	"""get_all filters limiting a listing to the user's own records."""
	user = user or frappe.session.user
	if can_audit_conversations(user):
		return {}
	return {field: user}


# ---------------------------------------------------------------------------
# DocType permission hooks
# ---------------------------------------------------------------------------


def _owner_of(doc):
	field = OWNED_DOCTYPES.get(doc.doctype)
	if field:
		return doc.get(field)
	if doc.doctype in RUN_LINKED_DOCTYPES:
		return frappe.db.get_value("AI Run", doc.get("run"), "user") if doc.get("run") else None
	if doc.doctype == "AI Message Block":
		return frappe.db.get_value("AI Chat Message", doc.get("message"), "user") if doc.get("message") else None
	return None


def has_owned_permission(doc, ptype=None, user=None, debug=False):
	user = user or frappe.session.user
	owner = _owner_of(doc)
	if owner == user:
		return True
	# Reads by an auditor are logged; writes to another user's conversation never allowed.
	if ptype in (None, "read", "select", "print", "email", "export", "report"):
		return can_access_owned(owner, user, doctype=doc.doctype, name=doc.name)
	return False


def has_approval_permission(doc, ptype=None, user=None, debug=False):
	"""Approval requests are a work queue: requester, run owner and approvers see them."""
	user = user or frappe.session.user
	if doc.get("requested_by") == user:
		return True
	if doc.get("run") and frappe.db.get_value("AI Run", doc.run, "user") == user:
		return True
	roles = set(frappe.get_roles(user))
	approver_role = frappe.db.get_single_value("AI Settings", "approver_role")
	return AUDITOR_ROLE in roles or bool(approver_role and approver_role in roles)


def _owned_condition(doctype, field, user):
	user = user or frappe.session.user
	if can_audit_conversations(user):
		return ""
	return f"(`tab{doctype}`.`{field}` = {frappe.db.escape(user)})"


def ai_run_query(user=None):
	return _owned_condition("AI Run", "user", user)


def ai_chat_session_query(user=None):
	return _owned_condition("AI Chat Session", "user", user)


def ai_memory_query(user=None):
	return _owned_condition("AI Memory", "user", user)


def ai_tool_call_query(user=None):
	user = user or frappe.session.user
	if can_audit_conversations(user):
		return ""
	return (
		"(`tabAI Tool Call`.`run` in (select `name` from `tabAI Run` "
		f"where `user` = {frappe.db.escape(user)}))"
	)


def ai_approval_request_query(user=None):
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))
	approver_role = frappe.db.get_single_value("AI Settings", "approver_role")
	if AUDITOR_ROLE in roles or (approver_role and approver_role in roles):
		return ""
	escaped = frappe.db.escape(user)
	return (
		f"(`tabAI Approval Request`.`requested_by` = {escaped} or `tabAI Approval Request`.`run` in "
		f"(select `name` from `tabAI Run` where `user` = {escaped}))"
	)


def ai_chat_message_query(user=None):
	return _owned_condition("AI Chat Message", "user", user)


def ai_message_block_query(user=None):
	user = user or frappe.session.user
	if can_audit_conversations(user):
		return ""
	return (
		"(`tabAI Message Block`.`message` in (select `name` from `tabAI Chat Message` "
		f"where `user` = {frappe.db.escape(user)}))"
	)


def has_provider_permission(doc, ptype=None, user=None, debug=False):
	user = user or frappe.session.user
	return user == "Administrator" or "System Manager" in frappe.get_roles(user)


def ai_provider_query(user=None):
	user = user or frappe.session.user
	return "" if user == "Administrator" or "System Manager" in frappe.get_roles(user) else "1=0"
