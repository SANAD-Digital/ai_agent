"""List view settings of one DocType: ``customize.request_list_settings``.

Where Frappe 16 keeps each list setting (read from its own code):

- **List View Settings** (``frappe/desk/doctype/list_view_settings``, named after the DocType, autoname Prompt): the
  flags ``disable_count``, ``disable_comment_count``, ``disable_sidebar_stats``, ``disable_auto_refresh``,
  ``disable_automatic_recency_filters``, ``disable_scrolling``, ``show_tags``, ``allow_edit`` and ``fields``, a JSON
  list ``[{"fieldname", "label"}]`` that orders the columns (``list_view.js reorder_listview_fields``). It is the
  same record for every user.
- **Columns** render only for fields with ``in_list_view`` (``list_view.js setup_columns``), so Frappe's own Save
  (``save_listview_settings`` -> ``set_listview_fields``, list_view_settings.py:31-86) also writes ``in_list_view``
  Property Setters: 1 for the listed fields, 0 for the ones removed. This kind does the same.
- **Default sort**: the DocType's ``sort_field`` / ``sort_order``, Property Setters on the DocType (what Customize
  Form writes; ``doctype.py check_sort_field`` validates the field).
- **Group-by sidebar fields** are per user: ``__UserSettings[doctype].group_by_fields``
  (``list_sidebar_group_by.js``:9-12), cached in Redis and synced from the browser. Not a site setting, so not
  offered here; each user picks them from the sidebar's "Edit" menu.

Operations: ``update`` (creates the record when missing), ``reset`` (deletes the record: Frappe's defaults come
back; Property Setters stay) and ``restore`` (only as an undo: puts back an exact earlier state).
"""

from __future__ import annotations

import json
import re

import frappe
from frappe import _
from frappe.model import no_value_fields
from frappe.utils import cint, cstr

from ai_agent.customization.contract import Check, CustomizationError
from ai_agent.customization.kinds import _base

TOP_KEYS = ("operation", "doctype", "settings", "columns", "sort_field", "sort_order", "purpose")
OPERATIONS = ("update", "reset")
FLAGS = ("disable_count", "disable_comment_count", "disable_sidebar_stats", "disable_auto_refresh",
	"disable_automatic_recency_filters", "disable_scrolling", "show_tags", "allow_edit")
SORT_KEYS = ("sort_field", "sort_order")
STANDARD_SORT = ("name", "owner", "creation", "modified", "modified_by", "docstatus", "idx")
MAX_COLUMNS = 20
WIDE_COLUMNS = 8
FIELDNAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
FLAG_TEXT = {
	"disable_count": "the total count above the list",
	"disable_comment_count": "the comment count of each row",
	"disable_sidebar_stats": "the group-by counts in the sidebar",
	"disable_auto_refresh": "live refresh when documents change",
	"disable_automatic_recency_filters": "the automatic 'last 90 days' filter on large tables",
	"disable_scrolling": "infinite scrolling",
	"show_tags": "the tags column",
	"allow_edit": "editing cells in the list",
}


def _record(doctype):
	if not frappe.db.exists("List View Settings", doctype):
		return None
	doc = frappe.get_doc("List View Settings", doctype)
	out = {flag: cint(doc.get(flag)) for flag in FLAGS}
	try:
		out["fields"] = json.loads(doc.fields) if doc.fields else None
	except ValueError:
		out["fields"] = None
	return out


def _ps(doctype, field, prop):
	filters = {"doc_type": doctype, "property": prop}
	if field:
		filters["field_name"] = field
		filters["doctype_or_field"] = "DocField"
	else:
		filters["doctype_or_field"] = "DocType"
	return frappe.db.get_value("Property Setter", filters, ["name", "value"], as_dict=True)


def state(doctype):
	"""The snapshot: the record, every in_list_view Property Setter and the sort Property Setters."""
	listed = frappe.get_all("Property Setter", filters={"doc_type": doctype, "property": "in_list_view",
		"doctype_or_field": "DocField"}, fields=["field_name", "value"], order_by="field_name asc")
	sort = {}
	for prop in SORT_KEYS:
		row = _ps(doctype, None, prop)
		sort[prop] = row.value if row else None
	return {"doctype": doctype, "record": _record(doctype),
		"in_list_view": {r.field_name: cstr(r.value) for r in listed}, "sort": sort}


