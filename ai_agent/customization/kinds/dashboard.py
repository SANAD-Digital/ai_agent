"""Number Cards and Dashboard Charts: ``customize.request_dashboard``.

Rules read from Frappe 16:

- ``frappe/desk/doctype/number_card/number_card.py``: ``validate`` (:57-74) needs document_type + function and an
  aggregate field unless Count, a parent for a child DocType, report_name + report_field + function for a Report
  card; ``get_result`` (:121-159) computes the value with ``frappe.get_list`` as the viewer (row permissions
  apply); ``has_permission`` (:102-118) lets a non System Manager open a card only when they can read its DocType
  (or run its report). A Report card is computed in the browser from ``query_report.run``
  (``widgets/number_card_widget.js`` get_number_for_report_card).
- ``frappe/desk/doctype/dashboard_chart/dashboard_chart.py``: ``validate`` (:389-427) refuses standard charts outside
  developer mode, needs ``based_on`` for Count/Sum/Average, ``group_by_based_on`` (+ aggregate field for Sum/Average)
  for Group By, refuses Single DocTypes; ``get`` (:90-139) builds the data with ``frappe.get_list`` as the viewer,
  except Heatmap, which uses ``frappe.get_all`` (:243, no permissions); ``has_permission`` (:70-87) lets anyone holding
  one of the chart's ``roles`` open it; and ``utils/dashboard.py cache_source`` caches the data per chart, not per
  user, so a viewer can receive totals computed for someone else.
- Field options mirror the forms (``dashboard_chart.js update_options``, ``number_card.js``): ``based_on`` is a
  Date/Datetime field (or creation/modified), aggregates are numeric, Group By is a Link/Select field (or owner).

Neither DocType has a ``disabled`` flag, so the third operation is ``delete`` (the undo recreates the record and
its workspace placements). ``is_standard`` is always 0 and ``module`` empty: in developer mode a standard card or
chart is exported into its app on save (number_card.py:76-78, dashboard_chart.py:384-387).
"""

from __future__ import annotations

import json
import re
import typing

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt

from ai_agent.customization.analyzers.python_code import protected_doctype
from ai_agent.customization.contract import Check, CustomizationError
from ai_agent.customization.kinds import _base
from ai_agent.customization.kinds import workspace_spec as ws

TOP_KEYS = ("operation", "object", "name", "definition", "workspace", "purpose")
OPERATIONS = ("create", "update", "delete")
OBJECTS = ("number_card", "chart")
DOCTYPES = {"number_card": "Number Card", "chart": "Dashboard Chart"}
CARD_KEYS = ("label", "type", "document_type", "parent_document_type", "function", "aggregate_function_based_on", "filters",
	"report_name", "report_field", "report_function", "is_public", "show_percentage_stats", "stats_time_interval", "color",
	"background_color", "currency", "show_full_number")
CHART_KEYS = ("chart_type", "document_type", "parent_document_type", "based_on", "value_based_on", "timespan", "time_interval",
	"from_date", "to_date", "group_by_based_on", "group_by_type", "aggregate_function_based_on", "number_of_groups",
	"report_name", "x_field", "y_axis", "use_report_chart", "filters", "type", "heatmap_year", "color", "is_public", "roles",
	"currency", "show_values_over_chart")
CARD_TYPES = ("Document Type", "Report")
CARD_FUNCTIONS = ("Count", "Sum", "Average", "Minimum", "Maximum")
REPORT_FUNCTIONS = ("Sum", "Average", "Minimum", "Maximum")
STATS_INTERVALS = ("Daily", "Weekly", "Monthly", "Yearly")
CHART_TYPES = ("Count", "Sum", "Average", "Group By", "Report")
TIMESERIES_TYPES = ("Count", "Sum", "Average")
GROUP_BY_TYPES = ("Count", "Sum", "Average")
CHART_SHAPES = ("Line", "Bar", "Percentage", "Pie", "Donut", "Heatmap")
TIMESPANS = ("Last Year", "Last Quarter", "Last Month", "Last Week", "Select Date Range")
INTERVALS = ("Yearly", "Quarterly", "Monthly", "Weekly", "Daily")
# dashboard_chart.js timespan(): the intervals the form offers for each timespan
INTERVALS_FOR = {"Select Date Range": ("Quarterly", "Monthly", "Weekly", "Daily"),
	"Last Year": ("Quarterly", "Monthly", "Weekly", "Daily"), "Last Quarter": ("Monthly", "Weekly", "Daily"),
	"Last Month": ("Weekly", "Daily"), "Last Week": ("Daily",)}
NUMERIC = ("Int", "Long Int", "Float", "Currency", "Percent", "Duration")
DATE_TYPES = ("Date", "Datetime")
GROUPABLE = ("Link", "Select")
STANDARD_FIELDS = ("name", "owner", "creation", "modified", "modified_by", "docstatus", "idx", "parent")
OPERATORS = ("=", "!=", ">", "<", ">=", "<=", "in", "not in", "like", "not like", "between", "is", "timespan",
	"descendants of", "ancestors of", "not descendants of", "not ancestors of", "descendants of (inclusive)")
PLACEMENT_KEYS = ("name", "label", "col")
COLS = (3, 4, 6, 8, 12)
FIELDNAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
PREVIEW_POINTS = 24


def _timespans():
	from frappe.utils.data import TimespanOptions

	return typing.get_args(TimespanOptions)


# -- filters --------------------------------------------------------------------------------------------


