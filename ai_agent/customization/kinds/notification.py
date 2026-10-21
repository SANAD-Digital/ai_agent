"""Notifications: Frappe's Notification DocType (docs/customization/01 §3.1 v2, §4.2).

Frappe rules followed (frappe/email/doctype/notification/notification.py, Frappe 16.28):
- autoname Prompt (notification.json), only System Manager writes it (its permissions).
- ``validate`` (:156-183): subject template for Email/Slack/System Notification, message template,
  ``date_changed`` for Days events, ``datetime_changed`` + ``minutes_offset >= 10`` for Minutes events,
  ``value_changed`` for Value Change, forbidden document types (Email Queue; child tables only with Days
  events), ``validate_condition`` = ``frappe.safe_eval(condition, None, get_context(new_doc))``, and a
  standard notification cannot be enabled outside developer mode.
- Events fire from ``Document.run_notifications`` (frappe/model/document.py:1263): after_insert=New,
  on_update=Save, on_submit=Submit, on_cancel=Cancel, on_change=Value Change, Method = a named doc method;
  Days/Minutes run from the scheduler. ``Custom`` only fires from code (``queue_send``) and is refused here.
- Recipients (``get_list_of_recipients``, :571): ``receiver_by_document_field`` ("field" or "field,child_table"),
  ``receiver_by_role`` (every user with the role), ``cc``/``bcc`` (addresses or Jinja), per-row ``condition``.
  The form offers owner, User links, Customer links and Data fields with options Email (notification.js:36-66).
- ``set_property_after_alert`` makes a notification write to the document after sending: forbidden here.

The agent's own restrictions (design §4.2): channels Email and System Notification, SMS / Slack only when
the site already has them configured; plain cc/bcc addresses that are not site users need
``allow_external: true`` from the requester (then a warning); condition, subject and message are checked
(analyzers.jinja) and evaluated / rendered on a real document inside a rolled-back savepoint.
"""

from __future__ import annotations

import difflib
import json
import re

import frappe
from frappe import _
from frappe.utils import add_days, cint, cstr, nowdate, validate_email_address

from ai_agent.customization.analyzers import jinja as jinja_analyzer
from ai_agent.customization.contract import Check, CustomizationError
from ai_agent.customization.kinds import _base

TOP_KEYS = ("operation", "name", "document_type", "event", "method", "date_changed", "days_in_advance",
	"datetime_changed", "minutes_offset", "value_changed", "channel", "slack_webhook_url", "condition", "subject",
	"message", "recipients", "attach_print", "print_format", "send_system_notification", "send_to_all_assignees",
	"allow_external", "set_property_after_alert", "property_value", "sample_document", "purpose")
OPERATIONS = ("create", "update", "disable", "enable")
EVENTS = ("New", "Save", "Submit", "Cancel", "Days After", "Days Before", "Minutes After", "Minutes Before",
	"Value Change", "Method")
DATE_EVENTS = ("Days After", "Days Before")
MINUTE_EVENTS = ("Minutes After", "Minutes Before")
CHANNELS = ("Email", "System Notification", "SMS", "Slack")
RECIPIENT_KEYS = ("receiver_by_document_field", "receiver_by_role", "cc", "bcc", "condition")
RECORD_FIELDS = ("document_type", "event", "method", "date_changed", "days_in_advance", "datetime_changed",
	"minutes_offset", "value_changed", "channel", "slack_webhook_url", "condition", "subject", "message",
	"attach_print", "print_format", "send_system_notification", "send_to_all_assignees", "recipients")
INT_FIELDS = ("days_in_advance", "minutes_offset", "attach_print", "send_system_notification", "send_to_all_assignees")
NAME_RE = re.compile(r"^[^<>\"'`\\/%#?]{3,140}$")
METHOD_RE = re.compile(r"^[a-z_][a-z0-9_]{2,60}$")
HEADER_RE = re.compile(r"^\s*\{#-?\s*ai_agent:.*?-?#\}\s*\n?", re.S)
FORBIDDEN_DOCUMENT_TYPES = ("Email Queue", "Notification", "Notification Log", "Communication", "Email Account")
PREVIEW_CHARS = 4000
MASS_RECIPIENTS = 50


def strip_header(text):
	return HEADER_RE.sub("", cstr(text), count=1)


