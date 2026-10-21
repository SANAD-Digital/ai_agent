"""The "customization" section of an approval card (docs/customization/01-gap-audit-and-design.md §3.4).

One builder for both surfaces:
- Desk chat: ``engine/tools.approval_snapshot`` puts ``section(approval, user)`` under ``snapshot["customization"]``
  and ``public/js/chat/blocks.js`` draws it.
- MCP Apps: ``mcp/approvals.build_card`` puts it under ``card["customization"]`` and ``mcp/ui/src/approval.js`` draws it.

Input is the frozen payload of ``contract.py`` (kind, operation, spec, before, report, purpose, context, undo_of) and,
once executed, ``execution_result`` (targets, undo, verified, checks). Everything shown is computed here, already in the
viewer's language; the views add no wording of their own beyond their chrome. Code is never shortened.
"""

from __future__ import annotations

import difflib
import json

import frappe
from frappe.utils import cint

from ai_agent.presentation.i18n import t
from ai_agent.presentation.labels import doctype_label, label_for
from ai_agent.presentation.urls import doc_href

ACTION_TYPE = "Customize"
LEVEL_ORDER = {"critical": 0, "warning": 1, "info": 2, "ok": 3}
RISK_TONE = {"low": "success", "medium": "warning", "high": "danger"}
MAX_PREVIEW_ROWS = 20

