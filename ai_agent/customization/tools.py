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
INSPECT_PARTS = ("fields", "properties", "client_scripts", "server_scripts", "reports", "print_formats", "workflows",
	"notifications", "permissions", "list_settings", "naming_rules", "dashboards")
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
	if not doctype and (arguments or {}).get("kind"):
		return list_kind(arguments["kind"], arguments.get("search"), ctx)
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
		# Frappe lets only Script Manager read Server Script: others see names, not code.
		show_code = policy.has_roles(ctx.user, ("Script Manager",))
		out["server_scripts"] = [_script_row(r, ("name", "script_type", "doctype_event", "api_method", "disabled"), code=show_code) for r in rows]
	if "reports" in include:
		rows = frappe.get_all("Report", filters={"ref_doctype": doctype},
			fields=["name", "report_type", "is_standard", "disabled", "query", "report_script"])
		out["reports"] = [{**{k: r.get(k) for k in ("name", "report_type", "is_standard", "disabled")}, "ai_made": _ai_made(r.get("query") or r.get("report_script"))} for r in rows]
	if "print_formats" in include:
		out["print_formats"] = frappe.get_all("Print Format", filters={"doc_type": doctype},
			fields=["name", "print_format_type", "standard", "disabled"])
	if "workflows" in include:
		out["workflows"] = frappe.get_all("Workflow", filters={"document_type": doctype}, fields=["name", "is_active"])
	if "notifications" in include:
		out["notifications"] = frappe.get_all("Notification", filters={"document_type": doctype},
			fields=["name", "channel", "event", "enabled"])
	for part, kind_name in (("permissions", "permissions"), ("list_settings", "list_settings")):
		if part in include:
			out[part] = _kind_source(kind_name, doctype, ctx)
	if "naming_rules" in include:
		out["naming"] = {"autoname": meta.autoname, "naming_series": (meta.get_field("naming_series").options or "").split("\n") if meta.get_field("naming_series") else []}
		out["naming_rules"] = frappe.get_all("Document Naming Rule", filters={"document_type": doctype},
			fields=["name", "prefix", "prefix_digits", "counter", "priority", "disabled"], order_by="priority desc")
	if "dashboards" in include:
		out["number_cards"] = frappe.get_all("Number Card", filters={"document_type": doctype}, fields=["name", "label", "function", "is_public"])
		out["dashboard_charts"] = frappe.get_all("Dashboard Chart", filters={"document_type": doctype}, fields=["name", "chart_type", "type", "is_public"])
	return out


def _kind_source(kind_name, name, ctx):
	try:
		getter = getattr(registry.get(kind_name), "get_source", None)
		return getter(name, ctx.user) if callable(getter) else None
	except (CustomizationError, frappe.DoesNotExistError):
		frappe.clear_last_message()
		return None


# customize.inspect without a DocType: the records of one kind, to find a name (API and scheduler scripts,
# standalone reports, cards, workspaces, web pages have no single DocType to start from).
LIST_KINDS = {
	"server_script": ("Server Script", ["name", "script_type", "reference_doctype", "doctype_event", "api_method", "disabled"], "script"),
	"client_script": ("Client Script", ["name", "dt", "view", "enabled"], "script"),
	"report": ("Report", ["name", "report_type", "ref_doctype", "is_standard", "disabled"], None),
	"print_format": ("Print Format", ["name", "doc_type", "print_format_type", "standard", "disabled"], None),
	"notification": ("Notification", ["name", "document_type", "channel", "event", "enabled"], None),
	"workflow": ("Workflow", ["name", "document_type", "is_active"], None),
	"naming": ("Document Naming Rule", ["name", "document_type", "prefix", "disabled"], None),
	"workspace": ("Workspace", ["name", "label", "module", "public"], None),
	"doctype": ("DocType", ["name", "module", "custom", "istable", "issingle"], None),
	"web": ("Web Page", ["name", "title", "route", "published"], None),
	"dashboard": ("Number Card", ["name", "label", "document_type"], None),
}