def subject_field(meta):
	title = (meta.title_field or "").strip()
	return title if title and meta.get_field(title) else "name"


def has_indicator(meta):
	"""Python side of ``frappe.has_indicator`` (indicator.js): a status field, or a submittable DocType."""
	return bool(meta.get_field("status") or meta.is_submittable)


def columns_of(doctype):
	"""The columns a user sees today, in order (subject first)."""
	meta = frappe.get_meta(doctype)
	record = _record(doctype)
	if record and record.get("fields"):
		return [f.get("fieldname") for f in record["fields"] if f.get("fieldname")]
	cols = [subject_field(meta)]
	if has_indicator(meta):
		cols.append("status_field")
	cols += [df.fieldname for df in meta.fields if df.in_list_view and df.fieldtype not in no_value_fields
		and df.fieldname != cols[0] and not (has_indicator(meta) and df.fieldname == "status")]
	return cols


class ListSettingsKind:
	name = "list_settings"
	setting = "allow_customize_fields"
	required_roles = ("System Manager",)

	# -- normalize -----------------------------------------------------------------------------------
	def normalize(self, args, user):
		args = dict(args or {})
		_base.reject_unknown(args, TOP_KEYS, "customize.request_list_settings")
		_base.require(args, ("operation", "doctype"), "customize.request_list_settings")
		op = args["operation"]
		if op not in OPERATIONS:
			raise CustomizationError(_("operation must be update or reset"),
				hints=[_("Group-by sidebar fields are chosen by each user (stored per user), not here")])
		doctype = cstr(args["doctype"]).strip()
		if not frappe.db.exists("DocType", doctype):
			raise CustomizationError(_("There is no DocType {0}").format(doctype), hints=[_("Find the exact name with schema__search_doctypes")])
		spec = {"operation": op, "doctype": doctype, "name": doctype, "requested_by": user}
		if args.get("purpose"):
			spec["purpose"] = re.sub(r"\s+", " ", cstr(args["purpose"])).strip()[:140]
		if op == "reset":
			extra = set(args) - {"operation", "doctype", "purpose"}
			if extra:
				raise CustomizationError(_("reset takes only doctype and purpose"))
			return spec
		if args.get("settings") is not None:
			settings = args["settings"]
			if not isinstance(settings, dict):
				raise CustomizationError(_("settings must be an object of flags"))
			_base.reject_unknown(settings, FLAGS, "settings")
			spec["settings"] = {k: 1 if cint(v) else 0 for k, v in settings.items()}
		if args.get("columns") is not None:
			columns = args["columns"]
			if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
				raise CustomizationError(_("columns must be a list of fieldnames, in order"), hints=[_("The title field (or name) is always the first column")])
			if len(columns) > MAX_COLUMNS:
				raise CustomizationError(_("At most {0} columns").format(MAX_COLUMNS))
			bad = [c for c in columns if not FIELDNAME_RE.match(c)]
			if bad:
				raise CustomizationError(_("columns are fieldnames, not labels: {0}").format(", ".join(bad)),
					hints=[_("Read the fieldnames with schema__describe_doctype")])
			spec["columns"] = list(dict.fromkeys(columns))
		for key in SORT_KEYS:
			if args.get(key) not in (None, ""):
				spec[key] = cstr(args[key]).strip()
		if spec.get("sort_order"):
			spec["sort_order"] = spec["sort_order"].upper()
			if spec["sort_order"] not in ("ASC", "DESC"):
				raise CustomizationError(_("sort_order must be ASC or DESC"))
		if not (set(spec) & {"settings", "columns", "sort_field", "sort_order"}):
			raise CustomizationError(_("Nothing to change"), hints=[_("Send settings, columns, sort_field or sort_order")])
		return spec

	# -- snapshot -----------------------------------------------------------------------------------------
	def snapshot(self, spec):
		return json.loads(json.dumps(state(spec["doctype"]), default=str))

	# -- plan: the exact writes ------------------------------------------------------------------------------
	def plan(self, spec, before=None):
		"""{record: dict | None | "keep", in_list_view: {field: "1"|"0"|None}, sort: {prop: value|None}}."""
		before = before or state(spec["doctype"])
		op = spec["operation"]
		if op == "restore":
			return {"record": spec.get("record", "keep"), "in_list_view": dict(spec.get("in_list_view") or {}),
				"sort": dict(spec.get("sort") or {})}
		if op == "reset":
			return {"record": None, "in_list_view": {}, "sort": {}}
		meta = frappe.get_meta(spec["doctype"])
		out = {"record": "keep", "in_list_view": {}, "sort": {k: spec[k] for k in SORT_KEYS if k in spec}}
		if "settings" in spec or "columns" in spec:
			record = dict(before["record"] or {flag: 0 for flag in FLAGS})
			record.setdefault("fields", None)
			record.update(spec.get("settings") or {})
			if "columns" in spec:
				subject = subject_field(meta)
				fields = [{"label": _label(meta, subject), "fieldname": subject}]
				if has_indicator(meta):
					fields.append({"type": "Status", "label": "Status", "fieldname": "status_field"})
				for name in spec["columns"]:
					if name in (subject, "status_field") or (name == "status" and has_indicator(meta)):
						continue
					fields.append({"label": _label(meta, name), "fieldname": name})
				record["fields"] = fields
				# list_view_settings.set_listview_fields: 1 for listed fields, 0 for the ones taken out
				listed = [f["fieldname"] for f in fields if f["fieldname"] not in ("status_field", "name")]
				shown = [c for c in columns_of(spec["doctype"]) if c not in ("status_field", "name")]
				for name in listed:
					out["in_list_view"][name] = "1"
				for name in shown:
					if name not in listed and meta.get_field(name):
						out["in_list_view"][name] = "0"
			out["record"] = record
		return out

	# -- check ----------------------------------------------------------------------------------------------
	def check(self, spec, user):
		doctype = spec["doctype"]
		checks = _base.role_checks(user, self.required_roles, _("Frappe lets only System Managers save List View Settings"))
		reason = _base.customizable_reason(doctype, allow_custom=True)
		if reason:
			checks.append(Check("list_settings.doctype", "critical", reason))
			return _base.build_report(checks, risk="medium")
		meta = frappe.get_meta(doctype)
		if meta.istable:
			checks.append(Check("list_settings.doctype", "critical", _("{0} is a child table: it has no list").format(doctype)))
			return _base.build_report(checks, risk="medium")
		if not frappe.has_permission(doctype, "read", user=user):
			checks.append(Check("list_settings.read", "critical", _("{0} cannot read {1}").format(user, doctype)))
		before = state(doctype)
		if spec["operation"] == "reset" and not before["record"]:
			checks.append(Check("list_settings.reset", "critical", _("{0} has no List View Settings: it already uses Frappe's defaults").format(doctype)))
		subject = subject_field(meta)
		for name in spec.get("columns") or []:
			if name in ("status_field",) or name == subject or (name == "status" and has_indicator(meta)):
				checks.append(Check("list_settings.column", "info", _("{0} is always shown: the title (or ID) comes first and the status indicator next").format(name)))
				continue
			df = meta.get_field(name)
			if not df:
				checks.append(Check("list_settings.column", "critical", _("{0} has no field {1}").format(doctype, name)))
			elif df.fieldtype in no_value_fields:
				checks.append(Check("list_settings.column", "critical", _("{0} is a {1}: it holds no value to show in a column").format(name, df.fieldtype)))
			elif cint(df.is_virtual):
				checks.append(Check("list_settings.column", "critical", _("{0} is a virtual field: the list never shows it (list_view.js)").format(name)))
			else:
				if cint(df.permlevel):
					checks.append(Check("list_settings.column", "warning", _("{0} has permission level {1}: users without that level see an empty column").format(name, df.permlevel)))
				if cint(df.hidden):
					checks.append(Check("list_settings.column", "warning", _("{0} is hidden on the form but will show in the list").format(name)))
		if len(spec.get("columns") or []) > WIDE_COLUMNS:
			checks.append(Check("list_settings.columns_many", "warning", _("{0} columns: on a laptop screen only about 6 fit before the list scrolls sideways").format(len(spec["columns"]))))
		if spec.get("sort_field"):
			fields = {df.fieldname for df in meta.fields if df.fieldtype not in no_value_fields}
			for part in spec["sort_field"].split(","):
				name = part.strip().split(" ")[0]
				if name not in fields and name not in STANDARD_SORT:
					checks.append(Check("list_settings.sort", "critical", _("sort_field: {0} has no field {1} (doctype.py check_sort_field)").format(doctype, name)))
		if spec["operation"] == "update" and spec.get("settings"):
			for flag, value in spec["settings"].items():
				if flag == "allow_edit" and value:
					checks.append(Check("list_settings.allow_edit", "info", _("Inline editing still needs write permission on each document")))
		plan = self.plan(spec, before)
		preview = self._preview(spec, before, plan, meta)
		if not any(c.level == "critical" for c in checks) and spec["operation"] != "restore":
			checks += self._dry_run(spec, user)
		impact = [_("Every user who opens the {0} list").format(doctype)]
		if plan["in_list_view"]:
			impact.append(_("Sets 'In List View' on {0}, as Frappe's own List Settings dialog does").format(
				", ".join(sorted(plan["in_list_view"]))))
		if plan["sort"]:
			impact.append(_("Changes the default sort of {0} for everyone (a user's own sort choice still wins)").format(doctype))
		return _base.build_report(checks, impact=impact, preview=preview, risk="low")

	def _preview(self, spec, before, plan, meta):
		preview = {"doctype": spec["doctype"], "operation": spec["operation"]}
		now = columns_of(spec["doctype"])
		preview["columns_before"] = [_label(meta, c) for c in now]
		record = plan["record"]
		if record not in ("keep", None) and record.get("fields"):
			preview["columns_after"] = [f.get("label") for f in record["fields"]]
		elif record is None:
			preview["columns_after"] = _("Frappe's defaults")
		flags = []
		old = before["record"] or {flag: 0 for flag in FLAGS}
		new = old if record == "keep" else (record or {flag: 0 for flag in FLAGS})
		for flag in FLAGS:
			if cint(old.get(flag)) != cint(new.get(flag)):
				flags.append({"label": flag, "field": _(FLAG_TEXT[flag]), "before": cint(old.get(flag)), "after": cint(new.get(flag))})
		preview["diff"] = flags + [{"label": k, "before": before["sort"].get(k), "after": v} for k, v in plan["sort"].items()]
		return preview

	def _dry_run(self, spec, user):
		from ai_agent.customization.kinds.workspace import dry_run, frappe_error

		collect = _base.capture_messages()
		try:
			with dry_run(), _base.as_user(user):
				self._apply(spec["doctype"], self.plan(spec))
		except Exception as exc:
			return [Check("list_settings.frappe", "critical", _("Frappe refuses the change: {0}").format(frappe_error(exc, collect())))]
		collect()
		return [Check("list_settings.frappe", "ok", _("Frappe's own validation passes (dry run, rolled back)"))]

	# -- writing -------------------------------------------------------------------------------------------
	def _apply(self, doctype, plan):
		record = plan["record"]
		if record is None:
			if frappe.db.exists("List View Settings", doctype):
				frappe.delete_doc("List View Settings", doctype)
		elif record != "keep":
			if frappe.db.exists("List View Settings", doctype):
				doc = frappe.get_doc("List View Settings", doctype)
			else:
				doc = frappe.new_doc("List View Settings")
				doc.name = doctype  # save_listview_settings names it after the DocType
			for flag in FLAGS:
				doc.set(flag, cint(record.get(flag)))
			doc.fields = json.dumps(record["fields"]) if record.get("fields") else None
			doc.save() if not doc.is_new() else doc.insert()
		for field, value in plan["in_list_view"].items():
			self._set_ps(doctype, field, "in_list_view", value, "Check", validate=False)
		for prop, value in plan["sort"].items():
			self._set_ps(doctype, None, prop, value, "Data" if prop == "sort_field" else "Select", validate=True)
		frappe.clear_cache(doctype=doctype)

	def _set_ps(self, doctype, field, prop, value, prop_type, validate):
		row = _ps(doctype, field, prop)
		if value is None:
			if row:
				frappe.delete_doc("Property Setter", row.name)
			return
		if row:
			doc = frappe.get_doc("Property Setter", row.name)
			doc.value = value
			doc.flags.ignore_validate = not validate
			doc.save()
			return
		frappe.make_property_setter({"doctype": doctype, "doctype_or_field": "DocField" if field else "DocType",
			"fieldname": field, "property": prop, "value": value, "property_type": prop_type},
			ignore_validate=not validate)

	def _restore_spec(self, spec, before, plan):
		undo = {"operation": "restore", "doctype": spec["doctype"], "name": spec["doctype"],
			"purpose": _("undo of {0}").format(_base.approval_name() or "")}
		if plan["record"] != "keep":
			undo["record"] = before["record"]
		if plan["in_list_view"]:
			undo["in_list_view"] = {f: before["in_list_view"].get(f) for f in plan["in_list_view"]}
		if plan["sort"]:
			undo["sort"] = {k: before["sort"].get(k) for k in plan["sort"]}
		return undo

	# -- execute / verify -------------------------------------------------------------------------------------
	def execute(self, spec, user, approval=None):
		before = state(spec["doctype"])
		plan = self.plan(spec, before)
		with _base.as_user(user):
			self._apply(spec["doctype"], plan)
		return {"targets": [{"doctype": "List View Settings", "name": spec["doctype"]}],
			"undo": self._restore_spec(spec, before, plan), "plan": plan}

	def verify(self, spec, result):
		plan = result.get("plan") or self.plan(spec)
		after = state(spec["doctype"])
		checks = []
		record = plan["record"]
		if record is None and after["record"]:
			checks.append(Check("verify.record", "critical", _("The List View Settings of {0} still exist").format(spec["doctype"])))
		elif record not in ("keep", None):
			got = after["record"] or {}
			for flag in FLAGS:
				if cint(got.get(flag)) != cint(record.get(flag)):
					checks.append(Check("verify.flag", "critical", _("{0} is {1}, not {2}").format(flag, got.get(flag), record.get(flag))))
			if (got.get("fields") or None) != (record.get("fields") or None):
				checks.append(Check("verify.columns", "critical", _("The stored columns differ from the approved ones")))
		for field, value in plan["in_list_view"].items():
			if after["in_list_view"].get(field) != value:
				checks.append(Check("verify.in_list_view", "critical", _("In List View of {0} is {1}, not {2}").format(field, after["in_list_view"].get(field), value)))
		for prop, value in plan["sort"].items():
			if after["sort"].get(prop) != value:
				checks.append(Check("verify.sort", "critical", _("{0} is {1}, not {2}").format(prop, after["sort"].get(prop), value)))
		meta = frappe.get_meta(spec["doctype"], cached=False)
		if plan["sort"].get("sort_field") and meta.sort_field != plan["sort"]["sort_field"]:
			checks.append(Check("verify.sort", "critical", _("The DocType still sorts by {0}").format(meta.sort_field)))
		if not checks:
			checks.append(Check("verify.list_settings", "ok", _("The {0} list settings are in place").format(spec["doctype"])))
		return checks

	def summary(self, spec):
		verb = {"update": _("Update"), "reset": _("Reset"), "restore": _("Restore")}[spec["operation"]]
		return _("{0} the list settings of {1}").format(verb, spec["doctype"])

	def get_source(self, name, user=None):
		if not frappe.db.exists("DocType", name):
			raise frappe.DoesNotExistError(_("There is no DocType {0}").format(name))
		out = state(name)
		meta = frappe.get_meta(name)
		out["kind"] = self.name
		out["columns"] = columns_of(name)
		out["sort_field"], out["sort_order"] = meta.sort_field, meta.sort_order
		return out


def _label(meta, fieldname):
	if fieldname == "name":
		return "ID"
	if fieldname == "status_field":
		return "Status"
	df = meta.get_field(fieldname)
	return df.label if df and df.label else fieldname


KIND = ListSettingsKind()
