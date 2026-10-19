"""Custom fields: add, update, remove (docs/customization/01 §3.3.1).

Every check runs on a fresh ``frappe.get_meta(dt, cached=False)`` with the change applied in memory,
then Frappe's own ``validate_fields``. Execution inserts/saves/deletes ``Custom Field`` records as the
approver; ``on_update`` runs ``updatedb`` (DDL, implicit commit), so a failed field n deletes the
fields created before it (compensation) instead of relying on a rollback.
"""

from __future__ import annotations

import ast
import re

import frappe
from frappe import _
from frappe.utils import cint, cstr

from ai_agent.customization.contract import Check, CustomizationError
from ai_agent.customization.kinds import _base

FIELDTYPES = ("Data", "Small Text", "Text", "Long Text", "Text Editor", "Int", "Float", "Currency", "Percent",
	"Check", "Date", "Datetime", "Time", "Duration", "Select", "Link", "Dynamic Link", "Table",
	"Table MultiSelect", "Attach", "Attach Image", "Phone", "Rating", "Color", "Read Only", "HTML",
	"Section Break", "Column Break", "Tab Break")
LAYOUT_TYPES = ("Section Break", "Column Break", "Tab Break")
NO_VALUE_TYPES = LAYOUT_TYPES + ("HTML", "Table", "Table MultiSelect", "Button", "Image", "Heading", "Fold")
FLAG_KEYS = ("reqd", "hidden", "read_only", "fetch_if_empty", "in_list_view", "in_standard_filter", "bold",
	"allow_on_submit", "no_copy", "print_hide", "unique", "translatable")
INT_KEYS = FLAG_KEYS + ("permlevel", "length")
TEXT_KEYS = ("fieldname", "label", "fieldtype", "options", "insert_after", "default", "description", "depends_on",
	"mandatory_depends_on", "read_only_depends_on", "fetch_from", "precision")
FIELD_KEYS = TEXT_KEYS + INT_KEYS
TOP_KEYS = ("doctype", "operation", "fields", "reason", "purpose")
OPERATIONS = ("add", "update", "remove")
FIELDNAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,60}$")
RESTRICTED = ("name", "parent", "creation", "modified", "modified_by", "parentfield", "parenttype", "file_list",
	"flags", "docstatus", "doctype", "owner", "idx")
DATA_OPTIONS = ("", "Email", "Name", "Phone", "URL", "Barcode", "IBAN")
MAX_FIELDS = 10


def _snake(label):
	text = re.sub(r"[^A-Za-z0-9]+", "_", cstr(label)).strip("_").lower()
	return text if text and re.match(r"^[a-z]", text) else None


