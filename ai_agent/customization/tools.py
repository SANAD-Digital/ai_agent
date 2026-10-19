"""customize.* handlers (docs/customization/01 §3.3).

Read tools (no approval): ``customize.inspect``, ``customize.get``, ``customize.check``.
Request tools create a ``Customize`` approval through ``request()``:
normalize -> policy (request phase) -> snapshot -> check -> ApprovalEngine.create_approval_request.
A critical check returns an error with the lines instead of an approval.

The caller sets ``ctx.surface`` ("Desk" / "MCP"; anything else is refused) and ``ctx.tainted`` (untrusted
content entered the conversation before this request) on the PermissionContext.
"""

from __future__ import annotations

import json

import frappe

from ai_agent.customization import policy, registry
from ai_agent.customization.contract import CheckReport, CustomizationError
from ai_agent.customization.executor import canonical, critical_message, same_state
from ai_agent.runtime.exceptions import PermissionDenied

AI_HEADER = "ai_agent:"
INSPECT_PARTS = ("fields", "properties", "client_scripts", "server_scripts", "reports", "print_formats", "workflows", "notifications")
INSPECT_DEFAULT = ("fields", "properties", "client_scripts", "server_scripts")
TRANSPORT_KEYS = ("run", "then")  # added by the MCP bridge, not the model's arguments


def _surface(ctx):
	return getattr(ctx, "surface", None)


def _clean(arguments):
	return {k: v for k, v in (arguments or {}).items() if k not in TRANSPORT_KEYS}


def _error(message, hints=None, **extra):
	text = message
	if hints:
		text += " Hints: " + "; ".join(str(h) for h in hints)
	return {"error": text, **({"hints": list(hints)} if hints else {}), **extra}


def _assert_group(ctx, tool):
	if not policy.tool_available(tool, ctx.user, _surface(ctx)):
		raise PermissionDenied("Customization tools are not available here (turned off, or this user lacks the roles).")


# -- read tools -------------------------------------------------------------------------


def handle_inspect(arguments, ctx):
	_assert_group(ctx, "customize.inspect")
	doctype = (arguments or {}).get("doctype")
	if not doctype or not frappe.db.exists("DocType", doctype):
		return _error(f"DocType '{doctype}' not found.", ["Find the exact name with schema__search_doctypes."])
	if not (frappe.has_permission(doctype, "read", user=ctx.user) or policy.has_roles(ctx.user, ("System Manager",))):
		return _error(f"No read permission on {doctype}.")
	include = [p for p in (arguments.get("include") or INSPECT_DEFAULT) if p in INSPECT_PARTS]
	meta = frappe.get_meta(doctype)
	out = {"doctype": doctype, **_customizable(doctype, meta)}
	if "fields" in include:
		out["custom_fields"] = frappe.get_all("Custom Field", filters={"dt": doctype},
			fields=["name", "fieldname", "label", "fieldtype", "options", "insert_after", "reqd", "hidden", "owner"],
			order_by="idx asc, creation asc")
	if "properties" in include:
		out["property_setters"] = frappe.get_all("Property Setter", filters={"doc_type": doctype},
			fields=["name", "field_name", "row_name", "property", "value", "doctype_or_field"], order_by="field_name asc")
	if "client_scripts" in include:
		rows = frappe.get_all("Client Script", filters={"dt": doctype}, fields=["name", "view", "enabled", "script"])
		out["client_scripts"] = [_script_row(r, ("name", "view", "enabled")) for r in rows]
	if "server_scripts" in include:
		rows = frappe.get_all("Server Script", filters={"reference_doctype": doctype},
			fields=["name", "script_type", "doctype_event", "api_method", "disabled", "script"])
		out["server_scripts"] = [_script_row(r, ("name", "script_type", "doctype_event", "api_method", "disabled")) for r in rows]
	if "reports" in include:
		rows = frappe.get_all("Report", filters={"ref_doctype": doctype},
			fields=["name", "report_type", "is_standard", "disabled", "query"])
		out["reports"] = [{**{k: r.get(k) for k in ("name", "report_type", "is_standard", "disabled")}, "ai_made": _ai_made(r.get("query"))} for r in rows]
	if "print_formats" in include:
		out["print_formats"] = frappe.get_all("Print Format", filters={"doc_type": doctype},
			fields=["name", "print_format_type", "standard", "disabled"])
	if "workflows" in include:
		out["workflows"] = frappe.get_all("Workflow", filters={"document_type": doctype}, fields=["name", "is_active"])
	if "notifications" in include:
		out["notifications"] = frappe.get_all("Notification", filters={"document_type": doctype},
			fields=["name", "channel", "event", "enabled"])
	return out