# English sources, translated with t() where shown.
KIND_LABELS = {
	"custom_field": "Custom field",
	"properties": "Field and form properties",
	"client_script": "Client script",
	"server_script": "Server script",
	"report": "Report",
	"print_format": "Print format",
	"notification": "Notification",
	"workflow": "Workflow",
	"naming": "Naming",
	"dashboard": "Number card or chart",
	"workspace": "Workspace and sidebar",
	"list_settings": "List settings",
	"web": "Web page or form",
	"permissions": "Role permissions",
	"doctype": "New DocType",
}
OPERATION_LABELS = {
	"add": "Add", "create": "Create", "update": "Change", "remove": "Remove",
	"enable": "Enable", "disable": "Disable", "delete": "Delete", "restore": "Restore", "reset": "Reset to standard",
	"publish": "Publish", "unpublish": "Unpublish", "grant": "Grant", "revoke": "Revoke",
	"activate": "Activate", "deactivate": "Deactivate", "set_default": "Make default", "unset_default": "Stop being default",
	"set_doctype_naming": "Change naming", "create_rule": "Create naming rule", "update_rule": "Change naming rule",
	"disable_rule": "Disable naming rule", "enable_rule": "Enable naming rule",
}
RISK_LABELS = {"low": "Low risk", "medium": "Medium risk", "high": "High risk"}
CODE_LANGUAGES = {"client_script": "javascript", "server_script": "python"}
# Where an executed target opens in ERPNext, and how the button reads.
OPEN_LABELS = {
	"Custom Field": "Open the custom field", "Property Setter": "Open the property setter",
	"Client Script": "Open the client script", "Server Script": "Open the server script",
	"Report": "Open the report", "DocType": "Open the form",
}
# The reverse operation (D10: a created script or report is disabled, not deleted).
UNDO_TEXT = {
	("custom_field", "add"): "Remove the added fields. Data entered in them stays in the database column.",
	("custom_field", "update"): "Restore the previous field settings.",
	("custom_field", "remove"): "Re-create the field from its saved definition; data left in its column comes back.",
	("properties", None): "Restore the previous values; property setters created by this change are deleted.",
	("client_script", "create"): "Disable the script (it is kept for review).",
	("client_script", "update"): "Restore the previous code.",
	("client_script", "enable"): "Disable the script again.",
	("client_script", "disable"): "Enable the script again.",
	("server_script", "create"): "Disable the script (it is kept for review).",
	("server_script", "update"): "Restore the previous code.",
	("server_script", "enable"): "Disable the script again.",
	("server_script", "disable"): "Enable the script again.",
	("report", "create"): "Disable the report (it is kept for review).",
	("report", "update"): "Restore the previous version of the report.",
	("report", "disable"): "Enable the report again.",
	("print_format", None): "Reverse it: a created format is disabled, a change or the default is restored.",
	("notification", None): "Reverse it: a created notification is disabled, a change is restored.",
	("workflow", None): "Reverse it: a created workflow is removed and the previous one reactivated; a change is restored. The workflow state field Frappe adds stays.",
	("naming", None): "Restore the previous naming; a created rule is disabled. Names already given stay.",
	("dashboard", None): "Reverse it: a created card or chart is deleted, a change restored, a deleted one re-created.",
	("workspace", None): "Reverse it: a created workspace is deleted, a change restored, a hidden one shown again.",
	("list_settings", None): "Restore the previous list settings.",
	("web", None): "Reverse it: a created page or form is deleted (it is unpublished first), a change restored.",
	("permissions", None): "Restore the exact previous permission rules.",
	("doctype", None): "Delete the new DocType while it has no records; a change is restored from the saved definition.",
}
PROPERTY_LABELS = {
	"label": "Label", "hidden": "Hidden", "reqd": "Mandatory", "read_only": "Read only", "bold": "Bold",
	"in_list_view": "Column in list", "in_standard_filter": "Filter in list sidebar", "in_global_search": "In global search",
	"in_preview": "In preview", "allow_on_submit": "Allow on submit", "default": "Default", "options": "Options",
	"description": "Description", "depends_on": "Display depends on", "mandatory_depends_on": "Mandatory depends on",
	"read_only_depends_on": "Read only depends on", "collapsible": "Collapsible", "print_hide": "Hide in print",
	"no_copy": "No copy", "permlevel": "Permission level", "precision": "Precision", "columns": "Columns", "width": "Width",
	"fetch_from": "Fetch from", "fetch_if_empty": "Fetch only if empty", "allow_in_quick_entry": "In quick entry",
	"placeholder": "Placeholder", "unique": "Unique", "length": "Length", "fieldtype": "Type", "insert_after": "Insert after",
	"title_field": "Title field", "search_fields": "Search fields", "sort_field": "Sort field", "sort_order": "Sort order",
	"image_field": "Image field", "show_title_field_in_link": "Show title in links", "quick_entry": "Quick entry",
	"track_changes": "Track changes", "track_views": "Track views", "allow_copy": "Allow copy", "allow_import": "Allow import",
	"editable_grid": "Editable grid", "max_attachments": "Maximum attachments", "default_view": "Default view",
	"field_order": "Field order", "enabled": "Enabled", "disabled": "Disabled", "translatable": "Translatable",
	"set_only_once": "Set only once", "non_negative": "Non negative", "link_filters": "Link filters",
	"remember_last_selected_value": "Remember last value", "report_hide": "Hide in report", "mask": "Mask",
}
FIELD_FACTS = ("fieldtype", "options", "insert_after", "reqd", "hidden", "read_only", "default", "in_list_view",
	"in_standard_filter", "depends_on", "mandatory_depends_on", "fetch_from", "unique", "permlevel", "allow_on_submit", "bold")
EVENT_LABELS = {
	"Before Insert": "before insert", "Before Validate": "before validate", "Before Save": "before save",
	"After Insert": "after insert", "After Save": "after save", "Before Submit": "before submit",
	"After Submit": "after submit", "Before Cancel": "before cancel", "After Cancel": "after cancel",
	"Before Delete": "before delete", "After Delete": "after delete",
	"Before Save (Submitted Document)": "before saving a submitted document",
	"After Save (Submitted Document)": "after saving a submitted document", "Before Print": "before print",
}


# ---------------------------------------------------------------- entry point