class CustomFieldKind:
	name = "custom_field"
	setting = "allow_customize_fields"
	required_roles = ("System Manager",)

	# -- normalize --------------------------------------------------------------
	def normalize(self, args, user):
		args = dict(args or {})
		_base.reject_unknown(args, TOP_KEYS, "customize.request_field")
		_base.require(args, ("doctype", "operation", "fields"), "customize.request_field")
		doctype, operation = cstr(args["doctype"]).strip(), args["operation"]
		if operation not in OPERATIONS:
			raise CustomizationError(_("operation must be one of: {0}").format(", ".join(OPERATIONS)))
		fields = args["fields"]
		if not isinstance(fields, list) or not fields:
			raise CustomizationError(_("fields must be a non-empty list"))
		if len(fields) > MAX_FIELDS:
			raise CustomizationError(_("At most {0} fields per request").format(MAX_FIELDS))
		out = []
		for i, raw in enumerate(fields, 1):
			if not isinstance(raw, dict):
				raise CustomizationError(_("fields[{0}] must be an object").format(i))
			_base.reject_unknown(raw, FIELD_KEYS, f"fields[{i}]")
			field = {}
			for key, value in raw.items():
				if value is None:
					continue
				field[key] = cint(value) if key in INT_KEYS else cstr(value)
			field["fieldname"] = self._fieldname(doctype, operation, field, i)
			if operation == "add":
				fieldtype = field.get("fieldtype") or "Data"
				if fieldtype not in FIELDTYPES:
					raise CustomizationError(_("fields[{0}].fieldtype {1} is not supported").format(i, fieldtype),
						hints=[", ".join(FIELDTYPES)])
				field["fieldtype"] = fieldtype
				if not field.get("label") and fieldtype not in LAYOUT_TYPES:
					raise CustomizationError(_("fields[{0}] needs a label").format(i))
			elif operation == "update" and field.get("fieldtype") and field["fieldtype"] not in FIELDTYPES:
				raise CustomizationError(_("fields[{0}].fieldtype {1} is not supported").format(i, field["fieldtype"]))
			elif operation == "remove":
				field = {"fieldname": field["fieldname"]}
			out.append(field)
		names = [f["fieldname"] for f in out]
		if len(set(names)) != len(names):
			raise CustomizationError(_("The same fieldname appears twice in the request"))
		spec = {"doctype": doctype, "operation": operation, "fields": out}
		if args.get("reason") or args.get("purpose"):
			spec["reason"] = cstr(args.get("reason") or args.get("purpose"))[:300]
		return spec

	def _fieldname(self, doctype, operation, field, i):
		raw = cstr(field.get("fieldname")).strip()
		if operation == "add":
			if not raw:
				raw = _snake(field.get("label")) or ""
				if not raw:
					if field.get("fieldtype") in LAYOUT_TYPES:
						raw = frappe.scrub(field["fieldtype"]) + "_" + frappe.generate_hash(length=5).lower()
					else:
						raise CustomizationError(
							_("fields[{0}]: the label is not English, so send an English snake_case fieldname").format(i),
							hints=[_("Example: {\"fieldname\": \"customer_rank\", \"label\": \"ترتيب العميل\"}")])
			name = raw.lower()
			if not name.startswith("custom_"):
				name = "custom_" + name
			if not FIELDNAME_RE.match(name):
				raise CustomizationError(
					_("fields[{0}].fieldname {1} must be English snake_case (a-z, 0-9, _)").format(i, raw))
			return name
		if not raw:
			raise CustomizationError(_("fields[{0}].fieldname is required for {1}").format(i, operation))
		for candidate in (raw, "custom_" + raw):
			if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": candidate}):
				return candidate
		raise CustomizationError(
			_("{0} has no custom field {1}").format(doctype, raw),
			hints=[_("Standard fields change through customize.request_properties; see customize.inspect")])

	# -- snapshot -----------------------------------------------------------------
	def snapshot(self, spec):
		doctype = spec["doctype"]
		out = {}
		for field in spec["fields"]:
			row = frappe.db.get_value("Custom Field", {"dt": doctype, "fieldname": field["fieldname"]},
				["name", "owner", *FIELD_KEYS], as_dict=True)
			out[field["fieldname"]] = self._clean(row) if row else None
		return {"doctype": doctype, "fields": out}

	@staticmethod
	def _clean(row):
		out = {}
		for key in ("name", "owner", *FIELD_KEYS):
			value = row.get(key)
			out[key] = cint(value) if key in INT_KEYS else cstr(value)
		return out

	# -- check --------------------------------------------------------------------
	def check(self, spec, user):
		doctype, operation = spec["doctype"], spec["operation"]
		checks = _base.role_checks(user, self.required_roles, _("Frappe requires it to add custom fields"))
		reason = _base.customizable_reason(doctype)
		if reason:
			checks.append(Check("field.doctype", "critical", reason))
			return _base.build_report(checks, risk="high")
		meta = frappe.get_meta(doctype, cached=False)
		current = self.snapshot(spec)["fields"]
		preview = {"doctype": doctype, "operation": operation, "fields": spec["fields"]}
		impact = []
		count = frappe.db.count(doctype)

		if operation == "add":
			checks += self._check_add(spec, meta, current)
			preview["layout"] = self._layout(meta, spec["fields"])
			value_fields = [f for f in spec["fields"] if f["fieldtype"] not in NO_VALUE_TYPES]
			if value_fields:
				impact.append(_("Adds {0} column(s) to the {1} table (a schema change that cannot be rolled back in a transaction)").format(len(value_fields), doctype))
			impact.append(_("Shown on every {0} form").format(doctype))
		elif operation == "update":
			checks += self._check_update(spec, meta, current)
			preview["diff"] = [
				{"field": f["fieldname"], "label": key, "before": cstr((current.get(f["fieldname"]) or {}).get(key)), "after": cstr(value)}
				for f in spec["fields"] for key, value in f.items()
				if key != "fieldname" and cstr((current.get(f["fieldname"]) or {}).get(key)) != cstr(value)
			]
			impact.append(_("Changes how {0} forms show these fields").format(doctype))
		else:
			checks += self._check_remove(spec, meta, current, user)
			impact.append(_("Removes the field(s) from every {0} form; the data stays in an orphan column").format(doctype))

		for f in spec["fields"]:
			if f.get("reqd") and operation != "remove" and count:
				checks.append(Check("field.reqd_existing", "warning",
					_("{0} existing {1} records will ask for {2} the next time they are saved").format(count, doctype, f["fieldname"])))
			if cint(f.get("permlevel")) > 0:
				roles = sorted({p.role for p in meta.permissions if p.permlevel == cint(f["permlevel"]) and p.read})
				if roles:
					checks.append(Check("field.permlevel", "info", _("Level {0} is visible to: {1}").format(f["permlevel"], ", ".join(roles))))
				else:
					checks.append(Check("field.permlevel", "warning", _("No role has level {0} on {1}: nobody will see {2}").format(f["permlevel"], doctype, f["fieldname"])))

		if operation != "remove":
			checks += self._frappe_validate(spec, doctype)
		if not any(c.level == "critical" for c in checks):
			checks.insert(0, Check("field.valid", "ok", _("Frappe accepts the field definition(s)")))
		return _base.build_report(checks, impact=impact, preview=preview, risk="medium" if operation != "update" else "low")

	def _check_add(self, spec, meta, current):
		from frappe.model.base_document import RESERVED_KEYWORDS

		checks = []
		existing = {df.fieldname for df in meta.fields}
		new_names = []
		try:
			instance = frappe.new_doc(spec["doctype"])
		except Exception:
			instance = None
		for f in spec["fields"]:
			name = f["fieldname"]
			if name in existing or current.get(name):
				checks.append(Check("field.name_conflict", "critical", _("{0} already has a field {1}").format(spec["doctype"], name)))
			if name in RESERVED_KEYWORDS or name in RESTRICTED:
				checks.append(Check("field.name_reserved", "critical", _("{0} is a reserved name").format(name)))
			if instance is not None and hasattr(type(instance), name):
				checks.append(Check("field.name_reserved", "critical", _("{0} clashes with a method or property of {1}").format(name, spec["doctype"])))
			after = f.get("insert_after")
			if after and after not in existing and after not in new_names:
				checks.append(Check("field.insert_after", "critical", _("insert_after: {0} has no field {1}").format(spec["doctype"], after)))
			checks += self._check_options(spec["doctype"], meta, f, new_names)
			checks += self._check_fetch(meta, f)
			checks += self._check_depends(meta, f, new_names)
			new_names.append(name)
		return checks

	def _check_update(self, spec, meta, current):
		from frappe.custom.doctype.customize_form.customize_form import CustomizeForm

		checks = []
		for f in spec["fields"]:
			old = current.get(f["fieldname"]) or {}
			if f.get("fieldtype") and f["fieldtype"] != old.get("fieldtype") and \
				not CustomizeForm.allow_fieldtype_change(old.get("fieldtype"), f["fieldtype"]):
				checks.append(Check("field.fieldtype_change", "critical",
					_("{0}: the type cannot change from {1} to {2}").format(f["fieldname"], old.get("fieldtype"), f["fieldtype"])))
			after = f.get("insert_after")
			if after and not meta.get_field(after):
				checks.append(Check("field.insert_after", "critical", _("insert_after: {0} has no field {1}").format(spec["doctype"], after)))
			merged = {**old, **f}
			if "options" in f or "fieldtype" in f:
				checks += self._check_options(spec["doctype"], meta, merged, [])
			if "fetch_from" in f:
				checks += self._check_fetch(meta, merged)
			checks += self._check_depends(meta, f, [])
			if f.get("unique") and frappe.db.has_column(spec["doctype"], f["fieldname"]):
				dupes = frappe.db.sql(
					f"select count(*) from (select `{f['fieldname']}` from `tab{spec['doctype']}` where ifnull(`{f['fieldname']}`, '') != '' "
					f"group by `{f['fieldname']}` having count(*) > 1) d")[0][0]
				if dupes:
					checks.append(Check("field.unique_existing", "warning", _("{0} values of {1} are already repeated; making it unique will fail").format(dupes, f["fieldname"])))
		return checks

	def _check_remove(self, spec, meta, current, user):
		checks = []
		for f in spec["fields"]:
			row = current.get(f["fieldname"]) or {}
			if row.get("owner") == "Administrator" and user != "Administrator":
				checks.append(Check("field.admin_owned", "critical",
					_("{0} was created by Administrator and only Administrator can remove it").format(f["fieldname"])))
			if row.get("fieldtype") not in NO_VALUE_TYPES and frappe.db.has_column(spec["doctype"], f["fieldname"]):
				filled = frappe.db.sql(
					f"select count(*) from `tab{spec['doctype']}` where `{f['fieldname']}` is not null and `{f['fieldname']}` != ''")[0][0] \
					if row.get("fieldtype") not in ("Int", "Float", "Currency", "Percent", "Check") else \
					frappe.db.sql(f"select count(*) from `tab{spec['doctype']}` where ifnull(`{f['fieldname']}`, 0) != 0")[0][0]
				if filled:
					checks.append(Check("field.has_data", "warning",
						_("{0} records have a value in {1}; the column stays orphaned in the database").format(filled, f["fieldname"])))
			for df in meta.fields:
				refs = " ".join(cstr(df.get(k)) for k in ("depends_on", "mandatory_depends_on", "read_only_depends_on", "fetch_from"))
				if df.fieldname != f["fieldname"] and re.search(rf"\b{re.escape(f['fieldname'])}\b", refs):
					checks.append(Check("field.referenced", "warning", _("{0} refers to {1}").format(df.fieldname, f["fieldname"])))
		return checks

	def _check_options(self, doctype, meta, f, new_names):
		fieldtype, options, name = f.get("fieldtype"), cstr(f.get("options")).strip(), f.get("fieldname")
		out = []
		if fieldtype in ("Link", "Table", "Table MultiSelect"):
			if not options or not frappe.db.exists("DocType", options):
				out.append(Check("field.options", "critical", _("{0}: options must name an existing DocType (got '{1}')").format(name, options)))
				return out
			target = frappe.get_meta(options)
			if fieldtype == "Table" and not target.istable:
				out.append(Check("field.options", "critical", _("{0}: {1} is not a child table").format(name, options)))
			if fieldtype == "Link" and target.istable:
				out.append(Check("field.options", "critical", _("{0}: a Link cannot point to the child table {1}").format(name, options)))
			if fieldtype == "Table MultiSelect" and (not target.istable or not any(df.fieldtype == "Link" for df in target.fields)):
				out.append(Check("field.options", "critical", _("{0}: Table MultiSelect needs a child table with a Link field").format(name)))
		elif fieldtype == "Select":
			if not options:
				out.append(Check("field.options", "critical", _("{0}: a Select needs options, one per line").format(name)))
			elif "," in options and "\n" not in options:
				out.append(Check("field.options", "warning", _("{0}: Select options go one per line, not comma-separated").format(name)))
		elif fieldtype == "Dynamic Link":
			link = meta.get_field(options)
			if not link or link.fieldtype != "Link" or link.options != "DocType":
				if options not in new_names:
					out.append(Check("field.options", "critical", _("{0}: options must be a Link-to-DocType field of {1}").format(name, doctype)))
		elif fieldtype == "Currency" and options:
			if not meta.get_field(options) and options not in new_names and ":" not in options:
				out.append(Check("field.options", "critical", _("{0}: options must be the currency field").format(name)))
		elif fieldtype == "Data" and options not in DATA_OPTIONS:
			out.append(Check("field.options", "critical", _("{0}: Data options must be one of Email, Phone, URL, Name").format(name)))
		return out

	def _check_fetch(self, meta, f):
		fetch = cstr(f.get("fetch_from")).strip()
		if not fetch:
			return []
		parts = fetch.split(".")
		if len(parts) != 2:
			return [Check("field.fetch_from", "critical", _("{0}: fetch_from must be link_field.source_field").format(f.get("fieldname")))]
		link = meta.get_field(parts[0])
		if not link or link.fieldtype != "Link":
			return [Check("field.fetch_from", "critical", _("{0}: {1} is not a Link field").format(f.get("fieldname"), parts[0]))]
		source = frappe.get_meta(link.options)
		if not source.get_field(parts[1]) and parts[1] not in ("name", "owner"):
			return [Check("field.fetch_from", "critical", _("{0}: {1} has no field {2}").format(f.get("fieldname"), link.options, parts[1]))]
		return []

	def _check_depends(self, meta, f, new_names):
		out = []
		for key in ("depends_on", "mandatory_depends_on", "read_only_depends_on"):
			expr = cstr(f.get(key)).strip()
			if not expr:
				continue
			if expr.startswith("eval:"):
				code = expr[5:]
				code = code.replace("===", "==").replace("!==", "!=").replace("&&", " and ").replace("||", " or ")
				code = re.sub(r"!(?!=)", " not ", code)
				code = re.sub(r"\b(doc|parent)\.", "", code)
				try:
					ast.parse(code.strip(), mode="eval")
				except SyntaxError:
					out.append(Check("field.depends_on", "warning", _("{0}.{1}: the expression looks invalid: {2}").format(f.get("fieldname"), key, expr)))
			elif not meta.get_field(expr) and expr not in new_names:
				out.append(Check("field.depends_on", "warning", _("{0}.{1}: there is no field {2}").format(f.get("fieldname"), key, expr)))
		return out

	def _frappe_validate(self, spec, doctype):
		"""Frappe's own validate_fields on the meta with the change applied in memory."""
		from frappe.core.doctype.doctype.doctype import validate_fields
		from frappe.model.meta import Meta

		meta = Meta(doctype)  # a private copy: get_meta(cached=False) would cache the mutated object
		for f in spec["fields"]:
			values = {k: v for k, v in f.items() if k != "insert_after"}
			if spec["operation"] == "add":
				row = meta.append("fields", {**values, "is_custom_field": 1})
				row.parent, row.parenttype, row.parentfield = doctype, "DocType", "fields"
				row.set("__islocal", 1)
			else:
				row = next((df for df in meta.fields if df.fieldname == f["fieldname"]), None)
				if row:
					row.update(values)
		collect = _base.capture_messages()
		out = []
		try:
			validate_fields(meta)
		except Exception as e:
			messages = collect()
			text = frappe.utils.strip_html(str(e)) or "; ".join(messages)
			out.append(Check("field.frappe", "critical", _("Frappe rejects it: {0}").format(text)))
			return out
		for message in collect():
			out.append(Check("field.frappe", "warning", message))
		return out

	@staticmethod
	def _layout(meta, fields):
		"""Where each new field lands: section and neighbours."""
		order = [df.fieldname for df in meta.fields]
		labels = {df.fieldname: (df.label or df.fieldname, df.fieldtype) for df in meta.fields}
		out = []
		for f in fields:
			after = f.get("insert_after") or (order[-1] if order else None)
			pos = order.index(after) + 1 if after in order else len(order)
			order.insert(pos, f["fieldname"])
			labels[f["fieldname"]] = (f.get("label") or f["fieldname"], f.get("fieldtype"))
			section = next((labels[n][0] for n in reversed(order[:pos]) if labels[n][1] in ("Section Break", "Tab Break")), "")
			out.append({
				"fieldname": f["fieldname"], "label": f.get("label"), "section": section,
				"after": order[pos - 1] if pos > 0 else None,
				"before": order[pos + 1] if pos + 1 < len(order) else None,
			})
		return out

	# -- execute / verify -----------------------------------------------------------
	def execute(self, spec, user, approval=None):
		doctype, operation = spec["doctype"], spec["operation"]
		before = self.snapshot(spec)["fields"]
		targets = []
		with _base.as_user(user):
			if operation == "add":
				created = []
				last = None
				meta_fields = [df.fieldname for df in frappe.get_meta(doctype).fields]
				try:
					for f in spec["fields"]:
						values = dict(f)
						values.setdefault("insert_after", last or (meta_fields[-1] if meta_fields else None))
						doc = frappe.get_doc({"doctype": "Custom Field", "dt": doctype, **values})
						doc.insert()
						created.append(doc.name)
						last = doc.fieldname
				except Exception as e:
					for name in reversed(created):
						try:
							frappe.delete_doc("Custom Field", name, force=True)
						except Exception:
							frappe.log_error(title="ai_agent customize: compensation failed")
					err = frappe.ValidationError(_("Adding the fields failed at {0}: {1}. Removed the ones created before it: {2}").format(
						f.get("fieldname"), frappe.utils.strip_html(str(e)), ", ".join(created) or "-"))
					err.created_before_compensation = created
					raise err from e
				targets = [{"doctype": "Custom Field", "name": n} for n in created]
				undo = {"doctype": doctype, "operation": "remove", "fields": [{"fieldname": f["fieldname"]} for f in spec["fields"]]}
			elif operation == "update":
				undo_fields = []
				for f in spec["fields"]:
					old = before.get(f["fieldname"]) or {}
					doc = frappe.get_doc("Custom Field", old.get("name") or f"{doctype}-{f['fieldname']}")
					doc.update({k: v for k, v in f.items() if k != "fieldname"})
					doc.save()
					targets.append({"doctype": "Custom Field", "name": doc.name})
					undo_fields.append({"fieldname": f["fieldname"], **{k: old.get(k) for k in f if k != "fieldname"}})
				undo = {"doctype": doctype, "operation": "update", "fields": undo_fields}
			else:
				for f in spec["fields"]:
					old = before.get(f["fieldname"]) or {}
					frappe.delete_doc("Custom Field", old.get("name") or f"{doctype}-{f['fieldname']}")
					targets.append({"doctype": "Custom Field", "name": old.get("name")})
				undo = {"doctype": doctype, "operation": "add", "fields": [
					{k: v for k, v in (before.get(f["fieldname"]) or {}).items() if k in FIELD_KEYS and v not in ("", None) and not (k in INT_KEYS and v == 0)}
					for f in spec["fields"]]}
		frappe.clear_cache(doctype=doctype)
		return {"targets": targets, "undo": undo}

	def verify(self, spec, result):
		doctype = spec["doctype"]
		meta = frappe.get_meta(doctype, cached=False)
		checks = []
		for f in spec["fields"]:
			df = meta.get_field(f["fieldname"])
			if spec["operation"] == "remove":
				if df:
					checks.append(Check("verify.field", "critical", _("{0} is still on {1}").format(f["fieldname"], doctype)))
				continue
			if not df:
				checks.append(Check("verify.field", "critical", _("{0} is not on {1}").format(f["fieldname"], doctype)))
				continue
			for key, value in f.items():
				if key in ("fieldname", "insert_after"):
					continue
				actual = df.get(key)
				same = cint(actual) == cint(value) if key in INT_KEYS else cstr(actual) == cstr(value)
				if not same:
					checks.append(Check("verify.property", "critical", _("{0}.{1} is {2}, expected {3}").format(f["fieldname"], key, actual, value)))
			if df.fieldtype not in NO_VALUE_TYPES and not frappe.db.has_column(doctype, f["fieldname"]):
				checks.append(Check("verify.column", "critical", _("The database column {0} is missing").format(f["fieldname"])))
		if not checks:
			checks.append(Check("verify.field", "ok", _("The form of {0} shows the change").format(doctype)))
		return checks

	def summary(self, spec):
		names = ", ".join(f["fieldname"] for f in spec["fields"])
		verb = {"add": _("Add custom field(s)"), "update": _("Change custom field(s)"), "remove": _("Remove custom field(s)")}[spec["operation"]]
		return f"{verb} {names} — {spec['doctype']}"

	def get_source(self, name, user=None):
		doc = frappe.get_doc("Custom Field", name)
		return {"kind": self.name, "name": doc.name, "doctype": doc.dt,
			"field": {k: doc.get(k) for k in FIELD_KEYS if doc.get(k) not in (None, "")}, "owner": doc.owner}


KIND = CustomFieldKind()
