"""Reports: Report Builder and Query Report (docs/customization/01 §3.3.5).

Report Builder first: it runs through ``frappe.get_list(..., user=user)``, so row permissions apply,
and it needs no Script Manager. A Query Report runs raw SQL (``frappe.db.sql``) without row
permissions: ``analyzers.sql`` checks it is one read-only SELECT on readable, non-system tables, that
``%(x)s`` placeholders match the filters, and warns for every role that would see more than in the list.
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

TOP_KEYS = ("operation", "report_name", "ref_doctype", "report_type", "builder", "query", "filters", "columns",
	"roles", "sample_filters", "purpose")
OPERATIONS = ("create", "update", "disable", "enable")
REPORT_TYPES = ("Report Builder", "Query Report")
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
		return []
	out = []
	for statement in statements:
		if statement is None:
			continue
		ctes = {c.alias_or_name for c in statement.find_all(exp.CTE)}
		for table in statement.find_all(exp.Table):
			if table.name.startswith("tab") and table.name not in ctes and table.name[3:] not in out:
				out.append(table.name[3:])
	return out


class ReportKind:
	name = "report"
	setting = "allow_customize_reports"
	required_roles = ("System Manager",)

	def roles_for(self, spec):
		report_type = spec.get("report_type") or (frappe.db.get_value("Report", spec.get("report_name"), "report_type") if spec.get("report_name") else None)
		if report_type == "Query Report" and spec.get("operation") != "disable":
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
				raise CustomizationError(_("{0} is a {1}; only Report Builder and Query Report reports can be changed").format(name, current.report_type))
			if args.get("report_type") and args["report_type"] != current.report_type:
				raise CustomizationError(_("The report type cannot change; create a new report"))
			spec["report_type"] = current.report_type
			spec["ref_doctype"] = cstr(args.get("ref_doctype") or current.ref_doctype)
		else:
			_base.require(args, ("ref_doctype", "report_type"), "customize.request_report")
			if args["report_type"] not in REPORT_TYPES:
				raise CustomizationError(_("report_type must be Report Builder or Query Report"))
			spec["report_type"] = args["report_type"]
			spec["ref_doctype"] = cstr(args["ref_doctype"]).strip()
			spec.setdefault("purpose", name)

		if spec["report_type"] == "Report Builder":
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
			spec["roles"] = sql_analyzer.default_roles(_tables_of(spec["query"]))
		if args.get("sample_filters") is not None:
			if not isinstance(args["sample_filters"], dict):
				raise CustomizationError(_("sample_filters must be an object"))
			spec["sample_filters"] = args["sample_filters"]
		return spec

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
			}
		merged = {**current, **{k: v for k, v in spec.items() if k in
			("report_type", "ref_doctype", "query", "builder", "filters", "columns", "roles")}}
		return merged, current

	# -- check --------------------------------------------------------------------
	def check(self, spec, user):
		merged, current = self._effective(spec)
		report_type, ref = merged.get("report_type"), merged.get("ref_doctype")
		roles_needed = self.roles_for(spec)
		why = _("Frappe allows only Script Managers to save Query Reports") if "Script Manager" in roles_needed else _("Frappe requires it")
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

	def _report_doc(self, spec, merged, json_text=None, query=None):
		doc = frappe.new_doc("Report")
		doc.update({"report_name": spec["report_name"], "ref_doctype": merged.get("ref_doctype"),
			"report_type": merged.get("report_type"), "is_standard": "No",
			"module": frappe.db.get_value("DocType", merged.get("ref_doctype"), "module")})
		if merged.get("report_type") == "Report Builder":
			doc.json = json_text or json.dumps(merged.get("builder") or {})
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
				if spec["operation"] == "create":
					doc = self._report_doc(spec, merged, json_text, query)
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
		return {"targets": [{"doctype": "Report", "name": doc.name}], "undo": undo,
			"query_sha256": _base.sha256(doc.query), "json_sha256": _base.sha256(doc.json), "disabled": cint(doc.disabled)}

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
		if spec["operation"] in ("create", "update") and not cint(doc.disabled):
			user = frappe.session.user
			try:
				if doc.report_type == "Report Builder":
					doc.run_standard_report({}, 5, user)
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
		return out


KIND = ReportKind()
