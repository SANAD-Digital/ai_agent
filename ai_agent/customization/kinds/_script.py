"""Shared lifecycle of the two script kinds (Client Script, Server Script).

create / update / enable / disable; a provenance header on line 1; snapshot = the record's state by
name (so the request, the execution and the undo of one script compare the same thing); undo:
create -> disable (D10), update -> the previous version, enable <-> disable.
"""

from __future__ import annotations

import difflib
import re

import frappe
from frappe import _
from frappe.utils import cint, cstr

from ai_agent.customization.contract import Check, CustomizationError
from ai_agent.customization.kinds import _base

OPERATIONS = ("create", "update", "enable", "disable")
MAX_CHARS = 20000


class ScriptKind:
	doctype = ""  # "Client Script" | "Server Script"
	tool = ""
	comment = "#"
	allowed_keys = ()
	record_fields = ()  # fields copied from spec to the record (besides script)
	enabled_field = "enabled"  # Client Script: enabled=1; Server Script: disabled=0

	# -- helpers --------------------------------------------------------------------
	def is_enabled(self, doc):
		value = cint(doc.get(self.enabled_field))
		return bool(value) if self.enabled_field == "enabled" else not value

	def enabled_values(self, enabled):
		return {self.enabled_field: (1 if enabled else 0) if self.enabled_field == "enabled" else (0 if enabled else 1)}

	def default_name(self, spec):
		raise NotImplementedError

	def with_header(self, script, purpose, requested_by, approved_by=None, approval=None):
		return _base.header(self.comment, purpose, requested_by, approved_by, approval) + "\n" + _base.strip_header(script).lstrip("\n")

	# -- normalize ----------------------------------------------------------------------
	def normalize(self, args, user):
		args = dict(args or {})
		_base.reject_unknown(args, self.allowed_keys, self.tool)
		_base.require(args, ("operation",), self.tool)
		operation = args["operation"]
		if operation not in OPERATIONS:
			raise CustomizationError(_("operation must be one of: {0}").format(", ".join(OPERATIONS)))
		spec = {"operation": operation}
		for key in self.allowed_keys:
			if key in ("operation",) or args.get(key) in (None, ""):
				continue
			spec[key] = args[key]
		if spec.get("script") is not None:
			spec["script"] = cstr(spec["script"])
			if len(spec["script"]) > MAX_CHARS:
				raise CustomizationError(_("The script is longer than {0} characters").format(MAX_CHARS))
		spec["requested_by"] = user
		if operation == "create":
			self.normalize_create(spec, user)
		else:
			_base.require(spec, ("name",), self.tool)
			if not frappe.db.exists(self.doctype, spec["name"]):
				raise CustomizationError(_("There is no {0} named {1}").format(self.doctype, spec["name"]),
					hints=[_("customize.inspect lists the scripts of a DocType")])
			if operation == "update":
				self.normalize_update(spec, user)
			else:
				spec = {"operation": operation, "name": spec["name"], "requested_by": user,
					**({"purpose": spec["purpose"]} if spec.get("purpose") else {})}
		return spec

	def normalize_create(self, spec, user):
		_base.require(spec, ("script", "purpose"), self.tool)
		spec["purpose"] = re.sub(r"\s+", " ", cstr(spec["purpose"])).strip()[:140]
		if not spec.get("name"):
			spec["name"] = self.default_name(spec)
		spec["name"] = re.sub(r"\s+", " ", cstr(spec["name"])).strip()[:140]
		spec["script"] = self.with_header(spec["script"], spec["purpose"], user)

	def normalize_update(self, spec, user):
		doc = frappe.get_doc(self.doctype, spec["name"])
		changes = [k for k in self.record_fields + ("script",) if k in spec]
		if not changes:
			raise CustomizationError(_("Nothing to update: send the new script or settings"))
		spec.setdefault("purpose", _base.header_purpose(doc.script) or _("update"))
		if "script" in spec:
			spec["script"] = self.with_header(spec["script"], spec["purpose"], user)

	# -- snapshot ----------------------------------------------------------------------
	def snapshot(self, spec):
		name = spec.get("name")
		if not name or not frappe.db.exists(self.doctype, name):
			return {"doctype": self.doctype, "name": name, "exists": False}
		doc = frappe.get_doc(self.doctype, name)
		out = {"doctype": self.doctype, "name": name, "exists": True, "enabled": self.is_enabled(doc),
			"sha256": _base.sha256(doc.script)}
		for key in self.record_fields:
			out[key] = cstr(doc.get(key)) if not isinstance(doc.get(key), int) else doc.get(key)
		return out

	# -- shared parts of check ------------------------------------------------------------
	def common_checks(self, spec):
		checks = []
		exists = frappe.db.exists(self.doctype, spec.get("name"))
		if spec["operation"] == "create" and exists:
			checks.append(Check("script.name_taken", "critical", _("A {0} named {1} already exists; use operation update").format(self.doctype, spec["name"])))
		if spec["operation"] != "create" and not exists:
			checks.append(Check("script.missing", "critical", _("There is no {0} named {1}").format(self.doctype, spec.get("name"))))
		if spec["operation"] in ("enable", "disable") and exists:
			doc = frappe.get_doc(self.doctype, spec["name"])
			if self.is_enabled(doc) == (spec["operation"] == "enable"):
				checks.append(Check("script.no_change", "info", _("{0} is already {1}d").format(spec["name"], spec["operation"])))
		return checks

	def effective(self, spec):
		"""The record as it would be after the change (dict), for analysis and preview."""
		current = {}
		if spec["operation"] != "create" and frappe.db.exists(self.doctype, spec.get("name")):
			doc = frappe.get_doc(self.doctype, spec["name"])
			current = {k: doc.get(k) for k in self.record_fields + ("script",)}
			current["enabled"] = self.is_enabled(doc)
		merged = {**current, **{k: v for k, v in spec.items() if k in self.record_fields + ("script",)}}
		if spec["operation"] in ("enable", "disable"):
			merged["enabled"] = spec["operation"] == "enable"
		elif spec["operation"] == "create":
			merged["enabled"] = True
		return merged, current

	def code_preview(self, spec, language):
		merged, current = self.effective(spec)
		text = cstr(merged.get("script"))
		code = {"language": language, "text": text}
		if spec["operation"] == "update" and "script" in spec:
			code["diff"] = "".join(difflib.unified_diff(
				cstr(current.get("script")).splitlines(True), text.splitlines(True), "current", "proposed"))
		return code

	# -- execute / verify ---------------------------------------------------------------
	def record_values(self, spec):
		return {k: spec[k] for k in self.record_fields if k in spec}

	def execute(self, spec, user, approval=None):
		approval = _base.approval_name(approval)
		before = self.snapshot(spec)
		previous = frappe.get_doc(self.doctype, spec["name"]) if before.get("exists") else None
		with _base.as_user(user):
			if spec["operation"] == "create":
				doc = frappe.get_doc({"doctype": self.doctype, "name": spec["name"], **self.record_values(spec),
					**self.enabled_values(True),
					"script": self.with_header(spec["script"], spec.get("purpose"), spec.get("requested_by"), user, approval)})
				doc.insert()
				undo = {"operation": "disable", "name": doc.name}
			elif spec["operation"] == "update":
				doc = frappe.get_doc(self.doctype, spec["name"])
				values = self.record_values(spec)
				if "script" in spec:
					values["script"] = self.with_header(spec["script"], spec.get("purpose"), spec.get("requested_by"), user, approval)
				undo = {"operation": "update", "name": doc.name, "purpose": _("undo of {0}").format(approval or spec.get("purpose") or "")}
				for key in values:
					undo[key] = _base.strip_header(previous.get(key)) if key == "script" else previous.get(key)
				doc.update(values)
				doc.save()
			else:
				doc = frappe.get_doc(self.doctype, spec["name"])
				doc.update(self.enabled_values(spec["operation"] == "enable"))
				doc.save()
				undo = {"operation": "disable" if spec["operation"] == "enable" else "enable", "name": doc.name}
		self.after_execute(doc)
		return {"targets": [{"doctype": self.doctype, "name": doc.name}], "undo": undo,
			"sha256": _base.sha256(doc.script), "enabled": self.is_enabled(doc)}

	def after_execute(self, doc):
		pass

	def verify(self, spec, result):
		name = (result.get("targets") or [{}])[0].get("name") or spec.get("name")
		if not frappe.db.exists(self.doctype, name):
			return [Check("verify.exists", "critical", _("{0} {1} does not exist").format(self.doctype, name))]
		doc = frappe.get_doc(self.doctype, name)
		checks = []
		expected_enabled = {"create": True, "enable": True, "disable": False}.get(spec["operation"], result.get("enabled"))
		if expected_enabled is not None and self.is_enabled(doc) != expected_enabled:
			checks.append(Check("verify.enabled", "critical", _("{0} is not {1}").format(name, _("enabled") if expected_enabled else _("disabled"))))
		if result.get("sha256") and _base.sha256(doc.script) != result["sha256"]:
			checks.append(Check("verify.script", "critical", _("The stored script differs from the approved one")))
		checks += self.extra_verify(spec, doc)
		if not checks:
			checks.append(Check("verify.script", "ok", _("{0} is saved as approved").format(name)))
		return checks

	def extra_verify(self, spec, doc):
		return []

	def get_source(self, name, user=None):
		doc = frappe.get_doc(self.doctype, name)
		out = {"kind": self.name, "name": doc.name, "enabled": self.is_enabled(doc), "script": doc.script,
			"ai_made": _base.is_ai_made(doc.script)}
		for key in self.record_fields:
			out[key] = doc.get(key)
		return out
