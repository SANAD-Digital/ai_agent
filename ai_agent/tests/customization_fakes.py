"""Shared helpers for the customization runtime tests: a fake kind that never touches the database,
settings switches, test users, and the Customize option check."""

from __future__ import annotations

import os

import frappe

from ai_agent.customization import registry
from ai_agent.customization.contract import Check, CheckReport, CustomizationError
from ai_agent.tests.utils import delete_docs, disable_users, quota_bypass

SWITCHES = (
	"allow_customization_actions", "allow_customize_fields", "allow_customize_client_scripts",
	"allow_customize_server_scripts", "allow_customize_reports", "allow_customize_print_formats",
	"allow_customize_notifications", "allow_customize_workflows", "allow_customize_dashboards", "allow_customize_web",
	"allow_customize_permissions", "allow_customize_doctypes", "customization_allow_mcp",
	"customization_require_four_eyes", "customization_max_requests_per_hour",
	"default_approval_ttl_minutes", "require_four_eyes", "allow_auto_approval",
	"mcp_enable_action_requests", "mcp_require_approval", "enable_mcp",
)
SM_USER = "cust-sm@example.com"  # System Manager only
SCRIPT_USER = "cust-script@example.com"  # System Manager + Script Manager
PLAIN_USER = "cust-plain@example.com"  # no admin roles
MISSING_OPTION = (
	"AI Approval Request.action_type has no 'Customize' option in this database: reload the DocType "
	"(bench migrate) to run this test, or set AI_AGENT_TEST_PATCH_META=1 to add it to the in-process meta only."
)


def customize_option_ready():
	"""True when an approval of type Customize can be inserted: the DB has the option, or
	AI_AGENT_TEST_PATCH_META=1 (then ``option_patch()`` lets the option through Frappe's select validation in
	this test process; the cached meta is never mutated and nothing is written to the database)."""
	df = frappe.get_meta("AI Approval Request").get_field("action_type")
	if "Customize" in (df.options or "").split("\n"):
		return True
	return os.environ.get("AI_AGENT_TEST_PATCH_META") == "1"


def option_patch():
	"""A patcher (start/stop) that accepts action_type=Customize on AI Approval Request while the DB lacks it."""
	from unittest.mock import patch

	from frappe.model.base_document import BaseDocument

	original = BaseDocument._validate_selects

	def validate_selects(self):
		if self.doctype == "AI Approval Request" and self.get("action_type") == "Customize":
			value = self.action_type
			self.action_type = "Approved Method"  # a valid option, only for the check
			try:
				return original(self)
			finally:
				self.action_type = value
		return original(self)

	return patch.object(BaseDocument, "_validate_selects", validate_selects)


def read_settings():
	rows = frappe.db.sql(
		"select field, value from tabSingles where doctype='AI Settings' and field in %(f)s", {"f": SWITCHES}, as_dict=True
	)
	return {r.field: r.value for r in rows}


def restore_settings(saved):
	for field in SWITCHES:
		if field in saved:
			frappe.db.set_single_value("AI Settings", field, saved[field])
		else:
			frappe.db.sql("delete from tabSingles where doctype='AI Settings' and field=%s", field)
	frappe.clear_document_cache("AI Settings", "AI Settings")


def set_settings(**values):
	for field, value in values.items():
		frappe.db.set_single_value("AI Settings", field, value)
	frappe.clear_document_cache("AI Settings", "AI Settings")


def enable_all(**extra):
	set_settings(
		allow_customization_actions=1, allow_customize_fields=1, allow_customize_client_scripts=1,
		allow_customize_server_scripts=1, allow_customize_reports=1, customization_allow_mcp=0,
		customization_require_four_eyes=0, customization_max_requests_per_hour=1000, require_four_eyes=0,
		**extra,
	)