def section(approval, user=None):
	"""The card section for a Customize approval, or None for any other approval."""
	if not approval or approval.get("action_type") != ACTION_TYPE:
		return None
	user = user or frappe.session.user
	payload = _json(approval.get("payload_json"))
	stored = _json(approval.get("execution_result"))
	kind = str(payload.get("kind") or "")
	operation = str(payload.get("operation") or "")
	spec = payload.get("spec") if isinstance(payload.get("spec"), dict) else {}
	before = payload.get("before") if isinstance(payload.get("before"), dict) else {}
	report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
	preview = report.get("preview") if isinstance(report.get("preview"), dict) else {}
	context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
	status = approval.get("status") or "Pending"
	state = _state(status, stored)

	checks = _checks(report.get("checks"))
	critical = any(c["level"] == "critical" for c in checks)
	risk = report.get("risk") if report.get("risk") in RISK_LABELS else "medium"
	tainted = bool(context.get("tainted"))
	requested_by = context.get("requested_by") or approval.get("requested_by")
	pending = state == "pending"
	gate = _approval_gate(approval, user, kind, spec) if pending else {"allowed": False, "reason": None}

	out = {
		"kind": kind,
		"kind_label": t(KIND_LABELS.get(kind, "Customization")),
		"operation": operation,
		"operation_label": t(OPERATION_LABELS[operation]) if operation in OPERATION_LABELS else operation,
		"target": _target(kind, spec),
		"name": spec.get("name") or spec.get("report_name"),
		"purpose": payload.get("purpose") or spec.get("purpose") or spec.get("reason") or "",
		"summary": payload.get("summary") or approval.get("action_summary") or "",
		"state": state,
		"risk": risk,
		"risk_label": t(RISK_LABELS[risk]),
		"risk_tone": RISK_TONE[risk],
		"checks": checks,
		"counts": {level: sum(1 for c in checks if c["level"] == level) for level in LEVEL_ORDER},
		"has_critical": critical,
		"impact": [str(i) for i in (report.get("impact") or []) if i],
		"diff": _diff(kind, operation, spec, before, preview),
		"fields": _new_fields(kind, operation, spec) if not preview.get("diff") else [],
		"layout": _layout(preview.get("layout")),
		"lists": _lists(preview) + _more_lists(preview),
		"code": _code(kind, operation, spec, before, preview),
		"codes": _codes(kind, spec, preview),
		"rows": _rows(preview.get("rows")),
		"dry_run": _dry_run(preview.get("dry_run")),
		"undo": t(UNDO_TEXT.get((kind, operation)) or UNDO_TEXT.get((kind, None)) or "Reverse this change with a new request."),
		"undo_of": payload.get("undo_of"),
		"tainted": tainted,
		"tainted_text": t("This was requested after the assistant read outside content (a file or a web page). Make sure the request is yours.") if tainted else None,
		"provenance": {"via": context.get("via"), "surface": context.get("surface"), "tainted": tainted, "requested_by": requested_by},
		"can_approve": bool(pending and not critical and gate["allowed"]),
		"approve_hidden_reason": _hidden_reason(pending, critical, gate),
		"approve_label": _approve_label(kind, spec, risk),
		"allow_always": False,
		"next_steps": _next_steps(kind, operation, spec) if pending else [],
		"result": _result(approval, stored, state, kind),
		"labels": _labels(),
	}
	return out


# ---------------------------------------------------------------- pieces


def _json(value):
	if isinstance(value, dict):
		return value
	try:
		data = json.loads(value or "{}")
	except (TypeError, ValueError):
		return {}
	return data if isinstance(data, dict) else {}


def _state(status, stored):
	if status == "Executed":
		return "verified" if stored.get("verified") else "executed"
	return {"Pending": "pending", "Approved": "approved", "Rejected": "rejected", "Expired": "expired", "Failed": "failed"}.get(status, "pending")


