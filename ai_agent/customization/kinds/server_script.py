"""Server Scripts: DocType Event and API, never for guests (docs/customization/01 §3.3.4).

Checked by ``analyzers.python_code`` (compile + RestrictedPython + AST rules, critical on network,
mail, queues, imports, permission bypass, protected DocTypes; warnings on unchecked reads and writes)
and, for ``Before *`` events, a trial run on a copy of a readable document inside a rolled-back
savepoint. Needs Script Manager (``ServerScript.validate`` -> ``frappe.only_for``) and server scripts
enabled in common_site_config (``server_script_enabled``); this module never changes that setting.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import add_days, cint, nowdate

from ai_agent.customization.analyzers import python_code
from ai_agent.customization.contract import Check, CustomizationError
from ai_agent.customization.kinds import _base
from ai_agent.customization.kinds._script import ScriptKind

SCRIPT_TYPES = ("DocType Event", "API")
EVENTS = ("Before Insert", "Before Validate", "Before Save", "After Insert", "After Save", "Before Submit",
	"After Submit", "Before Cancel", "After Cancel", "Before Delete", "After Delete",
	"Before Save (Submitted Document)", "After Save (Submitted Document)", "Before Print")
API_METHOD_RE = re.compile(r"^[a-z][a-z0-9_.]{2,80}$")


def server_scripts_enabled():
	from frappe.utils.safe_exec import is_safe_exec_enabled

	return is_safe_exec_enabled()


class ServerScriptKind(ScriptKind):
	name = "server_script"
	setting = "allow_customize_server_scripts"
	required_roles = ("System Manager", "Script Manager")
	doctype = "Server Script"
	tool = "customize.request_server_script"
	comment = "#"
	allowed_keys = ("operation", "name", "script_type", "reference_doctype", "doctype_event", "api_method", "script",
		"enable_rate_limit", "rate_limit_count", "rate_limit_seconds", "purpose", "allow_guest")
	record_fields = ("script_type", "reference_doctype", "doctype_event", "api_method", "enable_rate_limit",
		"rate_limit_count", "rate_limit_seconds", "allow_guest")
	enabled_field = "disabled"

	def normalize(self, args, user):
		args = dict(args or {})
		forced = []
		if "allow_guest" in args:
			if cint(args.get("allow_guest")):
				forced.append("allow_guest")
			args.pop("allow_guest")
		spec = super().normalize(args, user)
		if spec["operation"] in ("create", "update"):
			for key in ("enable_rate_limit", "rate_limit_count", "rate_limit_seconds"):
				if key in spec:
					spec[key] = cint(spec[key])
			spec["allow_guest"] = 0
		if forced:
			spec["forced"] = forced
		if spec["operation"] == "create":
			spec.setdefault("script_type", "DocType Event")
			if spec["script_type"] not in SCRIPT_TYPES:
				raise CustomizationError(_("script_type must be DocType Event or API"),
					hints=[_("Scheduler Event, Permission Query and Workflow Task are not available to the agent")])
			if spec["script_type"] == "DocType Event":
				_base.require(spec, ("reference_doctype", "doctype_event"), self.tool)
				spec.pop("api_method", None)
			else:
				_base.require(spec, ("api_method",), self.tool)
				spec.pop("reference_doctype", None)
				spec.pop("doctype_event", None)
		elif spec["operation"] == "update" and spec.get("script_type") and spec["script_type"] not in SCRIPT_TYPES:
			raise CustomizationError(_("script_type must be DocType Event or API"))
		if spec.get("doctype_event") and spec["doctype_event"] not in EVENTS:
			raise CustomizationError(_("doctype_event {0} is not available").format(spec["doctype_event"]),
				hints=[", ".join(EVENTS)])
		if spec.get("api_method") and not API_METHOD_RE.match(spec["api_method"]):
			raise CustomizationError(_("api_method must match ^[a-z][a-z0-9_.]{2,80}$ (it becomes /api/method/<api_method>)"))
		return spec

	def normalize_create(self, spec, user):
		spec.setdefault("script_type", "DocType Event")
		super().normalize_create(spec, user)

	def default_name(self, spec):
		if spec.get("script_type") == "API":
			return spec.get("api_method") or spec.get("purpose")
		return f"{spec.get('reference_doctype')} - {spec.get('doctype_event')} - {spec.get('purpose')}"[:140]

	def record_values(self, spec):
		values = super().record_values(spec)
		if spec["operation"] in ("create", "update"):
			values["allow_guest"] = 0
		return values

	def check(self, spec, user):
		checks = _base.role_checks(user, self.required_roles, _("Frappe allows only Script Managers to save Server Scripts"))
		if not server_scripts_enabled():
			checks.append(Check("script.server_scripts_disabled", "critical",
				_("Server Scripts are disabled on this server (bench set-config -g server_script_enabled 1)")))
		checks += self.common_checks(spec)
		for key in spec.get("forced") or []:
			checks.append(Check("script.allow_guest", "warning", _("{0} is not allowed for agent scripts and was set to 0 (only logged-in users)").format(key)))
		merged, current = self.effective(spec)
		script_type = merged.get("script_type") or "DocType Event"
		if spec["operation"] != "disable" and script_type not in SCRIPT_TYPES:
			checks.append(Check("script.type", "critical", _("{0} scripts cannot be changed or enabled by the agent").format(script_type)))
		if spec["operation"] == "enable" and cint(current.get("allow_guest")):
			checks.append(Check("script.allow_guest", "critical", _("{0} allows guests and cannot be enabled by the agent").format(spec["name"])))
		reference = merged.get("reference_doctype")
		event = merged.get("doctype_event")
		impact = []

		if script_type == "DocType Event":
			reason = _base.customizable_reason(reference, allow_custom=True) if reference else _("No reference_doctype given")
			if reason:
				checks.append(Check("script.doctype", "critical", reason))
			else:
				monthly = frappe.db.count(reference, {"creation": (">=", add_days(nowdate(), -30))})
				impact.append(_("Runs on {0} for every {1} (~{2} new in the last 30 days), for every user, also in imports and the REST API").format(event, reference, monthly))
				others = [n for n in self._event_scripts(reference, event) if n != spec.get("name")]
				if others:
					checks.append(Check("script.same_event", "info", _("Other enabled scripts on the same event: {0}").format(", ".join(others))))
		else:
			method = merged.get("api_method")
			taken = frappe.get_all("Server Script", filters={"api_method": method, "name": ("!=", spec.get("name") or "")}, pluck="name")
			if taken:
				checks.append(Check("script.api_taken", "critical", _("/api/method/{0} is already served by {1}").format(method, ", ".join(taken))))
			impact.append(_("Adds /api/method/{0}: any logged-in user can call it, and it runs with their permissions").format(method))
			if not cint(merged.get("enable_rate_limit")):
				checks.append(Check("script.rate_limit", "info", _("Consider enable_rate_limit for an API script")))

		code_ok = True
		if spec["operation"] in ("create", "update"):
			analysis = python_code.analyze(merged.get("script"), script_type, reference)
			checks += analysis
			code_ok = not any(c.level == "critical" for c in analysis)

		preview = {"name": spec.get("name"), "script_type": script_type, "reference_doctype": reference,
			"doctype_event": event, "api_method": merged.get("api_method"), "enabled": merged.get("enabled"),
			"code": self.code_preview(spec, "python")}
		if (code_ok and spec["operation"] in ("create", "update") and script_type == "DocType Event"
				and reference and not _base.customizable_reason(reference, allow_custom=True)
				and server_scripts_enabled() and (event or "").startswith("Before")):
			try:
				trial = python_code.dry_run(merged.get("script"), reference, event, user)
			except Exception as e:
				trial = {"ran": False, "checks": [Check("py.dry_run_error", "warning", _("The trial run could not start: {0}").format(str(e)[:200]))]}
			checks += trial.pop("checks", [])
			preview["dry_run"] = trial
		return _base.build_report(checks, impact=impact, preview=preview, risk="high")

	@staticmethod
	def _event_scripts(reference, event):
		from frappe.core.doctype.server_script.server_script_utils import get_server_script_map

		return list((get_server_script_map().get(reference) or {}).get(event) or [])

	def after_execute(self, doc):
		frappe.client_cache.delete_value("server_script_map")
		if doc.reference_doctype:
			frappe.clear_cache(doctype=doc.reference_doctype)

	def extra_verify(self, spec, doc):
		from frappe.core.doctype.server_script.server_script_utils import get_server_script_map

		if doc.disabled:
			return []
		frappe.client_cache.delete_value("server_script_map")
		script_map = get_server_script_map()
		if doc.script_type == "API":
			found = (script_map.get("_api") or {}).get(doc.api_method) == doc.name
		else:
			found = doc.name in ((script_map.get(doc.reference_doctype) or {}).get(doc.doctype_event) or [])
		if not found:
			return [Check("verify.script_map", "critical", _("{0} is not registered for its event or API path").format(doc.name))]
		if cint(doc.allow_guest):
			return [Check("verify.allow_guest", "critical", _("{0} allows guests").format(doc.name))]
		return []

	def summary(self, spec):
		verb = {"create": _("Create"), "update": _("Update"), "enable": _("Enable"), "disable": _("Disable")}[spec["operation"]]
		return _("{0} server script {1}").format(verb, spec.get("name"))


KIND = ServerScriptKind()