def with_header(message, purpose, requested_by, approved_by=None, approval=None):
	line = _base.header("{#", cstr(purpose).replace("#}", ""), requested_by, approved_by, approval, suffix=" #}")
	return line + "\n" + strip_header(message)


def _addresses(text):
	return [a.strip() for a in re.split(r"[,\n;]", cstr(text)) if a.strip()]


def _is_site_user(email):
	return bool(frappe.db.get_value("User", {"name": email, "enabled": 1}) or
		frappe.db.get_value("User", {"email": email, "enabled": 1}))


class NotificationKind:
	name = "notification"
	setting = "allow_customize_notifications"
	required_roles = ("System Manager",)

	# -- normalize --------------------------------------------------------------
	def normalize(self, args, user):
		args = dict(args or {})
		where = "customize.request_notification"
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
		existing = frappe.db.get_value("Notification", name, ["document_type", "is_standard"], as_dict=True)
		if operation == "create":
			if existing:
				raise CustomizationError(_("A Notification named {0} already exists").format(name),
					hints=[_("Use operation update to change it, or choose another name")])
			_base.require(args, ("document_type", "event", "message"), where + " (create)")
			spec.setdefault("purpose", name)
		else:
			if not existing:
				raise CustomizationError(_("There is no Notification {0}").format(name),
					hints=[_("List them with customize.inspect (include notifications)")])
			if operation == "update" and cint(existing.is_standard):
				raise CustomizationError(_("{0} is a standard notification and cannot be changed; disable it and create a copy").format(name))
			if args.get("document_type") and args["document_type"] != existing.document_type:
				raise CustomizationError(_("The DocType of a notification cannot change; create a new one"))
			spec["document_type"] = existing.document_type

		if operation in ("create", "update"):
			for key in RECORD_FIELDS:
				if key not in args or args[key] is None or key == "document_type" and operation == "update":
					continue
				value = args[key]
				if key == "recipients":
					spec[key] = self._recipients(value)
				elif key in INT_FIELDS:
					spec[key] = cint(value)
				elif key in ("condition", "subject", "message"):
					if not isinstance(value, str):
						raise CustomizationError(_("{0} must be text").format(key))
					spec[key] = strip_header(value).strip() if key == "message" else value.strip()
				else:
					spec[key] = cstr(value).strip()
			if operation == "create":
				spec.setdefault("channel", "Email")
			if spec.get("event") is not None and spec["event"] not in EVENTS:
				hint = [_("Custom fires only from code (Notification.queue_send); use a document event")] if spec["event"] == "Custom" else []
				raise CustomizationError(_("event must be one of: {0}").format(", ".join(EVENTS)), hints=hint)
			if spec.get("channel") is not None and spec["channel"] not in CHANNELS:
				raise CustomizationError(_("channel must be one of: {0}").format(", ".join(CHANNELS)))
			if operation == "update" and not any(k in spec for k in RECORD_FIELDS if k != "document_type") \
					and not args.get("set_property_after_alert"):
				raise CustomizationError(_("Nothing to update"))
		for key in ("set_property_after_alert", "property_value"):
			if args.get(key):
				spec[key] = cstr(args[key])  # refused by check() (critical), kept so the card can say why
		if args.get("allow_external") is not None:
			spec["allow_external"] = 1 if cint(args["allow_external"]) else 0
		if args.get("sample_document"):
			spec["sample_document"] = cstr(args["sample_document"])
		return spec

	def _recipients(self, value):
		if not isinstance(value, list):
			raise CustomizationError(_("recipients must be a list of objects"),
				hints=[_('e.g. [{"receiver_by_role": "Accounts Manager"}, {"receiver_by_document_field": "owner"}]')])
		out = []
		for i, row in enumerate(value, 1):
			if not isinstance(row, dict):
				raise CustomizationError(_("recipients[{0}] must be an object").format(i))
			_base.reject_unknown(row, RECIPIENT_KEYS, f"recipients[{i}]")
			clean = {k: cstr(row[k]).strip() for k in RECIPIENT_KEYS if row.get(k) not in (None, "")}
			if not any(clean.get(k) for k in ("receiver_by_document_field", "receiver_by_role", "cc", "bcc")):
				raise CustomizationError(_("recipients[{0}] names nobody: give receiver_by_document_field, receiver_by_role, cc or bcc").format(i))
			out.append(clean)
		return out

	# -- snapshot -----------------------------------------------------------------
	def snapshot(self, spec):
		name = spec["name"]
		if not frappe.db.exists("Notification", name):
			return {"name": name, "exists": False}
		record = self._record(name)
		return {"name": name, "exists": True, "enabled": record.pop("enabled"),
			"message_sha256": _base.sha256(record.pop("message")), "condition_sha256": _base.sha256(record.pop("condition")),
			"set_property_after_alert": cstr(frappe.db.get_value("Notification", name, "set_property_after_alert")),
			**record}

	def _record(self, name):
		doc = frappe.get_doc("Notification", name)
		out = {k: (cint(doc.get(k)) if k in INT_FIELDS else cstr(doc.get(k))) for k in RECORD_FIELDS if k != "recipients"}
		out["message"] = strip_header(doc.message)
		out["recipients"] = [{k: cstr(r.get(k)) for k in RECIPIENT_KEYS if r.get(k)} for r in doc.recipients]
		out["enabled"] = cint(doc.enabled)
		return out

	def _merged(self, spec):
		current = self._record(spec["name"]) if spec["operation"] != "create" and frappe.db.exists("Notification", spec["name"]) else {}
		merged = {**current, **{k: spec[k] for k in RECORD_FIELDS if k in spec}}
		if spec["operation"] != "create":
			merged["set_property_after_alert"] = spec.get("set_property_after_alert") or \
				cstr(frappe.db.get_value("Notification", spec["name"], "set_property_after_alert"))
		else:
			merged["set_property_after_alert"] = spec.get("set_property_after_alert") or ""
		return merged, current

	def _unsaved(self, spec, merged):
		values = {k: merged.get(k) for k in RECORD_FIELDS if k != "recipients" and merged.get(k) not in (None, "")}
		doc = frappe.get_doc({"doctype": "Notification", "name": spec["name"], "enabled": 1, "is_standard": 0,
			"condition_type": "Python", **values,
			"recipients": [dict(r) for r in merged.get("recipients") or []]})
		return doc

	# -- check --------------------------------------------------------------------
	def check(self, spec, user):
		op, name = spec["operation"], spec["name"]
		checks = _base.role_checks(user, self.required_roles, _("Frappe lets only System Managers write Notifications"))
		if op == "create" and frappe.db.exists("Notification", name):
			checks.append(Check("notification.name_taken", "critical", _("A Notification named {0} already exists; use operation update").format(name)))
		merged, current = self._merged(spec)
		dt = merged.get("document_type")
		preview = {"name": name, "operation": op, "document_type": dt}
		if op == "disable":
			return _base.build_report(checks, impact=[_("{0} stops sending").format(name)], preview=preview, risk="low")

		if merged.get("set_property_after_alert"):
			checks.append(Check("notification.set_property", "critical",
				_("set_property_after_alert ({0}) makes the notification write to the document; it is not allowed through the agent").format(merged["set_property_after_alert"])))
		reason = _base.customizable_reason(dt, allow_custom=True)
		if reason:
			checks.append(Check("notification.doctype", "critical", reason))
			return _base.build_report(checks, preview=preview, risk="medium")
		meta = frappe.get_meta(dt)
		event, channel = merged.get("event"), merged.get("channel") or "Email"
		if dt in FORBIDDEN_DOCUMENT_TYPES:
			checks.append(Check("notification.doctype", "critical", _("Notifications on {0} are not allowed").format(dt)))
		if meta.istable and event not in DATE_EVENTS:
			checks.append(Check("notification.doctype", "critical", _("{0} is a child table: only Days Before / Days After work on it (Frappe)").format(dt)))
		if not frappe.has_permission(dt, "read", user=user):
			checks.append(Check("notification.doctype", "critical", _("{0} cannot read {1}").format(user, dt)))
		checks += self._event_checks(merged, meta)
		checks += self._channel_checks(merged, channel)

		# templates and condition
		subject, message, condition = cstr(merged.get("subject")), cstr(merged.get("message")), cstr(merged.get("condition"))
		if channel in ("Email", "System Notification", "Slack") and not subject.strip():
			checks.append(Check("notification.subject", "critical", _("subject is required for the {0} channel").format(channel)))
		if not message.strip():
			checks.append(Check("notification.message", "critical", _("message is required")))
		compiled = True
		for text, where, html in ((subject, "subject", False), (message, "message", channel == "Email")):
			found, ok = jinja_analyzer.compile_checks(text, where)
			checks += found + jinja_analyzer.text_checks(text, where, html=html)
			compiled = compiled and ok
		found, condition_ok = jinja_analyzer.condition_checks(condition, "condition")
		checks += found
		recipient_checks, audience = self._recipient_checks(merged, meta, channel, spec)
		checks += recipient_checks
		checks += self._attach_checks(merged, meta, channel)

		preview.update({"event": event, "channel": channel, "condition": condition, "subject": subject,
			"code": {"language": "jinja", "text": message}, "recipients": merged.get("recipients") or []})
		if op == "update" and "message" in spec:
			preview["code"]["diff"] = "".join(difflib.unified_diff(cstr(current.get("message")).splitlines(True),
				message.splitlines(True), "current", "proposed"))
		if not any(c.level == "critical" for c in checks):
			checks += self._frappe_validate(spec, merged)
		if not any(c.level == "critical" for c in checks):
			checks += self._dry_run(spec, merged, user, preview)
		impact = self._impact(merged, meta, channel, audience, preview)
		risk = "low" if channel == "System Notification" else "medium"
		if any(c.code.startswith("external.") for c in checks):
			risk = "high"
		return _base.build_report(checks, impact=impact, preview=preview, risk=risk)

	def _event_checks(self, merged, meta):
		out = []
		event = merged.get("event")

		def field_of(fieldname, types):
			if fieldname in ("creation", "modified"):
				return True
			df = meta.get_field(fieldname) if fieldname else None
			return bool(df and df.fieldtype in types)

		if event in DATE_EVENTS:
			if not field_of(merged.get("date_changed"), ("Date", "Datetime")):
				out.append(Check("notification.event", "critical", _("{0} needs date_changed: a Date/Datetime field of {1} (or creation/modified)").format(event, meta.name)))
			if cint(merged.get("days_in_advance")) < 0:
				out.append(Check("notification.event", "critical", _("days_in_advance cannot be negative")))
		elif event in MINUTE_EVENTS:
			if not field_of(merged.get("datetime_changed"), ("Datetime",)):
				out.append(Check("notification.event", "critical", _("{0} needs datetime_changed: a Datetime field of {1} (or creation/modified)").format(event, meta.name)))
			if cint(merged.get("minutes_offset")) < 10:
				out.append(Check("notification.event", "critical", _("minutes_offset must be at least 10 (the scheduler runs every few minutes)")))
		elif event == "Value Change":
			df = meta.get_field(cstr(merged.get("value_changed")))
			from frappe.model import no_value_fields

			if not df or df.fieldtype in no_value_fields:
				out.append(Check("notification.event", "critical", _("Value Change needs value_changed: a field of {0} that holds a value").format(meta.name)))
		elif event == "Method":
			if not METHOD_RE.match(cstr(merged.get("method"))):
				out.append(Check("notification.event", "critical", _("Method needs method: the document method name, e.g. on_update_after_submit")))
		elif event in ("Submit", "Cancel") and not meta.is_submittable:
			out.append(Check("notification.event", "critical", _("{0} is not submittable, so {1} never happens").format(meta.name, event)))
		elif not event:
			out.append(Check("notification.event", "critical", _("event is required")))
		return out

	def _channel_checks(self, merged, channel):
		out = []
		if channel == "SMS" and not frappe.db.get_single_value("SMS Settings", "sms_gateway_url"):
			out.append(Check("notification.channel", "critical", _("SMS is not configured on this site (SMS Settings has no gateway); use Email or System Notification")))
		if channel == "Slack":
			url = merged.get("slack_webhook_url")
			if not url or not frappe.db.exists("Slack Webhook URL", url):
				out.append(Check("notification.channel", "critical", _("Slack needs slack_webhook_url naming an existing Slack Webhook URL; none is configured for {0}").format(url or "-")))
		elif merged.get("slack_webhook_url"):
			out.append(Check("notification.channel", "critical", _("slack_webhook_url only goes with the Slack channel")))
		return out

	def _recipient_field_ok(self, meta, spec_text, channel):
		"""(error or None, is_external) for one receiver_by_document_field value."""
		fieldname, _sep, child = spec_text.partition(",")
		fieldname, child = fieldname.strip(), child.strip()
		if fieldname == "owner" and not child:
			return None, False
		target_meta = meta
		if child:
			table = meta.get_field(child)
			if not table or table.fieldtype not in ("Table", "Table MultiSelect"):
				return _("{0} has no child table {1}").format(meta.name, child), False
			target_meta = frappe.get_meta(table.options)
		df = target_meta.get_field(fieldname)
		if not df:
			return _("{0} has no field {1}").format(target_meta.name, fieldname), False
		if channel == "SMS":
			ok = (df.fieldtype in ("Data", "Phone") and (df.options in ("Phone", "Mobile") or df.fieldtype == "Phone")) or \
				(df.fieldtype == "Link" and df.options in ("User", "Customer"))
			return (None, df.options != "User") if ok else (_("{0} is not a phone/mobile field nor a User or Customer link").format(fieldname), False)
		if df.fieldtype == "Link" and df.options == "User":
			return None, False
		if channel == "System Notification":
			return _("System Notification goes to users: use owner or a User link field, not {0}").format(fieldname), False
		if df.fieldtype == "Link" and df.options == "Customer":
			return _("{0} links to a Customer, which is not an email address (Frappe skips it); use the email field, e.g. contact_email").format(fieldname), False
		if df.fieldtype == "Data" and df.options == "Email":
			return None, True
		return _("{0} is not an email field (Data with options Email) nor a User link").format(fieldname), False

	def _recipient_checks(self, merged, meta, channel, spec):
		out = []
		audience = {"roles": {}, "fields": [], "cc": [], "external": [], "computed": False}
		rows = merged.get("recipients") or []
		if channel != "Slack" and not rows and not cint(merged.get("send_to_all_assignees")):
			out.append(Check("notification.recipients", "critical", _("Nobody would receive it: add recipients (role, document field or cc)")))
		for i, row in enumerate(rows, 1):
			role = row.get("receiver_by_role")
			if role:
				if not frappe.db.exists("Role", role):
					out.append(Check("notification.role", "critical", _("There is no role {0}").format(role)))
				else:
					from frappe.core.doctype.role.role import get_info_based_on_role

					count = len(get_info_based_on_role(role, "email", ignore_permissions=True))
					audience["roles"][role] = count
					if count > MASS_RECIPIENTS:
						out.append(Check("notification.mass", "warning", _("The role {0} has {1} users: each one receives every message").format(role, count)))
			field = row.get("receiver_by_document_field")
			if field:
				error, external = self._recipient_field_ok(meta, field, channel)
				if error:
					out.append(Check("notification.field", "critical", error))
				else:
					audience["fields"].append(field)
					if external and channel in ("Email", "SMS"):
						out.append(Check("external.document_field", "warning", _("Sent to the address stored in {0} of each document: it can be outside the company").format(field)))
			for key in ("cc", "bcc"):
				text = row.get(key)
				if not text:
					continue
				if channel not in ("Email", "System Notification"):
					out.append(Check("notification.cc", "critical", _("{0} only works with Email or System Notification").format(key)))
					continue
				if "{" in text:
					found, _ok = jinja_analyzer.compile_checks(text, f"recipients[{i}].{key}")
					out += found
					audience["computed"] = True
					continue
				for address in _addresses(text):
					if not validate_email_address(address):
						out.append(Check("notification.cc", "critical", _("{0} is not a valid email address").format(address)))
					elif not _is_site_user(address):
						audience["external"].append(address)
					audience["cc"].append(address)
			if row.get("condition"):
				found, _ok = jinja_analyzer.condition_checks(row["condition"], f"recipients[{i}].condition")
				out += found
		if audience["external"]:
			if cint(spec.get("allow_external")):
				out.append(Check("external.address", "warning", _("Sent outside the site's users to: {0} (confirmed with allow_external)").format(", ".join(audience["external"]))))
			else:
				out.append(Check("external.confirm", "critical", _("{0} are not users of this site. Ask the user to confirm, then send allow_external: true").format(", ".join(audience["external"]))))
		if channel == "System Notification" and audience["cc"] and any(a in audience["external"] for a in audience["cc"]):
			out.append(Check("notification.cc", "critical", _("System Notification goes to users only; remove the non-user addresses")))
		if audience["computed"]:
			out.append(Check("external.computed", "warning", _("Some cc/bcc addresses are computed from the document; the preview shows them for one document")))
		return out, audience

	def _attach_checks(self, merged, meta, channel):
		out = []
		if not cint(merged.get("attach_print")):
			if merged.get("print_format"):
				out.append(Check("notification.print", "info", _("print_format is ignored unless attach_print is 1")))
			return out
		if channel != "Email":
			out.append(Check("notification.print", "critical", _("attach_print only works with the Email channel")))
		pf = merged.get("print_format")
		if pf:
			row = frappe.db.get_value("Print Format", pf, ["doc_type", "disabled"], as_dict=True)
			if not row:
				out.append(Check("notification.print", "critical", _("There is no Print Format {0}").format(pf)))
			elif row.doc_type != meta.name:
				out.append(Check("notification.print", "critical", _("Print Format {0} is for {1}, not {2}").format(pf, row.doc_type, meta.name)))
			elif cint(row.disabled):
				out.append(Check("notification.print", "critical", _("Print Format {0} is disabled").format(pf)))
		settings = frappe.get_cached_doc("Print Settings")
		# Notification.get_print (notification.py:657-668) throws for draft/cancelled documents unless allowed
		if meta.is_submittable and merged.get("event") in ("New", "Save", "Value Change") and not cint(settings.allow_print_for_draft):
			out.append(Check("notification.print_draft", "warning", _("Draft documents cannot be printed (Print Settings), so the email fails for drafts")))
		return out

	def _frappe_validate(self, spec, merged):
		doc = self._unsaved(spec, merged)
		savepoint = "ai_customize_notification_validate"
		collect = _base.capture_messages()
		frappe.db.savepoint(savepoint)
		try:
			doc.validate()
		except Exception as e:
			return [Check("notification.validate", "critical", _("Frappe refuses the notification: {0}").format(jinja_analyzer.error_text(e)))]
		finally:
			frappe.db.rollback(save_point=savepoint)
			collect()
		return [Check("notification.validate", "ok", _("Frappe accepts the notification"))]

	def _sample(self, dt, user, wanted=None):
		if wanted:
			if not frappe.db.exists(dt, wanted):
				return None, _("There is no {0} {1}").format(dt, wanted)
			if not frappe.has_permission(dt, "read", doc=wanted, user=user):
				return None, _("{0} cannot read {1} {2}").format(user, dt, wanted)
			return wanted, None
		rows = frappe.get_list(dt, fields=["name"], order_by="modified desc", limit=1, user=user)
		return (rows[0].name, None) if rows else (None, _("No {0} that {1} can read").format(dt, user))

	def _dry_run(self, spec, merged, user, preview):
		"""Evaluate the condition and render subject/message/recipients on one document, as Frappe's
		``Notification.send`` builds them, without sending; everything is rolled back."""
		from frappe.email.doctype.notification.notification import get_context

		from ai_agent.runtime import masking

		dt = merged["document_type"]
		docname, note = self._sample(dt, user, spec.get("sample_document"))
		if not docname:
			level = "critical" if spec.get("sample_document") else "warning"
			return [Check("notification.sample", level, note + ". " + _("The templates compile but were not rendered"))]
		preview["document"] = {"doctype": dt, "name": docname}
		out = []
		alert = self._unsaved(spec, merged)
		savepoint = "ai_customize_notification_preview"
		collect = _base.capture_messages()
		frappe.db.savepoint(savepoint)
		try:
			with _base.as_user(user):
				doc = frappe.get_doc(dt, docname)
				context = get_context(doc)
				if merged.get("condition"):
					try:
						met = bool(frappe.safe_eval(merged["condition"], None, context))
						preview["meets_condition"] = met
						out.append(Check("notification.condition", "ok", _("The condition evaluates on {0}: {1}").format(docname, _("Yes") if met else _("No"))))
					except Exception as e:
						out.append(Check("notification.condition", "critical", _("The condition fails on {0}: {1}").format(docname, jinja_analyzer.error_text(e))))
				context.update({"alert": alert, "comments": json.loads(doc.get("_comments")) if doc.get("_comments") else None})
				try:
					subject = cstr(merged.get("subject"))
					preview["subject_rendered"] = frappe.render_template(subject, context) if "{" in subject else subject
					message = frappe.render_template(cstr(merged.get("message")), context)
					if dt in masking.salary_doctypes():
						preview["message_rendered"] = None
						preview["message_note"] = _("Not shown: {0} holds pay data").format(dt)
					else:
						preview["message_rendered"] = message[:PREVIEW_CHARS]
					out.append(Check("notification.render", "ok", _("Subject and message render on {0}").format(docname)))
				except Exception as e:
					out.append(Check("notification.render", "critical", _("Rendering on {0} fails: {1}").format(docname, jinja_analyzer.error_text(e))))
				try:
					if merged.get("channel") == "SMS":
						to, cc, bcc = alert.get_receiver_list(doc, context), [], []
					elif merged.get("channel") == "Slack":
						to, cc, bcc = [], [], []
					else:
						to, cc, bcc = alert.get_list_of_recipients(doc, context)
					preview["resolved"] = {"to": sorted(to)[:20], "cc": sorted(cc)[:20], "bcc": sorted(bcc)[:20],
						"count": len(set(to) | set(cc) | set(bcc))}
					computed = [a for a in set(cc) | set(bcc) if a not in _addresses("\n".join(r.get("cc", "") + "\n" + r.get("bcc", "") for r in merged.get("recipients") or []))]
					outside = sorted(a for a in computed if not _is_site_user(a))
					if outside:
						out.append(Check("external.computed", "warning", _("On {0} the computed cc/bcc reach non-users: {1}").format(docname, ", ".join(outside[:10]))))
				except Exception as e:
					out.append(Check("notification.recipients", "critical", _("Resolving the recipients fails on {0}: {1}").format(docname, jinja_analyzer.error_text(e))))
		finally:
			frappe.db.rollback(save_point=savepoint)
			collect()
		return out

	def _impact(self, merged, meta, channel, audience, preview):
		dt, event = meta.name, merged.get("event")
		out = []
		who = [_("{0} ({1} users)").format(role, n) for role, n in audience["roles"].items()]
		who += [_("the address in {0}").format(f) for f in audience["fields"]]
		who += audience["cc"]
		if cint(merged.get("send_to_all_assignees")):
			who.append(_("everyone assigned to the document"))
		verb = {"Email": _("Sends email to"), "System Notification": _("Notifies in the app"), "SMS": _("Sends SMS to"), "Slack": _("Posts to Slack")}[channel]
		if who:
			out.append(_("{0}: {1}").format(verb, ", ".join(who)))
		elif channel == "Slack":
			out.append(verb)
		since = add_days(nowdate(), -30)
		created = frappe.db.count(dt, {"creation": [">=", since]}) if not meta.istable else 0
		per_day = round(created / 30.0, 1)
		recipients = max(1, cint((preview.get("resolved") or {}).get("count")) or sum(audience["roles"].values()) + len(audience["fields"]) + len(audience["cc"]))
		when = {"New": _("each new {0}"), "Save": _("every save of a {0}"), "Submit": _("each submitted {0}"),
			"Cancel": _("each cancelled {0}"), "Value Change": _("each change of {1} on a {0}"),
			"Method": _("each {2} call on a {0}")}.get(event, _("each {0} whose date matches (checked daily or every few minutes)"))
		out.append(_("Fires on {0}").format(when.format(dt, merged.get("value_changed") or "", merged.get("method") or "")))
		out.append(_("About {0} {1} created per day over the last 30 days: roughly {2} message(s) a day{3}").format(
			per_day, dt, round(per_day * recipients, 1), _(" or more (every save counts)") if event == "Save" else ""))
		if cint(merged.get("attach_print")):
			out.append(_("Attaches the {0} print ({1})").format(dt, merged.get("print_format") or "Standard"))
		if cint(merged.get("send_system_notification")) and channel != "System Notification":
			out.append(_("Also shows a notification in the app"))
		return out

	# -- execute / verify ---------------------------------------------------------------
	def execute(self, spec, user, approval=None):
		approval = _base.approval_name(approval)
		op, name = spec["operation"], spec["name"]
		with _base.as_user(user):
			if op == "create":
				merged, _current = self._merged(spec)
				doc = self._unsaved(spec, merged)
				doc.message = with_header(spec["message"], spec.get("purpose"), spec.get("requested_by"), user, approval)
				doc.insert()
				undo = {"operation": "disable", "name": doc.name}
			elif op == "update":
				doc = frappe.get_doc("Notification", name)
				previous = self._record(name)
				undo = {"operation": "update", "name": name, "purpose": _("undo of {0}").format(approval or "")}
				for key in RECORD_FIELDS:
					if key not in spec or key == "document_type":
						continue
					undo[key] = previous[key]
					if key == "recipients":
						doc.set("recipients", [])
						for row in spec["recipients"]:
							doc.append("recipients", row)
					elif key == "message":
						doc.message = with_header(spec["message"], spec.get("purpose"), spec.get("requested_by"), user, approval)
					else:
						doc.set(key, spec[key])
				if "recipients" in undo or spec.get("allow_external"):
					undo["allow_external"] = 1  # restores recipients a person already approved
				doc.save()
			else:
				doc = frappe.get_doc("Notification", name)
				doc.enabled = 1 if op == "enable" else 0
				doc.save()
				undo = {"operation": "disable" if op == "enable" else "enable", "name": name}
		return {"targets": [{"doctype": "Notification", "name": doc.name}], "undo": undo,
			"message_sha256": _base.sha256(strip_header(doc.message)), "enabled": cint(doc.enabled)}

	def verify(self, spec, result):
		name, op = spec["name"], spec["operation"]
		if not frappe.db.exists("Notification", name):
			return [Check("verify.exists", "critical", _("Notification {0} does not exist").format(name))]
		doc = frappe.get_doc("Notification", name)
		checks = []
		expected = {"create": 1, "enable": 1, "disable": 0}.get(op)
		if expected is not None and cint(doc.enabled) != expected:
			checks.append(Check("verify.enabled", "critical", _("{0} is not {1}").format(name, _("enabled") if expected else _("disabled"))))
		if result.get("message_sha256") and _base.sha256(strip_header(doc.message)) != result["message_sha256"]:
			checks.append(Check("verify.message", "critical", _("The stored message differs from the approved one")))
		if doc.set_property_after_alert and op != "disable":
			checks.append(Check("verify.set_property", "critical", _("{0} sets a property after sending").format(name)))
		for key in RECORD_FIELDS:
			if key in spec and key not in ("message", "recipients") and cstr(doc.get(key)) != cstr(spec[key]):
				checks.append(Check("verify.field", "critical", _("{0} is stored as {1}, not {2}").format(key, doc.get(key), spec[key])))
		if "recipients" in spec and _recipient_set(doc.get("recipients")) != _recipient_set(spec["recipients"]):
			# Who receives the mail is the sensitive part: it must be exactly what the person approved.
			checks.append(Check("verify.recipients", "critical", _("The stored recipients differ from the approved ones")))
		if not checks:
			checks.append(Check("verify.notification", "ok", _("{0} is saved as approved").format(name)))
		return checks

	def summary(self, spec):
		verbs = {"create": _("Create"), "update": _("Update"), "disable": _("Disable"), "enable": _("Enable")}
		target = spec.get("document_type") or ""
		return _("{0} notification {1} ({2})").format(verbs[spec["operation"]], spec["name"], target)

	def get_source(self, name, user=None):
		doc = frappe.get_doc("Notification", name)
		out = {"kind": self.name, "name": doc.name, "enabled": cint(doc.enabled), "is_standard": cint(doc.is_standard),
			"ai_made": bool(HEADER_RE.match(cstr(doc.message))), "condition_type": doc.condition_type,
			"set_property_after_alert": doc.set_property_after_alert}
		out.update(self._record(name))
		return out


KIND = NotificationKind()


RECIPIENT_KEYS = ("receiver_by_document_field", "receiver_by_role", "cc", "bcc", "condition")


def _recipient_set(rows):
	"""Recipient rows compared by what they say, not their order or row names."""
	out = set()
	for row in rows or []:
		get = row.get if isinstance(row, dict) else (lambda k, r=row: r.get(k))
		out.add(tuple(" ".join(cstr(get(k) or "").split()) for k in RECIPIENT_KEYS))
	return out