def _checks(raw):
	out = []
	for item in raw or []:
		if not isinstance(item, dict):
			continue
		level = item.get("level") if item.get("level") in LEVEL_ORDER else "info"
		line = item.get("line")
		try:
			line = int(line) if line not in (None, "") else None
		except (TypeError, ValueError):
			line = None
		out.append({"level": level, "message": str(item.get("message") or item.get("text") or ""), "line": line, "code": item.get("code") or ""})
	# Most serious first; the order inside one level is the analyzer's (usually by line).
	return sorted(out, key=lambda c: LEVEL_ORDER[c["level"]])


def _doctype(spec):
	return spec.get("doctype") or spec.get("dt") or spec.get("reference_doctype") or spec.get("ref_doctype") or ""


def _target(kind, spec):
	doctype = _doctype(spec)
	dt_label = doctype_label(doctype) if doctype else ""
	if kind == "custom_field":
		names = [str(f.get("label") or f.get("fieldname")) for f in spec.get("fields") or [] if isinstance(f, dict)]
		return " — ".join(x for x in (dt_label, "، ".join(names) if _rtl() else ", ".join(names)) if x)
	if kind == "properties":
		return dt_label
	if kind == "client_script":
		view = t("List") if spec.get("view") == "List" else t("Form")
		return " — ".join(x for x in (dt_label, view) if x)
	if kind == "server_script":
		if spec.get("script_type") == "API" or spec.get("api_method"):
			return f"/api/method/{spec.get('api_method') or ''}"
		event = spec.get("doctype_event")
		return " — ".join(x for x in (dt_label, t(EVENT_LABELS.get(event, event or ""))) if x)
	if kind == "report":
		report_type = t(spec.get("report_type")) if spec.get("report_type") else ""
		return " — ".join(x for x in (spec.get("report_name") or spec.get("name") or "", report_type) if x)
	return dt_label or spec.get("name") or ""


def _rtl():
	from ai_agent.presentation.i18n import is_rtl
	from ai_agent.presentation.labels import label_language

	return is_rtl(label_language())


def _prop_label(prop):
	return t(PROPERTY_LABELS[prop]) if prop in PROPERTY_LABELS else str(prop).replace("_", " ").capitalize()


def _value(value):
	"""A property value as the card shows it: checks as yes/no, empty as a dash, lists joined."""
	if value is None or value == "":
		return "—"
	if isinstance(value, bool):
		return t("Yes") if value else t("No")
	if isinstance(value, (list, tuple)):
		return ", ".join(str(v) for v in value) or "—"
	if isinstance(value, dict):
		return json.dumps(value, ensure_ascii=False)
	return str(value)


def _check_value(prop, value):
	if prop in ("hidden", "reqd", "read_only", "bold", "in_list_view", "in_standard_filter", "in_global_search", "in_preview",
			"allow_on_submit", "collapsible", "print_hide", "no_copy", "unique", "fetch_if_empty", "allow_in_quick_entry",
			"quick_entry", "track_changes", "track_views", "allow_copy", "allow_import", "editable_grid", "enabled", "disabled",
			"show_title_field_in_link", "translatable", "set_only_once", "non_negative", "report_hide", "mask"):
		if value in (None, ""):
			return "—"
		return t("Yes") if cint(value) else t("No")
	if prop == "options" and isinstance(value, str) and "\n" in value:
		return " / ".join(x for x in value.splitlines() if x.strip())  # Select: one option per line
	return _value(value)