def norm_filters(value, doctype, where="filters"):
	"""Frappe's stored form: ``[[doctype, fieldname, operator, value, false], ...]`` (what the filter area saves).
	Accepts ``[[field, op, value]]``, ``[[doctype, field, op, value(, false)]]`` or ``{field: value | [op, value]}``."""
	if value in (None, "", [], {}):
		return []
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			raise CustomizationError(_("{0} must be JSON").format(where))
	rows = []
	if isinstance(value, dict):
		for field, item in value.items():
			if isinstance(item, list) and len(item) == 2 and isinstance(item[0], str) and item[0].lower() in OPERATORS:
				rows.append([doctype, field, item[0], item[1]])
			else:
				rows.append([doctype, field, "=", item])
	elif isinstance(value, list):
		for i, item in enumerate(value, 1):
			if not isinstance(item, list) or len(item) not in (3, 4, 5):
				raise CustomizationError(_("{0}[{1}] must be [fieldname, operator, value] or [doctype, fieldname, operator, value]").format(where, i))
			rows.append([doctype, *item] if len(item) == 3 else list(item[:4]))
	else:
		raise CustomizationError(_("{0} must be a list or an object").format(where))
	out = []
	for row in rows:
		op = cstr(row[2]).strip()
		op = "Timespan" if op.lower() == "timespan" else op.lower()
		out.append([cstr(row[0]), cstr(row[1]), op, row[3], False])
	return out


def filter_checks(filters, doctype, parent=None, code="dashboard.filter"):
	"""Each filter names an existing field of the DocType (its parent or a child table), a Frappe operator and a
	value of the right shape (``ai_agent.query.validator.validate_filter_value`` for the shared operators)."""
	from ai_agent.query.validator import validate_filter_value
	from ai_agent.runtime.exceptions import QueryValidationError

	checks = []
	try:  # stored filters (an existing card) may be 4-item rows or an object
		filters = norm_filters(filters, doctype)
	except CustomizationError as exc:
		return [Check(code, "critical", exc.message)]
	meta = frappe.get_meta(doctype)
	allowed = {doctype: meta, **{df.options: None for df in meta.get_table_fields()}}
	if parent:
		allowed[parent] = None
	for dt, field, op, value, _flag in filters:
		where = f"{dt}.{field}"
		if dt not in allowed:
			checks.append(Check(code, "critical", _("Filter {0}: {1} is neither {2} nor one of its tables").format(where, dt, doctype)))
			continue
		df = frappe.get_meta(dt).get_field(field)
		if not df and field not in STANDARD_FIELDS:
			checks.append(Check(code, "critical", _("Filter {0}: {1} has no field {2}").format(where, dt, field)))
			continue
		if op.lower() not in OPERATORS:
			checks.append(Check(code, "critical", _("Filter {0}: operator {1} is not one of {2}").format(where, op, ", ".join(OPERATORS))))
			continue
		if op == "Timespan":
			if value not in _timespans():
				checks.append(Check(code, "critical", _("Filter {0}: Timespan takes one of {1}").format(where, ", ".join(_timespans()))))
			continue
		if op == "is":
			if value not in ("set", "not set"):
				checks.append(Check(code, "critical", _("Filter {0}: 'is' takes \"set\" or \"not set\"").format(where)))
			continue
		if op in ("in", "not in") and isinstance(value, str):
			continue  # Frappe splits "a,b" itself
		shared = op if op in ("=", "!=", ">", "<", ">=", "<=", "in", "not in", "like", "not like", "between", "descendants of", "ancestors of") else None
		if shared:
			try:
				validate_filter_value(shared, value)
			except QueryValidationError as exc:
				checks.append(Check(code, "critical", _("Filter {0}: {1}").format(where, str(exc))))
	return checks


# -- reading ----------------------------------------------------------------------------------------------


def _json(value, default):
	if value in (None, ""):
		return default
	if isinstance(value, (list, dict)):
		return value
	try:
		return json.loads(value)
	except ValueError:
		return default


def definition_of(obj, doc):
	keys = CARD_KEYS if obj == "number_card" else CHART_KEYS
	out = {}
	for key in keys:
		if key == "filters":
			out[key] = _json(doc.get("filters_json"), [] if (doc.get("type") != "Report" and doc.get("chart_type") != "Report") else {})
		elif key == "y_axis":
			out[key] = [row.y_field for row in doc.get("y_axis") or []]
		elif key == "roles":
			out[key] = sorted(row.role for row in doc.get("roles") or [])
		else:
			value = doc.get(key)
			out[key] = cstr(value) if not isinstance(value, int) else value
	if obj == "chart":
		out["timeseries"] = cint(doc.get("timeseries"))  # set by the form from chart_type, kept for a restore
	return out


def placements_of(obj, name):
	table, field, block_type = _table(obj)
	child = "Workspace Number Card" if obj == "number_card" else "Workspace Chart"
	out = []
	for row in frappe.get_all(child, filters={field: name, "parenttype": "Workspace"}, fields=["parent", "label"], order_by="parent asc"):
		col = None
		content = _json(frappe.db.get_value("Workspace", row.parent, "content"), [])
		key = ws.BLOCKS[block_type][1]
		for block in content if isinstance(content, list) else []:
			if block.get("type") == block_type and (block.get("data") or {}).get(key) == (row.label or name):
				col = (block.get("data") or {}).get("col")
				break
		out.append({"workspace": row.parent, "label": row.label or name, "col": cint(col) or ws.BLOCKS[block_type][2]})
	return out


def _table(obj):
	return ("number_cards", "number_card_name", "number_card") if obj == "number_card" else ("charts", "chart_name", "chart")


def dashboards_of(obj, name):
	child, field = ("Number Card Link", "card") if obj == "number_card" else ("Dashboard Chart Link", "chart")
	return sorted(set(frappe.get_all(child, filters={field: name, "parenttype": "Dashboard"}, pluck="parent")))


# -- the kind -------------------------------------------------------------------------------------------------


