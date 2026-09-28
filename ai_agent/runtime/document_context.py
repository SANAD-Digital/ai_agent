"""Open-document context: the record the user is looking at when they ask.

The chatbot widget sends the current form's doctype and name. The document is
added to the model context only if the run's owner may read that exact row,
and only as a compact summary of permitted fields.
"""

import frappe

from ai_agent.runtime import masking
from ai_agent.runtime.permission_context import PermissionContext

MAX_FIELDS = 25
MAX_CHILD_ROWS = 5


def normalize(doctype, name):
	"""Validated (doctype, name) from untrusted widget input, or (None, None)."""
	doctype = str(doctype or "").strip()[:140]
	name = str(name or "").strip()[:140]
	if not doctype or not name or not frappe.db.exists("DocType", doctype):
		return None, None
	meta = frappe.get_meta(doctype)
	if meta.istable or meta.issingle:
		return None, None
	return doctype, name


def build(doctype, name, user):
	"""Markdown-ish summary of the open document, or "" when not readable."""
	doctype, name = normalize(doctype, name)
	if not doctype:
		return ""
	ctx = PermissionContext(user)
	if not ctx.has_doctype_permission(doctype, "read"):
		return ""
	try:
		doc = frappe.get_doc(doctype, name)
	except frappe.DoesNotExistError:
		return ""
	if not ctx.can_access_document(doc, "read"):
		return ""

	from ai_agent.tools.data import _child_rows, _is_blocked_field
	from ai_agent.tools.schema import _fields_by_usefulness

	lines = [f"{doctype} {doc.name} (docstatus {doc.docstatus})"]
	shown = 0
	tables = []
	for df in _fields_by_usefulness(doc.meta):
		if shown >= MAX_FIELDS:
			break
		if not df.fieldname or df.hidden or _is_blocked_field(df):
			continue
		if df.fieldtype in frappe.model.no_value_fields and df.fieldtype not in frappe.model.table_fields:
			continue
		if not ctx.check_field_access(doctype, df.fieldname, "read"):
			continue
		if df.fieldtype in frappe.model.table_fields:
			tables.append(df)
			continue
		value = doc.get(df.fieldname)
		if value in (None, "", 0) and not df.reqd:
			continue
		value = masking.mask({df.fieldname: value}, doctype)[df.fieldname]
		lines.append(f"- {df.fieldname}: {str(value)[:120]}")
		shown += 1
	for df in tables[:3]:
		children = doc.get(df.fieldname) or []
		rows = masking.mask(_child_rows(df.options, doctype, children[:MAX_CHILD_ROWS], ctx), doctype)
		if rows:
			compact = [{k: v for k, v in row.items() if v not in (None, "", 0)} for row in rows]
			lines.append(f"- {df.fieldname} ({len(children)} rows, first {len(rows)}): {frappe.as_json(compact, indent=None)[:1500]}")
	return "\n".join(lines)
