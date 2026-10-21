"""Document naming: a DocType's naming (autoname / naming rule / naming series) and Document Naming Rules
(docs/customization/01 §3.1 "Naming", §4.2).

Rules come from Frappe's own source:

- ``frappe/model/naming.py``: ``set_new_name`` order (autoincrement and UUID first, then amended documents, then
  enabled Document Naming Rules by priority desc, then the controller's ``autoname`` method, then the DocType's
  ``autoname`` property, then a random hash), ``set_name_from_naming_options`` (``field:``, ``naming_series:``,
  ``prompt``, ``format:``, and a dotted expression only when it contains ``#``), ``NamingSeries.validate`` (a dot,
  ``NAMING_SERIES_PATTERN``, ``.#`` before the digits), ``parse_naming_series`` (counter key = the evaluated
  prefix), ``_format_autoname`` (each braced part on its own, so its counter key is the empty prefix).
- ``frappe/core/doctype/doctype/doctype.py``: ``validate_series`` (``naming_series:`` needs the ``naming_series``
  field, ``field:`` makes the field unique, a plain expression's prefix must not be used by another DocType),
  ``validate_autoincrement_autoname`` ("Cannot change to/from autoincrement autoname in Customize Form").
- ``frappe/custom/doctype/customize_form/customize_form.py``: ``autoname`` and ``naming_rule`` are Customize Form
  DocType properties; ``save_customization`` runs ``validate_series``; Select ``options`` are not changeable there.
- ``frappe/core/doctype/document_naming_settings/document_naming_settings.py``: series options are written as
  Property Setters on ``naming_series`` (``options`` and ``default`` = the first option) after
  ``NamingSeries.validate`` and ``check_duplicate`` (a series used by another DocType is refused).
- ``frappe/core/doctype/document_naming_rule/document_naming_rule.py``: conditions name fields of the DocType;
  ``apply`` = ``parse_naming_series(prefix, doc)`` + the counter + 1 zero-padded to ``prefix_digits``.

The preview computes the next names without touching ``tabSeries`` or a rule's counter: it reads the counters
and passes its own ``number_generator`` to ``parse_naming_series``.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import cint, cstr

from ai_agent.customization.contract import Check, CustomizationError
from ai_agent.customization.kinds import _base

TOOL = "customize.request_naming"
OPERATIONS = ("set_doctype_naming", "create_rule", "update_rule", "disable_rule", "enable_rule")
RULE_OPERATIONS = OPERATIONS[1:]
TOP_KEYS = ("operation", "doctype", "autoname", "naming_series_options", "rule", "prefix", "prefix_digits",
	"priority", "conditions", "purpose", "reason")
CONDITION_KEYS = ("field", "condition", "value")
CONDITION_OPERATORS = ("=", "!=", ">", "<", ">=", "<=")  # document_naming_rule_condition.json
SPECIAL_AUTONAMES = {"prompt": "prompt", "hash": "hash", "uuid": "UUID", "autoincrement": "autoincrement"}
DATE_PARTS = ("YY", "MM", "DD", "YYYY", "JJJ", "WW", "timestamp")
FIELD_NAMING_TYPES = ("Data", "Link", "Dynamic Link", "Select", "Int", "Autocomplete", "Date", "Read Only")
MAX_SERIES = 20
SAMPLE_SIZE = 200
DIGITS_RE = re.compile(r"\{(#+)\}")


def naming_rule_for(autoname):
	"""Customize Form's naming_rule for an autoname (frappe/public/js/frappe/doctype/index.js ``autoname()``)."""
	value = cstr(autoname)
	low = value.lower()
	if not value:
		return "By script"
	if low == "prompt":
		return "Set by user"
	if low == "autoincrement":
		return "Autoincrement"
	if low.startswith("field:"):
		return "By fieldname"
	if low.startswith("naming_series:"):
		return 'By "Naming Series" field'
	if low.startswith("format:"):
		return "Expression (old style)"
	if low == "hash":
		return "Random"
	if low == "uuid":
		return "UUID"
	return "Expression"


def _peek(offset):
	"""A parse_naming_series number_generator that reads tabSeries and never writes it."""

	def generate(prefix, digits):
		current = cint(frappe.db.get_value("Series", prefix, "current", order_by="name"))
		return str(current + offset).zfill(digits)

	return generate


def _series_names(series, doc, count=3):
	from frappe.model.naming import parse_naming_series

	return [parse_naming_series(series, doc=doc, number_generator=_peek(i)) for i in range(1, count + 1)]


def _format_names(autoname, doc, count=3):
	from frappe.model.naming import BRACED_PARAMS_PATTERN, parse_naming_series

	body = autoname[autoname.find(":") + 1:]
	out = []
	for i in range(1, count + 1):
		out.append(BRACED_PARAMS_PATTERN.sub(
			lambda m: parse_naming_series([m.group()[1:-1]], doc=doc, number_generator=_peek(i)), body))
	return out


