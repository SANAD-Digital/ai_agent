"""Reports: Report Builder, Query Report and Script Report (docs/customization/01 §3.3.5, §3.1 v2).

Report Builder first: it runs through ``frappe.get_list(..., user=user)``, so row permissions apply,
and it needs no Script Manager. A Query Report runs raw SQL (``frappe.db.sql``) without row
permissions: ``analyzers.sql`` checks it is one read-only SELECT on readable, non-system tables, that
``%(x)s`` placeholders match the filters, and warns for every role that would see more than in the list.

A Script Report (``is_standard="No"``) keeps Python in ``report_script``, run by ``Report.execute_script``
through ``safe_exec`` with the locals ``filters``/``data``/``result`` (frappe/core/doctype/report/report.py
``execute_script``), and its filters as JavaScript in ``javascript`` (``frappe.query_reports["<name>"] =
{filters: [...]}``, served by ``frappe.desk.query_report.get_script`` when the module has no .js file), which
allows MultiSelectList and Link filters with ``get_query``. The Python goes through ``analyzers.python_code``
(the Server Script rules), the JavaScript through ``analyzers.js_code``, and the check runs the script once on
sample filters in a rolled-back savepoint as the requester. Saving it needs Script Manager
(``Report.validate`` -> ``frappe.only_for("Script Manager")``) and server scripts enabled (``safe_exec``).
"""

from __future__ import annotations

import json
import re

import frappe
from frappe import _
from frappe.utils import cint, cstr

from ai_agent.customization.analyzers import sql as sql_analyzer
from ai_agent.customization.analyzers.python_code import protected_doctype
from ai_agent.customization.contract import Check, CustomizationError
from ai_agent.customization.kinds import _base

TOP_KEYS = ("operation", "report_name", "ref_doctype", "report_type", "builder", "query", "report_script", "javascript",
	"filters", "columns", "roles", "sample_filters", "purpose")
OPERATIONS = ("create", "update", "disable", "enable")
REPORT_TYPES = ("Report Builder", "Query Report", "Script Report")
CODE_TYPES = ("Query Report", "Script Report")  # need Script Manager (Report.validate)
MAX_SCRIPT_CHARS = 20000
JS_FILTER_TYPES = ("Autocomplete", "Check", "Currency", "Data", "Date", "DateRange", "Datetime",
	"Dynamic Link", "Float", "Int", "Link", "MultiSelect", "MultiSelectList", "Select", "Time")
FILTER_KEYS = ("fieldname", "label", "fieldtype", "options", "mandatory", "default")
COLUMN_KEYS = ("fieldname", "label", "fieldtype", "options", "width")
FILTER_TYPES = ("Check", "Currency", "Data", "Date", "Datetime", "Dynamic Link", "Float", "Int", "Link", "Select", "Time")
COLUMN_TYPES = FILTER_TYPES + ("Duration",)
BUILDER_KEYS = ("fields", "filters", "order_by", "group_by", "add_totals_row")
# Keys the Report Builder screen itself saves: accepted so an undo can restore a report made there.
BUILDER_UI_KEYS = ("page_length", "column_widths", "sort_by", "sort_order", "sort_by_next", "sort_order_next",
	"columns", "chart_args", "add_total_row", "group_by_args")
BUILDER_OPERATORS = ("=", "!=", ">", "<", ">=", "<=", "in", "not in", "like", "not like", "between", "is",
	"descendants of", "ancestors of", "not descendants of", "not ancestors of")
AGGREGATES = ("count", "sum", "avg")
STANDARD_COLUMNS = ("name", "owner", "creation", "modified", "modified_by", "docstatus", "idx", "parent")
FIELDNAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
QUALIFIED_RE = re.compile(r"^`?(?:tab([^`.]+))`?\.`?([a-z_][a-z0-9_]*)`?$")
SAVED_FIELDS = ("report_type", "ref_doctype", "query", "json", "disabled")
JS_FILTER_RE = re.compile(r"""\bfieldname\s*:\s*(["'])([A-Za-z_][A-Za-z0-9_]*)\1""")
JS_PROP_RE = {key: re.compile(r"\b" + key + r"""\s*:\s*(["'`])(.*?)\1""", re.S) for key in ("fieldtype", "options", "label", "default")}
JS_REQD_RE = re.compile(r"\breqd\s*:\s*(1|true)\b")
SCRIPT_FILTER_USE_RE = re.compile(r"""\bfilters\s*(?:\.\s*get\s*\(\s*(["'])([A-Za-z_][A-Za-z0-9_]*)\1|\[\s*(["'])([A-Za-z_][A-Za-z0-9_]*)\3\s*\]|\.([A-Za-z_][A-Za-z0-9_]*))""")
SCRIPT_REPORT_PREVIEW_ROWS = 5


