"""Print Formats: custom (non-standard) Jinja print formats (docs/customization/01 §3.1 v2, §3.5).

Frappe rules followed (Frappe 16.28):
- ``Print Format`` autoname is Prompt (print_format.json); System Manager writes it (its permissions).
- ``PrintFormat.validate`` (print_format.py:73-104): a standard format cannot be changed outside developer
  mode, ``validate_template(html)`` for Jinja, ``custom_format`` needs ``html``.
- ``www/printview.py::get_rendered_template(doc, print_format=<PrintFormat>)`` renders an unsaved object;
  it checks read/print permission of the session user (``validate_print_permission``, :392) and refuses
  disabled formats (``get_print_format``, :446), drafts/cancelled per Print Settings (:160-165).
- Default format: ``print_format.py::make_default`` sets a ``default_print_format`` Property Setter on a
  standard DocType (as Customize Form does) and edits the DocType itself only for a custom DocType; this
  kind refuses the latter (DocType records are protected).

The template is checked by ``analyzers.jinja`` (writes/sends are critical, permission-less reads warn) and
rendered once on a real document the requester can read, inside a savepoint that is rolled back.
"""

from __future__ import annotations

import difflib
import re

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt

from ai_agent.customization.analyzers import jinja as jinja_analyzer
from ai_agent.customization.contract import Check, CustomizationError
from ai_agent.customization.kinds import _base

TOP_KEYS = ("operation", "name", "doc_type", "html", "css", "align_labels_right", "font_size", "margin_top",
	"margin_bottom", "margin_left", "margin_right", "default_print_format", "sample_document", "purpose",
	"restore_default")
OPERATIONS = ("create", "update", "disable", "enable", "set_default", "unset_default")
RECORD_FIELDS = ("html", "css", "align_labels_right", "font_size", "margin_top", "margin_bottom", "margin_left",
	"margin_right")
MARGINS = ("margin_top", "margin_bottom", "margin_left", "margin_right")
NAME_RE = re.compile(r"^[^<>\"'`\\/%#?]{3,140}$")
HEADER_RE = re.compile(r"^\s*\{#-?\s*ai_agent:.*?-?#\}\s*\n?", re.S)
PREVIEW_CHARS = 6000
PS_PROPERTY = "default_print_format"


def strip_header(html):
	return HEADER_RE.sub("", cstr(html), count=1)


def with_header(html, purpose, requested_by, approved_by=None, approval=None):
	"""Provenance as a Jinja comment: invisible in the printed output."""
	line = _base.header("{#", cstr(purpose).replace("#}", ""), requested_by, approved_by, approval, suffix=" #}")
	return line + "\n" + strip_header(html)


def current_default(doc_type):
	"""The Property Setter value of default_print_format on ``doc_type`` ('' when none)."""
	return cstr(frappe.db.get_value("Property Setter", {"doc_type": doc_type, "doctype_or_field": "DocType",
		"property": PS_PROPERTY}, "value"))


def set_default(doc_type, value):
	"""Set ('' removes) the default_print_format Property Setter, like print_format.make_default."""
	from frappe.custom.doctype.property_setter.property_setter import delete_property_setter

	if value:
		frappe.make_property_setter({"doctype_or_field": "DocType", "doctype": doc_type, "property": PS_PROPERTY,
			"value": value, "property_type": "Data"}, is_system_generated=False)
	else:
		delete_property_setter(doc_type, PS_PROPERTY)
	frappe.clear_cache(doctype=doc_type)


