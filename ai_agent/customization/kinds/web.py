"""Web Page and Web Form: the public website surface (docs/customization/01 §3.1, v3).

What a visitor of the site sees, so the risks are defacement, phishing and records created by strangers:

- Web Page: content_type Rich Text / Markdown / HTML only (Page Builder and Slideshow pull in Web Templates
  that are not reviewed here). ``context_script`` (Python run by safe_exec on every view,
  ``web_page.py::get_context``) is never written. ``<script>``, inline event handlers, ``javascript:`` URLs,
  iframes/objects, ``<base>``, ``<meta refresh>``, password inputs and forms posting to another site are
  critical unless ``allow_scripts: true`` (then a high-risk warning). Jinja is rendered by Frappe whenever
  ``dynamic_template`` is set or the content has ``{{`` (``web_page.py::render_dynamic``) with the full
  safe_exec globals (``frappe.get_all``, ``frappe.db``, ``frappe.form_dict``…) for anonymous visitors: any data
  access or request echo in it is critical.
- Web Form: the target DocType must be customizable; ``login_required`` defaults to 1. Frappe saves every
  submission with ``ignore_permissions`` (``web_form.py::accept``), so a guest form lets the internet create
  records: critical unless ``allow_guest: true``, then edit/list are forced off and sensitive fields warned.
- Everything is created unpublished; ``published: true`` is explicit and makes the change high risk.

The route must be free: not a Desk/API path, not a www page, not a route rule or redirect target, not another
Web Page / Web Form / web-view document (``website/path_resolver.py`` + ``website/router.py`` order:
redirects, route rules, then WebFormPage before DocumentPage before TemplatePage — a Web Page with the route
``login`` would replace the login page).

Operations: create, update, publish, unpublish (alias disable), delete (unpublished records only; it is the
undo of a create). Undo: create -> delete, update -> previous values, publish <-> unpublish, delete -> create.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import re

import frappe
from frappe import _
from frappe.utils import cint, cstr

from ai_agent.customization.contract import Check, CustomizationError
from ai_agent.customization.kinds import _base
from ai_agent.customization.analyzers.web_content import (  # noqa: F401 (scan_html, scan_jinja: re-exported)
	_bit,
	_excerpt,
	_is_external,
	_scrub,
	PREVIEW_CHARS,
	is_jinja,
	render_page,
	scan_css,
	scan_html,
	scan_jinja,
)

TOOL = "customize.request_web"
TYPES = ("Web Page", "Web Form")
OPERATIONS = ("create", "update", "publish", "unpublish", "delete")
ALIASES = {"disable": "unpublish", "enable": "publish"}
ROLES = ("System Manager", "Website Manager")

COMMON_KEYS = ("operation", "doctype", "name", "title", "route", "published", "meta_title", "meta_description",
	"purpose", "allow_scripts", "context_script")
PAGE_KEYS = ("content_type", "content", "css", "javascript", "show_title", "dynamic_template")
FORM_KEYS = ("doc_type", "form_fields", "login_required", "allow_guest", "allow_edit", "allow_multiple", "show_list",
	"introduction_text", "success_title", "success_message", "success_url", "button_label", "client_script",
	"custom_css", "apply_document_permissions")
CONTENT_FIELDS = {"Rich Text": "main_section", "Markdown": "main_section_md", "HTML": "main_section_html"}

# Frappe fields the kind writes (and snapshots / restores on undo)
PAGE_VALUES = ("title", "route", "published", "meta_title", "meta_description", "content_type", "main_section",
	"main_section_md", "main_section_html", "css", "insert_style", "javascript", "show_title", "dynamic_template")
FORM_VALUES = ("title", "route", "published", "meta_title", "meta_description", "doc_type", "login_required",
	"allow_edit", "allow_multiple", "show_list", "introduction_text", "success_title", "success_message",
	"success_url", "button_label", "client_script", "custom_css", "apply_document_permissions")
BIT_VALUES = ("published", "show_title", "dynamic_template", "insert_style", "login_required", "allow_edit",
	"allow_multiple", "show_list", "apply_document_permissions")

FIELD_KEYS = ("fieldname", "label", "fieldtype", "options", "reqd", "hidden", "read_only", "default", "description",
	"placeholder", "depends_on", "mandatory_depends_on", "read_only_depends_on", "max_length", "max_value",
	"precision", "allow_read_on_all_link_options", "show_in_filter")
FIELD_INTS = ("reqd", "hidden", "read_only", "max_length", "max_value", "allow_read_on_all_link_options", "show_in_filter")
LAYOUT_TYPES = ("Section Break", "Column Break", "Page Break")
MAX_FORM_FIELDS = 60

MAX_CONTENT = 100_000
MAX_CODE = 20_000
ROUTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*(/[A-Za-z0-9][A-Za-z0-9_\-]*)*$")
# paths served before the website router (frappe/app.py) or owned by Desk
RESERVED_PREFIXES = ("desk", "app", "apps", "api", "assets", "files", "private", "backups", "socket.io", ".well-known",
	"method", "website_script.js", "robots.txt", "sitemap.xml", "favicon.ico", "index")
SENSITIVE_LINKS = ("User", "Employee")
SENSITIVE_TYPES = ("Currency", "Password")

class WebKind:
	name = "web"
	setting = "allow_customize_web"
	required_roles = ROLES

	# -- normalize -----------------------------------------------------------------------------
	def normalize(self, args, user):
		args = dict(args or {})
		_base.reject_unknown(args, COMMON_KEYS + PAGE_KEYS + FORM_KEYS, TOOL)
		_base.require(args, ("operation", "doctype"), TOOL)
		operation = ALIASES.get(args["operation"], args["operation"])
		if operation not in OPERATIONS:
			raise CustomizationError(_("operation must be one of: {0}").format(", ".join(OPERATIONS)))
		doctype = args["doctype"]
		if doctype not in TYPES:
			raise CustomizationError(_("doctype must be Web Page or Web Form"))
		other = FORM_KEYS if doctype == "Web Page" else PAGE_KEYS
		wrong = sorted(k for k in other if k in args)
		if wrong:
			raise CustomizationError(_("{0} are not arguments of a {1}").format(", ".join(wrong), doctype),
				hints=[_("Web Page takes: {0}").format(", ".join(PAGE_KEYS)), _("Web Form takes: {0}").format(", ".join(FORM_KEYS))])
		spec = {"operation": operation, "doctype": doctype, "requested_by": user,
			"allow_scripts": bool(_bit(args.get("allow_scripts"))), "values": {}}
		if args.get("purpose"):
			spec["purpose"] = re.sub(r"\s+", " ", cstr(args["purpose"])).strip()[:140]
		if args.get("context_script") not in (None, ""):
			spec["context_script"] = cstr(args["context_script"])[:MAX_CODE]  # only to report it (critical)

		if operation == "create":
			_base.require(args, ("title",), TOOL + " (create)")
			title = re.sub(r"\s+", " ", cstr(args["title"])).strip()[:140]
			spec["name"] = cstr(args.get("name") or "").strip()[:140] or _scrub(title)
			if not spec["name"]:
				raise CustomizationError(_("The title gives no usable name; send name"))
		else:
			_base.require(args, ("name",), TOOL)
			spec["name"] = cstr(args["name"]).strip()
			if not frappe.db.exists(doctype, spec["name"]):
				hint = frappe.db.get_value(doctype, {"route": spec["name"].strip("/")}, "name")
				raise CustomizationError(_("There is no {0} named {1}").format(doctype, spec["name"]),
					hints=[_("Its name is {0}").format(hint)] if hint else [_("customize.get with kind web returns a page by name")])
			if operation in ("publish", "unpublish", "delete"):
				extra = sorted(k for k in args if k not in ("operation", "doctype", "name", "purpose", "allow_scripts", "allow_guest"))
				if extra:
					raise CustomizationError(_("{0} takes only name (send operation update to change {1})").format(operation, ", ".join(extra)))
				if operation == "publish" and doctype == "Web Form":
					spec["allow_guest"] = bool(_bit(args.get("allow_guest")))
				return spec

		values = spec["values"]
		if args.get("title"):
			values["title"] = re.sub(r"\s+", " ", cstr(args["title"])).strip()[:140]
		if args.get("route") not in (None, ""):
			values["route"] = self._route(args["route"])
		elif operation == "create":
			values["route"] = self._route(_scrub(values["title"]), derived=True)
		if "published" in args:
			values["published"] = _bit(args["published"])
		elif operation == "create":
			values["published"] = 0
		for key in ("meta_title", "meta_description"):
			if key in args:
				values[key] = cstr(args[key])[:500]

		if doctype == "Web Page":
			self._normalize_page(args, spec, operation)
		else:
			self._normalize_form(args, spec, operation)
		if operation == "update" and not values and "fields" not in spec:
			raise CustomizationError(_("Nothing to update: send the values to change"))
		return spec

	def _route(self, route, derived=False):
		route = cstr(route).strip().strip("/").strip()
		if not ROUTE_RE.match(route) or len(route) > 139:
			raise CustomizationError(
				_("The route {0} is not valid: use ASCII letters, digits, - and _ separated by /").format(route or '""'),
				hints=[_("Send route, e.g. offers/summer-2026")] if derived else [_("e.g. offers/summer-2026")])
		return route

	def _normalize_page(self, args, spec, operation):
		values = spec["values"]
		current_type = frappe.db.get_value("Web Page", spec["name"], "content_type") if operation == "update" else None
		if operation == "create":
			_base.require(args, ("content_type", "content"), TOOL + " (Web Page)")
		content_type = args.get("content_type") or current_type
		if args.get("content_type") and args["content_type"] not in CONTENT_FIELDS:
			raise CustomizationError(_("content_type must be Rich Text, Markdown or HTML"),
				hints=[_("Page Builder and Slideshow pages are edited in Desk")])
		if "content_type" in args:
			values["content_type"] = args["content_type"]
		if "content" in args:
			if content_type not in CONTENT_FIELDS:
				raise CustomizationError(_("This page is built with {0}; its content is edited in Desk").format(content_type),
					hints=[_("Send content_type Rich Text, Markdown or HTML to replace it")])
			content = cstr(args["content"])
			if len(content) > MAX_CONTENT:
				raise CustomizationError(_("The content is longer than {0} characters").format(MAX_CONTENT))
			values[CONTENT_FIELDS[content_type]] = content
		for key, field in (("css", "css"), ("javascript", "javascript")):
			if key in args:
				text = cstr(args[key])
				if len(text) > MAX_CODE:
					raise CustomizationError(_("{0} is longer than {1} characters").format(key, MAX_CODE))
				values[field] = text
		if "css" in args:
			values["insert_style"] = 1 if cstr(args["css"]).strip() else 0
		for key in ("show_title", "dynamic_template"):
			if key in args:
				values[key] = _bit(args[key])

	def _normalize_form(self, args, spec, operation):
		values = spec["values"]
		current = frappe.db.get_value("Web Form", spec["name"], ["doc_type", "login_required"], as_dict=True) if operation == "update" else None
		if operation == "create":
			_base.require(args, ("doc_type", "form_fields"), TOOL + " (Web Form)")
		if args.get("doc_type"):
			values["doc_type"] = cstr(args["doc_type"]).strip()
		doc_type = values.get("doc_type") or (current or {}).get("doc_type")
		spec["allow_guest"] = bool(_bit(args.get("allow_guest")))
		for key in ("login_required", "allow_edit", "allow_multiple", "show_list", "apply_document_permissions"):
			if key in args:
				values[key] = _bit(args[key])
		if operation == "create" and "login_required" not in values:
			values["login_required"] = 1
		for key in ("introduction_text", "success_title", "success_message", "success_url", "button_label"):
			if key in args:
				values[key] = cstr(args[key])[:MAX_CONTENT if key == "introduction_text" else 1000]
		for key in ("client_script", "custom_css"):
			if key in args:
				text = cstr(args[key])
				if len(text) > MAX_CODE:
					raise CustomizationError(_("{0} is longer than {1} characters").format(key, MAX_CODE))
				values[key] = text
		login = values.get("login_required", (current or {}).get("login_required", 1))
		if not cint(login):
			# a guest form never edits or lists records (Frappe: allow_edit/show_list depend on login or a key)
			forced = [k for k in ("allow_edit", "show_list") if values.get(k)]
			values["allow_edit"] = 0
			values["show_list"] = 0
			spec["forced"] = sorted(set(forced))
		if "form_fields" in args:
			spec["fields"] = self._form_fields(args["form_fields"], doc_type)

	def _form_fields(self, rows, doc_type):
		if not isinstance(rows, list) or not rows:
			raise CustomizationError(_("form_fields must be a non-empty list of {fieldname, ...} objects"))
		if len(rows) > MAX_FORM_FIELDS:
			raise CustomizationError(_("At most {0} form fields").format(MAX_FORM_FIELDS))
		meta = frappe.get_meta(doc_type) if doc_type and frappe.db.exists("DocType", doc_type) else None
		out = []
		for i, row in enumerate(rows, 1):
			if isinstance(row, str):
				row = {"fieldname": row}
			if not isinstance(row, dict):
				raise CustomizationError(_("form_fields[{0}] must be an object").format(i))
			_base.reject_unknown(row, FIELD_KEYS, f"form_fields[{i}]")
			clean = {k: (cint(v) if k in FIELD_INTS else cstr(v)) for k, v in row.items() if v is not None}
			if not clean.get("fieldname"):
				if clean.get("fieldtype") not in LAYOUT_TYPES:
					raise CustomizationError(_("form_fields[{0}] needs a fieldname (or fieldtype {1})").format(i, " / ".join(LAYOUT_TYPES)))
				out.append(clean)
				continue
			df = meta.get_field(clean["fieldname"]) if meta else None
			if df:
				clean.setdefault("fieldtype", df.fieldtype)
				clean.setdefault("label", df.label or clean["fieldname"])
				if df.options and df.fieldtype in ("Link", "Select", "Table", "Dynamic Link"):
					clean.setdefault("options", df.options)
			out.append(clean)
		return out

	# -- state -------------------------------------------------------------------------------------
	def _value_keys(self, doctype):
		return PAGE_VALUES if doctype == "Web Page" else FORM_VALUES

	def _state(self, doc):
		state = {k: (cint(doc.get(k)) if k in BIT_VALUES else cstr(doc.get(k))) for k in self._value_keys(doc.doctype)}
		state["context_script"] = cstr(doc.get("context_script"))
		if doc.doctype == "Web Form":
			state["fields"] = self._rows(doc)
			state["is_standard"] = cint(doc.is_standard)
			state["anonymous"] = cint(doc.anonymous)
			state["key_required"] = cint(doc.key_required)
		return state

	def _rows(self, doc):
		rows = []
		for row in doc.get("web_form_fields") or []:
			rows.append({k: (cint(row.get(k)) if k in FIELD_INTS else cstr(row.get(k)))
				for k in FIELD_KEYS if row.get(k) not in (None, "", 0)})
		return rows

	def snapshot(self, spec):
		doctype, name = spec["doctype"], spec.get("name")
		if not name or not frappe.db.exists(doctype, name):
			return {"doctype": doctype, "name": name, "exists": False}
		doc = frappe.get_doc(doctype, name)
		state = self._state(doc)
		return {"doctype": doctype, "name": name, "exists": True, "published": cint(doc.published),
			"route": cstr(doc.route), "sha256": _base.sha256(json.dumps(state, sort_keys=True, default=str))}

	def _effective(self, spec):
		"""(unsaved doc as it would be after the change, current doc or None)."""
		doctype = spec["doctype"]
		current = frappe.get_doc(doctype, spec["name"]) if spec["operation"] != "create" and frappe.db.exists(doctype, spec["name"]) else None
		if current:
			doc = frappe.get_doc(current.as_dict())
		else:
			doc = frappe.new_doc(doctype)
			doc.name = spec["name"]
		doc.update(spec.get("values") or {})
		if spec["operation"] in ("publish", "unpublish"):
			doc.published = 1 if spec["operation"] == "publish" else 0
		if "fields" in spec:
			doc.set("web_form_fields", [])
			for row in spec["fields"]:
				doc.append("web_form_fields", row)
		if doctype == "Web Form" and not current:
			doc.update({"is_standard": 0, "anonymous": 0, "key_required": 0, "allow_delete": 0})
		if doctype == "Web Page" and not current:
			doc.update({"dynamic_route": 0, "context_script": None})
		return doc, current

	# -- check -------------------------------------------------------------------------------------
	def check(self, spec, user):
		doctype, operation = spec["doctype"], spec["operation"]
		checks = _base.role_checks(user, ROLES, _("Frappe lets only Website Managers write {0}").format(doctype))
		exists = frappe.db.exists(doctype, spec.get("name"))
		if operation == "create" and exists:
			checks.append(Check("web.name_taken", "critical", _("A {0} named {1} already exists; use operation update").format(doctype, spec["name"])))
		if operation != "create" and not exists:
			checks.append(Check("web.missing", "critical", _("There is no {0} named {1}").format(doctype, spec.get("name"))))
			return _base.build_report(checks, risk="medium")
		if spec.get("context_script"):
			checks.append(Check("web.context_script", "critical",
				_("context_script runs Python on the server for every visitor; it is never written by the assistant")))
		doc, current = self._effective(spec)
		url = frappe.utils.get_url(cstr(doc.route)) if doc.route else None
		preview = {"doctype": doctype, "name": spec["name"], "operation": operation, "route": doc.route, "url": url,
			"published": cint(doc.published), "title": doc.title}

		if current is not None and operation != "unpublish":
			if cstr(current.get("context_script")).strip():
				checks.append(Check("web.context_script", "critical",
					_("{0} has a context_script (server Python); only unpublish is allowed here, change it in Desk").format(spec["name"])))
			if doctype == "Web Form" and cint(current.is_standard) and not frappe.conf.developer_mode:
				checks.append(Check("web.standard", "critical",
					_("{0} is a standard Web Form; Frappe changes it only in developer mode").format(spec["name"])))
			if doctype == "Web Form" and (cint(current.anonymous) or cint(current.key_required)):
				checks.append(Check("web.form_mode", "critical",
					_("{0} uses anonymous or key-based access; change it in Desk").format(spec["name"])))

		if operation == "unpublish":
			if current is not None and not cint(current.published):
				checks.append(Check("web.no_change", "info", _("{0} is already unpublished").format(spec["name"])))
			impact = [_("{0} stops being served at {1}").format(doctype, url or "-")]
			return _base.build_report(checks, impact=impact, preview=preview, risk="low")
		if operation == "delete":
			if cint(current.published) and not spec.get("of_create"):  # the undo of a create may take a page down
				checks.append(Check("web.delete_published", "critical",
					_("{0} is published; unpublish it first (only unpublished records are deleted)").format(spec["name"])))
			impact = [_("Deletes the unpublished {0} {1}").format(doctype, spec["name"])] if not cint(current.published) else \
				[_("{0} stops being served at {1}").format(doctype, url or "-")]
			return _base.build_report(checks, impact=impact, preview=preview, risk="low")

		checks += self._route_checks(doc, spec)
		impact = []
		if doctype == "Web Page":
			checks += self._page_checks(doc, spec, current, preview)
		else:
			checks += self._form_checks(doc, spec, user, preview, impact)
		risk = "medium"
		if cint(doc.published):
			risk = "high"
			checks.append(Check("web.published", "warning",
				_("Published: visible to anyone on the internet at {0}").format(url)))
			impact.insert(0, _("Visible to anyone on the internet at {0}").format(url))
		else:
			impact.insert(0, _("Saved unpublished: nobody on the internet sees it until it is published at {0}").format(url or "-"))
		if spec.get("allow_scripts"):
			risk = "high"
		if preview.get("external_links"):
			impact.append(_("Links to other sites: {0}").format(", ".join(preview["external_links"][:10])))
		if not any(c.level == "critical" for c in checks):
			checks += self._preview(doc, spec, user, preview)
		if operation == "update" and current is not None:
			preview["diff"] = self._diff(current, doc)
		return _base.build_report(checks, impact=impact, preview=preview, risk=risk)

	def _route_checks(self, doc, spec):
		route = cstr(doc.route).strip("/")
		if not route:
			return [Check("web.route", "critical", _("The {0} has no route").format(spec["doctype"]))]
		checks = [Check("web.route_taken", "critical", r) for r in route_conflicts(route, spec["doctype"], spec["name"])]
		home = cstr(frappe.db.get_single_value("Website Settings", "home_page")).strip("/")
		if home and home == route:
			checks.append(Check("web.route_home", "warning", _("{0} is the site's home page").format(route)))
		if not checks:
			checks.append(Check("web.route", "ok", _("The route /{0} is free").format(route)))
		return checks

	def _page_checks(self, doc, spec, current, preview):
		from frappe.website.utils import get_html_content_based_on_type

		checks = []
		allow = spec.get("allow_scripts")
		if doc.content_type not in CONTENT_FIELDS:
			values = spec.get("values") or {}
			if not ("content_type" in values or any(f in values for f in CONTENT_FIELDS.values())):
				checks.append(Check("web.content_type", "warning",
					_("The page is built with {0}; its blocks are not reviewed here").format(doc.content_type)))
				return checks
			checks.append(Check("web.content_type", "critical", _("content_type must be Rich Text, Markdown or HTML")))
			return checks
		main = get_html_content_based_on_type(doc, "main_section", doc.content_type)
		html_checks, links = scan_html(main, allow, _("content"))
		checks += html_checks
		if cstr(doc.css).strip():
			css_checks, css_links = scan_css(doc.css, allow, "css")
			checks += css_checks
			links += [u for u in css_links if u not in links]
		if cstr(doc.javascript).strip():
			if not allow:
				checks.append(Check("web.javascript", "critical",
					_("javascript runs in every visitor's browser; send allow_scripts: true only if the user explicitly wants it")))
			else:
				from ai_agent.customization.analyzers import js_code

				checks.append(Check("web.javascript", "warning", _("javascript runs in every visitor's browser")))
				checks += [c for c in js_code.analyze(doc.javascript, "Web", None) if c.level != "ok"]
		if is_jinja(main, doc.dynamic_template):
			checks.append(Check("jinja.dynamic", "info", _("The content is rendered as Jinja (dynamic_template or '{{' in it)")))
			checks += scan_jinja(main)
		if links:
			preview["external_links"] = links[:50]
			checks.append(Check("html.external_links", "info", _("Links to other sites: {0}").format(", ".join(links[:10]))))
		if not any(c.level in ("critical", "warning") and c.code.startswith(("html.", "css.")) for c in checks):
			checks.append(Check("html.safe", "ok", _("The HTML has no scripts, embeds or off-site forms")))
		preview["content_type"] = doc.content_type
		preview["code"] = {"language": "markdown" if doc.content_type == "Markdown" else "html",
			"text": cstr(doc.get(CONTENT_FIELDS[doc.content_type]))}
		if current is not None and spec["operation"] == "update" and CONTENT_FIELDS[doc.content_type] in (spec.get("values") or {}):
			before = cstr(current.get(CONTENT_FIELDS.get(current.content_type, "main_section")))
			preview["code"]["diff"] = "".join(difflib.unified_diff(before.splitlines(True),
				preview["code"]["text"].splitlines(True), "current", "proposed"))
		return checks

	def _form_checks(self, doc, spec, user, preview, impact):
		from frappe.model import no_value_fields

		checks = []
		doc_type = doc.doc_type
		reason = _base.customizable_reason(doc_type, allow_custom=True) if doc_type else _("No doc_type given")
		if not reason and frappe.get_meta(doc_type).istable:
			reason = _("{0} is a child table").format(doc_type)
		if reason:
			return [Check("form.doctype", "critical", reason)]
		meta = frappe.get_meta(doc_type)
		if not frappe.has_permission(doc_type, "create", user=user):
			checks.append(Check("form.create_permission", "critical",
				_("{0} cannot create {1}, so cannot open a form that creates them").format(user, doc_type)))
		allowed_types = frappe.get_meta("Web Form Field").get_field("fieldtype").options.split("\n")
		rows = doc.get("web_form_fields") or []
		if not any(r.fieldname for r in rows):
			checks.append(Check("form.fields", "critical", _("The form has no fields")))
		names = set()
		guest = not cint(doc.login_required)
		for row in rows:
			if row.fieldtype and row.fieldtype not in allowed_types:
				checks.append(Check("form.fieldtype", "critical", _("{0}: fieldtype {1} is not allowed in a Web Form").format(row.fieldname or row.label, row.fieldtype)))
			if not row.fieldname:
				continue
			names.add(row.fieldname)
			df = meta.get_field(row.fieldname)
			# web_form.py::validate_fields
			if not df and row.fieldtype not in no_value_fields:
				checks.append(Check("form.field_missing", "critical", _("{0} has no field {1}").format(doc_type, row.fieldname)))
				continue
			if not df:
				continue
			if cint(df.permlevel) > 0:
				checks.append(Check("form.permlevel", "critical",
					_("{0} is protected by field permissions (level {1}); Frappe saves web form values with ignore_permissions, which would bypass them").format(row.fieldname, df.permlevel)))
			if cint(df.read_only) and not cint(row.read_only):
				checks.append(Check("form.read_only", "warning", _("{0} is read only in Desk but the form lets visitors fill it").format(row.fieldname)))
			# web_form.py::validate_hidden_and_mandatory
			if cint(row.hidden) and cint(row.reqd) and not row.default:
				checks.append(Check("form.hidden_mandatory", "critical", _("{0} is hidden and mandatory without a default").format(row.fieldname)))
			if row.fieldtype == "Link" and row.options:
				if guest and not frappe.has_permission(row.options, "read", user="Guest"):
					checks.append(Check("form.guest_link", "critical",
						_("{0} links to {1}: the form would list every {1} name to anonymous visitors (web_form.py::get_link_options)").format(row.fieldname, row.options)))
				elif not guest and cint(row.allow_read_on_all_link_options):
					checks.append(Check("form.link_all_options", "warning",
						_("{0}: every signed-in user sees all {1} names").format(row.fieldname, row.options)))
		missing = [df.fieldname for df in meta.fields if cint(df.reqd) and df.fieldname not in names
			and df.fieldtype not in no_value_fields and df.fieldtype not in ("Select", "Check") and not df.default
			and not df.fetch_from and df.fieldname != "naming_series"]
		if missing:
			checks.append(Check("form.mandatory_missing", "warning",
				_("{0} requires {1}, which the form does not ask for: submissions fail unless they get a default").format(doc_type, ", ".join(missing[:10]))))

		route = cstr(doc.route)
		url = frappe.utils.get_url(route + "/new") if route else "-"
		if guest:
			if not spec.get("allow_guest"):
				checks.append(Check("form.guest", "critical",
					_("login_required 0 lets anyone on the internet create {0} records; send allow_guest: true only if the user explicitly wants a public form").format(doc_type)))
			else:
				checks.append(Check("form.guest", "warning", _("Anyone on the internet, without signing in, can create {0} records").format(doc_type)))
			sensitive = [df.fieldname for df in meta.fields if df.fieldtype in SENSITIVE_TYPES
				or (df.fieldtype == "Link" and df.options in SENSITIVE_LINKS)]
			if sensitive:
				checks.append(Check("form.guest_sensitive", "warning",
					_("{0} has sensitive fields ({1}); a public form on it is risky").format(doc_type, ", ".join(sensitive[:8]))))
			if cint(doc.allow_multiple):
				checks.append(Check("form.allow_multiple", "warning", _("allow_multiple: one visitor can submit any number of records (only Frappe's rate limit of 10 per minute applies)")))
			if spec.get("forced"):
				checks.append(Check("form.forced", "info", _("A guest form cannot edit or list records: {0} set to 0").format(", ".join(spec["forced"]))))
			impact.append(_("The internet can create {0} records at {1}").format(doc_type, url))
		else:
			impact.append(_("Every signed-in user, website users included, can create {0} records at {1}; Frappe saves them with ignore_permissions").format(doc_type, url))
			if not cint(frappe.db.get_single_value("Website Settings", "disable_signup")):
				checks.append(Check("form.signup_open", "warning",
					_("Sign-up is open on this site, so 'login required' still means anyone who registers")))
			if cint(doc.allow_edit):
				impact.append(_("Users can edit the {0} records they created through the form").format(doc_type))
		if doc.success_url and _is_external(doc.success_url):
			checks.append(Check("form.success_url", "warning", _("After submitting, visitors are sent to another site: {0}").format(doc.success_url)))
		for field in ("introduction_text", "success_message"):
			if cstr(doc.get(field)).strip():
				html_checks, links = scan_html(doc.get(field), spec.get("allow_scripts"), field)
				checks += html_checks
				if links:
					preview.setdefault("external_links", [])
					preview["external_links"] += [u for u in links if u not in preview["external_links"]]
		if cstr(doc.custom_css).strip():
			css_checks, links = scan_css(doc.custom_css, spec.get("allow_scripts"), "custom_css")
			checks += css_checks
			preview.setdefault("external_links", [])
			preview["external_links"] += [u for u in links if u not in preview["external_links"]]
		if cstr(doc.client_script).strip():
			from ai_agent.customization.analyzers import js_code

			level = "warning" if guest else "info"
			checks.append(Check("form.client_script", level, _("client_script runs in the browser of every visitor of the form")))
			checks += js_code.analyze(doc.client_script, "Web", doc_type)
			preview["code"] = {"language": "javascript", "text": cstr(doc.client_script)}
		if not preview.get("external_links"):
			preview.pop("external_links", None)
		preview["doc_type"] = doc_type
		preview["login_required"] = cint(doc.login_required)
		preview["fields"] = [{k: r.get(k) for k in ("fieldname", "label", "fieldtype", "reqd") if r.get(k) not in (None, "")} for r in rows]
		return checks

	def _preview(self, doc, spec, user, preview):
		route = cstr(doc.route)
		guest_view = spec["doctype"] == "Web Page" or not cint(doc.login_required)
		viewer = "Guest" if guest_view else user
		render_doc = frappe.get_doc(doc.as_dict())
		render_doc.name = doc.name
		form_dict = None
		path = route
		if spec["doctype"] == "Web Form":
			path = route + "/new"
			form_dict = {"is_new": True}
			render_doc.allow_multiple = 1  # show the empty form, not the viewer's earlier submission
			for row in render_doc.web_form_fields:
				if row.fieldtype == "Link":  # Link options are read by the saved form's name; text in the preview
					row.fieldtype = "Data"
		try:
			html = render_page(render_doc, path, viewer, form_dict)
		except frappe.Redirect:
			return [Check("web.preview", "info", _("The page redirects to {0}").format(frappe.local.flags.get("redirect_location") or "?"))]
		except Exception as e:
			message = frappe.utils.strip_html(str(e))[:300] or e.__class__.__name__
			return [Check("web.preview", "critical", _("The page does not render: {0}").format(message))]
		markers = ('<div class="web-form', '<form', '<main', 'class="page-content', '<body') if spec["doctype"] == "Web Form" \
			else ('<main', 'class="page-content', 'class="page_content', '<body')
		preview["html"] = _excerpt(html, markers)
		preview["html_chars"] = len(html)
		preview["rendered_as"] = viewer
		return [Check("web.preview", "ok", _("The page renders for {0} ({1} characters of HTML)").format(viewer, len(html)))]

	def _diff(self, current, doc):
		rows = []
		for key in self._value_keys(doc.doctype):
			before, after = cstr(current.get(key)), cstr(doc.get(key))
			if before != after and not key.startswith("main_section"):
				rows.append({"field": key, "label": key, "before": before[:300], "after": after[:300]})
		if doc.doctype == "Web Form":
			before, after = self._rows(current), self._rows(doc)
			if before != after:
				rows.append({"field": "web_form_fields", "label": "form_fields",
					"before": ", ".join(r.get("fieldname") or r.get("fieldtype", "") for r in before),
					"after": ", ".join(r.get("fieldname") or r.get("fieldtype", "") for r in after)})
		return rows

	# -- execute / verify ----------------------------------------------------------------------------
	def _create_spec(self, doc):
		"""A create spec that rebuilds ``doc`` unpublished (the undo of a delete)."""
		state = self._state(doc)
		values = {k: state[k] for k in self._value_keys(doc.doctype) if state.get(k) not in ("", None)}
		values["published"] = 0
		spec = {"operation": "create", "doctype": doc.doctype, "name": doc.name, "values": values,
			"allow_scripts": True, "purpose": _("restore {0}").format(doc.name)}
		if doc.doctype == "Web Form":
			spec["fields"] = state["fields"]
			spec["allow_guest"] = not cint(doc.login_required)
		return spec

	def execute(self, spec, user, approval=None):
		approval = _base.approval_name(approval)
		doctype, operation, name = spec["doctype"], spec["operation"], spec["name"]
		with _base.as_user(user):
			if operation == "create":
				doc, _current = self._effective(spec)
				doc.insert()
				undo = {"operation": "delete", "doctype": doctype, "name": doc.name}
				if cint(doc.published):
					undo["of_create"] = True  # set by the kind only (normalize never accepts it)
			elif operation == "delete":
				previous = frappe.get_doc(doctype, name)
				undo = self._create_spec(previous)
				frappe.delete_doc(doctype, name)
				return {"targets": [{"doctype": doctype, "name": name}], "undo": undo, "deleted": True}
			elif operation in ("publish", "unpublish"):
				doc = frappe.get_doc(doctype, name)
				doc.published = 1 if operation == "publish" else 0
				doc.save()
				undo = {"operation": "unpublish" if operation == "publish" else "publish", "doctype": doctype, "name": name}
				if operation == "unpublish" and doctype == "Web Form" and not cint(doc.login_required):
					undo["allow_guest"] = True
				if operation == "unpublish" and doctype == "Web Page":
					undo["allow_scripts"] = True  # republishing restores what was there; the checks still list it
			else:
				doc = frappe.get_doc(doctype, name)
				previous = self._state(doc)
				undo = {"operation": "update", "doctype": doctype, "name": name, "values": {},
					"allow_scripts": True, "purpose": _("undo of {0}").format(approval or spec.get("purpose") or "")}
				for key in spec.get("values") or {}:
					undo["values"][key] = previous.get(key)
				if "fields" in spec:
					undo["fields"] = previous["fields"]
				if doctype == "Web Form":
					undo["allow_guest"] = not cint(previous.get("login_required"))
				doc.update(spec.get("values") or {})
				if "fields" in spec:
					doc.set("web_form_fields", [])
					for row in spec["fields"]:
						doc.append("web_form_fields", row)
				doc.save()
		from frappe.website.utils import clear_cache

		clear_cache(cstr(doc.route) or None)
		return {"targets": [{"doctype": doctype, "name": doc.name}], "undo": undo,
			"published": cint(doc.published), "route": cstr(doc.route),
			"sha256": _base.sha256(json.dumps(self._state(doc), sort_keys=True, default=str))}

	def verify(self, spec, result):
		doctype, name = spec["doctype"], (result.get("targets") or [{}])[0].get("name") or spec.get("name")
		if spec["operation"] == "delete":
			if frappe.db.exists(doctype, name):
				return [Check("verify.deleted", "critical", _("{0} {1} still exists").format(doctype, name))]
			return [Check("verify.deleted", "ok", _("{0} {1} is deleted").format(doctype, name))]
		if not frappe.db.exists(doctype, name):
			return [Check("verify.exists", "critical", _("{0} {1} does not exist").format(doctype, name))]
		doc = frappe.get_doc(doctype, name)
		checks = []
		expected = {"publish": 1, "unpublish": 0}.get(spec["operation"])
		if expected is None and "published" in (spec.get("values") or {}):
			expected = cint(spec["values"]["published"])
		if expected is not None and cint(doc.published) != expected:
			checks.append(Check("verify.published", "critical", _("{0} is not {1}").format(name, _("published") if expected else _("unpublished"))))
		if result.get("sha256") and _base.sha256(json.dumps(self._state(doc), sort_keys=True, default=str)) != result["sha256"]:
			checks.append(Check("verify.content", "critical", _("The stored {0} differs from the approved one").format(doctype)))
		if cstr(doc.get("context_script")).strip() and spec["operation"] == "create":
			checks.append(Check("verify.context_script", "critical", _("{0} has a context_script").format(name)))
		if cint(doc.published) and spec["operation"] in ("create", "update", "publish"):
			for problem in route_conflicts(cstr(doc.route), doctype, name):
				checks.append(Check("verify.route", "critical", problem))
		if not checks:
			state = _("published at {0}").format(frappe.utils.get_url(doc.route)) if cint(doc.published) else _("unpublished")
			checks.append(Check("verify.saved", "ok", _("{0} is saved as approved ({1})").format(name, state)))
		return checks

	def summary(self, spec):
		verb = {"create": _("Create"), "update": _("Update"), "publish": _("Publish"), "unpublish": _("Unpublish"),
			"delete": _("Delete")}[spec["operation"]]
		published = (spec.get("values") or {}).get("published")
		suffix = _(" (published)") if spec["operation"] in ("create", "update") and published else ""
		return _("{0} {1} {2}{3}").format(verb, _(spec["doctype"]), spec.get("name"), suffix)

	def get_source(self, name, user=None, doctype=None):
		doctype = doctype or ("Web Page" if frappe.db.exists("Web Page", name) else "Web Form")
		doc = frappe.get_doc(doctype, name)
		state = self._state(doc)
		out = {"kind": self.name, "doctype": doctype, "name": doc.name, **state}
		if doctype == "Web Page":
			out["content"] = cstr(doc.get(CONTENT_FIELDS.get(doc.content_type, "main_section")))
		out["url"] = frappe.utils.get_url(doc.route) if doc.route else None
		return out


def route_conflicts(route, doctype, own_name):
	"""Why ``route`` cannot be used by ``doctype`` ``own_name`` (empty when it is free)."""
	from werkzeug.exceptions import MethodNotAllowed, NotFound
	from werkzeug.routing import Map, Rule

	from frappe.website.path_resolver import get_website_rules
	from frappe.website.router import get_doctypes_with_web_view, get_pages

	route = cstr(route).strip("/")
	out = []
	first = route.split("/", 1)[0].lower()
	if first in RESERVED_PREFIXES:
		out.append(_("/{0} belongs to Desk or the server (/{1}…)").format(route, first))
		return out
	# www pages of every installed app (TemplatePage); a Web Page/Form would replace them (renderer order)
	if route in get_pages():
		out.append(_("/{0} is a page of an installed app (for example the login or contact page)").format(route))
	# website_route_rules from hooks + DocType routes: the path is rewritten before any page is looked up
	try:
		adapter = Map([Rule(r["from_route"], endpoint=r["to_route"]) for r in get_website_rules()]).bind("localhost")
		endpoint, _args = adapter.match("/" + route)
		out.append(_("/{0} is taken by the route rule of {1}").format(route, endpoint))
	except (NotFound, MethodNotAllowed):
		pass
	except Exception:
		frappe.log_error(frappe.get_traceback(), "ai_agent customize: web route check could not read the route rules")
	# redirects (hooks + Website Settings): the visitor would be sent elsewhere (path_resolver.resolve_redirect)
	redirects = list(frappe.get_hooks("website_redirects") or [])
	redirects += [{"source": r.source} for r in (frappe.get_website_settings("route_redirects") or [])]
	for rule in redirects:
		with contextlib.suppress(re.error, TypeError):
			if re.match(cstr(rule.get("source")).strip("/ ") + "$", route):
				out.append(_("/{0} is redirected by the rule {1}").format(route, rule.get("source")))
				break
	# other Web Pages / Web Forms / web-view documents
	for row in frappe.get_all("Web Page", filters={"route": route}, pluck="name"):
		if not (doctype == "Web Page" and row == own_name):
			out.append(_("/{0} is already the route of Web Page {1}").format(route, row))
	for row in frappe.get_all("Web Form", fields=["name", "route"]):
		other = cstr(row.route).strip("/")
		if not other or (doctype == "Web Form" and row.name == own_name):
			continue
		# a Web Form serves route, route/new, route/list, route/<name>, route/<name>/edit (router.get_page_info_from_web_form)
		if route == other or route.startswith(other + "/") or (doctype == "Web Form" and other.startswith(route + "/")):
			out.append(_("/{0} overlaps Web Form {1} (/{2}/…)").format(route, row.name, other))
	if doctype == "Web Form":
		for row in frappe.get_all("Web Page", filters={"route": ["like", route + "/%"]}, pluck="name"):
			out.append(_("/{0} would hide Web Page {1} under it").format(route, row))
	for other_doctype in get_doctypes_with_web_view():
		if other_doctype in TYPES:
			continue
		with contextlib.suppress(Exception):
			name = frappe.db.get_value(other_doctype, {"route": route}, "name")
			if name:
				out.append(_("/{0} is already the page of {1} {2}").format(route, other_doctype, name))
	return out


KIND = WebKind()