def _rows(value, keys, types, where):
	if value in (None, ""):
		return None
	if not isinstance(value, list):
		raise CustomizationError(_("{0} must be a list").format(where))
	out = []
	for i, row in enumerate(value, 1):
		if not isinstance(row, dict):
			raise CustomizationError(_("{0}[{1}] must be an object").format(where, i))
		_base.reject_unknown(row, keys, f"{where}[{i}]")
		_base.require(row, ("fieldname", "label", "fieldtype"), f"{where}[{i}]")
		if not FIELDNAME_RE.match(cstr(row["fieldname"])):
			raise CustomizationError(_("{0}[{1}].fieldname must be snake_case").format(where, i))
		if row["fieldtype"] not in types:
			raise CustomizationError(_("{0}[{1}].fieldtype must be one of {2}").format(where, i, ", ".join(types)))
		clean = {k: (cint(v) if k in ("mandatory", "width") else cstr(v)) for k, v in row.items() if v is not None}
		out.append(clean)
	return out


def _sql_header(purpose, requested_by, approved_by=None, approval=None):
	text = _base.header("/*", cstr(purpose).replace("*/", "").replace("%", "%%"), requested_by, approved_by, approval, suffix=" */")
	return "\n" + text


def strip_sql_header(query):
	return re.sub(r"\s*/\*\s*ai_agent:.*?\*/\s*$", "", cstr(query), flags=re.S).rstrip()


def _tables_of(query):
	from sqlglot import exp

	try:
		statements = sql_analyzer._parse(query)
	except Exception:
		# None, not []: no tables means no roles, and a Report without roles is open to every user
		frappe.log_error(frappe.get_traceback(), "ai_agent customize: report could not parse the query for its roles")
		return None
	out = []
	for statement in statements:
		if statement is None:
			continue
		ctes = {c.alias_or_name for c in statement.find_all(exp.CTE)}
		for table in statement.find_all(exp.Table):
			if table.name.startswith("tab") and table.name not in ctes and table.name[3:] not in out:
				out.append(table.name[3:])
	return out


def _py_header(purpose, requested_by, approved_by=None, approval=None):
	return _base.header("#", purpose, requested_by, approved_by, approval)


def with_py_header(script, purpose, requested_by, approved_by=None, approval=None):
	return _py_header(purpose, requested_by, approved_by, approval) + "\n" + _base.strip_header(script).lstrip("\n")


def with_js_header(javascript, purpose, requested_by, approved_by=None, approval=None):
	return _base.header("//", purpose, requested_by, approved_by, approval) + "\n" + _base.strip_header(javascript).lstrip("\n")


def js_filters(javascript):
	"""Best-effort list of the filters a ``frappe.query_reports[...] = {filters: [...]}`` script declares:
	``[{fieldname, fieldtype, options, label, default, mandatory}]`` (used for sample values and checks only)."""
	text = _base.strip_header(javascript or "")
	matches = list(JS_FILTER_RE.finditer(text))
	out = []
	for i, match in enumerate(matches):
		start = text.rfind("{", 0, match.start())
		end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
		chunk = text[max(start, 0):end]
		row = {"fieldname": match.group(2)}
		for key, regex in JS_PROP_RE.items():
			found = regex.search(chunk)
			if found:
				row[key] = found.group(2)
		row["mandatory"] = 1 if JS_REQD_RE.search(chunk) else 0
		out.append(row)
	return out


def script_filter_names(script):
	"""Filter names a report script reads: filters.get("x"), filters["x"], filters.x."""
	names = []
	for match in SCRIPT_FILTER_USE_RE.finditer(_base.strip_header(script or "")):
		name = match.group(2) or match.group(4) or match.group(5)
		if name and name not in ("get", "items", "keys", "values", "copy", "update", "setdefault", "pop") and name not in names:
			names.append(name)
	return names


def report_sample_values(filters, given=None):
	"""Sample filter values for a preview run; MultiSelectList filters get a list (the report screen sends a list)."""
	given = dict(given or {})
	plain = [f for f in filters or [] if f.get("fieldtype") not in ("MultiSelectList", "MultiSelect", "DateRange")]
	values = sql_analyzer.sample_values(plain, given)
	for f in filters or []:
		name = f.get("fieldname")
		if not name or name in values:
			continue
		if f.get("fieldtype") == "MultiSelectList":
			default = sql_analyzer.sample_values([{"fieldname": name, "fieldtype": "Link", "options": f.get("options")}]).get(name)
			values[name] = [default] if default else []
		elif f.get("fieldtype") == "DateRange":
			from frappe.utils import get_first_day, nowdate

			values[name] = [str(get_first_day(nowdate())), nowdate()]
		else:
			values[name] = ""
	return values


def _column_dicts(columns):
	from frappe.desk.query_report import get_column_as_dict

	return [get_column_as_dict(c) for c in columns or []]