class PrintFormatKind:
	name = "print_format"
	setting = "allow_customize_print_formats"
	required_roles = ("System Manager",)

	# -- normalize --------------------------------------------------------------
	def normalize(self, args, user):
		args = dict(args or {})
		where = "customize.request_print_format"
		_base.reject_unknown(args, TOP_KEYS, where)
		_base.require(args, ("operation", "name"), where)
		operation = args["operation"]
		if operation not in OPERATIONS:
			raise CustomizationError(_("operation must be one of: {0}").format(", ".join(OPERATIONS)))
		name = re.sub(r"\s+", " ", cstr(args["name"])).strip()
		if not NAME_RE.match(name):
			raise CustomizationError(_("name must be 3-140 characters without < > \" ' ` \\ / % # ?"))
		spec = {"operation": operation, "name": name, "requested_by": user}
		if args.get("purpose"):
			spec["purpose"] = re.sub(r"\s+", " ", cstr(args["purpose"])).strip()[:140]
		existing = frappe.db.get_value("Print Format", name, ["doc_type", "standard", "custom_format",
			"print_format_type", "print_format_for", "disabled"], as_dict=True)

		if operation == "create":
			if existing:
				raise CustomizationError(_("A Print Format named {0} already exists").format(name),
					hints=[_("Use operation update to change it, or choose another name")])
			_base.require(args, ("doc_type", "html"), where + " (create)")
			spec["doc_type"] = cstr(args["doc_type"]).strip()
			spec.setdefault("purpose", name)
		else:
			if not existing:
				raise CustomizationError(_("There is no Print Format {0}").format(name),
					hints=[_("List the print formats with customize.inspect (include print_formats)")])
			if args.get("doc_type") and args["doc_type"] != existing.doc_type:
				raise CustomizationError(_("{0} belongs to {1}; the DocType of a print format cannot change").format(name, existing.doc_type))
			spec["doc_type"] = existing.doc_type
			if existing.print_format_for != "DocType":
				raise CustomizationError(_("{0} is a report print format; only DocType print formats are supported").format(name))
			# PrintFormat.validate (print_format.py:74-81) refuses saving a standard format outside developer mode
			if operation in ("update", "enable", "disable") and existing.standard == "Yes":
				raise CustomizationError(_("{0} is a standard print format and cannot be changed; create a copy under a new name").format(name),
					hints=[_("Read it with customize.get (kind print_format) and create a new one from its html")])
			if operation == "update" and not (cint(existing.custom_format) and existing.print_format_type == "Jinja"):
				raise CustomizationError(_("{0} was made with the Print Format Builder; only custom Jinja (HTML) formats can be changed here").format(name),
					hints=[_("Create a new Jinja print format instead")])

		if operation in ("create", "update"):
			for key in RECORD_FIELDS:
				if key not in args or args[key] is None:
					continue
				value = args[key]
				if key in ("html", "css"):
					if not isinstance(value, str):
						raise CustomizationError(_("{0} must be text").format(key))
					spec[key] = strip_header(value) if key == "html" else value
				elif key == "align_labels_right":
					spec[key] = 1 if cint(value) else 0
				elif key == "font_size":
					size = cint(value)
					if not 6 <= size <= 40:
						raise CustomizationError(_("font_size must be between 6 and 40"))
					spec[key] = size
				else:
					margin = flt(value)
					if not 0 <= margin <= 100:
						raise CustomizationError(_("{0} must be between 0 and 100 (mm)").format(key))
					spec[key] = margin
			if operation == "update" and not any(k in spec for k in RECORD_FIELDS) and "default_print_format" not in args:
				raise CustomizationError(_("Nothing to update"), hints=[_("Pass html, css, font_size, margins, align_labels_right or default_print_format")])
			if operation == "create" and not cstr(spec.get("html")).strip():
				raise CustomizationError(_("html is required for a Jinja print format"))
		if args.get("default_print_format") is not None:
			if operation not in ("create", "update", "enable"):
				raise CustomizationError(_("default_print_format goes with create, update or enable; use set_default / unset_default alone"))
			spec["default_print_format"] = 1 if cint(args["default_print_format"]) else 0
		if args.get("restore_default") is not None:  # written by execute() into undo specs
			spec["restore_default"] = cstr(args["restore_default"])
		if args.get("sample_document"):
			spec["sample_document"] = cstr(args["sample_document"])
		return spec

	# -- snapshot -----------------------------------------------------------------
	def snapshot(self, spec):
		name = spec["name"]
		doc_type = spec.get("doc_type") or frappe.db.get_value("Print Format", name, "doc_type")
		out = {"name": name, "doc_type": doc_type, "default": current_default(doc_type) if doc_type else ""}
		if not frappe.db.exists("Print Format", name):
			return {**out, "exists": False}
		doc = frappe.get_doc("Print Format", name)
		return {**out, "exists": True, "disabled": cint(doc.disabled), "html_sha256": _base.sha256(strip_header(doc.html)),
			"css_sha256": _base.sha256(doc.css), "align_labels_right": cint(doc.align_labels_right),
			"font_size": cint(doc.font_size), **{m: flt(doc.get(m)) for m in MARGINS}}

	# -- helpers ----------------------------------------------------------------------
	def _record(self, name):
		doc = frappe.get_doc("Print Format", name)
		return {"html": strip_header(doc.html), "css": cstr(doc.css), "align_labels_right": cint(doc.align_labels_right),
			"font_size": cint(doc.font_size), **{m: flt(doc.get(m)) for m in MARGINS}, "disabled": cint(doc.disabled)}

	def _unsaved(self, spec, merged):
		pf = frappe.new_doc("Print Format")
		pf.update({"doc_type": spec["doc_type"], "print_format_for": "DocType", "standard": "No", "custom_format": 1,
			"print_format_type": "Jinja", "disabled": 0,
			"module": frappe.db.get_value("DocType", spec["doc_type"], "module"),
			**{k: merged[k] for k in RECORD_FIELDS if k in merged}})
		pf.name = spec["name"]
		return pf

	def _sample(self, doc_type, user, wanted=None):
		"""(name, note): the document to render, chosen among those ``user`` can read."""
		if wanted:
			if not frappe.db.exists(doc_type, wanted):
				return None, _("There is no {0} {1}").format(doc_type, wanted)
			if not frappe.has_permission(doc_type, "read", doc=wanted, user=user):
				return None, _("{0} cannot read {1} {2}").format(user, doc_type, wanted)
			return wanted, None
		filters = {}
		meta = frappe.get_meta(doc_type)
		if meta.is_submittable:
			settings = frappe.get_cached_doc("Print Settings")
			allowed = [1] + ([0] if cint(settings.allow_print_for_draft) else []) + ([2] if cint(settings.allow_print_for_cancelled) else [])
			filters["docstatus"] = ["in", allowed]
		rows = frappe.get_list(doc_type, filters=filters, fields=["name"], order_by="modified desc", limit=1, user=user)
		return (rows[0].name, None) if rows else (None, _("No {0} that {1} can read to render").format(doc_type, user))

	def _render(self, spec, merged, user, preview):
		"""Render once inside a rolled-back savepoint (before_print hooks may touch the document)."""
		from frappe.www.printview import get_rendered_template

		from ai_agent.runtime import masking

		name, note = self._sample(spec["doc_type"], user, spec.get("sample_document"))
		if not name:
			level = "critical" if spec.get("sample_document") else "warning"
			return [Check("print.sample", level, note + ". " + _("The template compiles but was not rendered"))]
		preview["document"] = {"doctype": spec["doc_type"], "name": name}
		savepoint = "ai_customize_print_preview"
		collect = _base.capture_messages()
		frappe.db.savepoint(savepoint)
		try:
			with _base.as_user(user):
				doc = frappe.get_doc(spec["doc_type"], name)
				html = get_rendered_template(doc, print_format=self._unsaved(spec, merged), no_letterhead=1)
		except frappe.PermissionError as e:
			return [Check("print.permission", "critical", _("{0} cannot print {1} {2}: {3}").format(user, spec["doc_type"], name, jinja_analyzer.error_text(e)))]
		except Exception as e:
			return [Check("print.render", "critical", _("Rendering on {0} {1} fails: {2}").format(spec["doc_type"], name, jinja_analyzer.error_text(e)))]
		finally:
			frappe.db.rollback(save_point=savepoint)
			collect()
		if spec["doc_type"] in masking.salary_doctypes():
			preview["html"] = None
			preview["html_note"] = _("Not shown: {0} holds pay data").format(spec["doc_type"])
		else:
			preview["html"] = html[:PREVIEW_CHARS]
			preview["truncated"] = len(html) > PREVIEW_CHARS
		preview["html_length"] = len(html)
		return [Check("print.render", "ok", _("Renders on {0} {1} ({2} characters)").format(spec["doc_type"], name, len(html)))]

	# -- check --------------------------------------------------------------------
	def check(self, spec, user):
		op, name, doc_type = spec["operation"], spec["name"], spec["doc_type"]
		checks = _base.role_checks(user, self.required_roles, _("Frappe lets only System Managers write Print Formats"))
		reason = _base.customizable_reason(doc_type, allow_custom=True)
		if reason:
			checks.append(Check("print.doctype", "critical", reason))
			return _base.build_report(checks, risk="medium")
		meta = frappe.get_meta(doc_type)
		if meta.istable:
			checks.append(Check("print.doctype", "critical", _("{0} is a child table and is printed inside its parent").format(doc_type)))
		if not frappe.has_permission(doc_type, "read", user=user):
			checks.append(Check("print.doctype", "critical", _("{0} cannot read {1}").format(user, doc_type)))
		exists = frappe.db.exists("Print Format", name)
		if op == "create" and exists:
			checks.append(Check("print.name_taken", "critical", _("A Print Format named {0} already exists; use operation update").format(name)))
		default_now = current_default(doc_type)
		own_default = cstr(frappe.db.get_value("DocType", doc_type, "default_print_format"))
		effective_default = default_now or own_default
		preview = {"name": name, "doc_type": doc_type, "operation": op, "default_before": effective_default or "Standard"}
		impact = []
		wants_default = spec.get("default_print_format")
		if op == "set_default" or wants_default == 1:
			if meta.custom:
				checks.append(Check("print.default_custom", "critical", _("{0} is a custom DocType: set its default print format in the DocType itself").format(doc_type)))
			impact.append(_("Becomes the default print format of {0} for everyone (now: {1})").format(doc_type, effective_default or "Standard"))
			preview["default_after"] = name
		if op == "set_default" and exists and cint(frappe.db.get_value("Print Format", name, "disabled")):
			checks.append(Check("print.default_disabled", "critical", _("{0} is disabled; enable it first (operation enable with default_print_format 1)").format(name)))
		if op == "unset_default" or (wants_default == 0 and default_now == name):
			if default_now != name and op == "unset_default":
				checks.append(Check("print.not_default", "critical", _("{0} is not the default print format of {1}").format(name, doc_type)))
			impact.append(_("{0} goes back to its own default print format ({1})").format(doc_type, own_default or "Standard"))
		if op == "disable" and default_now == name:
			checks.append(Check("print.disable_default", "warning", _("{0} is the default print format of {1}: printing falls back to the standard default; undo restores it").format(name, doc_type)))
			impact.append(_("Removes it as the default of {0}").format(doc_type))
		if "restore_default" in spec:
			value = spec["restore_default"]
			if value and not frappe.db.exists("Print Format", value):
				checks.append(Check("print.restore_default", "critical", _("There is no Print Format {0} to restore as default").format(value)))
			impact.append(_("The default print format of {0} becomes {1}").format(doc_type, value or _("the standard one")))
		if op in ("disable", "enable", "set_default", "unset_default"):
			if op == "disable":
				impact.append(_("{0} is no longer offered when printing {1}").format(name, doc_type))
			elif op == "enable":
				impact.append(_("{0} is offered again when printing {1}").format(name, doc_type))
			return _base.build_report(checks, impact=impact, preview=preview, risk="low")

		merged = self._record(name) if exists and op != "create" else {"font_size": 14, **{m: 15.0 for m in MARGINS}}
		before = dict(merged)
		merged.update({k: spec[k] for k in RECORD_FIELDS if k in spec})
		html, css = cstr(merged.get("html")), cstr(merged.get("css"))
		syntax, compiled = jinja_analyzer.compile_checks(html, "html")
		checks += syntax + jinja_analyzer.text_checks(html, "html")
		checks += jinja_analyzer.text_checks(css, "css")
		if re.search(r"</\s*style", css, re.I):
			checks.append(Check("print.css", "critical", _("css must not close the <style> element")))
		if compiled:
			checks.append(Check("jinja.syntax", "ok", _("The template compiles")))
		preview["code"] = {"language": "jinja", "text": html}
		if op == "update" and "html" in spec:
			preview["code"]["diff"] = "".join(difflib.unified_diff(cstr(before.get("html")).splitlines(True),
				html.splitlines(True), "current", "proposed"))
		if css:
			preview["css"] = css
		preview["settings"] = {k: merged.get(k) for k in ("align_labels_right", "font_size", *MARGINS)}
		if not any(c.level == "critical" for c in checks):
			pf = self._unsaved(spec, merged)
			savepoint = "ai_customize_print_validate"
			collect = _base.capture_messages()
			frappe.db.savepoint(savepoint)
			try:
				pf.validate()
			except Exception as e:
				checks.append(Check("print.validate", "critical", _("Frappe refuses the print format: {0}").format(jinja_analyzer.error_text(e))))
			finally:
				frappe.db.rollback(save_point=savepoint)
				collect()
		if not any(c.level == "critical" for c in checks):
			checks += self._render(spec, merged, user, preview)
		users = _base.read_user_count(doc_type)
		if op == "create" and wants_default != 1:
			impact.append(_("Offered to the {0} user(s) who can read {1} when they print").format(users, doc_type))
		elif op == "update":
			impact.append(_("Changes what the {0} user(s) who can read {1} get when they print with {2}").format(users, doc_type, name))
		impact.append(_("The template runs on the server for every print and PDF (also email attachments)"))
		return _base.build_report(checks, impact=impact, preview=preview, risk="medium")

	# -- execute / verify ---------------------------------------------------------------
	def execute(self, spec, user, approval=None):
		approval = _base.approval_name(approval)
		op, name, doc_type = spec["operation"], spec["name"], spec["doc_type"]
		default_before = current_default(doc_type)
		undo = {"name": name, "doc_type": doc_type}
		with _base.as_user(user):
			if op == "create":
				pf = self._unsaved(spec, {k: spec[k] for k in RECORD_FIELDS if k in spec})
				pf.html = with_header(spec["html"], spec.get("purpose"), spec.get("requested_by"), user, approval)
				pf.insert()
				undo["operation"] = "disable"
			elif op == "update":
				pf = frappe.get_doc("Print Format", name)
				previous = self._record(name)
				undo.update({"operation": "update", "purpose": _("undo of {0}").format(approval or "")})
				for key in RECORD_FIELDS:
					if key in spec:
						undo[key] = previous[key]
						pf.set(key, spec[key])
				if "html" in spec:
					pf.html = with_header(spec["html"], spec.get("purpose"), spec.get("requested_by"), user, approval)
				pf.save()
			elif op in ("disable", "enable"):
				pf = frappe.get_doc("Print Format", name)
				pf.disabled = 1 if op == "disable" else 0
				pf.save()
				undo["operation"] = "enable" if op == "disable" else "disable"
			else:
				pf = frappe.get_doc("Print Format", name)
				undo["operation"] = "unset_default" if op == "set_default" else "set_default"

			if op == "set_default" or spec.get("default_print_format") == 1:
				set_default(doc_type, name)
			elif op == "unset_default" or (spec.get("default_print_format") == 0 and default_before == name):
				set_default(doc_type, "")
			elif op == "disable" and default_before == name:
				set_default(doc_type, "")
			if "restore_default" in spec:
				set_default(doc_type, spec["restore_default"])

		default_after = current_default(doc_type)
		# The undo keeps the same name/doc_type so its snapshot matches this one (request_undo compares them).
		if op == "set_default":
			undo = {"operation": "unset_default", "name": name, "doc_type": doc_type, "restore_default": default_before}
		elif op == "unset_default":
			undo = {"operation": "set_default", "name": name, "doc_type": doc_type}
		elif default_after != default_before:
			undo["restore_default"] = default_before
		return {"targets": [{"doctype": "Print Format", "name": pf.name}], "undo": undo,
			"html_sha256": _base.sha256(strip_header(pf.html)), "disabled": cint(pf.disabled), "default": default_after}

	def verify(self, spec, result):
		name, doc_type, op = spec["name"], spec["doc_type"], spec["operation"]
		if not frappe.db.exists("Print Format", name):
			return [Check("verify.exists", "critical", _("Print Format {0} does not exist").format(name))]
		doc = frappe.get_doc("Print Format", name)
		checks = []
		expected = {"create": 0, "enable": 0, "disable": 1}.get(op)
		if expected is not None and cint(doc.disabled) != expected:
			checks.append(Check("verify.disabled", "critical", _("{0} is not {1}").format(name, _("disabled") if expected else _("enabled"))))
		if result.get("html_sha256") and _base.sha256(strip_header(doc.html)) != result["html_sha256"]:
			checks.append(Check("verify.html", "critical", _("The stored template differs from the approved one")))
		if current_default(doc_type) != cstr(result.get("default")):
			checks.append(Check("verify.default", "critical", _("The default print format of {0} is not what was approved").format(doc_type)))
		if op in ("create", "update", "enable") and not cint(doc.disabled) and doc.custom_format:
			name_used, _note = self._sample(doc_type, frappe.session.user, spec.get("sample_document"))
			if name_used:
				from frappe.www.printview import get_rendered_template

				savepoint = "ai_customize_print_verify"
				frappe.db.savepoint(savepoint)
				try:
					get_rendered_template(frappe.get_doc(doc_type, name_used), print_format=doc, no_letterhead=1)
				except Exception as e:
					checks.append(Check("verify.render", "critical", _("The saved print format does not render: {0}").format(jinja_analyzer.error_text(e))))
				finally:
					frappe.db.rollback(save_point=savepoint)
		if not checks:
			checks.append(Check("verify.print_format", "ok", _("{0} is saved as approved").format(name)))
		return checks

	def summary(self, spec):
		verbs = {"create": _("Create"), "update": _("Update"), "disable": _("Disable"), "enable": _("Enable"),
			"set_default": _("Make default"), "unset_default": _("Remove as default")}
		return _("{0} print format {1} ({2})").format(verbs[spec["operation"]], spec["name"], spec["doc_type"])

	def get_source(self, name, user=None):
		doc = frappe.get_doc("Print Format", name)
		return {"kind": self.name, "name": doc.name, "doc_type": doc.doc_type, "standard": doc.standard,
			"custom_format": cint(doc.custom_format), "print_format_type": doc.print_format_type,
			"disabled": cint(doc.disabled), "html": strip_header(doc.html), "css": doc.css,
			"ai_made": bool(HEADER_RE.match(cstr(doc.html))), "is_default": current_default(doc.doc_type) == doc.name,
			**{k: doc.get(k) for k in ("align_labels_right", "font_size", *MARGINS)}}


KIND = PrintFormatKind()