def _diff(kind, operation, spec, before, preview):
	"""Before/after rows. The kind's own preview wins; otherwise it is derived from spec and the "before" snapshot."""
	rows = []
	for row in preview.get("diff") or []:
		if not isinstance(row, dict):
			continue
		prop = row.get("property") or row.get("prop")
		label = row.get("label") or (_prop_label(prop) if prop else "")
		rows.append({"field": row.get("field") or row.get("fieldname") or "", "field_label": row.get("field_label") or "",
			"property": prop or "", "label": label,
			"before": _check_value(prop, row.get("before")), "after": _check_value(prop, row.get("after"))})
	if rows or preview.get("diff") is not None:
		return rows
	doctype = _doctype(spec)
	if kind == "properties":
		old_dt = before.get("doctype_properties") if isinstance(before.get("doctype_properties"), dict) else before
		for prop, value in (spec.get("doctype_properties") or {}).items():
			rows.append({"field": "", "field_label": doctype_label(doctype), "property": prop, "label": _prop_label(prop),
				"before": _check_value(prop, (old_dt or {}).get(prop)), "after": _check_value(prop, value)})
		old_fields = _fields_by_name(before)
		for fieldname, props in (spec.get("field_properties") or {}).items():
			for prop, value in (props or {}).items() if isinstance(props, dict) else []:
				rows.append({"field": fieldname, "field_label": label_for(fieldname, doctype), "property": prop, "label": _prop_label(prop),
					"before": _check_value(prop, old_fields.get(fieldname, {}).get(prop)), "after": _check_value(prop, value)})
		if spec.get("field_order"):
			rows.append({"field": "", "field_label": doctype_label(doctype), "property": "field_order", "label": _prop_label("field_order"),
				"before": _value(before.get("field_order")), "after": _value(spec.get("field_order"))})
	elif kind == "custom_field" and operation == "update":
		old_fields = _fields_by_name(before)
		for f in spec.get("fields") or []:
			if not isinstance(f, dict):
				continue
			name = f.get("fieldname")
			for prop, value in f.items():
				if prop == "fieldname":
					continue
				old = old_fields.get(name, {}).get(prop)
				if str(old if old is not None else "") == str(value if value is not None else ""):
					continue
				rows.append({"field": name, "field_label": f.get("label") or old_fields.get(name, {}).get("label") or name, "property": prop,
					"label": _prop_label(prop), "before": _check_value(prop, old), "after": _check_value(prop, value)})
	elif kind in ("client_script", "server_script", "report") and operation in ("enable", "disable"):
		was_enabled = _was_enabled(kind, before)
		now = operation == "enable"
		rows.append({"field": "", "field_label": spec.get("name") or spec.get("report_name") or "", "property": "enabled", "label": _prop_label("enabled"),
			"before": "—" if was_enabled is None else (t("Yes") if was_enabled else t("No")), "after": t("Yes") if now else t("No")})
	return rows


def _was_enabled(kind, before):
	if not before:
		return None
	if "enabled" in before:
		return bool(cint(before.get("enabled")))
	if "disabled" in before:
		return not cint(before.get("disabled"))
	return None


def _fields_by_name(before):
	"""The "before" snapshot's fields keyed by fieldname, whether it holds a dict or a list."""
	raw = before.get("fields") if isinstance(before, dict) else None
	if raw is None and isinstance(before, dict):
		raw = before.get("field_properties")
	if isinstance(raw, dict):
		return {k: v for k, v in raw.items() if isinstance(v, dict)}
	out = {}
	for f in raw or []:
		if isinstance(f, dict) and f.get("fieldname"):
			out[f["fieldname"]] = f
	return out


def _new_fields(kind, operation, spec):
	"""Fields being added or removed: one block each with the properties that matter."""
	if kind != "custom_field" or operation not in ("add", "remove"):
		return []
	doctype = _doctype(spec)
	out = []
	for f in spec.get("fields") or []:
		if not isinstance(f, dict):
			continue
		facts = []
		for prop in FIELD_FACTS:
			value = f.get(prop)
			if value in (None, "", 0) and prop not in ("fieldtype",):
				continue
			shown = label_for(value, doctype) if prop == "insert_after" and value else _check_value(prop, value)
			facts.append({"label": _prop_label(prop), "d": shown})
		out.append({"fieldname": f.get("fieldname") or "", "label": f.get("label") or f.get("fieldname") or "", "fieldtype": f.get("fieldtype") or "",
			"facts": facts, "removed": operation == "remove"})
	return out