def list_kind(kind_name, search, ctx):
	if kind_name not in LIST_KINDS:
		return _error(f"Cannot list kind '{kind_name}'.", [f"kind is one of {', '.join(LIST_KINDS)}"])
	doctype, fields, source = LIST_KINDS[kind_name]
	filters = {"custom": 1} if kind_name == "doctype" else {}
	or_filters = {"name": ("like", f"%{search}%")} if search else None
	rows = frappe.get_all(doctype, filters=filters, or_filters=or_filters, fields=fields + ([source] if source else []), limit=200, order_by="modified desc")
	show_code = kind_name != "server_script" or policy.has_roles(ctx.user, ("Script Manager",))
	out = [{**{k: r.get(k) for k in fields}, **({"ai_made": _ai_made(r.get(source))} if source and show_code else {})} for r in rows]
	result = {"kind": kind_name, "doctype": doctype, "records": out, "count": len(out)}
	if kind_name == "dashboard":
		result["charts"] = frappe.get_all("Dashboard Chart", or_filters=or_filters, fields=["name", "chart_name", "document_type", "chart_type"], limit=200, order_by="modified desc")
	if kind_name == "web":
		result["web_forms"] = frappe.get_all("Web Form", or_filters=or_filters, fields=["name", "title", "route", "published", "doc_type"], limit=200, order_by="modified desc")
	return result


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


def _script_row(row, keys, code=True):
	lines = str(row.get("script") or "").strip().splitlines()
	if not code:
		return {**{k: row.get(k) for k in keys}, "lines": len(lines), "ai_made": _ai_made(row.get("script"))}
	return {**{k: row.get(k) for k in keys}, "first_lines": "\n".join(lines[:2]), "lines": len(lines), "ai_made": _ai_made(row.get("script"))}


GET_FIELDS = {
	"client_script": ("Client Script", ("name", "dt", "view", "enabled", "module", "script")),
	"server_script": ("Server Script", ("name", "script_type", "reference_doctype", "doctype_event", "api_method",
		"allow_guest", "disabled", "enable_rate_limit", "rate_limit_count", "rate_limit_seconds", "module", "script",
		"event_frequency", "cron_format")),
	"report": ("Report", ("name", "report_type", "ref_doctype", "is_standard", "disabled", "module", "query", "json",
		"prepared_report", "add_total_row", "report_script", "javascript")),
	"custom_field": ("Custom Field", ("name", "dt", "fieldname", "label", "fieldtype", "options", "insert_after", "reqd",
		"hidden", "read_only", "default", "description", "depends_on", "mandatory_depends_on", "read_only_depends_on",
		"fetch_from", "fetch_if_empty", "in_list_view", "in_standard_filter", "bold", "allow_on_submit", "no_copy",
		"print_hide", "unique", "permlevel", "precision", "length", "translatable", "owner")),
}


BASE_GET_KINDS = ("client_script", "server_script", "report", "custom_field")


def _has_source(kind_name):
	try:
		return callable(getattr(registry.get(kind_name), "get_source", None))
	except CustomizationError:
		return False


def handle_get(arguments, ctx):
	_assert_group(ctx, "customize.get")
	kind = (arguments or {}).get("kind")
	name = (arguments or {}).get("name")
	if kind == "server_script" and not policy.has_roles(ctx.user, ("Script Manager",)):
		return _error("Reading server script code needs the Script Manager role.")
	if kind in registry.KINDS and kind not in BASE_GET_KINDS and _has_source(kind):
		# A kind that reads its own record (recipients, states, sidebar, Web Form vs Web Page, card vs chart)
		# wins over the plain field list its _tool.json may also declare.
		return _get_from_kind(kind, name, arguments, ctx)
	if kind not in GET_FIELDS and kind in registry.KINDS:
		return _get_from_kind(kind, name, arguments, ctx)
	if kind not in GET_FIELDS:
		return _error(f"Unknown kind '{kind}'.", [f"kind is one of {', '.join(get_kinds())}"])
	doctype, keys = GET_FIELDS[kind]
	if not name or not frappe.db.exists(doctype, name):
		return _error(f"{doctype} '{name}' not found.", ["List what exists with customize__inspect."])
	doc = frappe.get_doc(doctype, name)
	out = {"kind": kind, "doctype": doctype, **{k: doc.get(k) for k in keys}}
	if kind == "report":
		out["filters"] = [{k: f.get(k) for k in ("fieldname", "label", "fieldtype", "options", "mandatory", "default")} for f in doc.get("filters") or []]
		out["columns"] = [{k: c.get(k) for k in ("fieldname", "label", "fieldtype", "options", "width")} for c in doc.get("columns") or []]
		out["roles"] = [r.role for r in doc.get("roles") or []]
	for key in ("script", "query", "report_script"):
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
	args = arguments.get("arguments")
	if isinstance(args, str):
		try:
			args = json.loads(args)
		except ValueError:
			return _error("arguments must be an object (the request tool's arguments), not text.")
	if not args:   # the request's arguments sent next to kind instead of inside "arguments"
		args = {k: v for k, v in arguments.items() if k not in ("kind", "arguments")}
	args = _clean(args)
	from ai_agent.runtime.tool_gateway import coerce_to_schema
	from ai_agent.runtime.tool_registry import CUSTOMIZATION_SCHEMAS

	coerce_to_schema(CUSTOMIZATION_SCHEMAS.get(tool) or {}, args)   # the request tool's own repairs
	try:
		policy.assert_size(args, kind_name)
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
		return ""  # the one-line summary is cosmetic; the checks and the request do not depend on it


