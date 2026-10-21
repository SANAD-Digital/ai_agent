"""Client Scripts for a form or list (docs/customization/01 §3.3.3).

Checked with node + acorn (``analyzers.js_code``): syntax, the target DocType, no eval/new Function,
no cookies/storage/csrf/boot, no network or navigation off the site, HTML injection warnings, and
every ``frappe.call`` method listed (missing = critical, data-changing = warning).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr

from ai_agent.customization.analyzers import js_code
from ai_agent.customization.contract import Check, CustomizationError
from ai_agent.customization.kinds import _base
from ai_agent.customization.kinds._script import ScriptKind


class ClientScriptKind(ScriptKind):
	name = "client_script"
	setting = "allow_customize_client_scripts"
	required_roles = ("System Manager",)
	doctype = "Client Script"
	tool = "customize.request_client_script"
	comment = "//"
	allowed_keys = ("operation", "name", "doctype", "view", "script", "purpose")
	record_fields = ("dt", "view")
	enabled_field = "enabled"

	def normalize(self, args, user):
		args = dict(args or {})
		_base.reject_unknown(args, self.allowed_keys, self.tool)
		if args.get("view") and args["view"] not in ("Form", "List"):
			raise CustomizationError(_("view must be Form or List"))
		spec = super().normalize({k: v for k, v in args.items() if k != "doctype"}, user)
		if args.get("doctype") and spec["operation"] in ("create", "update"):
			spec["dt"] = cstr(args["doctype"]).strip()
		if spec["operation"] == "create":
			_base.require(spec, ("dt",), self.tool + " (doctype)")
			spec.setdefault("view", "Form")
			if not args.get("name"):
				spec["name"] = self.default_name(spec)
		return spec

	def normalize_create(self, spec, user):
		# dt is set after the base normalize; the name is fixed again in normalize()
		spec.setdefault("name", "-")
		super().normalize_create(spec, user)

	def default_name(self, spec):
		return f"{spec.get('dt') or ''} - {spec.get('purpose') or ''}".strip(" -")[:140]

	def check(self, spec, user):
		checks = _base.role_checks(user, self.required_roles, _("Frappe requires it to save Client Scripts"))
		checks += self.common_checks(spec)
		merged, _current = self.effective(spec)
		doctype, view = merged.get("dt"), merged.get("view") or "Form"
		reason = _base.customizable_reason(doctype, allow_custom=True) if doctype else _("No DocType given")
		if reason and spec["operation"] in ("create", "update"):
			checks.append(Check("script.doctype", "critical", reason))
		if spec["operation"] in ("create", "update") and not reason:
			checks += js_code.analyze(merged.get("script"), view, doctype)
		impact = []
		if doctype and not reason:
			users = _base.read_user_count(doctype)
			where = _("form") if view == "Form" else _("list")
			impact.append(_("Runs in the browser of everyone who opens the {0} {1} (~{2} users)").format(doctype, where, users))
			impact.append(_("It changes the screen only: imports, REST and other scripts are not affected"))
		preview = {"name": spec.get("name"), "doctype": doctype, "view": view, "enabled": merged.get("enabled"),
			"code": self.code_preview(spec, "javascript")}
		return _base.build_report(checks, impact=impact, preview=preview, risk="medium")

	def summary(self, spec):
		verb = {"create": _("Create"), "update": _("Update"), "enable": _("Enable"), "disable": _("Disable")}[spec["operation"]]
		return _("{0} client script {1}").format(verb, spec.get("name"))

	def after_execute(self, doc):
		frappe.clear_cache(doctype=doc.dt)


KIND = ClientScriptKind()