def _layout(raw):
	"""Where a new field lands: the neighbours inside its section and column, the new one marked."""
	if isinstance(raw, dict):
		items = raw.get("fields") or raw.get("items") or []
		title = raw.get("section") or raw.get("title") or ""
	elif isinstance(raw, list):
		items, title = raw, ""
	else:
		return None
	fields = []
	for f in items:
		if isinstance(f, dict):
			fields.append({"label": str(f.get("label") or f.get("fieldname") or ""), "fieldname": f.get("fieldname") or "",
				"fieldtype": f.get("fieldtype") or "", "new": bool(f.get("new") or f.get("is_new"))})
		elif f:
			fields.append({"label": str(f), "fieldname": "", "fieldtype": "", "new": False})
	return {"title": title, "fields": fields} if fields else None


# Rendered output and second sources the newer kinds put in their preview, drawn as text blocks
# (never inserted as HTML): key -> (title, language)
EXTRA_CODES = (
	("javascript", "Report filters (JavaScript)", "javascript"),
	("html", "Rendered output (first part)", "html"),
	("subject_rendered", "Subject as it will be sent", "text"),
	("message_rendered", "Message as it will be sent", "html"),
	("css", "Styles", "css"),
	("definition", "Definition", "json"),
)
# Lists and tables the newer kinds compute: key -> title. A list of objects becomes one line per object.
EXTRA_LISTS = (
	("next_names", "The next names"), ("examples", "Examples"), ("series_options", "Series options"),
	("other_rules", "Other naming rules on this DocType"), ("next_runs", "Next runs"), ("paths", "Paths"),
	("states", "States"), ("transitions", "Transitions"), ("resolved", "Recipients"),
	("external_links", "External links"), ("sidebar", "Sidebar after the change"), ("placements", "Placed on"),
	("columns_before", "List columns before"), ("columns_after", "List columns after"), ("fields", "Form fields"),
	("permission_query", "Permission query per user"), ("users", "Who gains or loses rights"),
	("matrix_before", "Rules before"), ("matrix_after", "Rules after"), ("changes", "Rule changes"),
)


def _codes(kind, spec, preview):
	out = []
	for key, title, language in EXTRA_CODES:
		value = preview.get(key)
		if key == "javascript" and not value and kind == "report":
			value = spec.get("javascript")
		if isinstance(value, (dict, list)):
			value = json.dumps(value, ensure_ascii=False, indent=2, default=str)
		if not value or not isinstance(value, str):
			continue
		out.append({"language": language, "text": value, "lines": value.count("\n") + 1, "diff": None, "title": t(title)})
	return out


def _line(value):
	if isinstance(value, dict):
		parts = []
		for k, v in value.items():
			if v in (None, "", [], {}):
				continue
			if isinstance(v, (dict, list)):
				v = json.dumps(v, ensure_ascii=False, default=str)
			parts.append(f"{k}: {v}")
		return " · ".join(parts)
	if isinstance(value, (list, tuple)):
		return " · ".join(str(v) for v in value)
	return str(value)


def _more_lists(preview):
	out = []
	for key, title in EXTRA_LISTS:
		value = preview.get(key)
		if isinstance(value, dict) and value:
			items = [f"{k}: {_line(v)}" for k, v in value.items()]
		elif isinstance(value, list) and value:
			items = [_line(v) for v in value[:MAX_PREVIEW_ROWS * 2]]
		else:
			continue
		out.append({"key": key, "title": t(title), "items": [i for i in items if i]})
	return out


def _lists(preview):
	"""Named lists the kind computed, e.g. list columns and sidebar filters after a properties change."""
	out = []
	for key, title in (("list_columns", "List columns after the change"), ("standard_filters", "Sidebar filters after the change"),
			("columns", "Report columns"), ("filters", "Report filters"), ("methods", "Server methods the script calls")):
		value = preview.get(key)
		if not isinstance(value, list) or not value:
			continue
		items = []
		for v in value:
			if isinstance(v, dict):
				label = v.get("label") or v.get("fieldname") or v.get("method") or ""
				extra = v.get("fieldtype") or v.get("note") or ""
				items.append(f"{label} ({extra})" if extra else str(label))
			else:
				items.append(str(v))
		out.append({"key": key, "title": t(title), "items": items})
	return out