def _customizable(doctype, meta):
	from frappe.model import core_doctypes_list
	from ai_agent.runtime import policy as runtime_policy

	reason = None
	if doctype in core_doctypes_list:
		reason = "core DocType: Frappe does not allow customizing it"
	elif meta.issingle:
		reason = "Single (settings) DocType: Customize Form does not support it"
	elif meta.custom:
		reason = "custom DocType: it is edited as a DocType, not through Customize Form"
	elif doctype in runtime_policy.CODE_AND_ACCESS_DOCTYPES or runtime_policy.is_protected_doctype(doctype):
		reason = "system, access or agent configuration DocType"
	return {"customizable": reason is None, **({"reason": reason} if reason else {})}


def _ai_made(text):
	head = "\n".join(str(text or "").strip().splitlines()[:2])
	return AI_HEADER in head


def _script_row(row, keys):
	lines = str(row.get("script") or "").strip().splitlines()
	return {**{k: row.get(k) for k in keys}, "first_lines": "\n".join(lines[:2]), "lines": len(lines), "ai_made": _ai_made(row.get("script"))}


GET_FIELDS = {
	"client_script": ("Client Script", ("name", "dt", "view", "enabled", "module", "script")),
	"server_script": ("Server Script", ("name", "script_type", "reference_doctype", "doctype_event", "api_method",
		"allow_guest", "disabled", "enable_rate_limit", "rate_limit_count", "rate_limit_seconds", "module", "script")),
	"report": ("Report", ("name", "report_type", "ref_doctype", "is_standard", "disabled", "module", "query", "json",
		"prepared_report", "add_total_row")),
	"custom_field": ("Custom Field", ("name", "dt", "fieldname", "label", "fieldtype", "options", "insert_after", "reqd",
		"hidden", "read_only", "default", "description", "depends_on", "mandatory_depends_on", "read_only_depends_on",
		"fetch_from", "fetch_if_empty", "in_list_view", "in_standard_filter", "bold", "allow_on_submit", "no_copy",
		"print_hide", "unique", "permlevel", "precision", "length", "translatable", "owner")),
}


def handle_get(arguments, ctx):
	_assert_group(ctx, "customize.get")
	kind = (arguments or {}).get("kind")
	name = (arguments or {}).get("name")
	if kind not in GET_FIELDS:
		return _error(f"Unknown kind '{kind}'.", [f"kind is one of {', '.join(GET_FIELDS)}"])
	doctype, keys = GET_FIELDS[kind]
	if not name or not frappe.db.exists(doctype, name):
		return _error(f"{doctype} '{name}' not found.", ["List what exists with customize__inspect."])
	doc = frappe.get_doc(doctype, name)
	out = {"kind": kind, "doctype": doctype, **{k: doc.get(k) for k in keys}}
	if kind == "report":
		out["filters"] = [{k: f.get(k) for k in ("fieldname", "label", "fieldtype", "options", "mandatory", "default")} for f in doc.get("filters") or []]
		out["columns"] = [{k: c.get(k) for k in ("fieldname", "label", "fieldtype", "options", "width")} for c in doc.get("columns") or []]
		out["roles"] = [r.role for r in doc.get("roles") or []]
	for key in ("script", "query"):
		if out.get(key):
			out["ai_made"] = _ai_made(out[key])
	return out


def handle_check(arguments, ctx):
	"""Dry check without an approval: the same normalize + check a request runs."""
	arguments = arguments or {}
	kind_name = arguments.get("kind")
	tool = next((t for t, k in registry.REQUEST_TOOLS.items() if k == kind_name), None)
	if not tool:
		return _error(f"Unknown kind '{kind_name}'.", [f"kind is one of {', '.join(registry.KINDS)}"])
	_assert_group(ctx, tool)
	args = _clean(arguments.get("arguments") or {})
	try:
		policy.assert_size(args)
		kind = registry.get(kind_name)
		spec = kind.normalize(args, ctx.user)
		report = _report(kind.check(spec, ctx.user))
	except CustomizationError as exc:
		return {"valid": False, **_error(exc.message, exc.hints)}
	return {"valid": not report.critical(), "summary": _summary(kind, spec), **report.to_dict()}


# -- requests ---------------------------------------------------------------------------


def _report(value):
	if isinstance(value, CheckReport):
		return value
	return CheckReport.from_dict(value if isinstance(value, dict) else {})


def _summary(kind, spec):
	try:
		return str(kind.summary(spec) or "")[:500]
	except Exception:
		return ""


def _target_doctype(kind_name, spec):
	for key in ("doctype", "dt", "reference_doctype", "ref_doctype"):
		value = spec.get(key)
		if value and frappe.db.exists("DocType", value):
			return value
	return None


def _target_name(kind_name, spec):
	if kind_name == "report":
		return spec.get("report_name") or spec.get("name")
	if kind_name in ("client_script", "server_script"):
		return spec.get("name")
	return None


def request(kind_name, arguments, ctx, run, *, via=None):
	"""Create the Customize approval for one customize.request_* call (Desk engine and MCP)."""
	args = _clean(arguments)
	try:
		policy.assert_size(args)
		kind = registry.get(kind_name)
		spec = kind.normalize(args, ctx.user)
	except CustomizationError as exc:
		return _error(exc.message, exc.hints)
	purpose = args.get("purpose") or args.get("reason") or ""
	return _create(kind_name, kind, spec, ctx, run, via=via, purpose=purpose)