def get_kinds():
	"""The kinds customize.get can read: the four with a field list, and every kind with ``get_source``."""
	out = list(GET_FIELDS)
	for name in registry.KINDS:
		if name in out:
			continue
		try:
			if callable(getattr(registry.get(name), "get_source", None)):
				out.append(name)
		except CustomizationError:
			continue  # the kind is not installed on this site
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"ai_agent customize: kind {name} failed to load")
			continue
	return out


def _get_from_kind(kind_name, name, arguments, ctx):
	"""customize.get for a kind that reads its own record (``get_source``): a notification with its
	recipients, a workflow with its states, the permission rules of a DocType, a whole custom DocType…"""
	kind = registry.get(kind_name)
	getter = getattr(kind, "get_source", None)
	if not callable(getter):
		return _error(f"customize.get cannot read the kind '{kind_name}'.", [f"kind is one of {', '.join(get_kinds())}"])
	if not name:
		return _error("Name the record to read.", ["List what exists with customize__inspect."])
	extra = {}
	if kind_name == "dashboard" and arguments.get("object"):
		extra["obj"] = arguments["object"]
	if kind_name == "web" and arguments.get("doctype"):
		extra["doctype"] = arguments["doctype"]
	try:
		out = getter(name, ctx.user, **extra)
	except frappe.DoesNotExistError:
		frappe.clear_last_message()
		return _error(f"'{name}' not found for {kind_name}.", ["List what exists with customize__inspect."])
	return out if isinstance(out, dict) else {"kind": kind_name, "source": out}


def _target_doctype(kind_name, spec):
	for key in ("doctype", "dt", "reference_doctype", "ref_doctype"):
		value = spec.get(key)
		if value and frappe.db.exists("DocType", value):
			return value
	return None


def _target_name(kind_name, spec):
	if kind_name == "report":
		return spec.get("report_name") or spec.get("name")
	if kind_name in ("client_script", "server_script", "web", "doctype", "print_format", "notification",
		"workflow", "dashboard", "workspace"):
		return spec.get("name") or spec.get("workflow_name")
	return None


def request(kind_name, arguments, ctx, run, *, via=None):
	"""Create the Customize approval for one customize.request_* call (Desk engine and MCP)."""
	args = _clean(arguments)
	try:
		policy.assert_size(args, kind_name)
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

# the kinds that describe themselves in kinds/<kind>_tool.json get their handler here
for _tool, _kind in registry.REQUEST_TOOLS.items():
	_handler_name = "handle_" + _tool.split(".", 1)[1]
	if _handler_name not in globals():
		globals()[_handler_name] = _request_tool(_kind, _tool)
for _spec in registry.TOOL_SPECS:
	if _spec.get("get_doctype") and _spec.get("get_fields") and _spec["kind"] not in GET_FIELDS:
		GET_FIELDS[_spec["kind"]] = (_spec["get_doctype"], tuple(_spec["get_fields"]))


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
	if execution.get("after") is None:
		# Without the state recorded after execution, an undo could overwrite later changes it cannot see.
		return _error("This change kept no record of its result, so it cannot be undone safely; undo it by hand or request a new change.")
	if not same_state(current, execution.get("after")):
		return _error("The target changed after this customization was executed; undo it by hand or request a new change.")
	tainted = bool((payload.get("context") or {}).get("tainted")) or bool(getattr(ctx, "tainted", False))
	undo = _normalized_undo(kind, undo, ctx)
	return _create(kind_name, kind, undo, ctx, run, via="customize.request_undo",
		purpose=f"Undo {original.name}", undo_of=original.name, tainted=tainted)


def _normalized_undo(kind, undo, ctx):
	"""An undo step is the kind's own spec: normalize it like a request (defaults, requested_by) when the kind
	accepts its keys; a kind whose undo carries internal keys normalize refuses keeps the raw step."""
	try:
		return kind.normalize(dict(undo), ctx.user)
	except CustomizationError:
		return undo


def is_request_tool(name):
	return name in registry.REQUEST_TOOLS or name == registry.UNDO_TOOL


def route(name, arguments, ctx, run):
	"""ActionInterceptor entry: one customize.request_* call -> approval or error dict."""
	if name == registry.UNDO_TOOL:
		return request_undo(arguments, ctx, run)
	return request(registry.REQUEST_TOOLS[name], arguments, ctx, run, via=name)