def _is_query(spec):
	return spec.get("report_type") == "Query Report" or bool(spec.get("query"))


def _code(kind, operation, spec, before, preview):
	"""The full source to be saved, with a unified diff against the current version on an update. Never shortened."""
	raw = preview.get("code")
	text, language, diff = None, None, None
	if isinstance(raw, dict):
		text, language, diff = raw.get("text"), raw.get("language"), raw.get("diff")
	elif isinstance(raw, str):
		text = raw
	if text is None:
		if kind in CODE_LANGUAGES:
			text = spec.get("script")
		elif kind == "report":
			if spec.get("report_script"):
				text, language = spec.get("report_script"), "python"
			elif _is_query(spec):
				text = spec.get("query")
			elif spec.get("builder"):
				text = json.dumps(spec.get("builder"), ensure_ascii=False, indent=2)
	if not text:
		return None
	if not language:
		language = CODE_LANGUAGES.get(kind) or ("sql" if _is_query(spec) else "json")
	if diff is None and operation == "update":
		old = before.get("script") if kind in CODE_LANGUAGES else (
			before.get("report_script") if spec.get("report_script") else (before.get("query") if _is_query(spec) else before.get("json")))
		if isinstance(old, str) and old != text:
			diff = "\n".join(difflib.unified_diff(old.splitlines(), str(text).splitlines(), "before", "after", lineterm="", n=3))
	text = str(text)
	return {"language": language, "text": text, "lines": text.count("\n") + 1, "diff": diff or None,
		"title": t("The full code") if kind in CODE_LANGUAGES else (t("The query") if language == "sql" else t("Report definition"))}


def _rows(raw):
	"""Preview rows (already masked by the kind) as plain strings."""
	if not isinstance(raw, dict):
		return None
	cols = raw.get("columns") or []
	rows = raw.get("rows") or raw.get("data") or []
	keys, labels = [], []
	for c in cols:
		if isinstance(c, dict):
			keys.append(c.get("fieldname") or c.get("key") or c.get("label"))
			labels.append(str(c.get("label") or c.get("fieldname") or ""))
		else:
			keys.append(c)
			labels.append(str(c).split(":")[0])
	out = []
	for r in rows[:MAX_PREVIEW_ROWS]:
		if isinstance(r, dict):
			if not keys:
				keys = list(r.keys())
				labels = [str(k) for k in keys]
			out.append([_value(r.get(k)) for k in keys])
		elif isinstance(r, (list, tuple)):
			out.append([_value(v) for v in r])
	if not labels and not out:
		return None
	return {"columns": labels, "rows": out, "total": raw.get("total") or len(rows), "title": t("Preview rows")}


def _dry_run(raw):
	"""What a "Before" event did to a copy of a real document, when the kind ran it."""
	if not isinstance(raw, dict):
		return None
	changed = []
	for field, change in (raw.get("changed") or {}).items():
		if isinstance(change, (list, tuple)) and len(change) == 2:
			changed.append({"label": str(field), "before": _value(change[0]), "after": _value(change[1])})
		elif isinstance(change, dict):
			changed.append({"label": str(field), "before": _value(change.get("before")), "after": _value(change.get("after"))})
	return {"document": raw.get("document") or "", "threw": bool(raw.get("threw")), "message": str(raw.get("message") or ""), "changed": changed}


