"""Field and DocType properties with Customize Form semantics (docs/customization/01 §3.3.2).

Not a direct Property Setter write: the change goes through ``CustomizeForm.save_customization`` in
the process, so Frappe's rules apply (no un-requiring a mandatory standard field, no un-read-only,
``allow_on_submit``/``options``/``in_list_view``/``translatable``/``is_virtual`` limits, fieldtype
groups, length shrink checks). Customize Form skips a refused property silently (``msgprint`` then
``return False``), so ``check`` replays ``allow_property_change`` per property inside a savepoint and
turns every refusal into a critical check with Frappe's own message.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, cstr

from ai_agent.customization.contract import Check, CustomizationError
from ai_agent.customization.kinds import _base

DOCTYPE_PROPS = ("title_field", "search_fields", "sort_field", "sort_order", "image_field", "show_title_field_in_link",
	"quick_entry", "track_changes", "track_views", "allow_copy", "allow_import", "editable_grid", "max_attachments",
	"default_view", "show_name_in_global_search", "show_preview_popup", "default_print_format")
FIELD_PROPS = ("label", "hidden", "reqd", "read_only", "bold", "in_list_view", "in_standard_filter", "in_global_search",
	"in_preview", "allow_on_submit", "default", "options", "description", "depends_on", "mandatory_depends_on",
	"read_only_depends_on", "collapsible", "collapsible_depends_on", "print_hide", "print_hide_if_no_value",
	"report_hide", "no_copy", "permlevel", "precision", "columns", "width", "fetch_from", "fetch_if_empty",
	"allow_in_quick_entry", "placeholder", "unique", "length", "link_filters", "remember_last_selected_value",
	"set_only_once", "non_negative", "mask")
TOP_KEYS = ("doctype", "doctype_properties", "field_properties", "field_order", "reason", "purpose")
MAX_PROPERTIES = 40
STANDARD_SORT = ("name", "creation", "modified", "idx", "owner", "modified_by")
SAVEPOINT = "ai_customize_properties_check"


def _types():
	from frappe.custom.doctype.customize_form.customize_form import docfield_properties, doctype_properties

	return doctype_properties, docfield_properties


def _coerce(prop, value, prop_type):
	if prop_type in ("Check", "Int"):
		return cint(value)
	if prop_type == "JSON" and isinstance(value, dict | list):
		return json.dumps(value)
	return cstr(value)


def _norm(value, prop_type):
	return cint(value) if prop_type in ("Check", "Int") else cstr(value)


def _is_custom(df):
	return bool(df.get("is_custom_field")) and not df.get("is_system_generated")


class PropertiesKind:
	name = "properties"
	setting = "allow_customize_fields"
	required_roles = ("System Manager",)

	# -- normalize --------------------------------------------------------------
	def normalize(self, args, user):
		args = dict(args or {})
		_base.reject_unknown(args, TOP_KEYS, "customize.request_properties")
		_base.require(args, ("doctype",), "customize.request_properties")
		dt_types, df_types = _types()
		spec = {"doctype": cstr(args["doctype"]).strip(), "doctype_properties": {}, "field_properties": {}}
		raw_dt = args.get("doctype_properties") or {}
		raw_df = args.get("field_properties") or {}
		if not isinstance(raw_dt, dict) or not isinstance(raw_df, dict):
			raise CustomizationError(_("doctype_properties and field_properties must be objects"))
		for prop, value in raw_dt.items():
			if prop not in DOCTYPE_PROPS:
				raise CustomizationError(_("{0} is not a DocType property that can be customized").format(prop),
					hints=[_("Allowed: {0}").format(", ".join(DOCTYPE_PROPS))])
			spec["doctype_properties"][prop] = _coerce(prop, value, dt_types.get(prop))
		for fieldname, props in raw_df.items():
			if not isinstance(props, dict) or not props:
				raise CustomizationError(_("field_properties.{0} must be an object of property: value").format(fieldname))
			out = {}
			for prop, value in props.items():
				if prop not in FIELD_PROPS:
					hint = _("To change a field type use customize.request_field (custom fields only)") if prop == "fieldtype" else \
						_("Allowed: {0}").format(", ".join(FIELD_PROPS))
					raise CustomizationError(_("{0} is not a field property that can be customized").format(prop), hints=[hint])
				out[prop] = _coerce(prop, value, df_types.get(prop))
			spec["field_properties"][cstr(fieldname)] = out
		if args.get("field_order"):
			order = args["field_order"]
			if not isinstance(order, list) or not all(isinstance(f, str) for f in order):
				raise CustomizationError(_("field_order must be a list of fieldnames"))
			spec["field_order"] = list(order)
		total = len(spec["doctype_properties"]) + sum(len(p) for p in spec["field_properties"].values())
		if not total and not spec.get("field_order"):
			raise CustomizationError(_("Nothing to change: send doctype_properties, field_properties or field_order"))
		if total > MAX_PROPERTIES:
			raise CustomizationError(_("At most {0} properties per request").format(MAX_PROPERTIES))
		if args.get("reason") or args.get("purpose"):
			spec["reason"] = cstr(args.get("reason") or args.get("purpose"))[:300]
		return spec

	# -- snapshot -----------------------------------------------------------------
	def snapshot(self, spec):
		dt_types, df_types = _types()
		doctype = spec["doctype"]
		out = {"doctype": doctype, "doctype_properties": {}, "field_properties": {}}
		try:
			meta = frappe.get_meta(doctype, cached=False)
		except frappe.DoesNotExistError:
			return out
		for prop in spec.get("doctype_properties") or {}:
			out["doctype_properties"][prop] = _norm(meta.get(prop), dt_types.get(prop))
		for fieldname, props in (spec.get("field_properties") or {}).items():
			df = meta.get_field(fieldname)
			out["field_properties"][fieldname] = {p: _norm(df.get(p), df_types.get(p)) for p in props} if df else None
		if spec.get("field_order"):
			out["field_order"] = [df.fieldname for df in meta.fields]
		return out

	# -- check --------------------------------------------------------------------
	def check(self, spec, user):
		doctype = spec["doctype"]
		checks = _base.role_checks(user, self.required_roles, _("Customize Form needs it"))
		reason = _base.customizable_reason(doctype)
		if reason:
			checks.append(Check("properties.doctype", "critical", reason))
			return _base.build_report(checks, risk="high")
		meta = frappe.get_meta(doctype, cached=False)
		before = self.snapshot(spec)
		for fieldname, props in spec["field_properties"].items():
			if not meta.get_field(fieldname):
				checks.append(Check("properties.field", "critical", _("{0} has no field {1}").format(doctype, fieldname)))
		if any(c.level == "critical" for c in checks):
			return _base.build_report(checks, risk="medium")

		checks += self._simulate(spec)
		checks += self._references(spec, meta)
		checks += self._field_order(spec, meta)
		for fieldname, props in spec["field_properties"].items():
			df = meta.get_field(fieldname)
			hidden = props.get("hidden", df.hidden)
			reqd = props.get("reqd", df.reqd)
			default = props.get("default", df.default)
			if cint(hidden) and cint(reqd) and not default:
				checks.append(Check("properties.hidden_reqd", "critical", _("{0} cannot be hidden and mandatory without a default").format(fieldname)))
			if cint(props.get("reqd")) and not cint(df.reqd):
				count = frappe.db.count(doctype)
				if count:
					checks.append(Check("properties.reqd_existing", "warning", _("{0} existing records will ask for {1} the next time they are saved").format(count, fieldname)))
		checks += self._frappe_validate(spec)

		preview = self._preview(spec, meta, before)
		impact = [_("Changes the {0} form and list for every user").format(doctype)]
		if any(p in props for props in spec["field_properties"].values() for p in ("length", "unique", "precision")):
			impact.append(_("Changes the database column (schema change)"))
		if not any(c.level == "critical" for c in checks):
			checks.insert(0, Check("properties.valid", "ok", _("Customize Form accepts every property")))
		return _base.build_report(checks, impact=impact, preview=preview, risk="low")

	def _load_form(self, doctype):
		cf = frappe.get_doc("Customize Form")
		cf.doc_type = doctype
		cf.fetch_to_customize()
		return cf

	def _simulate(self, spec):
		"""Replay CustomizeForm.allow_property_change for each field property; nothing persists."""
		doctype = spec["doctype"]
		checks = []
		meta = frappe.get_meta(doctype)
		if meta.allow_auto_repeat and not meta.get_field("auto_repeat"):
			# fetch_to_customize would create the auto_repeat field (DDL commits, the savepoint is lost)
			return [Check("properties.not_simulated", "warning", _("Frappe's per-property rules could not be replayed for {0}").format(doctype))]
		collect = _base.capture_messages()
		frappe.db.savepoint(SAVEPOINT)
		try:
			cf = self._load_form(doctype)
			meta = frappe.get_meta(doctype)
			rows = {row.fieldname: row for row in cf.fields}
			cf.check_length_for_fieldtypes = []
			for fieldname, props in spec["field_properties"].items():
				row = rows.get(fieldname)
				meta_df = meta.get("fields", {"fieldname": fieldname})
				if not row or not meta_df:
					continue
				if _is_custom(meta_df[0]):
					continue  # Customize Form writes custom fields directly, without these rules
				for prop, value in props.items():
					row.set(prop, value)
					collect()  # drop earlier messages
					try:
						allowed = cf.allow_property_change(prop, meta_df, row, meta)
					except Exception as e:
						allowed = False
						messages = [frappe.utils.strip_html(str(e))]
					else:
						messages = collect()
					if allowed is False:
						text = "; ".join(m for m in messages if m) or _("not allowed")
						checks.append(Check("properties.refused", "critical", _("{0}.{1}: Frappe refuses it: {2}").format(fieldname, prop, text)))
		except Exception as e:
			checks.append(Check("properties.refused", "critical", _("Customize Form cannot load {0}: {1}").format(doctype, frappe.utils.strip_html(str(e)))))
		finally:
			frappe.db.rollback(save_point=SAVEPOINT)
			collect()
			frappe.clear_cache(doctype=doctype)
		return checks

	def _references(self, spec, meta):
		props = spec["doctype_properties"]
		out = []
		fields = {df.fieldname: df for df in meta.fields}
		for key in ("title_field", "image_field"):
			value = props.get(key)
			if value and value not in fields:
				out.append(Check("properties.reference", "critical", _("{0}: {1} has no field {2}").format(key, spec["doctype"], value)))
		if props.get("image_field") and props["image_field"] in fields and fields[props["image_field"]].fieldtype != "Attach Image":
			out.append(Check("properties.reference", "critical", _("image_field must be an Attach Image field")))
		if props.get("search_fields"):
			for name in [s.strip() for s in props["search_fields"].split(",") if s.strip()]:
				if name not in fields:
					out.append(Check("properties.reference", "critical", _("search_fields: {0} has no field {1}").format(spec["doctype"], name)))
		if props.get("sort_field"):
			for part in props["sort_field"].split(","):
				name = part.split()[0] if part.split() else ""
				if name and name not in fields and name not in STANDARD_SORT:
					out.append(Check("properties.reference", "critical", _("sort_field: {0} has no field {1}").format(spec["doctype"], name)))
		if props.get("sort_order") and props["sort_order"].upper() not in ("ASC", "DESC"):
			out.append(Check("properties.reference", "critical", _("sort_order must be ASC or DESC")))
		if props.get("default_print_format") and not frappe.db.exists("Print Format", props["default_print_format"]):
			out.append(Check("properties.reference", "critical", _("There is no Print Format {0}").format(props["default_print_format"])))
		for fieldname, fprops in spec["field_properties"].items():
			if fprops.get("in_list_view") and meta.get_field(fieldname).fieldtype in (
				"Section Break", "Column Break", "Tab Break", "HTML", "Table", "Table MultiSelect", "Button"):
				out.append(Check("properties.refused", "critical", _("{0} ({1}) cannot be a list column").format(fieldname, meta.get_field(fieldname).fieldtype)))
		return out

	def _field_order(self, spec, meta):
		order = spec.get("field_order")
		if not order:
			return []
		current = [df.fieldname for df in meta.fields]
		if sorted(order) != sorted(current):
			missing = sorted(set(current) - set(order))
			extra = sorted(set(order) - set(current))
			return [Check("properties.field_order", "critical",
				_("field_order must list exactly the current fields (missing: {0}; unknown: {1})").format(", ".join(missing) or "-", ", ".join(extra) or "-"))]
		return []

	def _frappe_validate(self, spec):
		from frappe.core.doctype.doctype.doctype import validate_fields
		from frappe.model.meta import Meta

		meta = Meta(spec["doctype"])  # a private copy: get_meta(cached=False) would cache the mutated object
		for prop, value in spec["doctype_properties"].items():
			meta.set(prop, value)
		for fieldname, props in spec["field_properties"].items():
			df = next((d for d in meta.fields if d.fieldname == fieldname), None)
			if df:
				df.update(props)
		if spec.get("field_order"):
			index = {name: i for i, name in enumerate(spec["field_order"])}
			meta.fields.sort(key=lambda d: index.get(d.fieldname, 0))
		collect = _base.capture_messages()
		try:
			validate_fields(meta)
		except Exception as e:
			text = frappe.utils.strip_html(str(e)) or "; ".join(collect())
			return [Check("properties.frappe", "critical", _("Frappe rejects it: {0}").format(text))]
		return [Check("properties.frappe", "warning", m) for m in collect()]

	def _preview(self, spec, meta, before):
		diff = []
		for prop, value in spec["doctype_properties"].items():
			old = before["doctype_properties"].get(prop)
			if cstr(old) != cstr(value):
				diff.append({"field": "", "label": prop, "property": prop, "before": cstr(old), "after": cstr(value)})
		for fieldname, props in spec["field_properties"].items():
			old_props = before["field_properties"].get(fieldname) or {}
			label = meta.get_label(fieldname)
			for prop, value in props.items():
				if cstr(old_props.get(prop)) != cstr(value):
					diff.append({"field": fieldname, "label": label, "property": prop, "before": cstr(old_props.get(prop)), "after": cstr(value)})

		def flag_after(df, prop):
			return cint((spec["field_properties"].get(df.fieldname) or {}).get(prop, df.get(prop)))

		return {
			"doctype": spec["doctype"],
			"diff": diff,
			"list_columns": [df.fieldname for df in meta.fields if flag_after(df, "in_list_view")],
			"standard_filters": [df.fieldname for df in meta.fields if flag_after(df, "in_standard_filter")],
			**({"field_order": spec["field_order"]} if spec.get("field_order") else {}),
		}

	# -- execute / verify -----------------------------------------------------------
	def execute(self, spec, user, approval=None):
		doctype = spec["doctype"]
		before = self.snapshot(spec)
		with _base.as_user(user):
			if not frappe.has_permission("Customize Form", "write"):
				raise frappe.PermissionError(_("{0} cannot use Customize Form").format(user))
			collect = _base.capture_messages()
			cf = self._load_form(doctype)
			for prop, value in spec["doctype_properties"].items():
				cf.set(prop, value)
			rows = {row.fieldname: row for row in cf.fields}
			for fieldname, props in spec["field_properties"].items():
				rows[fieldname].update(props)
			if spec.get("field_order"):
				index = {name: i for i, name in enumerate(spec["field_order"])}
				ordered = sorted(cf.fields, key=lambda row: index.get(row.fieldname, 0))
				for i, row in enumerate(ordered, 1):
					row.idx = i
				cf.fields = ordered
			cf.hide_success = True
			cf.save_customization()
			messages = collect()
		frappe.clear_cache(doctype=doctype)
		undo = {"doctype": doctype, "doctype_properties": dict(before["doctype_properties"]),
			"field_properties": {f: dict(p) for f, p in before["field_properties"].items() if p}}
		if spec.get("field_order"):
			undo["field_order"] = before["field_order"]
		return {"targets": [{"doctype": "DocType", "name": doctype}], "undo": undo, "messages": messages}

	def verify(self, spec, result):
		after = self.snapshot(spec)
		messages = "; ".join((result or {}).get("messages") or [])
		checks = []
		for prop, value in spec["doctype_properties"].items():
			if cstr(after["doctype_properties"].get(prop)) != cstr(value):
				checks.append(Check("verify.refused", "critical", _("{0} did not change: Frappe refused it: {1}").format(prop, messages or "-")))
		for fieldname, props in spec["field_properties"].items():
			actual = after["field_properties"].get(fieldname) or {}
			for prop, value in props.items():
				if cstr(actual.get(prop)) != cstr(value):
					checks.append(Check("verify.refused", "critical", _("{0}.{1} did not change: Frappe refused it: {2}").format(fieldname, prop, messages or "-")))
		if spec.get("field_order") and after.get("field_order") != spec["field_order"]:
			checks.append(Check("verify.refused", "critical", _("The field order did not change")))
		if not checks:
			checks.append(Check("verify.properties", "ok", _("Every property shows the new value")))
		return checks

	def summary(self, spec):
		count = len(spec["doctype_properties"]) + sum(len(p) for p in spec["field_properties"].values())
		fields = ", ".join(spec["field_properties"]) or "-"
		return _("Change {0} propert(ies) of {1} (fields: {2})").format(count, spec["doctype"], fields)


KIND = PropertiesKind()