class DashboardKind:
	name = "dashboard"
	setting = "allow_customize_dashboards"
	required_roles = ("System Manager",)

	def roles_for(self, spec):
		placements = list(spec.get("placements") or [])
		if spec.get("operation") == "delete" and spec.get("name"):
			placements += placements_of(spec.get("object"), spec["name"]) if spec.get("object") in OBJECTS else []
		if placements:
			return ("System Manager", "Workspace Manager")
		return self.required_roles

	# -- normalize ---------------------------------------------------------------------------------
	def normalize(self, args, user):
		args = dict(args or {})
		_base.reject_unknown(args, TOP_KEYS, "customize.request_dashboard")
		_base.require(args, ("operation", "object", "name"), "customize.request_dashboard")
		op, obj = args["operation"], args["object"]
		if op not in OPERATIONS:
			raise CustomizationError(_("operation must be one of: {0}").format(", ".join(OPERATIONS)),
				hints=[_("Number cards and charts have no disabled flag: delete removes them (the undo recreates them)")])
		if obj not in OBJECTS:
			raise CustomizationError(_("object must be number_card or chart"))
		name = re.sub(r"\s+", " ", cstr(args["name"])).strip()[:140]
		doctype = DOCTYPES[obj]
		spec = {"operation": op, "object": obj, "name": name, "requested_by": user}
		if args.get("purpose"):
			spec["purpose"] = re.sub(r"\s+", " ", cstr(args["purpose"])).strip()[:140]
		exists = frappe.db.exists(doctype, name)
		if op != "create" and not exists:
			raise CustomizationError(_("There is no {0} {1}").format(doctype, name), hints=[_("Use operation create")])
		if op == "delete":
			if args.get("definition") or args.get("workspace"):
				raise CustomizationError(_("delete takes only object, name and purpose"))
			return spec
		definition = args.get("definition") or {}
		if not isinstance(definition, dict):
			raise CustomizationError(_("definition must be an object"))
		if op == "create":
			_base.require(args, ("definition",), "customize.request_dashboard (create)")
		keys = CARD_KEYS if obj == "number_card" else CHART_KEYS
		_base.reject_unknown(definition, keys, f"definition ({obj})")
		current = definition_of(obj, frappe.get_doc(doctype, name)) if exists else {}
		spec["definition"] = self._clean(obj, definition, current, op)
		if op == "update" and not spec["definition"] and not args.get("workspace"):
			raise CustomizationError(_("Nothing to change"))
		spec["placements"] = self._placements(args.get("workspace"), obj, name, spec["definition"], current)
		return spec

	def _clean(self, obj, definition, current, op):
		d = {}
		for key, value in definition.items():
			if key in ("is_public", "show_percentage_stats", "show_full_number", "use_report_chart", "show_values_over_chart"):
				d[key] = 1 if cint(value) else 0
			elif key == "number_of_groups":
				d[key] = cint(value)
			elif key == "roles":
				if not isinstance(value, list) or not all(isinstance(r, str) for r in value):
					raise CustomizationError(_("roles must be a list of role names"))
				missing = [r for r in value if not frappe.db.exists("Role", r)]
				if missing:
					raise CustomizationError(_("Unknown role(s): {0}").format(", ".join(missing)))
				d[key] = sorted(set(value))
			elif key == "y_axis":
				if not isinstance(value, list) or not all(isinstance(r, str) for r in value):
					raise CustomizationError(_("y_axis must be a list of report column fieldnames"))
				d[key] = list(value)
			elif key == "filters":
				d[key] = value
			else:
				d[key] = cstr(value).strip() if value is not None else ""
		merged = {**current, **d}
		if obj == "number_card":
			card_type = merged.get("type") or "Document Type"
			if card_type == "Custom" or merged.get("method"):
				raise CustomizationError(_("Custom (method) number cards run code and are not offered"))
			if card_type not in CARD_TYPES:
				raise CustomizationError(_("type must be Document Type or Report"))
			if op == "create":
				d.setdefault("type", card_type)
				d.setdefault("label", "")
				if card_type == "Document Type":
					d.setdefault("function", "Count")
				d.setdefault("show_percentage_stats", 1)
				d.setdefault("stats_time_interval", "Daily")
				d.setdefault("is_public", 0)
			for key, options in (("function", CARD_FUNCTIONS), ("report_function", REPORT_FUNCTIONS), ("stats_time_interval", STATS_INTERVALS)):
				if d.get(key) and d[key] not in options:
					raise CustomizationError(_("{0} must be one of {1}").format(key, ", ".join(options)))
			if card_type == "Report" and d.get("report_function"):
				d["function"] = d["report_function"]  # validate (:68-70) also wants function
			if "filters" in d:
				d["filters"] = self._norm_filters(card_type == "Report", d["filters"], merged.get("document_type"))
		else:
			chart_type = merged.get("chart_type")
			if chart_type == "Custom" or merged.get("source"):
				raise CustomizationError(_("Custom (source) charts run code and are not offered"))
			if op == "create" and not chart_type:
				raise CustomizationError(_("definition.chart_type is required: one of {0}").format(", ".join(CHART_TYPES)))
			if chart_type not in CHART_TYPES:
				raise CustomizationError(_("chart_type must be one of {0}").format(", ".join(CHART_TYPES)))
			for key, options in (("type", CHART_SHAPES), ("timespan", TIMESPANS), ("time_interval", INTERVALS), ("group_by_type", GROUP_BY_TYPES)):
				if d.get(key) and d[key] not in options:
					raise CustomizationError(_("{0} must be one of {1}").format(key, ", ".join(options)))
			if op == "create":
				d.setdefault("type", "Line" if chart_type in TIMESERIES_TYPES else ("Bar" if chart_type == "Group By" else "Line"))
				d.setdefault("is_public", 0)
				if chart_type in TIMESERIES_TYPES:
					d.setdefault("timespan", "Last Year")
					d.setdefault("time_interval", "Monthly")
				if chart_type == "Group By":
					d.setdefault("group_by_type", "Count")
			if "chart_type" in d or op == "create":
				d["timeseries"] = 1 if chart_type in TIMESERIES_TYPES else 0  # dashboard_chart.js set_time_series
			if "filters" in d:
				d["filters"] = self._norm_filters(chart_type == "Report", d["filters"], merged.get("document_type"))
		return d

	def _norm_filters(self, report, value, doctype):
		if report:
			if value in (None, ""):
				return {}
			if not isinstance(value, dict):
				raise CustomizationError(_("A report card or chart takes filters as an object of report filter values"))
			return value
		if not doctype:
			raise CustomizationError(_("filters need definition.document_type"))
		return norm_filters(value, doctype)

	def _placements(self, value, obj, name, definition, current):
		if value in (None, "", []):
			return []
		rows = value if isinstance(value, list) else [value]
		out = []
		for i, row in enumerate(rows, 1):
			if isinstance(row, str):
				row = {"name": row}
			if not isinstance(row, dict):
				raise CustomizationError(_("workspace must be a workspace name or {name, label, col}"))
			_base.reject_unknown(row, PLACEMENT_KEYS, f"workspace[{i}]")
			_base.require(row, ("name",), f"workspace[{i}]")
			col = cint(row.get("col")) or ws.BLOCKS[_table(obj)[2]][2]
			if col not in COLS:
				raise CustomizationError(_("col must be one of {0}").format(", ".join(map(str, COLS))))
			label = cstr(row.get("label") or definition.get("label") or current.get("label") or name)
			out.append({"workspace": cstr(row["name"]), "label": label, "col": col})
		return out

	# -- snapshot -------------------------------------------------------------------------------------
	def snapshot(self, spec):
		obj, name = spec["object"], spec["name"]
		doctype = DOCTYPES[obj]
		if not frappe.db.exists(doctype, name):
			return {"object": obj, "name": name, "exists": False}
		doc = frappe.get_doc(doctype, name)
		return json.loads(json.dumps({"object": obj, "name": name, "exists": True, "is_standard": cint(doc.is_standard),
			"module": doc.module or "", "definition": definition_of(obj, doc), "placements": placements_of(obj, name),
			"dashboards": dashboards_of(obj, name)}, default=str))

	def effective(self, spec, state=None):
		state = state or self.snapshot(spec)
		base = dict(state.get("definition") or {}) if spec["operation"] != "create" else {}
		return {**base, **(spec.get("definition") or {})}

	# -- check -----------------------------------------------------------------------------------------------
	def check(self, spec, user):
		obj, op, name = spec["object"], spec["operation"], spec["name"]
		doctype = DOCTYPES[obj]
		state = self.snapshot(spec)
		checks = _base.role_checks(user, self.roles_for(spec), _("Frappe requires it to save {0} and the workspaces it sits on").format(doctype))
		preview = {"object": obj, "name": name, "operation": op}
		if op == "create" and state["exists"]:
			checks.append(Check("dashboard.name_taken", "critical", _("A {0} named {1} already exists; use operation update or another name").format(doctype, name)))
		if op != "create" and state.get("is_standard"):
			checks.append(Check("dashboard.standard", "critical",
				_("{0} is a standard {1} of the module {2}: it is not edited in place (developer mode exports it into the app). Create a copy under another name").format(
					name, doctype, state.get("module") or "-")))
		if op == "delete":
			checks += self._delete_checks(spec, state)
			preview["placements"] = state.get("placements") or []
			impact = [_("Deletes the {0} {1}").format(doctype, name)]
			if state.get("placements"):
				impact.append(_("Removes it from the workspaces {0}").format(", ".join(p["workspace"] for p in state["placements"])))
			return _base.build_report(checks, impact=impact, preview=preview, risk="low")

		d = self.effective(spec, state)
		preview["definition"] = d
		if obj == "number_card":
			source_checks, source = self._check_card(d, user)
		else:
			source_checks, source = self._check_chart(d, user)
		checks += source_checks
		checks += self._exposure(obj, d, source)
		checks += self._placement_checks(spec, state, user)
		if not any(c.level == "critical" for c in checks):
			checks += self._dry_save(spec, d, user)
		if not any(c.level == "critical" for c in checks):
			checks += self._preview(obj, d, user, preview, source)
		impact = []
		if source.get("doctype"):
			impact.append(_("Computes over {0} with each viewer's permissions").format(source["doctype"]) if not (obj == "chart" and d.get("type") == "Heatmap")
				else _("Computes over all of {0}, without permissions (Heatmap)").format(source["doctype"]))
		if source.get("report"):
			impact.append(_("Runs the report {0} each time it is shown").format(source["report"]))
		if cint(d.get("is_public")):
			impact.append(_("Every user can pick it when they edit a workspace"))
		for p in spec.get("placements") or []:
			impact.append(_("Shown on the workspace {0}").format(p["workspace"]))
		if spec.get("placements"):
			preview["placements"] = spec["placements"]
		return _base.build_report(checks, impact=impact, preview=preview, risk="low")

	def _source_doctype(self, d, user, code="dashboard.doctype"):
		checks = []
		dt = d.get("document_type")
		if not dt or not frappe.db.exists("DocType", dt):
			return [Check(code, "critical", _("There is no DocType {0}").format(dt or "-"))], None
		meta = frappe.get_meta(dt)
		if meta.issingle:
			checks.append(Check(code, "critical", _("{0} is a Single DocType: Frappe cannot count or chart it").format(dt)))
		if protected_doctype(dt):
			checks.append(Check(code, "critical", _("{0} controls code, access or settings; cards and charts on it are not allowed").format(dt)))
		parent = d.get("parent_document_type")
		if meta.istable:
			if not parent:
				checks.append(Check("dashboard.parent", "critical", _("{0} is a child table: give parent_document_type").format(dt)))
			elif not frappe.db.exists("DocType", parent) or dt not in {df.options for df in frappe.get_meta(parent).get_table_fields()}:
				checks.append(Check("dashboard.parent", "critical", _("{0} has no table of {1}").format(parent, dt)))
		from ai_agent.customization.analyzers import sql as sql_analyzer

		if not any(c.level == "critical" for c in checks) and not sql_analyzer.can_read(dt, user):
			checks.append(Check("dashboard.read", "critical", _("{0} cannot read {1}").format(user, dt)))
		return checks, meta

	def _field_check(self, meta, field, types, what, standard=()):
		if not field:
			return [Check("dashboard.field", "critical", _("{0} is required").format(what))]
		if field in standard:
			return []
		df = meta.get_field(field)
		if not df:
			return [Check("dashboard.field", "critical", _("{0}: {1} has no field {2}").format(what, meta.name, field))]
		if types and df.fieldtype not in types:
			return [Check("dashboard.field", "critical", _("{0}: {1} is a {2} field; it must be {3}").format(what, field, df.fieldtype, " / ".join(types)))]
		return []

	def _report_source(self, d, user):
		report = d.get("report_name")
		if not report or not frappe.db.exists("Report", report):
			return [Check("dashboard.report", "critical", _("There is no report {0}").format(report or "-"))], None
		doc = frappe.get_doc("Report", report)
		checks = []
		if cint(doc.disabled):
			checks.append(Check("dashboard.report", "critical", _("The report {0} is disabled").format(report)))
		with _base.as_user(user):
			permitted = user == "Administrator" or doc.is_permitted()
		if not permitted:
			checks.append(Check("dashboard.report", "critical", _("{0} may not run the report {1}").format(user, report)))
		if doc.ref_doctype and protected_doctype(doc.ref_doctype):
			checks.append(Check("dashboard.report", "critical", _("{0} reads {1}, which controls code, access or settings").format(report, doc.ref_doctype)))
		return checks, doc

	def _check_card(self, d, user):
		source = {}
		if (d.get("type") or "Document Type") == "Report":
			checks, report = self._report_source(d, user)
			source["report"] = d.get("report_name")
			if report:
				source["report_type"] = report.report_type
				source["doctype"] = report.ref_doctype
			if not d.get("report_field"):
				checks.append(Check("dashboard.field", "critical", _("report_field is required for a Report card")))
			if not d.get("report_function"):
				checks.append(Check("dashboard.function", "critical", _("report_function is required for a Report card: one of {0}").format(", ".join(REPORT_FUNCTIONS))))
			return checks, source
		checks, meta = self._source_doctype(d, user)
		if not meta:
			return checks, source
		source["doctype"] = d["document_type"]
		function = d.get("function") or "Count"
		if function != "Count":
			checks += self._field_check(meta, d.get("aggregate_function_based_on"), NUMERIC, _("aggregate_function_based_on ({0})").format(function))
		elif d.get("aggregate_function_based_on"):
			checks.append(Check("dashboard.field", "info", _("Count ignores aggregate_function_based_on")))
		checks += filter_checks(d.get("filters") or [], d["document_type"], d.get("parent_document_type"))
		return checks, source

	def _check_chart(self, d, user):
		source = {}
		chart_type = d.get("chart_type")
		shape = d.get("type") or "Line"
		if chart_type == "Report":
			checks, report = self._report_source(d, user)
			source["report"] = d.get("report_name")
			if report:
				source["report_type"] = report.report_type
				source["doctype"] = report.ref_doctype
			if shape == "Heatmap":
				checks.append(Check("dashboard.shape", "critical", _("A Report chart cannot be a Heatmap")))
			if not cint(d.get("use_report_chart")) and not (d.get("x_field") and d.get("y_axis")):
				checks.append(Check("dashboard.field", "critical", _("A Report chart needs x_field and y_axis, or use_report_chart: 1")))
			return checks, source
		checks, meta = self._source_doctype(d, user)
		if not meta:
			return checks, source
		source["doctype"] = d["document_type"]
		if chart_type in TIMESERIES_TYPES:
			checks += self._field_check(meta, d.get("based_on"), DATE_TYPES, _("based_on (the date axis)"), ("creation", "modified"))
			if chart_type != "Count":
				checks += self._field_check(meta, d.get("value_based_on"), NUMERIC, _("value_based_on ({0})").format(chart_type))
			if d.get("timespan") == "Select Date Range" and not (d.get("from_date") and d.get("to_date")):
				checks.append(Check("dashboard.timespan", "critical", _("Select Date Range needs from_date and to_date")))
			allowed = INTERVALS_FOR.get(d.get("timespan") or "")
			if allowed and d.get("time_interval") and d["time_interval"] not in allowed and shape != "Heatmap":
				checks.append(Check("dashboard.timespan", "warning", _("{0} by {1}: the chart form offers {2}").format(
					d.get("timespan"), d.get("time_interval"), ", ".join(allowed))))
			if shape in ("Percentage", "Pie", "Donut"):
				checks.append(Check("dashboard.shape", "warning", _("A {0} chart of a time series shows each period as a slice; Line or Bar reads better").format(shape)))
		else:  # Group By
			if d.get("group_by_based_on") != "owner":
				checks += self._field_check(meta, d.get("group_by_based_on"), None, _("group_by_based_on"))
				df = meta.get_field(d.get("group_by_based_on") or "")
				if df and df.fieldtype not in GROUPABLE:
					checks.append(Check("dashboard.field", "warning", _("group_by_based_on: {0} is a {1} field; the chart form offers Link and Select fields").format(df.fieldname, df.fieldtype)))
			if (d.get("group_by_type") or "Count") != "Count":
				checks += self._field_check(meta, d.get("aggregate_function_based_on"), NUMERIC, _("aggregate_function_based_on ({0})").format(d.get("group_by_type")))
			if shape == "Heatmap":
				checks.append(Check("dashboard.shape", "critical", _("A Group By chart cannot be a Heatmap")))
		if shape == "Heatmap":
			checks.append(Check("dashboard.heatmap", "warning",
				_("Heatmap charts count with frappe.get_all (dashboard_chart.py get_heatmap_chart_config): every viewer sees totals over all {0}, whatever their permissions").format(d["document_type"])))
		checks += filter_checks(d.get("filters") or [], d["document_type"], d.get("parent_document_type"))
		return checks, source

	def _exposure(self, obj, d, source):
		"""Who sees what: Frappe recomputes most numbers per viewer, but not all (see the module docstring)."""
		from ai_agent.customization.analyzers import sql as sql_analyzer
		from ai_agent.runtime import masking

		checks = []
		doctype = source.get("doctype")
		if doctype and frappe.db.exists("DocType", doctype):
			if doctype in masking.salary_doctypes() or any(masking.is_sensitive_field(d.get(k), doctype)
					for k in ("aggregate_function_based_on", "value_based_on", "report_field")):
				checks.append(Check("dashboard.sensitive", "warning", _("It totals pay or personal data of {0}: the preview is masked; check who will see it").format(doctype)))
			plain, owner_only = sql_analyzer.read_roles(doctype)
			readers = plain | owner_only
			if cint(d.get("is_public")) and not readers & ws.EVERYONE_ROLES:
				blocked = set(ws.desk_roles()) - readers - {"System Manager"}
				if blocked:
					checks.append(Check("dashboard.public_roles", "warning",
						_("is_public offers it to every user, but the roles {0} cannot read {1}: Frappe refuses them the {2} (or shows the report's numbers when it is a report)").format(
							ws.short_list(blocked), doctype, _("card") if obj == "number_card" else _("chart"))))
			if obj == "chart" and d.get("roles"):
				widened = set(d["roles"]) - readers - {"System Manager"}
				if widened and not readers & ws.EVERYONE_ROLES:
					checks.append(Check("dashboard.role_widening", "warning",
						_("The roles {0} may open this chart (dashboard_chart.py has_permission) but cannot read {1}; chart data is cached per chart (utils/dashboard.py cache_source), so they see totals computed for other users").format(
							ws.short_list(widened), doctype)))
			if obj == "chart" and d.get("chart_type") != "Report" and (owner_only or self._user_permission_rows(doctype)):
				checks.append(Check("dashboard.cache_shared", "warning",
					_("{0} is limited per user (if_owner or User Permissions), but chart data is cached per chart, not per user: a viewer can see the totals of whoever loaded it last").format(doctype)))
		if source.get("report_type") == "Query Report":
			checks.append(Check("dashboard.query_report", "warning", _("{0} is a Query Report: it reads without row permissions, so the number covers every row").format(source["report"])))
		return checks

	def _user_permission_rows(self, doctype):
		links = {df.options for df in frappe.get_meta(doctype).get_link_fields()} | {doctype}
		return frappe.db.count("User Permission", {"allow": ("in", list(links))}) if links else 0

	def _placement_checks(self, spec, state, user):
		checks = []
		table, field, _block = _table(spec["object"])
		for p in spec.get("placements") or []:
			name = p["workspace"]
			if not frappe.db.exists("Workspace", name):
				checks.append(Check("dashboard.workspace", "critical", _("There is no workspace {0}").format(name)))
				continue
			row = frappe.db.get_value("Workspace", name, ["module", "app", "public", "for_user"], as_dict=True)
			if ws.is_standard_workspace(row):
				checks.append(Check("dashboard.workspace", "critical",
					_("{0} belongs to the app {1}: copy it first (customize.request_workspace with copy_from) and place the widget on the copy").format(name, row.app or row.module)))
				continue
			if not row.public and row.for_user != spec.get("requested_by"):
				checks.append(Check("dashboard.workspace", "critical", _("{0} is someone else's private workspace").format(name)))
			child = frappe.get_all("Workspace Number Card" if spec["object"] == "number_card" else "Workspace Chart",
				filters={"parent": name, "parenttype": "Workspace"}, fields=[field, "label"])
			if any(r.get(field) == spec["name"] for r in child):
				checks.append(Check("dashboard.workspace", "info", _("It is already on {0}").format(name)))
			elif any((r.label or r.get(field)) == p["label"] for r in child):
				checks.append(Check("dashboard.workspace", "critical", _("{0} already has a widget labelled {1}: give another label").format(name, p["label"])))
		return checks

	def _delete_checks(self, spec, state):
		checks = []
		for p in state.get("placements") or []:
			row = frappe.db.get_value("Workspace", p["workspace"], ["module", "app"], as_dict=True) or {}
			if ws.is_standard_workspace(row):
				checks.append(Check("dashboard.in_use", "critical", _("The app workspace {0} shows it: it cannot be deleted").format(p["workspace"])))
		for dashboard in state.get("dashboards") or []:
			checks.append(Check("dashboard.in_use", "critical", _("The dashboard {0} shows it: remove it there first").format(dashboard)))
		if not any(c.level == "critical" for c in checks):
			collect = _base.capture_messages()
			try:
				with _base.dry_run():
					self._delete(spec, state)
				checks.append(Check("dashboard.frappe", "ok", _("It can be deleted (dry run, rolled back)")))
			except Exception as exc:
				checks.append(Check("dashboard.frappe", "critical", _("Frappe refuses the change: {0}").format(_base.frappe_error(exc, collect()))))
			collect()
		return checks

	def _dry_save(self, spec, d, user):
		collect = _base.capture_messages()
		try:
			with _base.dry_run(), _base.as_user(user):
				self._save(spec, d)
		except Exception as exc:
			return [Check("dashboard.frappe", "critical", _("Frappe refuses the change: {0}").format(_base.frappe_error(exc, collect())))]
		collect()
		return [Check("dashboard.frappe", "ok", _("Frappe's own validation passes (dry run, rolled back)"))]

	# -- preview ------------------------------------------------------------------------------------------------
	def _preview(self, obj, d, user, preview, source):
		from ai_agent.runtime import masking

		sensitive = bool(source.get("doctype") and (source["doctype"] in masking.salary_doctypes() or any(
			masking.is_sensitive_field(d.get(k), source["doctype"]) for k in ("aggregate_function_based_on", "value_based_on", "report_field"))))
		try:
			with _base.as_user(user):
				if obj == "number_card":
					value = self._card_value(d)
					preview["value"] = masking.MASK if sensitive else value
					return [Check("dashboard.preview", "ok", _("The card computes for {0}: {1}").format(user, preview["value"]))]
				data = self._chart_data(d)
		except Exception as exc:
			return [Check("dashboard.preview", "critical", _("It fails to compute: {0}").format(_base.frappe_error(exc)))]
		if sensitive:
			for dataset in data.get("datasets") or []:
				dataset["values"] = [masking.MASK for _v in dataset.get("values") or []]
		preview["data"] = data
		points = len(data.get("labels") or []) or len(data.get("dataPoints") or {})
		return [Check("dashboard.preview", "ok", _("The chart computes for {0}: {1} point(s)").format(user, points))]

	def _card_value(self, d):
		if (d.get("type") or "Document Type") == "Report":
			columns, rows = self._run_report(d)
			field = d.get("report_field")
			if field not in columns:
				raise frappe.ValidationError(_("The report has no column {0} (columns: {1})").format(field, ", ".join(columns)))
			values = [flt(r.get(field)) for r in rows if r.get(field) not in (None, "")]
			if not values:
				return None
			fn = d.get("report_function") or "Sum"
			return {"Sum": sum(values), "Average": sum(values) / len(values), "Minimum": min(values), "Maximum": max(values)}[fn]
		from frappe.desk.doctype.number_card.number_card import get_result

		doc = {"function": d.get("function") or "Count", "aggregate_function_based_on": d.get("aggregate_function_based_on"),
			"document_type": d["document_type"], "parent_document_type": d.get("parent_document_type")}
		return get_result(json.dumps(doc), json.dumps(d.get("filters") or []))

	def _chart_data(self, d):
		if d.get("chart_type") == "Report":
			columns, rows = self._run_report(d)
			if cint(d.get("use_report_chart")):
				return {"labels": [], "datasets": [], "report_columns": columns[:20]}
			missing = [f for f in [d.get("x_field")] + list(d.get("y_axis") or []) if f not in columns]
			if missing:
				raise frappe.ValidationError(_("The report has no column {0} (columns: {1})").format(", ".join(missing), ", ".join(columns)))
			rows = rows[:PREVIEW_POINTS]
			return {"labels": [cstr(r.get(d["x_field"])) for r in rows],
				"datasets": [{"name": y, "values": [flt(r.get(y)) for r in rows]} for y in d["y_axis"]]}
		from frappe.desk.doctype.dashboard_chart.dashboard_chart import get

		from frappe.utils import getdate

		chart = frappe._dict({k: v for k, v in d.items() if k not in ("filters", "roles", "y_axis")})
		chart.filters_json = json.dumps(d.get("filters") or [])
		chart.name = "preview"
		for key in ("from_date", "to_date"):  # get_chart_config calls .strftime on them, as a saved chart has dates
			chart[key] = getdate(chart[key]) if chart.get(key) else None
		data = get(chart=chart, no_cache=1) or {"labels": [], "datasets": []}
		data = json.loads(json.dumps(data, default=str))
		if data.get("labels"):
			data["labels"] = data["labels"][-PREVIEW_POINTS:]
			for dataset in data.get("datasets") or []:
				dataset["values"] = dataset.get("values", [])[-PREVIEW_POINTS:]
		if data.get("dataPoints"):
			data["dataPoints"] = dict(list(data["dataPoints"].items())[-PREVIEW_POINTS:])
		return data

	def _run_report(self, d):
		from frappe.desk.query_report import run

		result = run(d["report_name"], filters=json.dumps(d.get("filters") or {}), ignore_prepared_report=True)
		columns = []
		for col in result.get("columns") or []:
			if isinstance(col, dict):
				columns.append(col.get("fieldname") or frappe.scrub(col.get("label") or ""))
			else:
				columns.append(frappe.scrub(cstr(col).split(":")[0]))
		rows = []
		for row in result.get("result") or []:
			if isinstance(row, dict):
				rows.append(row)
			elif isinstance(row, (list, tuple)):
				rows.append(dict(zip(columns, row)))
		return columns, rows

	# -- writing ----------------------------------------------------------------------------------------------
	def _save(self, spec, d):
		obj, name = spec["object"], spec["name"]
		doctype = DOCTYPES[obj]
		if spec["operation"] == "create":
			doc = frappe.new_doc(doctype)
			if obj == "number_card":
				doc.name = name  # autoname keeps a given name (number_card.py:50-55)
			else:
				doc.chart_name = name  # autoname field:chart_name
		else:
			doc = frappe.get_doc(doctype, name)
		for key, value in d.items():
			if key == "filters":
				doc.filters_json = json.dumps(value if value not in (None, "") else ([] if not isinstance(value, dict) else {}))
			elif key == "roles":
				doc.set("roles", [{"role": r} for r in value or []])
			elif key == "y_axis":
				doc.set("y_axis", [{"y_field": f} for f in value or []])
			elif key == "label" and not value:
				doc.label = name
			else:
				doc.set(key, value)
		if obj == "number_card" and not doc.label:
			doc.label = name
		if obj == "chart" and not doc.filters_json:
			doc.filters_json = "[]"  # filters_json is mandatory on Dashboard Chart
		doc.is_standard = 0
		doc.module = None
		if spec["operation"] == "create":
			doc.insert()
		else:
			doc.save()
		for p in spec.get("placements") or []:
			self._place(obj, doc.name, p)
		return doc

	def _place(self, obj, name, placement):
		table, field, block_type = _table(obj)
		doc = frappe.get_doc("Workspace", placement["workspace"])
		if any(r.get(field) == name for r in doc.get(table)):
			return
		doc.append(table, {field: name, "label": placement["label"]})
		content = _json(doc.content, [])
		key = ws.BLOCKS[block_type][1]
		if not any(b.get("type") == block_type and (b.get("data") or {}).get(key) == placement["label"] for b in content):
			block = {"id": ws.block_id(), "type": block_type, "data": {key: placement["label"], "col": placement.get("col") or ws.BLOCKS[block_type][2]}}
			slots = [i for i, b in enumerate(content) if b.get("type") == block_type]
			if slots:
				content.insert(slots[-1] + 1, block)
			else:
				content.insert(0, block) if block_type == "number_card" else content.append(block)
		doc.content = json.dumps(content)
		doc.save()

	def _unplace(self, obj, name, placement):
		table, field, block_type = _table(obj)
		doc = frappe.get_doc("Workspace", placement["workspace"])
		rows = [r for r in doc.get(table) if r.get(field) != name]
		if len(rows) == len(doc.get(table)):
			return
		doc.set(table, rows)
		key = ws.BLOCKS[block_type][1]
		labels = {r.label or r.get(field) for r in rows}
		content = [b for b in _json(doc.content, []) if not (b.get("type") == block_type
			and (b.get("data") or {}).get(key) == placement["label"] and placement["label"] not in labels)]
		doc.content = json.dumps(content)
		doc.save()

	def _delete(self, spec, state):
		for p in state.get("placements") or []:
			self._unplace(spec["object"], spec["name"], p)
		frappe.delete_doc(DOCTYPES[spec["object"]], spec["name"])

	# -- execute / verify -----------------------------------------------------------------------------------
	def execute(self, spec, user, approval=None):
		state = self.snapshot(spec)
		obj, name, op = spec["object"], spec["name"], spec["operation"]
		with _base.as_user(user):
			if op == "delete":
				self._delete(spec, state)
				undo = {"operation": "create", "object": obj, "name": name, "definition": state["definition"],
					"placements": state.get("placements") or [], "purpose": _("restore {0}").format(name)}
			else:
				doc = self._save(spec, self.effective(spec, state) if op == "create" else spec.get("definition") or {})
				name = doc.name
				for p in spec.get("remove_placements") or []:
					self._unplace(obj, name, p)
				if op == "create":
					undo = {"operation": "delete", "object": obj, "name": name}
				else:
					before = state.get("definition") or {}
					placed = {p["workspace"] for p in state.get("placements") or []}
					undo = {"operation": "update", "object": obj, "name": name, "placements": [],
						"remove_placements": [p for p in spec.get("placements") or [] if p["workspace"] not in placed],
						"definition": {k: before.get(k) for k in (spec.get("definition") or {}) if k in before},
						"purpose": _("undo of {0}").format(_base.approval_name(approval) or "")}
					if "chart_type" in undo["definition"]:
						undo["definition"]["timeseries"] = before.get("timeseries", 0)
		return {"targets": [{"doctype": DOCTYPES[obj], "name": name}], "undo": undo}

	def verify(self, spec, result):
		state = self.snapshot(spec)
		obj, name, op = spec["object"], spec["name"], spec["operation"]
		checks = []
		if op == "delete":
			if state["exists"]:
				checks.append(Check("verify.deleted", "critical", _("{0} still exists").format(name)))
			return checks or [Check("verify.deleted", "ok", _("{0} is deleted").format(name))]
		if not state["exists"]:
			return [Check("verify.exists", "critical", _("{0} {1} does not exist").format(DOCTYPES[obj], name))]
		stored = state["definition"]
		for key, value in (spec.get("definition") or {}).items():
			if key == "label" and not value:
				continue
			if json.dumps(stored.get(key), sort_keys=True, default=str) != json.dumps(value, sort_keys=True, default=str) and \
					cstr(stored.get(key)) != cstr(value):
				checks.append(Check("verify.field", "critical", _("{0} is {1}, not {2}").format(key, stored.get(key), value)))
		placed = {p["workspace"] for p in state.get("placements") or []}
		for p in spec.get("placements") or []:
			if p["workspace"] not in placed:
				checks.append(Check("verify.workspace", "critical", _("It is not on {0}").format(p["workspace"])))
		for p in spec.get("remove_placements") or []:
			if p["workspace"] in placed:
				checks.append(Check("verify.workspace", "critical", _("It is still on {0}").format(p["workspace"])))
		if not checks:
			try:
				if obj == "number_card":
					self._card_value(state["definition"])
				else:
					self._chart_data(state["definition"])
				checks.append(Check("verify.dashboard", "ok", _("{0} is saved and computes").format(name)))
			except Exception as exc:
				checks.append(Check("verify.run", "critical", _("It does not compute: {0}").format(_base.frappe_error(exc))))
		return checks

	def summary(self, spec):
		verb = {"create": _("Create"), "update": _("Update"), "delete": _("Delete")}[spec["operation"]]
		what = _("number card") if spec["object"] == "number_card" else _("chart")
		where = ""
		if spec.get("placements"):
			where = _(" on {0}").format(", ".join(p["workspace"] for p in spec["placements"]))
		return _("{0} {1} {2}{3}").format(verb, what, spec["name"], where)

	def get_source(self, name, user=None, obj=None):
		for candidate in ([obj] if obj else OBJECTS):
			if frappe.db.exists(DOCTYPES[candidate], name):
				state = self.snapshot({"object": candidate, "name": name})
				state["kind"] = self.name
				return state
		raise frappe.DoesNotExistError(_("There is no number card or chart {0}").format(name))


KIND = DashboardKind()