def _approval_gate(approval, user, kind, spec):
	"""May this user press Approve here? Mirrors policy.assert_can_approve so no button is offered that can only fail."""
	try:
		from ai_agent.customization import policy, registry
	except Exception:
		return {"allowed": True, "reason": None}
	try:
		if policy.four_eyes(kind, spec) and approval.get("requested_by") == user:
			return {"allowed": False, "reason": t("Customization needs a second person: you cannot approve your own change. A colleague with the required roles approves it.")}
		try:
			kind_obj = registry.get(kind)
		except Exception:
			kind_obj = None
		roles = policy.kind_roles(kind, kind_obj, spec)
		if not policy.has_roles(user, roles):
			return {"allowed": False, "reason": t("Approving this change needs the roles: {0}.", ", ".join(t(r) for r in roles))}
	except Exception:
		return {"allowed": True, "reason": None}
	return {"allowed": True, "reason": None}


def _approve_label(kind, spec, risk):
	if risk != "high":
		return t("Approve and apply")
	is_code = kind in CODE_LANGUAGES or (kind == "report" and _is_query(spec))
	return t("Approve high-risk code") if is_code else t("Approve high-risk change")


def _hidden_reason(pending, critical, gate):
	if not pending:
		return None
	if critical:
		return t("A critical check failed, so this change cannot be approved. Ask the assistant to fix it and request again.")
	return gate.get("reason")


def _next_steps(kind, operation, spec):
	steps = [t("The change is applied under your name, after every check is run again.")]
	if kind in ("custom_field", "properties"):
		steps.append(t("The form is reloaded from the database to confirm each value took effect."))
	elif kind in CODE_LANGUAGES:
		steps.append(t("We confirm the script is saved with exactly this code and the requested state."))
	elif kind == "report":
		steps.append(t("We run the report once with a small sample to confirm it works."))
	steps.append(t("Undo stays available: it creates a new request that you approve the same way."))
	return steps


def _result(approval, stored, state, kind):
	if state not in ("executed", "verified", "failed"):
		return None
	if state == "failed":
		return {"verified": False, "error": str(stored.get("error") or ""), "checks": _checks(stored.get("checks")), "targets": [], "undo_prompt": None}
	targets = []
	for target in stored.get("targets") or []:
		if not isinstance(target, dict) or not target.get("doctype") or not target.get("name"):
			continue
		label = OPEN_LABELS.get(target["doctype"])
		targets.append({"doctype": target["doctype"], "name": target["name"], "href": doc_href(target["doctype"], target["name"]),
			"label": t(label) if label else t("Open {0}", doctype_label(target["doctype"]))})
	has_undo = bool(stored.get("undo"))
	return {
		"verified": bool(stored.get("verified")),
		"partial": bool(stored.get("partial")),
		"checks": _checks(stored.get("checks")),
		"targets": targets,
		"text": t("Applied and verified.") if stored.get("verified") else t("Applied, but the verification did not confirm every value. Review the checks below."),
		# The view sends this as the user's next message; the model answers with customize.request_undo.
		"undo_prompt": t("Undo customization {0}", approval.get("name")) if has_undo else None,
		"undo_label": t("Undo") if has_undo else None,
	}


def _labels():
	"""Section titles, so both views use the same wording."""
	return {
		"checks": t("Checks"), "impact": t("Who and what it affects"), "diff": t("Before and after"), "fields": t("Fields"),
		"layout": t("Place on the form"), "code": t("Code"), "diff_view": t("Changes"), "full_view": t("Full code"),
		"rows": t("Preview rows"), "undo": t("Undo plan"), "purpose": t("Purpose"), "target": t("Target"),
		"provenance": t("Requested"), "dry_run": t("Trial run on a copy"), "before": t("Before"), "after": t("After"),
		"property": t("Property"), "field": t("Field"), "line": t("Code line {0}"), "new": t("New"),
		"verification": t("Verification"), "no_checks": t("No findings."), "copy": t("Copy code"), "copied": t("Copied"),
		"threw": t("The script stopped the save with this message:"), "passed": t("The script ran without stopping the save."),
	}
