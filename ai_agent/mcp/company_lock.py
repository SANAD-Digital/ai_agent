"""A connection limited to one company (AI MCP.company): during that MCP request the user sees and works on
that company only. Frappe's own permission hooks enforce it, so every read and write path follows: list queries
(permission_query_conditions "*"), documents opened, saved or created (has_permission "*"), and child rows
through their parent. Reports that run their own SQL get the company filter (tools.reports). Outside an MCP
request with a locked connection both hooks do nothing.
"""

from __future__ import annotations

import frappe

ATTR = "ai_agent_company_lock"


def current():
	return getattr(frappe.local, ATTR, None)


def set_lock(company):
	setattr(frappe.local, ATTR, company or None)


def _company_field(doctype):
	"""The DocType's ``company`` field when it links to Company (the ERPNext convention), else None."""
	try:
		df = frappe.get_meta(doctype).get_field("company")
	except Exception:
		return None  # an unknown DocType fails in its own query
	return "company" if df and df.fieldtype == "Link" and df.options == "Company" else None


def _parents_with_company(child_doctype):
	"""Parent DocTypes with a company field that hold ``child_doctype`` in a table (cached per request)."""
	cache = frappe.local.__dict__.setdefault("ai_agent_company_lock_parents", {})
	if child_doctype not in cache:
		parents = frappe.get_all("DocField", filters={"options": child_doctype, "fieldtype": ("in", ("Table", "Table MultiSelect"))},
			pluck="parent", distinct=True)
		parents += frappe.get_all("Custom Field", filters={"options": child_doctype, "fieldtype": ("in", ("Table", "Table MultiSelect"))},
			pluck="dt", distinct=True)
		cache[child_doctype] = sorted({p for p in parents if _company_field(p)})
	return cache[child_doctype]


def query_conditions(user=None, doctype=None):
	"""hooks.permission_query_conditions["*"]: the WHERE condition that keeps a list to the locked company."""
	company = current()
	if not company or not doctype:
		return ""
	value = frappe.db.escape(company)
	if doctype == "Company":
		return f"`tabCompany`.`name` = {value}"
	if _company_field(doctype):
		return f"`tab{doctype}`.`company` = {value}"
	if frappe.get_meta(doctype).istable:
		parents = _parents_with_company(doctype)
		if parents:
			kept = " or ".join(
				f"(`tab{doctype}`.`parenttype` = {frappe.db.escape(p)} and `tab{doctype}`.`parent` in "
				f"(select `name` from `tab{p}` where `company` = {value}))" for p in parents)
			listed = ", ".join(frappe.db.escape(p) for p in parents)
			return f"(`tab{doctype}`.`parenttype` not in ({listed}) or {kept})"
	return ""


def has_permission(doc, ptype=None, user=None, debug=False):
	"""hooks.has_permission["*"]: a document of another company is neither read nor written (new ones too)."""
	company = current()
	if not company or not doc or isinstance(doc, str):
		return True
	if doc.doctype == "Company":
		return doc.name == company
	if _company_field(doc.doctype):
		return not doc.get("company") or doc.get("company") == company
	if doc.get("parenttype") and doc.get("parent") and _company_field(doc.parenttype):
		return frappe.db.get_value(doc.parenttype, doc.parent, "company") in (None, company)
	return True


def bind_payload(action_type, doctype, payload):
	"""A write request made on a locked connection: a new document goes to the locked company, and a payload naming
	another company is refused. Done before the request is checked and hashed, so the approved payload is the bound
	one even when it is approved later from Desk, where no lock is active."""
	company = current()
	if not company or not isinstance(payload, dict):
		return payload
	if action_type == "Bulk Create":
		documents = payload.get("documents")
		if isinstance(documents, list):
			payload = dict(payload)
			payload["documents"] = [
				{**d, "payload": _bind_one("Create", d.get("doctype") or (d.get("payload") or {}).get("doctype"), d.get("payload"), company)}
				if isinstance(d, dict) else d for d in documents]
		return payload
	if action_type in ("Create", "Update"):
		return _bind_one(action_type, doctype, payload, company)
	return payload


def _bind_one(action_type, doctype, payload, company):
	from ai_agent.runtime.exceptions import PermissionDenied

	if not isinstance(payload, dict) or not doctype or not _company_field(doctype):
		return payload
	named = payload.get("company")
	if named and named != company:
		raise PermissionDenied(f"This connection works on {company} only: {doctype} for {named} is not allowed.")
	if action_type == "Create" and not named:
		payload = {**payload, "company": company}
	return payload
