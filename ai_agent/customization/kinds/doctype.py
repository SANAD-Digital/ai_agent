"""New custom DocTypes (docs/customization/01 §3.1 v3: "DocType جديد").

Only ``custom = 1`` DocTypes, which live in the database alone: ``DocType.on_update`` exports files and makes
controllers only when ``not self.custom`` (``frappe/core/doctype/doctype/doctype.py`` on_update,
``allow_doctype_export``), so developer mode on acc.dev writes nothing to any app. ``check_developer_mode`` also
lets a custom DocType be saved without developer mode.

Operations: ``create``; ``update`` (add or change fields and form properties of a custom DocType; a standard
DocType is refused with a hint to use custom fields); ``delete`` only as the undo of a create, and only while the
DocType has no records. The undo of an update is ``restore`` (the whole previous definition). There is no disable.

The check runs Frappe's own ``DocType.validate`` (``validate_name``, ``validate_series``, ``validate_fields``,
``validate_permissions``…) on an unsaved DocType inside a savepoint that is rolled back. Field rules come from the
custom_field kind (``kinds/custom_field.py``), imported, not copied.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import cint, cstr

from ai_agent.customization.contract import Check, CustomizationError
from ai_agent.customization.kinds import _base
from ai_agent.customization.kinds import custom_field as cf
from ai_agent.customization.kinds import permissions as perm

OPERATIONS = ("create", "update", "delete", "restore")
PROPERTY_KEYS = ("istable", "is_submittable", "track_changes", "quick_entry", "title_field", "search_fields",
	"sort_field", "sort_order", "autoname", "allow_rename", "allow_import", "editable_grid", "description",
	"show_title_field_in_link")
PROPERTY_INT_KEYS = ("istable", "is_submittable", "track_changes", "quick_entry", "allow_rename", "allow_import",
	"editable_grid", "show_title_field_in_link")
UPDATE_PROPERTY_KEYS = ("is_submittable", "track_changes", "quick_entry", "title_field", "search_fields", "sort_field",
	"sort_order", "allow_rename", "allow_import", "editable_grid", "description", "show_title_field_in_link")
TOP_KEYS = ("operation", "name", "module", "fields", "permissions", "definition", "reason", "purpose") + PROPERTY_KEYS
FIELD_KEYS = tuple(k for k in cf.FIELD_KEYS if k != "insert_after") + ("collapsible", "search_index")
INT_FIELD_KEYS = cf.INT_KEYS + ("collapsible", "search_index")
PERM_KEYS = ("role", "rights", "permlevel", "if_owner")
MAX_FIELDS = 60
# doctype.py:45 START_WITH_LETTERS_PATTERN and validate_name (max 61 = 64 - len("tab"))
NAME_RE = re.compile(r"^(?![\W])[^\d_\s][\w -]+$", flags=re.ASCII)
MAX_NAME = 61
# Desk routes a DocType slug would shadow (/app/<slug>), besides Page / Workspace / DocType names.
RESERVED_NAMES = ("app", "list", "form", "report", "tree", "view", "dashboard", "kanban", "calendar", "gantt",
	"image", "inbox", "map", "print", "setup wizard", "user", "role", "desk", "home", "login", "logout", "api",
	"assets", "files", "private", "website", "me", "update password")
NAMING = {"hash": ("hash", "Random"), "autoincrement": ("autoincrement", "Autoincrement"),
	"prompt": ("Prompt", "Set by user"), "uuid": ("UUID", "UUID")}
FULL_RIGHTS = ("select", "read", "write", "create", "delete", "report", "export", "print", "email", "share")
SUBMIT_RIGHTS = ("submit", "cancel", "amend")
LINK_FIELDTYPES = ("Link", "Table", "Table MultiSelect")


def _clean_field(raw, i):
	if not isinstance(raw, dict):
		raise CustomizationError(_("fields[{0}] must be an object").format(i))
	_base.reject_unknown(raw, FIELD_KEYS + ("insert_after",), f"fields[{i}]")
	out = {}
	for key, value in raw.items():
		if value is None:
			continue
		out[key] = cint(value) if key in INT_FIELD_KEYS else cstr(value)
	return out


def _fieldname(field, i, required=False):
	raw = cstr(field.get("fieldname")).strip().lower()
	if not raw:
		if required:
			raise CustomizationError(_("fields[{0}].fieldname is required").format(i))
		raw = cf._snake(field.get("label")) or ""
		if not raw:
			if field.get("fieldtype") in cf.LAYOUT_TYPES:
				return frappe.scrub(field["fieldtype"]) + "_" + frappe.generate_hash(length=5).lower()
			raise CustomizationError(
				_("fields[{0}]: the label is not English, so send an English snake_case fieldname").format(i),
				hints=[_("Example: {\"fieldname\": \"visit_date\", \"label\": \"تاريخ الزيارة\"}")])
	if not cf.FIELDNAME_RE.match(raw):
		raise CustomizationError(_("fields[{0}].fieldname {1} must be English snake_case (a-z, 0-9, _)").format(i, raw))
	return raw


def _naming(value):
	"""(autoname, naming_rule) the way the DocType form sets them (doctype.js set_naming_rule_description)."""
	text = cstr(value).strip()
	if not text:
		return NAMING["hash"]
	if text.lower() in NAMING:
		return NAMING[text.lower()]
	if text.startswith("field:") and len(text) > 6:
		return text, "By fieldname"
	if text.startswith("naming_series:"):
		return text, 'By "Naming Series" field'
	# doctype.py patch_old_naming_expressions: "format:" is the discouraged old style, "PRE-.YYYY.-.####" is Expression
	if text.startswith("format:") and "{" in text:
		return text, "Expression (old style)"
	if re.match(r"^[A-Za-z0-9_\-/.]*\.#+$", text):
		return text, "Expression"
	raise CustomizationError(_("autoname {0} is not a naming rule Frappe knows").format(text), hints=[
		_("hash | autoincrement | prompt | uuid | field:<fieldname> | naming_series: | VST-.YYYY.-.#####")])


def _perm_rows(rows, submittable, importable=0):
	out = []
	for i, raw in enumerate(rows or [], 1):
		if not isinstance(raw, dict):
			raise CustomizationError(_("permissions[{0}] must be an object").format(i))
		_base.reject_unknown(raw, PERM_KEYS, f"permissions[{i}]")
		_base.require(raw, ("role", "rights"), f"permissions[{i}]")
		rights = raw["rights"]
		if isinstance(rights, str):
			rights = [r.strip() for r in rights.replace(",", " ").split()]
		unknown = [r for r in rights if r not in perm.RIGHTS]
		if unknown:
			raise CustomizationError(_("permissions[{0}]: unknown right(s) {1}").format(i, ", ".join(map(cstr, unknown))),
				hints=[", ".join(perm.RIGHTS)])
		role = cstr(raw["role"]).strip()
		if not frappe.db.exists("Role", role):
			raise CustomizationError(_("permissions[{0}]: there is no role {1}").format(i, role))
		out.append(perm._row({"role": role, "permlevel": raw.get("permlevel"), "if_owner": raw.get("if_owner"),
			**{r: 1 for r in rights}}))
	if not any(r["role"] == "System Manager" and not r["permlevel"] for r in out):
		# doctype.py validate_permissions: submit/cancel/amend need is_submittable, import needs allow_import
		rights = FULL_RIGHTS + (SUBMIT_RIGHTS if submittable else ()) + (("import",) if importable else ())
		out.insert(0, perm._row({"role": "System Manager", **{r: 1 for r in rights}}))
	keys = [perm._key(r) for r in out]
	if len(set(keys)) != len(keys):
		raise CustomizationError(_("permissions: one rule per role, level and if_owner"))
	return out


def _default_module():
	custom = frappe.get_all("Module Def", filters={"custom": 1}, pluck="name")
	if len(custom) == 1:
		return custom[0]
	if len(custom) > 1:
		raise CustomizationError(_("Choose the module: this site has several custom modules"), hints=[", ".join(sorted(custom))])
	if frappe.db.exists("Module Def", "Custom"):
		return "Custom"
	raise CustomizationError(_("module is required"), hints=[_("An existing Module Def, e.g. one of your own app's modules")])


def definition(name):
	"""The stored definition of DocType ``name`` (its own rows, without Custom Fields / Property Setters)."""
	if not frappe.db.exists("DocType", name):
		return None
	doc = frappe.get_doc("DocType", name)
	props = {}
	for key in PROPERTY_KEYS + ("naming_rule",):
		value = doc.get(key)
		props[key] = cint(value) if key in PROPERTY_INT_KEYS else cstr(value)
	fields = []
	for df in doc.fields:
		row = {}
		for key in FIELD_KEYS:
			value = df.get(key)
			if key in INT_FIELD_KEYS:
				if cint(value):
					row[key] = cint(value)
			elif cstr(value) != "":
				row[key] = cstr(value)
		fields.append(row)
	return {"name": doc.name, "module": doc.module, "custom": cint(doc.custom), "properties": props,
		"fields": fields, "permissions": perm._sorted([p.as_dict() for p in doc.permissions])}


def _is_value(field):
	return field.get("fieldtype") not in cf.NO_VALUE_TYPES


class DocTypeKind:
	name = "doctype"
	setting = "allow_customize_doctypes"
	required_roles = ("System Manager",)

	# -- normalize --------------------------------------------------------------
	def normalize(self, args, user):
		args = dict(args or {})
		where = "customize.request_doctype"
		_base.reject_unknown(args, TOP_KEYS, where)
		_base.require(args, ("operation", "name"), where)
		operation = cstr(args["operation"]).strip()
		if operation not in OPERATIONS:
			raise CustomizationError(_("operation must be one of: create, update"))
		name = " ".join(cstr(args["name"]).split())
		spec = {"operation": operation, "name": name}
		purpose = args.get("purpose") or args.get("reason")
		if purpose:
			spec["purpose"] = " ".join(cstr(purpose).split())[:300]
		exists = frappe.db.exists("DocType", name)

		if operation == "create":
			return self._normalize_create(args, spec)
		if not exists:
			raise CustomizationError(_("There is no DocType {0}").format(name),
				hints=[_("Use operation create for a new DocType")])
		if not cint(frappe.db.get_value("DocType", name, "custom")):
			raise CustomizationError(_("{0} is a standard DocType: it cannot be edited as a DocType").format(name),
				hints=[_("Add fields with customize.request_field and change properties with customize.request_properties")])
		if operation == "delete":
			extra = sorted(k for k in args if k not in ("operation", "name", "reason", "purpose"))
			if extra:
				raise CustomizationError(_("delete takes only name"))
			return spec
		if operation == "restore":
			value = args.get("definition")
			if not isinstance(value, dict) or not isinstance(value.get("fields"), list):
				raise CustomizationError(_("restore needs the definition of a previous snapshot"))
			spec["definition"] = value
			return spec
		return self._normalize_update(args, spec)

	def _normalize_create(self, args, spec):
		name = spec["name"]
		if len(name) > MAX_NAME:
			raise CustomizationError(_("The DocType name is limited to {0} characters").format(MAX_NAME))
		if not NAME_RE.match(name):
			raise CustomizationError(
				_("A DocType name starts with an English letter and has only letters, digits, spaces, - and _"),
				hints=[_("Example: 'Site Visit'; the label users see can be translated later")])
		module = cstr(args.get("module")).strip() or _default_module()
		if not frappe.db.exists("Module Def", module):
			raise CustomizationError(_("There is no module {0}").format(module), hints=[_("Leave module empty to use the site's custom module")])
		fields = args.get("fields")
		if not isinstance(fields, list) or not fields:
			raise CustomizationError(_("fields must be a non-empty list"))
		if len(fields) > MAX_FIELDS:
			raise CustomizationError(_("At most {0} fields per DocType request").format(MAX_FIELDS))
		out = []
		for i, raw in enumerate(fields, 1):
			field = _clean_field(raw, i)
			if field.pop("insert_after", None):
				raise CustomizationError(_("fields[{0}]: insert_after is for update; on create the list order is the form order").format(i))
			fieldtype = field.get("fieldtype") or "Data"
			if fieldtype not in cf.FIELDTYPES:
				raise CustomizationError(_("fields[{0}].fieldtype {1} is not supported").format(i, fieldtype), hints=[", ".join(cf.FIELDTYPES)])
			field["fieldtype"] = fieldtype
			if not field.get("label") and fieldtype not in cf.LAYOUT_TYPES:
				raise CustomizationError(_("fields[{0}] needs a label").format(i))
			field["fieldname"] = _fieldname(field, i)
			out.append(field)
		names = [f["fieldname"] for f in out]
		if len(set(names)) != len(names):
			raise CustomizationError(_("The same fieldname appears twice in the request"))
		props = self._properties(args, PROPERTY_KEYS)
		props.setdefault("track_changes", 1)
		props.setdefault("allow_rename", 1)
		props["autoname"], props["naming_rule"] = _naming(props.get("autoname"))
		if props.get("istable"):
			if args.get("permissions"):
				raise CustomizationError(_("A child table has no permissions of its own: it follows its parent"),
					hints=[_("Remove permissions, or create a normal DocType (istable 0)")])
			props["autoname"], props["naming_rule"] = NAMING["hash"]
			permissions = []
		else:
			permissions = _perm_rows(args.get("permissions"), cint(props.get("is_submittable")), cint(props.get("allow_import")))
		spec.update({"module": module, "properties": props, "fields": out, "permissions": permissions})
		return spec

	def _normalize_update(self, args, spec):
		name = spec["name"]
		refused = [k for k in ("istable", "autoname", "permissions", "module") if args.get(k) not in (None, "", [])]
		if refused:
			raise CustomizationError(_("{0} cannot change through update").format(", ".join(refused)), hints=[
				_("Permissions change with customize.request_permissions; naming and child-table status are fixed after creation")])
		current = {df.fieldname for df in frappe.get_doc("DocType", name).fields}
		fields = args.get("fields") or []
		if not isinstance(fields, list):
			raise CustomizationError(_("fields must be a list"))
		if len(fields) > MAX_FIELDS:
			raise CustomizationError(_("At most {0} fields per DocType request").format(MAX_FIELDS))
		out = []
		for i, raw in enumerate(fields, 1):
			field = _clean_field(raw, i)
			if cstr(field.get("fieldname")).strip().lower() in current:
				field["fieldname"] = cstr(field["fieldname"]).strip().lower()
				if field.get("fieldtype") and field["fieldtype"] not in cf.FIELDTYPES:
					raise CustomizationError(_("fields[{0}].fieldtype {1} is not supported").format(i, field["fieldtype"]))
				field["_change"] = 1
			else:
				fieldtype = field.get("fieldtype") or "Data"
				if fieldtype not in cf.FIELDTYPES:
					raise CustomizationError(_("fields[{0}].fieldtype {1} is not supported").format(i, fieldtype), hints=[", ".join(cf.FIELDTYPES)])
				field["fieldtype"] = fieldtype
				if not field.get("label") and fieldtype not in cf.LAYOUT_TYPES:
					raise CustomizationError(_("fields[{0}] needs a label (a new field)").format(i))
				field["fieldname"] = _fieldname(field, i)
			out.append(field)
		names = [f["fieldname"] for f in out]
		if len(set(names)) != len(names):
			raise CustomizationError(_("The same fieldname appears twice in the request"))
		props = self._properties(args, UPDATE_PROPERTY_KEYS)
		if not out and not props:
			raise CustomizationError(_("update needs fields or properties to change"))
		spec.update({"properties": props, "fields": out})
		return spec

	@staticmethod
	def _properties(args, allowed):
		props = {}
		for key in allowed:
			value = args.get(key)
			if value is None:
				continue
			if key == "search_fields" and isinstance(value, list):
				value = ",".join(cstr(v).strip() for v in value)
			props[key] = cint(value) if key in PROPERTY_INT_KEYS else cstr(value).strip()
		if props.get("sort_order") and props["sort_order"] not in ("ASC", "DESC"):
			raise CustomizationError(_("sort_order must be ASC or DESC"))
		return props

	# -- state ------------------------------------------------------------------
	def snapshot(self, spec):
		return {"name": spec["name"], "definition": definition(spec["name"])}

	def _doc_dict(self, spec):
		props = dict(spec.get("properties") or {})
		return {"doctype": "DocType", "name": spec["name"], "module": spec["module"], "custom": 1, **props,
			"fields": [{k: v for k, v in f.items() if k in FIELD_KEYS} for f in spec["fields"]],
			"permissions": [dict(r) for r in spec.get("permissions") or []]}

	def _apply_update(self, doc, spec):
		"""The change of an update / restore applied to a loaded DocType document (in memory)."""
		if spec["operation"] == "restore":
			value = spec["definition"]
			doc.update({k: v for k, v in (value.get("properties") or {}).items() if k in PROPERTY_KEYS + ("naming_rule",)})
			if value.get("module"):
				doc.module = value["module"]
			rows = {df.fieldname: df for df in doc.fields}
			fresh = []
			for field in value["fields"]:
				row = rows.get(field.get("fieldname"))
				values = {k: field.get(k, 0 if k in INT_FIELD_KEYS else None) for k in FIELD_KEYS}
				if row is None:
					fresh.append(values)
				else:
					row.update(values)
					fresh.append(row)
			doc.set("fields", [])
			for item in fresh:
				doc.append("fields", item)
			if value.get("permissions") is not None and not doc.istable:
				doc.set("permissions", [])
				for row in value["permissions"]:
					doc.append("permissions", dict(row))
			return doc
		doc.update(spec.get("properties") or {})
		order = list(doc.fields)
		for field in spec["fields"]:
			values = {k: v for k, v in field.items() if k in FIELD_KEYS}
			existing = next((df for df in order if df.fieldname == field["fieldname"]), None)
			if field.get("_change") and existing is not None:
				existing.update(values)
				continue
			row = doc.append("fields", values)
			after = field.get("insert_after")
			index = next((i for i, df in enumerate(order) if df.fieldname == after), None) if after else None
			order.insert(len(order) if index is None else index + 1, row)
		doc.set("fields", order)
		for i, df in enumerate(doc.fields, 1):
			df.idx = i
		return doc

	def build(self, spec):
		"""The DocType document the change produces (unsaved)."""
		if spec["operation"] == "create":
			doc = frappe.get_doc(self._doc_dict(spec))
			doc.set("__islocal", 1)  # is_new(): validate skips before_update and the child-table DDL
			return doc
		doc = frappe.get_doc("DocType", spec["name"])
		doc.load_doc_before_save()
		return self._apply_update(doc, spec)

	# -- check --------------------------------------------------------------------
	def check(self, spec, user):
		name, operation = spec["name"], spec["operation"]
		checks = _base.role_checks(user, self.required_roles, _("Frappe lets only System Managers create DocTypes"))
		preview = {"name": name, "table": f"tab{name}", "operation": operation}
		impact = []
		if operation == "create":
			checks += self._check_name(name)
		else:
			reason = _base.customizable_reason(name, allow_custom=True) if not name.startswith("AI ") else \
				_("{0} belongs to the assistant and is protected").format(name)
			if reason:
				checks.append(Check("dt.protected", "critical", reason))
				return _base.build_report(checks, risk="high")
			if not cint(frappe.db.get_value("DocType", name, "custom")):
				checks.append(Check("dt.not_custom", "critical", _("{0} is a standard DocType; use customize.request_field").format(name)))
				return _base.build_report(checks, risk="high")
		count = frappe.db.count(name) if operation != "create" and frappe.db.table_exists(name) else 0

		if operation == "delete":
			checks += self._check_delete(name, count)
			impact.append(_("Deletes the DocType {0} and its empty table {1}").format(name, f"tab{name}"))
			if not any(c.level == "critical" for c in checks):
				checks.insert(0, Check("dt.valid", "ok", _("{0} has no records and nothing depends on it").format(name)))
			preview["definition"] = definition(name)
			return _base.build_report(checks, impact=impact, preview=preview, risk="high")

		doc = self.build(spec)
		from frappe.model.meta import Meta

		try:
			meta = Meta(doc)
		except Exception as e:
			checks.append(Check("dt.frappe", "critical", _("Frappe cannot read the definition: {0}").format(frappe.utils.strip_html(cstr(e)))))
			return _base.build_report(checks, preview=preview, risk="high")
		new_fields = self._new_fields(spec)
		checks += self._check_fields(spec, meta, new_fields)
		checks += self._check_properties(spec, doc, meta)
		if operation == "update":
			checks += self._check_update(spec, count)
		elif operation == "restore":
			checks += self._check_restore(spec, count)
		if operation == "create" and not doc.istable:
			checks += self._check_permissions(spec)
		checks += self._frappe_validate(spec, user)
		if frappe.conf.developer_mode:
			checks.append(Check("dt.not_exported", "info",
				_("custom = 1: kept in the database only; developer mode does not export it to any app's files")))

		final = [{k: df.get(k) for k in ("fieldname", "label", "fieldtype", "options", "reqd", "hidden", "in_list_view")}
			for df in doc.fields]
		preview.update({"module": doc.module, "custom": 1, "istable": cint(doc.istable), "is_submittable": cint(doc.is_submittable),
			"autoname": doc.autoname, "naming_rule": doc.naming_rule, "title_field": doc.title_field,
			"fields": final, "layout": self.layout(final),
			"permissions": [perm._row(p.as_dict()) for p in doc.permissions]})
		if operation in ("update", "restore"):
			preview["diff"] = self._diff(definition(name), doc)
		value_fields = [f for f in new_fields if _is_value(f)]
		if operation == "create":
			impact.append(_("Creates the database table {0} (a schema change that cannot be rolled back in a transaction)").format(f"tab{name}"))
			if doc.istable:
				impact.append(_("A child table: it shows only inside a Table field of another DocType"))
			else:
				roles = sorted({r["role"] for r in spec["permissions"] if r["read"]})
				impact.append(_("Appears in the Desk at /app/{0} for: {1}").format(frappe.scrub(name).replace("_", "-"), ", ".join(roles)))
		else:
			if value_fields:
				impact.append(_("Adds {0} column(s) to {1}").format(len(value_fields), f"tab{name}"))
			impact.append(_("Changes the {0} form for everyone who uses it ({1} records)").format(name, count))
		if not any(c.level == "critical" for c in checks):
			checks.insert(0, Check("dt.valid", "ok", _("Frappe accepts the DocType definition")))
		return _base.build_report(checks, impact=impact, preview=preview, risk="medium")

	def _new_fields(self, spec):
		if spec["operation"] == "create":
			return list(spec["fields"])
		if spec["operation"] == "restore":
			current = {df.fieldname for df in frappe.get_doc("DocType", spec["name"]).fields}
			return [f for f in spec["definition"]["fields"] if f.get("fieldname") not in current]
		return [f for f in spec["fields"] if not f.get("_change")]

	def _check_name(self, name):
		from frappe.model import core_doctypes_list
		from frappe.desk.utils import slug

		out = []
		if frappe.db.exists("DocType", name):
			out.append(Check("dt.name_taken", "critical", _("A DocType named {0} already exists").format(name)))
		elif frappe.db.table_exists(name):
			out.append(Check("dt.table_exists", "critical",
				_("A table {0} is left over from a deleted DocType; choose another name").format(f"tab{name}")))
		if name in core_doctypes_list or name.lower() in RESERVED_NAMES or name.startswith("AI ") or \
			name in _protected_names():
			out.append(Check("dt.name_reserved", "critical", _("{0} is a reserved or protected name").format(name)))
		taken = [dt for dt in ("Page", "Workspace", "DocType")
			if any(slug(n) == slug(name) for n in frappe.get_all(dt, pluck="name") if n != name)]
		if taken:
			# frappe/desk/utils.py validate_route_conflict: /app/<slug> must be free
			out.append(Check("dt.route_conflict", "critical",
				_("/app/{0} is already used by a {1}; choose another name").format(slug(name), taken[0])))
		if name.endswith("Settings"):
			out.append(Check("dt.name_settings", "warning",
				_("Names ending in Settings are treated as system configuration: the assistant cannot change it later")))
		return out

	def _check_fields(self, spec, meta, new_fields):
		from frappe.model import child_table_fields, default_fields, optional_fields
		from frappe.model.base_document import RESERVED_KEYWORDS
		from frappe.model.document import Document

		out = []
		reserved = set(RESERVED_KEYWORDS) | set(default_fields) | set(child_table_fields) | set(optional_fields) | set(cf.RESTRICTED)
		new_names = []
		for f in new_fields:
			name = f.get("fieldname")
			if name in reserved:
				out.append(Check("field.name_reserved", "critical", _("{0} is a reserved name").format(name)))
			elif _is_value(f) and hasattr(Document, name):
				# doctype.py validate_field_name_conflicts: a custom DocType's controller is Document
				out.append(Check("field.name_reserved", "critical", _("{0} clashes with a method or property of a document").format(name)))
			out += cf.KIND._check_options(spec["name"], meta, f, new_names)
			out += cf.KIND._check_fetch(meta, f)
			out += cf.KIND._check_depends(meta, f, new_names)
			new_names.append(name)
		if spec["operation"] == "update":
			for f in spec["fields"]:
				if f.get("_change") and ("options" in f or "fieldtype" in f):
					merged = meta.get_field(f["fieldname"])
					if merged:
						out += cf.KIND._check_options(spec["name"], meta, merged.as_dict(), [])
		return out

	def _check_properties(self, spec, doc, meta):
		out = []
		names = {df.fieldname: df for df in doc.fields}
		if doc.istable and doc.is_submittable:
			out.append(Check("dt.child_submittable", "critical", _("A child table cannot be submittable")))
		if doc.istable and doc.quick_entry:
			out.append(Check("dt.child_quick_entry", "warning", _("quick_entry has no effect on a child table")))
		title = cstr(doc.title_field)
		if title and title not in names:
			out.append(Check("dt.title_field", "critical", _("title_field: there is no field {0}").format(title)))
		elif title and names[title].fieldtype in cf.NO_VALUE_TYPES:
			out.append(Check("dt.title_field", "critical", _("title_field: {0} holds no value").format(title)))
		for fieldname in [s.strip() for s in cstr(doc.search_fields).split(",") if s.strip()]:
			df = names.get(fieldname)
			if not df or df.fieldtype in cf.NO_VALUE_TYPES:
				out.append(Check("dt.search_fields", "critical", _("search_fields: {0} is not a value field of {1}").format(fieldname, spec["name"])))
		sort = cstr(doc.sort_field)
		if sort and sort not in names and sort not in ("modified", "creation", "name", "idx"):
			out.append(Check("dt.sort_field", "critical", _("sort_field: there is no field {0}").format(sort)))
		autoname = cstr(doc.autoname)
		if spec["operation"] == "create":
			if autoname.startswith("field:"):
				target = names.get(autoname[6:])
				if not target or target.fieldtype in cf.NO_VALUE_TYPES:
					out.append(Check("dt.naming", "critical", _("autoname: there is no value field {0}").format(autoname[6:])))
				else:
					out.append(Check("dt.naming", "info", _("{0} becomes unique: it names each record").format(autoname[6:])))
			elif autoname.startswith("naming_series:"):
				series = names.get("naming_series")
				if not series or series.fieldtype != "Select" or not cstr(series.options).strip():
					out.append(Check("dt.naming", "critical",
						_("autoname naming_series: needs a Select field naming_series with the series as options (e.g. VST-.YYYY.-)")))
			elif autoname == "Prompt":
				out.append(Check("dt.naming", "warning", _("Users must type the name of every new record")))
		return out

	def _check_permissions(self, spec):
		out = []
		for row in spec["permissions"]:
			if not any(row[r] for r in perm.RIGHTS):
				continue
			if row["role"] in (perm.GUEST_ROLE, perm.ALL_USER_ROLE):
				out.append(Check("perm.role_automatic", "critical",
					_("Giving rights to {0} opens {1} to every user and website visitor").format(row["role"], spec["name"])))
			elif row["role"] == perm.SYSTEM_USER_ROLE and any(row[r] for r in perm.WRITE_RIGHTS):
				out.append(Check("perm.role_desk_user", "critical",
					_("{0} would let every desk user change {1}; give it to a specific role").format(row["role"], spec["name"])))
			elif row["role"] == perm.ADMIN_ROLE:
				out.append(Check("perm.role_admin", "info", _("Administrator has every right anyway")))
			risky = [r for r in perm.RISKY_RIGHTS if row[r]]
			if risky and row["role"] != "System Manager":
				out.append(Check("perm.risky_right", "warning",
					_("{0} will be able to {1} {2} records").format(row["role"], ", ".join(risky), spec["name"])))
		return out

	def _check_update(self, spec, count):
		from frappe.custom.doctype.customize_form.customize_form import CustomizeForm

		out = []
		current = {df.fieldname: df for df in frappe.get_doc("DocType", spec["name"]).fields}
		for f in spec["fields"]:
			old = current.get(f["fieldname"])
			if f.get("_change") and old is not None and f.get("fieldtype") and f["fieldtype"] != old.fieldtype \
				and count and not CustomizeForm.allow_fieldtype_change(old.fieldtype, f["fieldtype"]):
				out.append(Check("field.fieldtype_change", "critical",
					_("{0}: {1} records hold data, so the type cannot change from {2} to {3}").format(f["fieldname"], count, old.fieldtype, f["fieldtype"])))
			if f.get("reqd") and count and not (old is not None and old.reqd):
				out.append(Check("field.reqd_existing", "warning",
					_("{0} existing {1} records will ask for {2} the next time they are saved").format(count, spec["name"], f["fieldname"])))
			if f.get("unique") and old is not None and count and frappe.db.has_column(spec["name"], f["fieldname"]):
				dupes = frappe.db.sql(
					f"select count(*) from (select `{f['fieldname']}` from `tab{spec['name']}` where ifnull(`{f['fieldname']}`, '') != '' "
					f"group by `{f['fieldname']}` having count(*) > 1) d")[0][0]
				if dupes:
					out.append(Check("field.unique_existing", "warning", _("{0} values of {1} are already repeated; making it unique will fail").format(dupes, f["fieldname"])))
		if (spec.get("properties") or {}).get("is_submittable") is not None and count:
			out.append(Check("dt.submittable_change", "warning", _("{0} records exist; changing is_submittable changes how they can be edited").format(count)))
		return out

	def _check_restore(self, spec, count):
		out = []
		keep = {f.get("fieldname") for f in spec["definition"]["fields"]}
		for df in frappe.get_doc("DocType", spec["name"]).fields:
			if df.fieldname not in keep and df.fieldtype not in cf.NO_VALUE_TYPES and count:
				out.append(Check("field.has_data", "warning",
					_("{0} leaves the form; its data stays in an unused column").format(df.fieldname)))
		return out

	def _check_delete(self, name, count):
		out = []
		if count:
			out.append(Check("dt.has_records", "critical",
				_("{0} has {1} record(s); it is deleted only while it is empty").format(name, count)))
		links = frappe.get_all("DocField", filters={"options": name, "fieldtype": ("in", LINK_FIELDTYPES), "parent": ("!=", name)},
			fields=["parent", "fieldname", "fieldtype"])
		links += frappe.get_all("Custom Field", filters={"options": name, "fieldtype": ("in", LINK_FIELDTYPES)},
			fields=["dt as parent", "fieldname", "fieldtype"])
		for link in links:
			level = "critical" if link.fieldtype != "Link" else "warning"
			out.append(Check("dt.referenced", level, _("{0}.{1} ({2}) points to {3}").format(link.parent, link.fieldname, link.fieldtype, name)))
		return out

	def _frappe_validate(self, spec, user):
		"""``DocType.validate`` on the unsaved result, in a savepoint that is always rolled back (validate only reads,
		but a hook or toast must not leave anything behind). Nothing reaches ``on_update`` / ``updatedb``."""
		out = []
		savepoint = "ai_doctype_check"
		collect = _base.capture_messages()
		frappe.db.savepoint(savepoint)
		try:
			with _base.as_user(user):
				doc = self.build(spec)
				# validate_child_table() on a saved child table runs ``alter table … add column if not exists parent``
				# (doctype.py add_child_table_fields): DDL commits implicitly, so the dry run skips it
				doc.validate_child_table = lambda: None
				doc.validate()
		except Exception as e:
			messages = collect()
			text = frappe.utils.strip_html(cstr(e)) or "; ".join(messages)
			out.append(Check("dt.frappe", "critical", _("Frappe rejects it: {0}").format(text)))
			return out
		finally:
			frappe.db.rollback(save_point=savepoint)
		for message in collect():
			if message:
				out.append(Check("dt.frappe", "warning", message))
		return out

	@staticmethod
	def layout(fields):
		"""Tabs → sections → columns, the way the form lays the fields out."""
		tabs = [{"tab": _("Details"), "sections": []}]

		def section(label=""):
			tabs[-1]["sections"].append({"label": label, "columns": [[]]})

		for f in fields:
			fieldtype, label = f.get("fieldtype"), f.get("label") or ""
			if fieldtype == "Tab Break":
				if not any(s["columns"] != [[]] for s in tabs[-1]["sections"]) and len(tabs) == 1 and not tabs[0]["sections"]:
					tabs[0]["tab"] = label or tabs[0]["tab"]
				else:
					tabs.append({"tab": label, "sections": []})
				continue
			if fieldtype == "Section Break" or not tabs[-1]["sections"]:
				section(label if fieldtype == "Section Break" else "")
				if fieldtype == "Section Break":
					continue
			if fieldtype == "Column Break":
				tabs[-1]["sections"][-1]["columns"].append([])
				continue
			tabs[-1]["sections"][-1]["columns"][-1].append(
				{"fieldname": f.get("fieldname"), "label": label, "fieldtype": fieldtype, "reqd": cint(f.get("reqd"))})
		return tabs

	@staticmethod
	def _diff(before, doc):
		old = {f["fieldname"]: f for f in (before or {}).get("fields") or []}
		out = []
		for df in doc.fields:
			was = old.pop(df.fieldname, None)
			if was is None:
				out.append({"field": df.fieldname, "change": "added", "fieldtype": df.fieldtype, "label": df.label})
				continue
			for key in FIELD_KEYS:
				a = cint(was.get(key)) if key in INT_FIELD_KEYS else cstr(was.get(key))
				b = cint(df.get(key)) if key in INT_FIELD_KEYS else cstr(df.get(key))
				if a != b:
					out.append({"field": df.fieldname, "change": "changed", "property": key, "before": a, "after": b})
		for name in old:
			out.append({"field": name, "change": "removed"})
		props = (before or {}).get("properties") or {}
		for key in PROPERTY_KEYS:
			a = cint(props.get(key)) if key in PROPERTY_INT_KEYS else cstr(props.get(key))
			b = cint(doc.get(key)) if key in PROPERTY_INT_KEYS else cstr(doc.get(key))
			if a != b:
				out.append({"property": key, "change": "changed", "before": a, "after": b})
		return out

	# -- execute / verify -----------------------------------------------------------
	def execute(self, spec, user, approval=None):
		name, operation = spec["name"], spec["operation"]
		before = definition(name)
		with _base.as_user(user):
			if operation == "create":
				doc = self.build(spec)
				try:
					doc.insert()
				except Exception:
					# DocType.on_update runs updatedb (DDL, implicit commit): remove what got created
					self._remove(name, quiet=True)
					raise
				undo = {"operation": "delete", "name": name}
			elif operation == "delete":
				count = frappe.db.count(name) if frappe.db.table_exists(name) else 0
				if count:
					frappe.throw(_("{0} has {1} record(s); it is deleted only while it is empty").format(name, count))
				self._remove(name)
				undo = {"operation": "create", "name": name, "module": before["module"],
					"properties": {k: v for k, v in before["properties"].items()}, "fields": before["fields"],
					"permissions": before["permissions"]}
			else:
				doc = self.build(spec)
				doc.save()
				undo = {"operation": "restore", "name": name, "definition": before}
		frappe.clear_cache(doctype=name)
		return {"targets": [{"doctype": "DocType", "name": name}], "undo": undo}

	@staticmethod
	def _remove(name, quiet=False):
		"""Delete a custom DocType and drop its table when the table is empty (Frappe leaves the table behind:
		delete_doc removes the record only; ``bench trim-tables`` drops orphans the same way)."""
		try:
			if frappe.db.exists("DocType", name):
				if not cint(frappe.db.get_value("DocType", name, "custom")):
					frappe.throw(_("{0} is a standard DocType").format(name))
				frappe.delete_doc("DocType", name)
			if frappe.db.table_exists(name) and not frappe.db.sql(f"select 1 from `tab{name}` limit 1"):
				frappe.db.sql_ddl(f"drop table if exists `tab{name}`")
		except Exception:
			if not quiet:
				raise
			frappe.log_error(title="ai_agent customize: doctype compensation failed")
		frappe.clear_cache(doctype=name)

	def verify(self, spec, result):
		name, operation = spec["name"], spec["operation"]
		checks = []
		if operation == "delete":
			if frappe.db.exists("DocType", name):
				checks.append(Check("verify.doctype", "critical", _("{0} still exists").format(name)))
			elif frappe.db.table_exists(name):
				checks.append(Check("verify.table", "warning", _("The table {0} is still there").format(f"tab{name}")))
			return checks or [Check("verify.doctype", "ok", _("{0} is deleted").format(name))]
		if not frappe.db.exists("DocType", name):
			return [Check("verify.doctype", "critical", _("{0} does not exist").format(name))]
		meta = frappe.get_meta(name, cached=False)
		if not meta.custom:
			checks.append(Check("verify.custom", "critical", _("{0} is not marked custom").format(name)))
		if not frappe.db.table_exists(name):
			checks.append(Check("verify.table", "critical", _("The table {0} is missing").format(f"tab{name}")))
		expected = spec["definition"]["fields"] if operation == "restore" else spec["fields"]
		for f in expected:
			df = meta.get_field(f.get("fieldname"))
			if not df:
				checks.append(Check("verify.field", "critical", _("{0} is not on {1}").format(f.get("fieldname"), name)))
				continue
			for key in ("fieldtype", "label", "options", "reqd"):
				if key in f and cstr(df.get(key)) != cstr(f[key]):
					checks.append(Check("verify.property", "critical", _("{0}.{1} is {2}, expected {3}").format(f["fieldname"], key, df.get(key), f[key])))
			if _is_value(f) and not frappe.db.has_column(name, f["fieldname"]):
				checks.append(Check("verify.column", "critical", _("The database column {0} is missing").format(f["fieldname"])))
		if operation == "create":
			from frappe.modules import get_doc_path

			try:
				path = get_doc_path(meta.module, "DocType", name)
			except Exception:
				path = None
			import os

			if path and os.path.exists(path):
				checks.append(Check("verify.exported", "warning", _("Files were written for {0} at {1}").format(name, path)))
		if not checks:
			checks.append(Check("verify.doctype", "ok", _("{0} is in place with its fields").format(name)))
		return checks

	def summary(self, spec):
		name, operation = spec["name"], spec["operation"]
		if operation == "create":
			kind = _("child table") if (spec.get("properties") or {}).get("istable") else _("DocType")
			return _("Create {0} {1} ({2} fields)").format(kind, name, len(spec["fields"]))
		if operation == "update":
			return _("Change DocType {0}: {1}").format(name, ", ".join(f["fieldname"] for f in spec["fields"]) or ", ".join(spec["properties"]))
		if operation == "restore":
			return _("Restore the previous definition of {0}").format(name)
		return _("Delete the empty DocType {0}").format(name)

	def get_source(self, name, user=None):
		value = definition(name)
		if not value:
			raise CustomizationError(_("There is no DocType {0}").format(name))
		return {"kind": self.name, **value}


def _protected_names():
	from ai_agent.runtime.policy import CODE_AND_ACCESS_DOCTYPES, PROTECTED_DOCTYPES

	return set(CODE_AND_ACCESS_DOCTYPES) | set(PROTECTED_DOCTYPES) | set(perm.PROTECTED)


KIND = DocTypeKind()