def ensure_user(email, roles):
	if not frappe.db.exists("User", email):
		with quota_bypass():
			frappe.get_doc({
				"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0,
				"user_type": "System User",
			}).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	if not user.enabled:
		user.db_set("enabled", 1, update_modified=False)
	current = {r.role for r in user.roles}
	if current != set(roles):
		user.set("roles", [])
		for role in roles:
			user.append("roles", {"role": role})
		with quota_bypass():
			user.save(ignore_permissions=True)
	frappe.clear_cache(user=email)
	return email


def ensure_users():
	ensure_user(SM_USER, ["System Manager"])
	ensure_user(SCRIPT_USER, ["System Manager", "Script Manager"])
	ensure_user(PLAIN_USER, [])


def release_users():
	disable_users([SM_USER, SCRIPT_USER, PLAIN_USER])


class FakeKind:
	"""A kind whose target lives in a dict: every step of the contract, no database."""

	setting = "allow_customize_client_scripts"
	required_roles = ("System Manager",)
	ALLOWED = {"operation", "name", "value", "purpose"}

	def __init__(self, name="client_script", roles=None):
		self.name = name
		if roles:
			self.required_roles = tuple(roles)
		self.store = {}
		self.fail_check = False
		self.fail_verify = False
		self.executed_by = []

	def normalize(self, args, user):
		unknown = set(args) - self.ALLOWED
		if unknown:
			raise CustomizationError(f"Unknown arguments: {', '.join(sorted(unknown))}", hints=["Send only operation, name, value, purpose."])
		if not args.get("name"):
			raise CustomizationError("name is required", hints=["Give the script a name."])
		return {"operation": args.get("operation") or "create", "name": args["name"], "value": args.get("value")}

	def snapshot(self, spec):
		return {"value": self.store.get(spec["name"])}

	def check(self, spec, user):
		checks = [Check("fake.syntax", "ok", "Syntax is fine")]
		if self.fail_check or spec.get("value") == "bad":
			checks.append(Check("fake.import", "critical", "import is not allowed", line=3))
		return CheckReport(checks=checks, risk="high", impact=["Runs for everyone"], preview={"code": spec.get("value")})

	def execute(self, spec, user):
		previous = self.store.get(spec["name"])
		self.store[spec["name"]] = spec.get("value")
		self.executed_by.append(frappe.session.user)
		return {"targets": [{"doctype": "Client Script", "name": spec["name"]}],
			"undo": {"operation": "update", "name": spec["name"], "value": previous}}

	def verify(self, spec, result):
		if self.fail_verify or self.store.get(spec["name"]) != spec.get("value"):
			return [Check("fake.verify", "critical", "The change is not in place")]
		return [Check("fake.verify", "ok", "In place")]

	def summary(self, spec):
		return f"Fake {spec['operation']} {spec['name']}"


def register_fakes():
	client = FakeKind("client_script")
	server = FakeKind("server_script", roles=("System Manager", "Script Manager"))
	server.setting = "allow_customize_server_scripts"
	registry.register("client_script", client)
	registry.register("server_script", server)
	return client, server


def unregister_fakes():
	registry.unregister("client_script")
	registry.unregister("server_script")


def cleanup_runs(runs, since=None):
	"""Delete the runs a test made (plus the MCP runs it caused after ``since``) and their approvals."""
	runs = set(runs or ())
	if since:
		runs |= set(frappe.get_all("AI Run", filters={"prompt": ("like", "MCP tool call: customize.%"), "creation": (">=", since)}, pluck="name"))
	if not runs:
		return
	for name in frappe.get_all("AI Approval Request", filters={"run": ("in", list(runs))}, pluck="name"):
		frappe.delete_doc("AI Approval Request", name, ignore_permissions=True, force=True)
	for name in frappe.get_all("AI Tool Call", filters={"run": ("in", list(runs))}, pluck="name"):
		frappe.delete_doc("AI Tool Call", name, ignore_permissions=True, force=True)
	delete_docs("AI Run", list(runs))