def _create(kind_name, kind, spec, ctx, run, *, via, purpose="", undo_of=None, tainted=None):
	from ai_agent.runtime.approval_engine import ApprovalEngine

	surface = _surface(ctx)
	policy.assert_allowed(kind_name, spec, ctx.user, "request", kind=kind, surface=surface)
	before = kind.snapshot(spec)
	report = _report(kind.check(spec, ctx.user))
	if report.critical():
		return {
			"valid": False,
			"error": "The change does not pass its checks, so no approval was requested. Fix these and call "
			f"customize__check again: {critical_message(report)}",
			"checks": [c.to_dict() for c in report.checks if c.level != "ok"],
		}
	summary = _summary(kind, spec) or f"Customize ({kind_name})"
	payload = {
		"kind": kind_name,
		"operation": spec.get("operation") or ("update" if kind_name == "properties" else None),
		"spec": spec,
		"before": json.loads(canonical(before)),
		"report": report.to_dict(),
		"summary": summary,
		"target": policy.target_key(kind_name, spec),
		"purpose": str(purpose or "")[:300],
		"context": {
			"surface": surface,
			"tainted": bool(getattr(ctx, "tainted", False) if tainted is None else tainted),
			"via": via,
			"requested_by": ctx.user,
		},
	}
	if undo_of:
		payload["undo_of"] = undo_of
	approval = ApprovalEngine(ctx).create_approval_request(
		run_name=run,
		action_type=policy.ACTION_TYPE,
		doctype_name=_target_doctype(kind_name, spec),
		document_name=_target_name(kind_name, spec),
		action_summary=summary,
		impact_summary="\n".join(str(i) for i in report.impact or []),
		payload=payload,
	)
	return {
		"approval_required": True,
		"approval_id": approval.name,
		"action_summary": summary,
		"risk": report.risk,
		"checks": [c.to_dict() for c in report.checks if c.level in ("warning", "info")],
		"note": "The user reviews the full change on the approval card. It runs only after a person approves it.",
	}


def _request_tool(kind_name, tool):
	def handler(arguments, ctx):
		return request(kind_name, arguments, ctx, (arguments or {}).get("run") or getattr(ctx, "run_name", None), via=tool)

	handler.__name__ = "handle_" + tool.split(".", 1)[1]
	return handler


handle_request_field = _request_tool("custom_field", "customize.request_field")
handle_request_properties = _request_tool("properties", "customize.request_properties")
handle_request_client_script = _request_tool("client_script", "customize.request_client_script")
handle_request_server_script = _request_tool("server_script", "customize.request_server_script")
handle_request_report = _request_tool("report", "customize.request_report")


def handle_request_undo(arguments, ctx):
	return request_undo(arguments, ctx, (arguments or {}).get("run") or getattr(ctx, "run_name", None))


def request_undo(arguments, ctx, run):
	"""A new Customize approval that reverses an executed one (same checks and card)."""
	from ai_agent.runtime.approval_engine import ApprovalEngine

	name = (arguments or {}).get("approval")
	if not name or not frappe.db.exists("AI Approval Request", name):
		return _error(f"Approval '{name}' not found.")
	original = frappe.get_doc("AI Approval Request", name)
	if not ApprovalEngine(ctx)._can_decide(original, ctx.user):
		return _error(f"Approval '{name}' not found.")
	if original.action_type != policy.ACTION_TYPE or original.status != "Executed":
		return _error("Only an executed customization can be undone.")
	try:
		payload = json.loads(original.payload_json or "{}")
		execution = json.loads(original.execution_result or "{}")
	except ValueError:
		return _error("This approval has no readable result to undo.")
	undo = execution.get("undo")
	if not isinstance(undo, dict) or not undo:
		return _error("This change has no undo step.")
	kind_name = payload.get("kind")
	try:
		kind = registry.get(kind_name)
		current = kind.snapshot(payload.get("spec") or {})
	except CustomizationError as exc:
		return _error(exc.message, exc.hints)
	if execution.get("after") is not None and not same_state(current, execution.get("after")):
		return _error("The target changed after this customization was executed; undo it by hand or request a new change.")
	tainted = bool((payload.get("context") or {}).get("tainted")) or bool(getattr(ctx, "tainted", False))
	return _create(kind_name, kind, undo, ctx, run, via="customize.request_undo",
		purpose=f"Undo {original.name}", undo_of=original.name, tainted=tainted)


def is_request_tool(name):
	return name in registry.REQUEST_TOOLS or name == registry.UNDO_TOOL


def route(name, arguments, ctx, run):
	"""ActionInterceptor entry: one customize.request_* call -> approval or error dict."""
	if name == registry.UNDO_TOOL:
		return request_undo(arguments, ctx, run)
	return request(registry.REQUEST_TOOLS[name], arguments, ctx, run, via=name)