def _sample_doc(doctype):
	"""The last document (as Document Naming Settings' preview does), else a new one."""
	rows = frappe.get_all(doctype, order_by="creation desc", limit=1, pluck="name")
	return frappe.get_doc(doctype, rows[0]) if rows else frappe.new_doc(doctype)


def _controller_names_itself(doctype):
	from frappe.model.base_document import get_controller

	try:
		return hasattr(get_controller(doctype), "autoname")
	except Exception:
		return False


def _options(text):
	return [o.strip() for o in cstr(text).split("\n") if o.strip()]


def _collisions(doctype, names):
	out = []
	for i, name in enumerate(names):
		if name and frappe.db.exists(doctype, name):
			level = "critical" if i == 0 else "warning"
			out.append(Check("naming.collision", level, _("The name {0} already exists in {1}: saving that document would fail").format(name, doctype)))
	return out


class NamingKind:
	name = "naming"
	setting = "allow_customize_fields"
	required_roles = ("System Manager",)

	# -- normalize --------------------------------------------------------------
	def normalize(self, args, user):
		args = dict(args or {})
		_base.reject_unknown(args, TOP_KEYS, TOOL)
		_base.require(args, ("operation",), TOOL)
		operation = args["operation"]
		if operation not in OPERATIONS:
			raise CustomizationError(_("operation must be one of: {0}").format(", ".join(OPERATIONS)))
		spec = {"operation": operation, "requested_by": user}
		purpose = args.get("purpose") or args.get("reason")
		if purpose:
			spec["purpose"] = re.sub(r"\s+", " ", cstr(purpose)).strip()[:300]
		if operation == "set_doctype_naming":
			return self._normalize_doctype(args, spec)
		return self._normalize_rule(args, spec)

	def _normalize_doctype(self, args, spec):
		_base.require(args, ("doctype",), f"{TOOL} (set_doctype_naming)")
		extra = sorted(k for k in ("rule", "prefix", "prefix_digits", "priority", "conditions") if args.get(k) not in (None, ""))
		if extra:
			raise CustomizationError(_("set_doctype_naming does not take {0}").format(", ".join(extra)),
				hints=[_("Those belong to a Document Naming Rule: use operation create_rule")])
		doctype = cstr(args["doctype"]).strip()
		spec.update({"doctype": doctype, "name": doctype})
		if "autoname" in args and args["autoname"] is not None:
			autoname = cstr(args["autoname"]).strip()
			low = autoname.lower()
			if low in SPECIAL_AUTONAMES:
				autoname = SPECIAL_AUTONAMES[low]
			elif low.startswith(("field:", "naming_series:", "format:")):
				prefix, rest = autoname.split(":", 1)
				autoname = f"{prefix.lower()}:{rest.strip()}"
				if low.startswith("naming_series:"):
					autoname = "naming_series:"
			spec["autoname"] = autoname
			spec["naming_rule"] = naming_rule_for(autoname)
		options = args.get("naming_series_options")
		if options not in (None, ""):
			if isinstance(options, str):
				options = _options(options)
			if not isinstance(options, list) or not all(isinstance(o, str) for o in options) or not [o for o in options if o.strip()]:
				raise CustomizationError(_("naming_series_options must be a list of series such as [\"INV-.YYYY.-\"]"))
			options = list(dict.fromkeys(o.strip() for o in options if o.strip()))
			if len(options) > MAX_SERIES:
				raise CustomizationError(_("At most {0} series").format(MAX_SERIES))
			spec["naming_series_options"] = options
		if "autoname" not in spec and "naming_series_options" not in spec:
			raise CustomizationError(_("Nothing to change: send autoname, naming_series_options or both"),
				hints=[_("autoname examples: naming_series:, field:customer_name, format:INV-{YYYY}-{#####}, INV-.YYYY.-.#####, hash, prompt")])
		return spec

	def _normalize_rule(self, args, spec):
		operation = spec["operation"]
		if args.get("autoname") not in (None, "") or args.get("naming_series_options") not in (None, ""):
			raise CustomizationError(_("{0} does not take autoname or naming_series_options").format(operation),
				hints=[_("Use operation set_doctype_naming for the DocType's own naming")])
		if operation == "create_rule":
			_base.require(args, ("doctype", "prefix"), f"{TOOL} (create_rule)")
			if args.get("rule"):
				raise CustomizationError(_("create_rule does not take rule; Frappe names the rule itself"))
			doctype = cstr(args["doctype"]).strip()
			spec.update({"doctype": doctype, "prefix": cstr(args["prefix"]).strip(),
				"prefix_digits": 5 if args.get("prefix_digits") in (None, "") else cint(args["prefix_digits"]),
				"priority": cint(args.get("priority") or 0),
				"conditions": self._conditions(args.get("conditions"))})
			spec["name"] = f"rule:{doctype}:{spec['prefix']}"
			return spec
		_base.require(args, ("rule",), f"{TOOL} ({operation})")
		rule = cstr(args["rule"]).strip()
		if not frappe.db.exists("Document Naming Rule", rule):
			raise CustomizationError(_("There is no Document Naming Rule {0}").format(rule),
				hints=[_("Rules are listed in the preview of set_doctype_naming and create_rule for the same DocType")])
		current = frappe.db.get_value("Document Naming Rule", rule, "document_type")
		if args.get("doctype") and cstr(args["doctype"]).strip() != current:
			raise CustomizationError(_("The rule {0} belongs to {1}").format(rule, current))
		spec.update({"rule": rule, "doctype": current, "name": rule})
		if operation in ("disable_rule", "enable_rule"):
			extra = sorted(k for k in ("prefix", "prefix_digits", "priority", "conditions") if args.get(k) not in (None, ""))
			if extra:
				raise CustomizationError(_("{0} takes only rule; got {1}").format(operation, ", ".join(extra)))
			return spec
		changed = False
		if args.get("prefix") not in (None, ""):
			spec["prefix"] = cstr(args["prefix"]).strip()
			changed = True
		for key in ("prefix_digits", "priority"):
			if args.get(key) not in (None, ""):
				spec[key] = cint(args[key])
				changed = True
		if args.get("conditions") is not None:
			spec["conditions"] = self._conditions(args["conditions"])
			changed = True
		if not changed:
			raise CustomizationError(_("Nothing to change: send prefix, prefix_digits, priority or conditions"))
		return spec

	def _conditions(self, value):
		if value in (None, ""):
			return []
		if not isinstance(value, list):
			raise CustomizationError(_("conditions must be a list of {field, condition, value}"))
		out = []
		for i, row in enumerate(value, 1):
			if not isinstance(row, dict):
				raise CustomizationError(_("conditions[{0}] must be an object").format(i))
			_base.reject_unknown(row, CONDITION_KEYS, f"conditions[{i}]")
			_base.require(row, ("field", "condition", "value"), f"conditions[{i}]")
			if row["condition"] not in CONDITION_OPERATORS:
				raise CustomizationError(_("conditions[{0}].condition must be one of {1}").format(i, " ".join(CONDITION_OPERATORS)))
			out.append({"field": cstr(row["field"]).strip(), "condition": row["condition"], "value": cstr(row["value"])})
		return out

	# -- snapshot -----------------------------------------------------------------
	def snapshot(self, spec):
		doctype = spec.get("doctype")
		if spec["operation"] == "set_doctype_naming":
			out = {"doctype": doctype}
			try:
				meta = frappe.get_meta(doctype, cached=False)
			except frappe.DoesNotExistError:
				return out
			field = meta.get_field("naming_series")
			out.update({"autoname": cstr(meta.autoname), "naming_rule": cstr(meta.naming_rule),
				"naming_series_options": _options(field.options) if field else None,
				"naming_series_default": cstr(field.default) if field else None})
			return out
		# every rule of the DocType (not the counters: they move with each new document)
		rules = []
		for name in frappe.get_all("Document Naming Rule", filters={"document_type": doctype}, order_by="name asc", pluck="name"):
			rules.append(self._rule_state(name))
		return {"doctype": doctype, "rules": rules}

	def _rule_state(self, name):
		doc = frappe.get_doc("Document Naming Rule", name)
		return {"rule": doc.name, "disabled": cint(doc.disabled), "prefix": cstr(doc.prefix),
			"prefix_digits": cint(doc.prefix_digits), "priority": cint(doc.priority),
			"conditions": [{"field": c.field, "condition": c.condition, "value": cstr(c.value)} for c in doc.conditions]}

	# -- check --------------------------------------------------------------------
	def check(self, spec, user):
		checks = _base.role_checks(user, self.required_roles, _("Frappe lets only System Managers change naming"))
		if spec["operation"] == "set_doctype_naming":
			return self._check_doctype(spec, checks)
		return self._check_rule(spec, checks)

	def _common_impact(self, doctype, meta):
		impact = [_("Only new {0} documents are named this way; the {1} existing names do not change").format(doctype, frappe.db.count(doctype))]
		if cint(meta.allow_rename):
			impact.append(_("Existing {0} documents can still be renamed by hand (Allow Rename is on)").format(doctype))
		return impact

	def _check_doctype(self, spec, checks):
		from frappe.core.doctype.doctype.doctype import validate_series
		from frappe.model.meta import Meta
		from frappe.model.naming import NAMING_SERIES_PATTERN, NamingSeries, has_custom_parser

		doctype = spec["doctype"]
		reason = _base.customizable_reason(doctype)
		if reason:
			checks.append(Check("naming.doctype", "critical", reason))
			return _base.build_report(checks, risk="high")
		meta = frappe.get_meta(doctype, cached=False)
		before = self.snapshot(spec)
		current = cstr(meta.autoname)
		autoname = spec.get("autoname", current)
		preview = {"doctype": doctype, "before": {"autoname": current, "naming_rule": before.get("naming_rule")},
			"after": {"autoname": autoname, "naming_rule": spec.get("naming_rule") or before.get("naming_rule")}}
		impact = self._common_impact(doctype, meta)
		low = autoname.lower()

		# autoincrement / UUID change the name column itself (doctype.py validate_autoincrement_autoname)
		for special in ("autoincrement", "uuid"):
			if (low == special) != (current.lower() == special):
				checks.append(Check("naming.column_type", "critical",
					_("Cannot change to/from {0} naming in Customize Form: it changes the name column of {1}").format(special, doctype)))

		series_field = meta.get_field("naming_series")
		if "naming_series_options" in spec and not series_field:
			checks.append(Check("naming.series_field", "critical",
				_("{0} has no naming_series field, so it has no series options").format(doctype)))
		if low.startswith("naming_series:") and not series_field:
			checks.append(Check("naming.series_field", "critical",
				_("{0} has no naming_series field: naming_series: needs one (validate_series: Field Missing)").format(doctype)))

		sample = _sample_doc(doctype)
		names = []
		if low.startswith("field:"):
			names = self._check_field(autoname[6:], doctype, meta, checks, preview)
		elif low.startswith("naming_series:") and series_field:
			options = spec.get("naming_series_options") or before.get("naming_series_options") or []
			if not options:
				checks.append(Check("naming.series_options", "critical", _("{0} has no series options: send naming_series_options").format(doctype)))
			preview["series"] = {}
			for series in options[:5]:
				try:
					# set_name_by_naming_series: make_autoname(doc.naming_series + ".#####")
					preview["series"][series] = _series_names(series + ".#####", sample)
				except Exception as e:
					checks.append(Check("naming.series_invalid", "critical", _("Series {0}: {1}").format(series, frappe.utils.strip_html(cstr(e))[:200])))
			if options:
				names = preview["series"].get(options[0]) or []
		elif low.startswith("format:"):
			body = autoname[7:]
			from frappe.model.naming import BRACED_PARAMS_PATTERN

			parts = [m[1:-1] for m in BRACED_PARAMS_PATTERN.findall(body)]
			stray = re.sub(r"\{[\w | #]+\}", "", body)
			if "{" in stray or "}" in stray:
				checks.append(Check("naming.format", "critical", _("format: has a brace that is not a {{part}}: {0}").format(body)))
			for part in parts:
				if part.startswith("#") or part in DATE_PARTS or has_custom_parser(part):
					continue
				if not meta.get_field(part) and part not in ("name", "owner"):
					checks.append(Check("naming.format", "critical", _("format: {{{0}}} is neither a date part (YY MM DD YYYY JJJ WW), a series {{#####}} nor a field of {1}").format(part, doctype)))
			if not any(p.startswith("#") for p in parts):
				checks.append(Check("naming.no_counter", "warning", _("format: has no {#####} counter: two documents with the same values get the same name and the second fails")))
			checks.append(Check("naming.old_style", "info", _("Frappe discourages format: (Expression (old style)); a dotted expression such as INV-.YYYY.-.##### is preferred")))
			if not any(c.level == "critical" for c in checks):
				try:
					names = _format_names(autoname, sample)
				except Exception as e:
					checks.append(Check("naming.format", "critical", _("format: cannot be evaluated: {0}").format(frappe.utils.strip_html(cstr(e))[:200])))
		elif low in ("hash", "prompt", "uuid", "autoincrement", ""):
			if low == "prompt":
				checks.append(Check("naming.prompt", "warning", _("Users must type the name of every new {0}; imports and the agent must send one").format(doctype)))
			if low == "hash":
				from frappe.model.naming import make_autoname

				names = [make_autoname("hash", doctype) for _i in range(3)]
			if low == "" and not _controller_names_itself(doctype):
				checks.append(Check("naming.empty", "warning", _("With no autoname and no naming code, {0} documents get random names").format(doctype)))
		else:
			# a dotted expression: set_name_from_naming_options uses it only when it contains "#"
			if "#" not in autoname:
				checks.append(Check("naming.expression", "critical",
					_("{0} has no # counter: Frappe ignores it and names documents randomly. Example: INV-.YYYY.-.#####").format(autoname)))
			else:
				try:
					NamingSeries(autoname).validate()
				except Exception as e:
					checks.append(Check("naming.expression", "critical", frappe.utils.strip_html(cstr(e))[:300]))
				else:
					names = _series_names(autoname, sample)

		# series options: Document Naming Settings rules (validate each, refuse one another DocType uses)
		if spec.get("naming_series_options") and series_field:
			options = spec["naming_series_options"]
			for series in options:
				if not NAMING_SERIES_PATTERN.match(series):
					checks.append(Check("naming.series_invalid", "critical",
						_("Series {0}: only letters, digits and - # . / {{ }} are allowed").format(series)))
					continue
				try:
					NamingSeries(series).validate()
				except Exception as e:
					checks.append(Check("naming.series_invalid", "critical", frappe.utils.strip_html(cstr(e))[:300]))
			checks += self._series_used_elsewhere(doctype, options)
			old = before.get("naming_series_options") or []
			dropped = [o for o in old if o not in options]
			if dropped:
				used = frappe.get_all(doctype, filters={"naming_series": ["in", dropped]}, pluck="naming_series", limit=1)
				checks.append(Check("naming.series_dropped", "warning" if used else "info",
					_("Series no longer offered: {0}; existing documents keep their names").format(", ".join(dropped))))
			preview["series_options"] = {"before": old, "after": options, "default": options[0]}
			if not low.startswith("naming_series:"):
				checks.append(Check("naming.series_unused", "warning",
					_("{0} is not named by its naming_series field (autoname {1}): the series options have no effect").format(doctype, autoname or "-")))

		# Frappe's own validate_series on a private copy of the meta
		if "autoname" in spec and not any(c.level == "critical" for c in checks):
			copy = Meta(doctype)
			copy.autoname = autoname
			collect = _base.capture_messages()
			try:
				validate_series(copy, autoname, doctype)
			except Exception as e:
				checks.append(Check("naming.frappe", "critical", _("Frappe rejects it: {0}").format(frappe.utils.strip_html(cstr(e)) or "; ".join(collect()))))
			collect()

		# who names first (model/naming.py set_new_name)
		rules = frappe.get_all("Document Naming Rule", filters={"document_type": doctype, "disabled": 0}, pluck="name")
		if rules and "autoname" in spec:
			checks.append(Check("naming.rules_first", "warning",
				_("Enabled Document Naming Rule(s) {0} name {1} first; this naming applies only when no rule matches").format(", ".join(rules), doctype)))
		if "autoname" in spec and _controller_names_itself(doctype):
			checks.append(Check("naming.controller", "warning",
				_("{0}'s own code (an autoname method) runs before this setting; the setting applies only when the code leaves the name empty").format(doctype)))

		if names:
			preview["next_names"] = names
			checks += _collisions(doctype, names)
		if "autoname" in spec and spec["autoname"] != current:
			impact.append(_("New {0} documents: {1} instead of {2}").format(doctype, spec["naming_rule"], before.get("naming_rule") or naming_rule_for(current)))
		if not any(c.level == "critical" for c in checks):
			checks.insert(0, Check("naming.valid", "ok", _("Frappe accepts this naming")))
		return _base.build_report(checks, impact=impact, preview=preview, risk="medium")

	def _check_field(self, fieldname, doctype, meta, checks, preview):
		df = meta.get_field(fieldname)
		if not fieldname or not df:
			checks.append(Check("naming.field", "critical", _("{0} has no field {1}").format(doctype, fieldname or "-")))
			return []
		if df.fieldtype not in FIELD_NAMING_TYPES:
			checks.append(Check("naming.field", "critical", _("{0} is a {1} field and cannot name documents").format(fieldname, df.fieldtype)))
			return []
		if fieldname not in frappe.db.get_table_columns(doctype):
			checks.append(Check("naming.field", "critical", _("{0} is not stored in the database").format(fieldname)))
			return []
		dupes = frappe.db.sql(
			f"""select `{fieldname}`, count(*) from `tab{doctype}` where ifnull(`{fieldname}`, '') != ''
			group by `{fieldname}` having count(*) > 1 order by count(*) desc limit 5""")
		if dupes:
			total = cint(frappe.db.sql(
				f"""select count(*) from (select 1 from `tab{doctype}` where ifnull(`{fieldname}`, '') != ''
				group by `{fieldname}` having count(*) > 1) d""")[0][0])
			sample = ", ".join(f"{cstr(v)} ({n})" for v, n in dupes)
			checks.append(Check("naming.field_duplicates", "critical",
				_("{0} value(s) of {1}.{2} repeat in existing documents (e.g. {3}); Frappe makes the field unique, which fails").format(total, doctype, fieldname, sample)))
		else:
			checks.append(Check("naming.field_unique", "info", _("{0} becomes unique (Frappe adds a unique index: a schema change)").format(fieldname)))
		empty = cint(frappe.db.sql(f"select count(*) from `tab{doctype}` where ifnull(`{fieldname}`, '') = ''")[0][0])
		if not cint(df.reqd):
			checks.append(Check("naming.field_optional", "warning",
				_("{0} is optional: a new document without it fails with '{1} is required'").format(fieldname, df.label or fieldname)))
		if empty:
			checks.append(Check("naming.field_empty", "info", _("{0} existing documents have no {1}").format(empty, fieldname)))
		values = frappe.get_all(doctype, filters={fieldname: ["is", "set"]}, pluck=fieldname, order_by="creation desc", limit=3)
		preview["examples"] = [cstr(v).strip() for v in values]
		preview["note"] = _("The name is the value of {0}").format(fieldname)
		return []

	def _series_used_elsewhere(self, doctype, options):
		"""document_naming_settings.py check_duplicate: a series another DocType offers is refused."""

		def stripped(s):
			return s.strip().rstrip("#")

		standard = frappe.get_all("DocField", {"fieldname": "naming_series"}, pluck="parent")
		custom = frappe.get_all("Custom Field", {"fieldname": "naming_series"}, pluck="dt")
		existing = {}
		for other in set(standard + custom) - {doctype}:
			try:
				for series in frappe.get_meta(other).get_naming_series_options():
					if series:
						existing[stripped(series)] = other
			except Exception:
				continue
		out = []
		for series in options:
			if stripped(series) in existing:
				out.append(Check("naming.series_taken", "critical", _("Series {0} already used in {1}").format(series, existing[stripped(series)])))
		return out

	def _effective_rule(self, spec):
		if spec["operation"] == "create_rule":
			return {"prefix": spec["prefix"], "prefix_digits": spec["prefix_digits"], "priority": spec["priority"],
				"conditions": spec["conditions"], "disabled": 0, "counter": 0}, None
		current = self._rule_state(spec["rule"])
		current["counter"] = cint(frappe.db.get_value("Document Naming Rule", spec["rule"], "counter"))
		merged = {**current, **{k: spec[k] for k in ("prefix", "prefix_digits", "priority", "conditions") if k in spec}}
		if spec["operation"] == "disable_rule":
			merged["disabled"] = 1
		elif spec["operation"] == "enable_rule":
			merged["disabled"] = 0
		return merged, current

	def _check_rule(self, spec, checks):
		from frappe.model import log_types
		from frappe.model.base_document import DOCTYPES_FOR_DOCTYPE
		from frappe.model.naming import NAMING_SERIES_PATTERN, parse_naming_series
		from frappe.utils.data import evaluate_filters

		doctype = spec["doctype"]
		reason = _base.customizable_reason(doctype, allow_custom=True)
		if not reason and (doctype in log_types or doctype in DOCTYPES_FOR_DOCTYPE or doctype in ("DefaultValue", "Patch Log")):
			reason = _("Frappe ignores naming rules for {0}").format(doctype)
		if not reason and frappe.get_meta(doctype).istable:
			reason = _("{0} is a child table; its rows are not named by rules").format(doctype)
		if reason:
			checks.append(Check("naming.doctype", "critical", reason))
			return _base.build_report(checks, risk="high")
		meta = frappe.get_meta(doctype, cached=False)
		rule, current = self._effective_rule(spec)
		operation = spec["operation"]
		preview = {"doctype": doctype, "rule": spec.get("rule") or "", "prefix": rule["prefix"],
			"prefix_digits": rule["prefix_digits"], "priority": rule["priority"], "conditions": rule["conditions"]}
		if current:
			preview["before"] = {k: current[k] for k in ("prefix", "prefix_digits", "priority", "conditions", "disabled")}
		impact = self._common_impact(doctype, meta)

		if operation == "disable_rule":
			impact.append(_("New {0} documents fall back to the next rule or the DocType's own naming ({1})").format(doctype, cstr(meta.autoname) or "-"))
			return _base.build_report(checks, impact=impact, preview=preview, risk="low")

		# set_new_name: autoincrement and UUID return before any rule is read
		if cstr(meta.autoname).lower() in ("autoincrement", "uuid"):
			checks.append(Check("naming.rule_ignored", "critical",
				_("{0} is named by {1}; Frappe never applies naming rules to it").format(doctype, meta.autoname)))
		prefix = rule["prefix"]
		if not prefix:
			checks.append(Check("naming.prefix", "critical", _("prefix is required")))
		if "#" in prefix:
			checks.append(Check("naming.prefix", "critical",
				_("prefix must not contain #: the rule adds its own counter of prefix_digits digits")))
		elif prefix and not NAMING_SERIES_PATTERN.match(prefix):
			checks.append(Check("naming.prefix", "warning", _("prefix {0} has characters other than letters, digits and - . / {{ }}").format(prefix)))
		if not 1 <= cint(rule["prefix_digits"]) <= 10:
			checks.append(Check("naming.prefix_digits", "critical", _("prefix_digits must be between 1 and 10")))
		fields = {df.fieldname: df for df in meta.fields}
		for c in rule["conditions"]:
			# document_naming_rule.py validate_fields_in_conditions
			if c["field"] not in fields:
				checks.append(Check("naming.condition_field", "critical", _("{0} is not a field of doctype {1}").format(c["field"], doctype)))
			elif fields[c["field"]].fieldtype in ("Table", "Table MultiSelect"):
				checks.append(Check("naming.condition_field", "critical", _("{0} is a table and cannot be compared").format(c["field"])))

		others = [r for r in self.snapshot(spec)["rules"] if r["rule"] != spec.get("rule") and not r["disabled"]]
		same = [r["rule"] for r in others if r["priority"] == cint(rule["priority"])]
		if same:
			checks.append(Check("naming.priority_tie", "warning",
				_("Rule(s) {0} have the same priority {1}: which one runs first is not defined").format(", ".join(same), rule["priority"])))
		preview["other_rules"] = [{"rule": r["rule"], "prefix": r["prefix"], "priority": r["priority"],
			"conditions": len(r["conditions"])} for r in others]
		if not rule["conditions"]:
			# rules run by priority desc (naming.py set_naming_from_document_naming_rule): lower ones never run
			shadowed = [r["rule"] for r in others if r["priority"] < cint(rule["priority"])]
			if shadowed:
				checks.append(Check("naming.shadows", "warning",
					_("Without conditions this rule names every new {0} before rule(s) {1} are reached").format(doctype, ", ".join(shadowed))))

		if not any(c.level == "critical" for c in checks):
			sample = _sample_doc(doctype)
			try:
				base = parse_naming_series(prefix, doc=sample, number_generator=_peek(1))
				fmt = "%0" + str(cint(rule["prefix_digits"])) + "d"
				names = [base + fmt % (cint(rule.get("counter")) + i) for i in range(1, 4)]
				preview["next_names"] = names
				checks += _collisions(doctype, names)
			except Exception as e:
				checks.append(Check("naming.prefix", "critical", _("prefix cannot be evaluated: {0}").format(frappe.utils.strip_html(cstr(e))[:200])))
			if rule["conditions"]:
				rows = frappe.get_all(doctype, fields=["*"], order_by="creation desc", limit=SAMPLE_SIZE)
				try:
					matched = sum(1 for r in rows if evaluate_filters(r, [(doctype, c["field"], c["condition"], c["value"]) for c in rule["conditions"]]))
				except Exception as e:
					checks.append(Check("naming.condition", "critical", _("The conditions cannot be evaluated: {0}").format(cstr(e)[:200])))
				else:
					if rows:
						impact.append(_("{0} of the last {1} {2} documents match the conditions").format(matched, len(rows), doctype))
						if not matched:
							checks.append(Check("naming.no_match", "warning", _("None of the last {0} documents match the conditions").format(len(rows))))
			else:
				impact.append(_("Applies to every new {0} document").format(doctype))
		if cstr(meta.autoname).lower().startswith("naming_series:"):
			checks.append(Check("naming.overrides_series", "warning",
				_("For matching documents the rule replaces the Series the user picks in {0}").format(doctype)))
		if _controller_names_itself(doctype):
			checks.append(Check("naming.overrides_code", "warning",
				_("The rule runs before {0}'s own naming code (autoname method) and replaces its names").format(doctype)))
		checks.append(Check("naming.amended", "info", _("Amended documents keep Frappe's -1, -2 names; the rule does not apply to them")))
		if not any(c.level == "critical" for c in checks):
			checks.insert(0, Check("naming.valid", "ok", _("Frappe accepts this naming rule")))
		return _base.build_report(checks, impact=impact, preview=preview, risk="medium")

	# -- execute ----------------------------------------------------------------------
	def execute(self, spec, user, approval=None):
		if spec["operation"] == "set_doctype_naming":
			return self._execute_doctype(spec, user)
		return self._execute_rule(spec, user)

	def _execute_doctype(self, spec, user):
		from frappe.custom.doctype.property_setter.property_setter import make_property_setter

		doctype = spec["doctype"]
		before = self.snapshot(spec)
		messages = []
		with _base.as_user(user):
			if not frappe.has_permission("Customize Form", "write"):
				raise frappe.PermissionError(_("{0} cannot use Customize Form").format(user))
			if spec.get("naming_series_options"):
				# document_naming_settings.py set_series_options_in_meta: erase default, set options, set default
				options = "\n".join(spec["naming_series_options"])
				make_property_setter(doctype, "naming_series", "default", "", "Text")
				make_property_setter(doctype, "naming_series", "options", options, "Text")
				make_property_setter(doctype, "naming_series", "default", spec["naming_series_options"][0], "Text")
				frappe.clear_cache(doctype=doctype)
			if "autoname" in spec:
				collect = _base.capture_messages()
				cf = frappe.get_doc("Customize Form")
				cf.doc_type = doctype
				cf.fetch_to_customize()
				cf.autoname = spec["autoname"]
				cf.naming_rule = spec["naming_rule"]
				cf.hide_success = True
				cf.save_customization()
				messages = collect()
		frappe.clear_cache(doctype=doctype)
		undo = {"operation": "set_doctype_naming", "doctype": doctype, "name": doctype,
			"purpose": _("Undo: restore the naming of {0}").format(doctype)}
		if "autoname" in spec:
			undo["autoname"] = before.get("autoname") or ""
			undo["naming_rule"] = before.get("naming_rule") or ""  # exactly as it was (often empty on old DocTypes)
		if spec.get("naming_series_options"):
			undo["naming_series_options"] = before.get("naming_series_options") or []
		return {"targets": [{"doctype": "DocType", "name": doctype}], "undo": undo, "messages": messages}

	def _execute_rule(self, spec, user):
		operation = spec["operation"]
		with _base.as_user(user):
			if operation == "create_rule":
				doc = frappe.new_doc("Document Naming Rule")
				doc.update({"document_type": spec["doctype"], "prefix": spec["prefix"], "prefix_digits": spec["prefix_digits"],
					"priority": spec["priority"], "disabled": 0})
				for c in spec["conditions"]:
					doc.append("conditions", dict(c))  # append() mutates the dict it gets
				doc.insert()
				undo = {"operation": "disable_rule", "rule": doc.name, "doctype": spec["doctype"], "name": doc.name,
					"purpose": _("Undo: disable the naming rule {0}").format(doc.name)}
			else:
				doc = frappe.get_doc("Document Naming Rule", spec["rule"])
				before = self._rule_state(doc.name)
				if operation == "update_rule":
					undo = {"operation": "update_rule", "rule": doc.name, "doctype": spec["doctype"], "name": doc.name,
						"purpose": _("Undo: restore the naming rule {0}").format(doc.name)}
					for key in ("prefix", "prefix_digits", "priority"):
						if key in spec:
							undo[key] = before[key]
							doc.set(key, spec[key])
					if "conditions" in spec:
						undo["conditions"] = before["conditions"]
						doc.set("conditions", [])
						for c in spec["conditions"]:
							doc.append("conditions", dict(c))  # append() mutates the dict it gets
				else:
					doc.disabled = 1 if operation == "disable_rule" else 0
					reverse = "enable_rule" if operation == "disable_rule" else "disable_rule"
					undo = {"operation": reverse, "rule": doc.name, "doctype": spec["doctype"], "name": doc.name}
				doc.save()
		return {"targets": [{"doctype": "Document Naming Rule", "name": doc.name}], "undo": undo}

	# -- verify -----------------------------------------------------------------------
	def verify(self, spec, result):
		checks = []
		if spec["operation"] == "set_doctype_naming":
			after = self.snapshot(spec)
			messages = "; ".join((result or {}).get("messages") or []) or "-"
			if "autoname" in spec and cstr(after.get("autoname")) != cstr(spec["autoname"]):
				checks.append(Check("verify.autoname", "critical", _("autoname did not change: Frappe refused it: {0}").format(messages)))
			if spec.get("naming_series_options") and after.get("naming_series_options") != spec["naming_series_options"]:
				checks.append(Check("verify.series", "critical", _("The series options did not change")))
			if not checks:
				checks.append(Check("verify.naming", "ok", _("{0} shows the new naming").format(spec["doctype"])))
			return checks
		name = (result.get("targets") or [{}])[0].get("name") or spec.get("rule")
		if not name or not frappe.db.exists("Document Naming Rule", name):
			return [Check("verify.exists", "critical", _("Document Naming Rule {0} does not exist").format(name))]
		state = self._rule_state(name)
		expected_disabled = {"create_rule": 0, "enable_rule": 0, "disable_rule": 1}.get(spec["operation"])
		if expected_disabled is not None and state["disabled"] != expected_disabled:
			checks.append(Check("verify.disabled", "critical", _("{0} is not {1}").format(name, _("disabled") if expected_disabled else _("enabled"))))
		for key in ("prefix", "prefix_digits", "priority", "conditions"):
			if key in spec and state[key] != spec[key]:
				checks.append(Check("verify.rule", "critical", _("{0} of {1} differs from the approved value").format(key, name)))
		if not checks:
			checks.append(Check("verify.naming", "ok", _("{0} is saved as approved").format(name)))
		return checks

	def summary(self, spec):
		operation = spec["operation"]
		doctype = spec.get("doctype") or "-"
		if operation == "set_doctype_naming":
			parts = []
			if "autoname" in spec:
				parts.append(spec["autoname"] or spec.get("naming_rule") or "-")
			if spec.get("naming_series_options"):
				parts.append(_("series {0}").format(", ".join(spec["naming_series_options"][:3])))
			return _("Name new {0} documents: {1}").format(doctype, "; ".join(parts))
		verb = {"create_rule": _("Create"), "update_rule": _("Update"), "disable_rule": _("Disable"), "enable_rule": _("Enable")}[operation]
		return _("{0} naming rule {1} on {2}").format(verb, spec.get("rule") or spec.get("prefix") or "", doctype)

	def get_source(self, name, user=None):
		doc = frappe.get_doc("Document Naming Rule", name)
		return {"kind": self.name, "document_type": doc.document_type, "counter": cint(doc.counter), **self._rule_state(name)}


KIND = NamingKind()