class ReportKind:
	name = "report"
	setting = "allow_customize_reports"
	required_roles = ("System Manager",)

	def roles_for(self, spec):
		report_type = spec.get("report_type") or (frappe.db.get_value("Report", spec.get("report_name"), "report_type") if spec.get("report_name") else None)
		if report_type in CODE_TYPES and spec.get("operation") != "disable":
			return ("System Manager", "Script Manager")
		return self.required_roles

	# -- normalize --------------------------------------------------------------
	def normalize(self, args, user):
		args = dict(args or {})
		_base.reject_unknown(args, TOP_KEYS, "customize.request_report")
		_base.require(args, ("operation", "report_name"), "customize.request_report")
		operation = args["operation"]
		if operation not in OPERATIONS:
			raise CustomizationError(_("operation must be one of: create, update, disable"))
		name = re.sub(r"\s+", " ", cstr(args["report_name"])).strip()[:140]
		spec = {"operation": operation, "report_name": name, "requested_by": user}
		if args.get("purpose"):
			spec["purpose"] = re.sub(r"\s+", " ", cstr(args["purpose"])).strip()[:140]
		exists = frappe.db.exists("Report", name)
		if operation in ("disable", "enable"):
			if not exists:
				raise CustomizationError(_("There is no report {0}").format(name))
			return spec
		if operation == "update":
			if not exists:
				raise CustomizationError(_("There is no report {0}").format(name), hints=[_("Use operation create")])
			current = frappe.db.get_value("Report", name, ["report_type", "ref_doctype", "is_standard"], as_dict=True)
			if current.is_standard == "Yes":
				raise CustomizationError(_("{0} is a standard report and cannot be changed; create a copy").format(name))
			if current.report_type not in REPORT_TYPES:
				raise CustomizationError(_("{0} is a {1}; only Report Builder, Query Report and Script Report reports can be changed").format(name, current.report_type))
			if args.get("report_type") and args["report_type"] != current.report_type:
				raise CustomizationError(_("The report type cannot change; create a new report"))
			spec["report_type"] = current.report_type
			spec["ref_doctype"] = cstr(args.get("ref_doctype") or current.ref_doctype)
		else:
			_base.require(args, ("ref_doctype", "report_type"), "customize.request_report")
			if args["report_type"] not in REPORT_TYPES:
				raise CustomizationError(_("report_type must be Report Builder, Query Report or Script Report"))
			spec["report_type"] = args["report_type"]
			spec["ref_doctype"] = cstr(args["ref_doctype"]).strip()
			spec.setdefault("purpose", name)

		code_keys = [k for k in ("report_script", "javascript") if args.get(k) not in (None, "")]
		if spec["report_type"] != "Script Report" and code_keys:
			raise CustomizationError(_("{0} belongs to a Script Report only").format(", ".join(code_keys)),
				hints=[_("Use report_type Script Report, or builder (Report Builder) / query (Query Report)")])
		if spec["report_type"] == "Script Report":
			self._normalize_script(args, spec, user, operation)
		elif spec["report_type"] == "Report Builder":
			if args.get("query"):
				raise CustomizationError(_("A Report Builder report takes builder, not query"))
			if operation == "create":
				_base.require(args, ("builder",), "customize.request_report (Report Builder)")
			if args.get("builder") is not None:
				builder = args["builder"]
				if not isinstance(builder, dict):
					raise CustomizationError(_("builder must be an object"))
				_base.reject_unknown(builder, BUILDER_KEYS + BUILDER_UI_KEYS, "builder")
				spec["builder"] = builder
		else:
			if args.get("builder"):
				raise CustomizationError(_("A Query Report takes query, not builder"))
			if operation == "create":
				_base.require(args, ("query",), "customize.request_report (Query Report)")
			if args.get("query"):
				query = strip_sql_header(args["query"]).strip().rstrip(";").strip()
				spec["query"] = query + _sql_header(spec.get("purpose") or name, user)

		if spec["report_type"] == "Script Report" and args.get("filters") and (args.get("javascript") or (
				operation == "update" and frappe.db.get_value("Report", name, "javascript"))):
			raise CustomizationError(_("A Script Report with javascript takes its filters from the javascript; the filters table would be ignored"),
				hints=[_("Put every filter in frappe.query_reports[\"{0}\"] = {{filters: [...]}} and leave out filters").format(name)])
		filters = _rows(args.get("filters"), FILTER_KEYS, FILTER_TYPES, "filters")
		columns = _rows(args.get("columns"), COLUMN_KEYS, COLUMN_TYPES, "columns")
		if filters is not None:
			spec["filters"] = filters
		if columns is not None:
			spec["columns"] = columns
		if args.get("roles") is not None:
			roles = args["roles"]
			if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
				raise CustomizationError(_("roles must be a list of role names"))
			missing = [r for r in roles if not frappe.db.exists("Role", r)]
			if missing:
				raise CustomizationError(_("Unknown role(s): {0}").format(", ".join(missing)))
			spec["roles"] = sorted(set(roles))
		elif operation == "create" and spec["report_type"] == "Query Report":
			tables = _tables_of(spec["query"])
			# A Report with no roles is open to every user: no common read role means System Manager only.
			spec["roles"] = (sql_analyzer.default_roles(tables) if tables is not None else None) or ["System Manager"]
		if args.get("sample_filters") is not None:
			if not isinstance(args["sample_filters"], dict):
				raise CustomizationError(_("sample_filters must be an object"))
			spec["sample_filters"] = args["sample_filters"]
		return spec

	def _normalize_script(self, args, spec, user, operation):
		for key in ("builder", "query"):
			if args.get(key):
				raise CustomizationError(_("A Script Report takes report_script (and javascript for filters), not {0}").format(key))
		if operation == "create":
			_base.require(args, ("report_script",), "customize.request_report (Script Report)")
		purpose = spec.get("purpose") or spec["report_name"]
		if operation == "update" and args.get("javascript") == "":
			spec["javascript"] = ""  # removes the javascript (an undo of a report that had none)
		for key in ("report_script", "javascript"):
			if args.get(key) in (None, ""):
				continue
			text = cstr(args[key])
			if len(text) > MAX_SCRIPT_CHARS:
				raise CustomizationError(_("{0} is longer than {1} characters").format(key, MAX_SCRIPT_CHARS))
			spec[key] = with_py_header(text, purpose, user) if key == "report_script" else with_js_header(text, purpose, user)

	# -- snapshot -----------------------------------------------------------------
	def snapshot(self, spec):
		name = spec["report_name"]
		if not frappe.db.exists("Report", name):
			return {"report_name": name, "exists": False}
		doc = frappe.get_doc("Report", name)
		return {
			"report_name": name, "exists": True, "disabled": cint(doc.disabled), "report_type": doc.report_type,
			"ref_doctype": doc.ref_doctype, "query_sha256": _base.sha256(doc.query), "json_sha256": _base.sha256(doc.json),
			"filters": [{k: cstr(f.get(k)) for k in FILTER_KEYS} for f in doc.filters],
			"columns": [{k: cstr(c.get(k)) for k in COLUMN_KEYS} for c in doc.columns],
			"roles": sorted(r.role for r in doc.roles),
			# Only Script Reports carry their code in the snapshot, so the snapshots of Builder and Query reports
			# keep the shape older pending approvals were stored with.
			**({"report_script_sha256": _base.sha256(doc.report_script), "javascript_sha256": _base.sha256(doc.javascript)}
				if doc.report_type == "Script Report" else {}),
		}

	# -- effective record ------------------------------------------------------------
	def _effective(self, spec):
		current = {}
		if spec["operation"] != "create" and frappe.db.exists("Report", spec["report_name"]):
			doc = frappe.get_doc("Report", spec["report_name"])
			current = {
				"report_type": doc.report_type, "ref_doctype": doc.ref_doctype, "query": doc.query,
				"builder": {k: v for k, v in json.loads(doc.json or "{}").items() if k != "ai_agent"}
				if doc.report_type == "Report Builder" else None,
				"filters": [{k: f.get(k) for k in FILTER_KEYS if f.get(k) not in (None, "")} for f in doc.filters],
				"columns": [{k: c.get(k) for k in COLUMN_KEYS if c.get(k) not in (None, "")} for c in doc.columns],
				"roles": sorted(r.role for r in doc.roles), "disabled": cint(doc.disabled),
				"report_script": doc.report_script, "javascript": doc.javascript,
			}
		merged = {**current, **{k: v for k, v in spec.items() if k in
			("report_type", "ref_doctype", "query", "builder", "report_script", "javascript", "filters", "columns", "roles")}}
		return merged, current

	# -- check --------------------------------------------------------------------
	def check(self, spec, user):
		merged, current = self._effective(spec)
		report_type, ref = merged.get("report_type"), merged.get("ref_doctype")
		roles_needed = self.roles_for(spec)
		why = (_("Frappe allows only Script Managers to save Query Reports and Script Reports")
			if "Script Manager" in roles_needed else _("Frappe requires it"))
		checks = _base.role_checks(user, roles_needed, why)
		exists = frappe.db.exists("Report", spec["report_name"])
		if spec["operation"] == "create" and exists:
			checks.append(Check("report.name_taken", "critical", _("A report named {0} already exists; use operation update").format(spec["report_name"])))
		if spec["operation"] in ("disable", "enable"):
			return _base.build_report(checks, impact=[_("{0} the report {1}").format(spec["operation"].title(), spec["report_name"])],
				preview={"report_name": spec["report_name"], "operation": spec["operation"]}, risk="low")
		if not ref or not frappe.db.exists("DocType", ref):
			checks.append(Check("report.ref_doctype", "critical", _("There is no DocType {0}").format(ref)))
			return _base.build_report(checks, risk="medium")
		if protected_doctype(ref):
			checks.append(Check("report.ref_doctype", "critical", _("{0} controls code or access; reports on it are not allowed").format(ref)))
		elif not frappe.has_permission(ref, "read", user=user):
			checks.append(Check("report.ref_doctype", "critical", _("{0} cannot read {1}").format(user, ref)))

		preview = {"report_name": spec["report_name"], "report_type": report_type, "ref_doctype": ref,
			"filters": merged.get("filters") or [], "columns": merged.get("columns") or [], "roles": merged.get("roles") or []}
		impact = []
		if report_type == "Script Report":
			return self._check_script(spec, merged, current, user, checks, preview)
		if report_type == "Report Builder":
			checks += self._check_builder(merged.get("builder") or {}, ref)
			impact.append(_("Applies each user's own permissions on {0}").format(ref))
			if not any(c.level == "critical" for c in checks):
				checks += self._preview_builder(spec, merged, user, preview)
			risk = "low"
		else:
			query = merged.get("query") or ""
			analysis, tables = sql_analyzer.analyze(query, merged.get("filters"), user, merged.get("roles"))
			checks += analysis
			preview["code"] = {"language": "sql", "text": query}
			if spec["operation"] == "update" and "query" in spec:
				import difflib

				preview["code"]["diff"] = "".join(difflib.unified_diff(cstr(current.get("query")).splitlines(True),
					query.splitlines(True), "current", "proposed"))
			preview["tables"] = tables
			roles = merged.get("roles") or []
			impact.append(_("Reads {0} without row permissions").format(", ".join(tables) or ref))
			impact.append(_("Can be run by: {0}").format(", ".join(roles) or _("every role that reads {0}").format(ref)))
			if not any(c.level == "critical" for c in checks):
				values = sql_analyzer.sample_values(merged.get("filters"), spec.get("sample_filters"))
				preview["sample_filters"] = values
				try:
					preview["rows"] = sql_analyzer.preview(query, values, ref)
					checks.append(Check("sql.preview", "ok", _("The query runs: {0} row(s) in the preview").format(len(preview["rows"]["rows"]))))
				except Exception as e:
					checks.append(Check("sql.preview", "critical", _("The query fails: {0}").format(str(e)[:300])))
				checks += sql_analyzer.explain(query, values)
				if not merged.get("columns"):
					checks.append(Check("report.columns", "info", _("No columns given: they come from the query's column names")))
			risk = "medium"
		return _base.build_report(checks, impact=impact, preview=preview, risk=risk)

	# -- Script Report --------------------------------------------------------------
	def _check_script(self, spec, merged, current, user, checks, preview):
		import difflib

		from ai_agent.customization.analyzers import js_code, python_code
		from ai_agent.customization.kinds.server_script import server_scripts_enabled

		ref, name = merged.get("ref_doctype"), spec["report_name"]
		script, javascript = cstr(merged.get("report_script")), cstr(merged.get("javascript"))
		enabled = server_scripts_enabled()
		if not enabled:
			checks.append(Check("script.server_scripts_disabled", "critical",
				_("Server Scripts are disabled on this server, so a Script Report cannot run (bench set-config -g server_script_enabled 1)")))

		def code(language, key, text):
			out = {"language": language, "text": text}
			if spec["operation"] == "update" and key in spec:
				out["diff"] = "".join(difflib.unified_diff(cstr(current.get(key)).splitlines(True), text.splitlines(True), "current", "proposed"))
			return out

		analysis = python_code.analyze(script, "Script Report", ref)
		checks += analysis
		code_ok = not any(c.level == "critical" for c in analysis)
		preview["code"] = code("python", "report_script", script)

		declared = merged.get("filters") or []
		if javascript:
			preview["javascript"] = code("javascript", "javascript", javascript)
			checks += js_code.analyze(javascript, "Report", ref)
			targets = [m.group(2) for m in re.finditer(r"""frappe\.query_reports\s*\[\s*(["'`])(.*?)\1\s*\]\s*=(?!=)""", javascript)]
			if not targets:
				checks.append(Check("report.js_target", "critical",
					_("javascript must set frappe.query_reports[\"{0}\"] = {{filters: [...]}}").format(name)))
			for target in targets:
				if target != name:
					checks.append(Check("report.js_target", "critical",
						_("javascript sets frappe.query_reports[\"{0}\"]; it must use this report's name: {1}").format(target, name)))
			declared = js_filters(javascript)
			for f in declared:
				if f.get("fieldtype") and f["fieldtype"] not in JS_FILTER_TYPES:
					checks.append(Check("report.filter_type", "warning", _("Filter {0} has the unknown fieldtype {1}").format(f["fieldname"], f["fieldtype"])))
				options = f.get("options")
				if f.get("fieldtype") == "MultiSelectList" and options and frappe.db.exists("DocType", options) and not re.search(
						r"""frappe\.(?:db\.)?get_list\(\s*(["'])""" + re.escape(options) + r"\1", script):
					checks.append(Check("report.multiselect_permission", "warning",
						_("Frappe checks a Link filter's value against the viewer's permissions but not the values of the MultiSelectList filter {0}: keep only the {1} the viewer may read, e.g. allowed = frappe.get_list(\"{1}\", pluck=\"name\")").format(f["fieldname"], options)))
		preview["filters"] = declared
		declared_names = {f.get("fieldname") for f in declared}
		for used in script_filter_names(script):
			if used not in declared_names:
				checks.append(Check("report.filter_undeclared", "warning",
					_("The script reads filters {0}, but no filter is named {0}: it is always empty").format(used)))

		if not merged.get("roles") and ref and frappe.db.exists("DocType", ref):
			preview["roles"] = sorted({p.role for p in frappe.get_meta(ref).permissions if not p.permlevel})
		impact = [
			_("Runs this Python each time someone opens the report, with that person's permissions: frappe.get_list applies them, frappe.get_all, frappe.db.get_value and frappe.get_doc do not"),
			_("Can be run by: {0}").format(", ".join(preview.get("roles") or merged.get("roles") or []) or _("every role that reads {0}").format(ref)),
		]
		if enabled and code_ok and not any(c.level == "critical" for c in checks):
			checks += self._preview_script(spec, merged, script, declared, user, preview)
		return _base.build_report(checks, impact=impact, preview=preview, risk="high")

	def _preview_script(self, spec, merged, script, declared, user, preview):
		"""Run the script once on sample filters as the requester (rolled back) and show the first rows."""
		from frappe.desk.query_report import get_filtered_data, normalize_result

		from ai_agent.customization.analyzers import python_code
		from ai_agent.runtime import masking

		values = report_sample_values(declared, spec.get("sample_filters"))
		preview["sample_filters"] = sql_analyzer.json_safe(values)
		try:
			with _base.as_user(user):
				out = python_code.run_report_script(script, values, spec["report_name"])
		except python_code.DryRunBlocked as e:
			return [Check("report.preview_blocked", "warning", _("The preview stopped at {0}, which is not run in a preview").format(e))]
		except frappe.ValidationError as e:
			return [Check("report.preview_throw", "warning",
				_("On the sample filters the report stops with: {0}. Give sample_filters to see rows").format(frappe.utils.strip_html(str(e))[:300]))]
		except Exception as e:
			return [Check("report.preview", "critical", _("The script fails when it runs: {0}: {1}").format(type(e).__name__, str(e)[:300]))]

		data, result = out["data"], out["result"]
		if data:  # Report.execute_script: `if loc["data"]: return loc["data"]`
			if not isinstance(data, list | tuple) or len(data) < 2:
				return [Check("report.output", "critical", _("data must be [columns, rows] (optionally [columns, rows, message, chart, report_summary])"))]
			columns, rows = data[0], data[1]
		else:
			columns, rows = merged.get("columns") or [], result
			if result is None:
				return [Check("report.output", "critical", _("On the sample filters the script set neither data nor result"))]
			if not columns:
				return [Check("report.columns", "critical",
					_("The script sets result without data and the report has no columns: set data = [columns, rows], or send columns"))]
		if not isinstance(columns, list | tuple) or not isinstance(rows, list | tuple):
			return [Check("report.output", "critical", _("columns and rows must both be lists"))]
		try:
			column_dicts = _column_dicts(columns)
			rows = normalize_result(list(rows), column_dicts) or []
			if rows and not isinstance(rows[0], dict):
				raise TypeError(_("each row must be a list or a dict"))
		except Exception as e:
			return [Check("report.output", "critical", _("The rows do not match the columns: {0}").format(str(e)[:300]))]
		try:  # what the viewer gets: Frappe drops rows whose Link columns the viewer's User Permissions exclude
			with _base.as_user(user):
				rows = get_filtered_data(merged.get("ref_doctype"), column_dicts, rows, user) if rows else rows
		except Exception:
			# fails closed: never show rows the viewer's User Permissions could exclude
			frappe.log_error(frappe.get_traceback(), "ai_agent customize: report preview could not apply User Permissions")
			rows = []
		labels = [c.get("fieldname") for c in column_dicts]
		preview["columns"] = [{k: c.get(k) for k in ("fieldname", "label", "fieldtype", "options") if c.get(k)} for c in column_dicts]
		preview["rows"] = {"columns": labels, "rows": sql_analyzer.json_safe(
			masking.mask([{k: r.get(k) for k in labels} for r in rows[:SCRIPT_REPORT_PREVIEW_ROWS]], merged.get("ref_doctype")))}
		return [Check("report.preview", "ok", _("The script runs with {0}'s permissions: {1} row(s) on the sample filters").format(user, len(rows)))]

	def _check_builder(self, builder, ref):
		out = []
		meta = frappe.get_meta(ref)
		children = {df.options for df in meta.get_table_fields()}

		def field_ok(fieldname, doctype):
			if doctype != ref and doctype not in children:
				return _("{0} is neither {1} nor one of its child tables").format(doctype, ref)
			if fieldname in STANDARD_COLUMNS or frappe.get_meta(doctype).get_field(fieldname):
				return None
			return _("{0} has no field {1}").format(doctype, fieldname)

		fields = builder.get("fields") or []
		if not isinstance(fields, list) or not fields:
			out.append(Check("builder.fields", "critical", _("builder.fields must list [fieldname, doctype] pairs")))
		for item in fields if isinstance(fields, list) else []:
			if not (isinstance(item, list) and len(item) == 2):
				out.append(Check("builder.fields", "critical", _("builder.fields items are [fieldname, doctype]: {0}").format(item)))
				continue
			error = field_ok(item[0], item[1])
			if error:
				out.append(Check("builder.fields", "critical", error))
		for item in builder.get("filters") or []:
			if not (isinstance(item, list) and len(item) in (3, 4)):
				out.append(Check("builder.filters", "critical", _("builder.filters items are [doctype, fieldname, operator, value]: {0}").format(item)))
				continue
			doctype, fieldname, op = (item[0], item[1], item[2]) if len(item) == 4 else (ref, item[0], item[1])
			error = field_ok(fieldname, doctype)
			if error:
				out.append(Check("builder.filters", "critical", error))
			if cstr(op).lower() not in BUILDER_OPERATORS:
				out.append(Check("builder.filters", "critical", _("Operator {0} is not allowed").format(op)))
		order_by = cstr(builder.get("order_by")).strip()
		for part in [p.strip() for p in order_by.split(",") if p.strip()]:
			pieces = part.split()
			if len(pieces) > 2 or (len(pieces) == 2 and pieces[1].lower() not in ("asc", "desc")):
				out.append(Check("builder.order_by", "critical", _("order_by must be `tabDocType`.`field` asc|desc")))
				continue
			match = QUALIFIED_RE.match(pieces[0])
			doctype, fieldname = (match.group(1), match.group(2)) if match else (ref, pieces[0].strip("`"))
			error = field_ok(fieldname, doctype)
			if error:
				out.append(Check("builder.order_by", "critical", error))
		group = builder.get("group_by")
		if group:
			if not isinstance(group, dict):
				out.append(Check("builder.group_by", "critical", _("group_by must be an object")))
			else:
				match = QUALIFIED_RE.match(cstr(group.get("group_by")))
				if not match:
					out.append(Check("builder.group_by", "critical", _("group_by.group_by must be `tabDocType`.`field`")))
				elif field_ok(match.group(2), match.group(1)):
					out.append(Check("builder.group_by", "critical", field_ok(match.group(2), match.group(1))))
				if cstr(group.get("aggregate_function")).lower() not in AGGREGATES:
					out.append(Check("builder.group_by", "critical", _("aggregate_function must be count, sum or avg")))
				if group.get("aggregate_on") and not FIELDNAME_RE.match(cstr(group["aggregate_on"])):
					out.append(Check("builder.group_by", "critical", _("aggregate_on must be a fieldname")))
		return out

	def _report_doc(self, spec, merged, json_text=None, query=None, code=None):
		doc = frappe.new_doc("Report")
		doc.update({"report_name": spec["report_name"], "ref_doctype": merged.get("ref_doctype"),
			"report_type": merged.get("report_type"), "is_standard": "No",
			"module": frappe.db.get_value("DocType", merged.get("ref_doctype"), "module")})
		if merged.get("report_type") == "Report Builder":
			doc.json = json_text or json.dumps(merged.get("builder") or {})
		elif merged.get("report_type") == "Script Report":
			doc.report_script = (code or {}).get("report_script") or merged.get("report_script")
			doc.javascript = (code or {}).get("javascript") or merged.get("javascript")
		else:
			doc.query = query or merged.get("query")
		for f in merged.get("filters") or []:
			doc.append("filters", f)
		for c in merged.get("columns") or []:
			doc.append("columns", c)
		for role in merged.get("roles") or []:
			doc.append("roles", {"role": role})
		return doc

	def _preview_builder(self, spec, merged, user, preview):
		from ai_agent.runtime import masking

		doc = self._report_doc(spec, merged)
		try:
			columns, rows = doc.run_standard_report({}, 5, user)
		except Exception as e:
			return [Check("builder.preview", "critical", _("The report fails: {0}").format(frappe.utils.strip_html(str(e))[:300]))]
		labels = [c.get("fieldname") for c in columns]
		preview["rows"] = {"columns": labels,
			"rows": sql_analyzer.json_safe(masking.mask([dict(zip(labels, r)) for r in rows[:5]], merged.get("ref_doctype")))}
		return [Check("builder.preview", "ok", _("The report runs with {0}'s permissions: {1} row(s) in the preview").format(user, len(rows)))]

	# -- execute / verify ---------------------------------------------------------------
	def execute(self, spec, user, approval=None):
		approval = _base.approval_name(approval)
		name = spec["report_name"]
		merged, current = self._effective(spec)
		with _base.as_user(user):
			if spec["operation"] in ("disable", "enable"):
				doc = frappe.get_doc("Report", name)
				doc.disabled = 1 if spec["operation"] == "disable" else 0
				doc.save()
				undo = {"operation": "enable" if spec["operation"] == "disable" else "disable", "report_name": name}
			else:
				json_text = query = None
				if merged.get("report_type") == "Report Builder" and merged.get("builder") is not None:
					builder = {k: v for k, v in (merged.get("builder") or {}).items() if k != "ai_agent"}
					builder["ai_agent"] = _base.header("", spec.get("purpose"), spec.get("requested_by"), user, approval).strip()
					json_text = json.dumps(builder)
				if merged.get("report_type") == "Query Report" and merged.get("query"):
					query = strip_sql_header(merged["query"]) + _sql_header(spec.get("purpose"), spec.get("requested_by"), user, approval)
				code = {}
				if merged.get("report_type") == "Script Report":
					purpose = spec.get("purpose") or spec["report_name"]
					if merged.get("report_script"):
						code["report_script"] = with_py_header(merged["report_script"], purpose, spec.get("requested_by"), user, approval)
					if merged.get("javascript"):
						code["javascript"] = with_js_header(merged["javascript"], purpose, spec.get("requested_by"), user, approval)
				if spec["operation"] == "create":
					doc = self._report_doc(spec, merged, json_text, query, code)
					doc.insert()
					undo = {"operation": "disable", "report_name": doc.name}
				else:
					doc = frappe.get_doc("Report", name)
					undo = {"operation": "update", "report_name": name, "purpose": _("undo of {0}").format(approval or "")}
					if "builder" in spec:
						undo["builder"] = current.get("builder") or {}
						doc.json = json_text
					if "query" in spec:
						undo["query"] = strip_sql_header(current.get("query"))
						doc.query = query
					for key in ("report_script", "javascript"):
						if key in spec:
							undo[key] = _base.strip_header(current.get(key)) or ""
							doc.set(key, code.get(key))
					if "ref_doctype" in spec and spec["ref_doctype"] != current.get("ref_doctype"):
						undo["ref_doctype"] = current.get("ref_doctype")
						doc.ref_doctype = spec["ref_doctype"]
					if "filters" in spec:
						undo["filters"] = current.get("filters") or []
						doc.set("filters", [])
						for f in spec["filters"]:
							doc.append("filters", f)
					if "columns" in spec:
						undo["columns"] = current.get("columns") or []
						doc.set("columns", [])
						for c in spec["columns"]:
							doc.append("columns", c)
					if "roles" in spec:
						undo["roles"] = current.get("roles") or []
						doc.set("roles", [])
						for role in spec["roles"]:
							doc.append("roles", {"role": role})
					doc.save()
		out = {"targets": [{"doctype": "Report", "name": doc.name}], "undo": undo,
			"query_sha256": _base.sha256(doc.query), "json_sha256": _base.sha256(doc.json), "disabled": cint(doc.disabled)}
		if doc.report_type == "Script Report":
			out["report_script_sha256"] = _base.sha256(doc.report_script)
			out["javascript_sha256"] = _base.sha256(doc.javascript)
		return out

	def verify(self, spec, result):
		name = spec["report_name"]
		if not frappe.db.exists("Report", name):
			return [Check("verify.exists", "critical", _("Report {0} does not exist").format(name))]
		doc = frappe.get_doc("Report", name)
		checks = []
		expected_disabled = {"disable": 1, "enable": 0, "create": 0}.get(spec["operation"], result.get("disabled"))
		if expected_disabled is not None and cint(doc.disabled) != cint(expected_disabled):
			checks.append(Check("verify.disabled", "critical", _("{0} is not {1}").format(name, _("disabled") if expected_disabled else _("enabled"))))
		if result.get("query_sha256") and _base.sha256(doc.query) != result["query_sha256"]:
			checks.append(Check("verify.query", "critical", _("The stored query differs from the approved one")))
		for key in ("report_script", "javascript"):
			if result.get(f"{key}_sha256") and _base.sha256(doc.get(key)) != result[f"{key}_sha256"]:
				checks.append(Check("verify.script", "critical", _("The stored {0} differs from the approved one").format(key)))
		if spec["operation"] in ("create", "update") and not cint(doc.disabled):
			user = frappe.session.user
			try:
				if doc.report_type == "Report Builder":
					doc.run_standard_report({}, 5, user)
				elif doc.report_type == "Script Report":
					from ai_agent.customization.analyzers import python_code

					declared = js_filters(doc.javascript) if doc.javascript else [f.as_dict() for f in doc.filters]
					try:
						python_code.run_report_script(doc.report_script, report_sample_values(declared, spec.get("sample_filters")), doc.name)
					except (frappe.ValidationError, python_code.DryRunBlocked):
						pass  # a frappe.throw on sample filters is the report's own answer, not a broken report
				else:
					values = sql_analyzer.sample_values([f.as_dict() for f in doc.filters], spec.get("sample_filters"))
					sql_analyzer.preview(doc.query, values, doc.ref_doctype)
			except Exception as e:
				checks.append(Check("verify.run", "critical", _("The report does not run: {0}").format(str(e)[:300])))
		if not checks:
			checks.append(Check("verify.report", "ok", _("{0} is saved and runs").format(name)))
		return checks

	def summary(self, spec):
		verb = {"create": _("Create"), "update": _("Update"), "disable": _("Disable"), "enable": _("Enable")}[spec["operation"]]
		kind = spec.get("report_type") or _("report")
		return _("{0} {1} {2}").format(verb, kind, spec["report_name"])

	def get_source(self, name, user=None):
		doc = frappe.get_doc("Report", name)
		if user and not doc.is_permitted() and not _base.has_roles(user, ("System Manager",)):
			raise frappe.PermissionError(_("Not permitted to read report {0}").format(name))
		out = {"kind": self.name, "name": doc.name, "report_type": doc.report_type, "ref_doctype": doc.ref_doctype,
			"is_standard": doc.is_standard, "disabled": cint(doc.disabled),
			"filters": [{k: f.get(k) for k in FILTER_KEYS if f.get(k) not in (None, "")} for f in doc.filters],
			"columns": [{k: c.get(k) for k in COLUMN_KEYS if c.get(k) not in (None, "")} for c in doc.columns],
			"roles": [r.role for r in doc.roles]}
		if doc.report_type == "Query Report":
			out["query"] = doc.query
			out["ai_made"] = _base.is_ai_made(doc.query)
		elif doc.report_type == "Report Builder":
			builder = json.loads(doc.json or "{}")
			out["ai_made"] = "ai_agent" in builder
			builder.pop("ai_agent", None)
			out["builder"] = builder
		elif doc.report_type == "Script Report" and doc.is_standard == "No":
			out["report_script"] = doc.report_script
			out["javascript"] = doc.javascript
			out["ai_made"] = _base.is_ai_made(doc.report_script)
		return out


KIND = ReportKind()
