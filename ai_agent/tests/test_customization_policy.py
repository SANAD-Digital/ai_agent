"""Customization gates (docs/customization/01 §3.6): settings, roles, surfaces, limits, and that a Customize
request is never executed without a person — not by auto mode, "approve always", MCP with
mcp_require_approval off, execute_direct, nor approved from an external AI tool."""

import json
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.customization import policy as cpolicy
from ai_agent.customization import tools as ctools
from ai_agent.engine.tools import TOOL_GROUPS, ToolExecutor
from ai_agent.engine.types import TOOL_USE, Block
from ai_agent.runtime import policy
from ai_agent.runtime.approval_engine import ApprovalEngine
from ai_agent.runtime.exceptions import PermissionDenied, ToolDenied
from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.runtime.tool_gateway import ToolGateway
from ai_agent.runtime.tool_registry import ToolRegistry
from ai_agent.tests import customization_fakes as fx
from ai_agent.tests.utils import ensure_test_agent, make_test_run


def acting_agent(**overrides):
	"""The test agent as an agent that may act (the shared test agent may be read-only on this site)."""
	agent = ensure_test_agent()
	return frappe._dict(agent.as_dict(), **{"allow_actions": 1, "read_only": 0, **overrides})


def _ctx(user, surface="Desk", tainted=False):
	ctx = PermissionContext(user)
	ctx.surface = surface
	ctx.tainted = tainted
	return ctx


class CustomizationTestCase(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.saved = fx.read_settings()
		self.since = frappe.utils.now_datetime() - timedelta(seconds=1)
		fx.ensure_users()
		self.client, self.server = fx.register_fakes()
		fx.enable_all()
		self.run = make_test_run()
		self.runs = [self.run.name]
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		fx.unregister_fakes()
		fx.cleanup_runs(self.runs, since=self.since)
		fx.restore_settings(self.saved)
		fx.release_users()
		frappe.db.commit()

	def need_customize_option(self):
		if not fx.customize_option_ready():
			self.skipTest(fx.MISSING_OPTION)
		patcher = fx.option_patch()
		patcher.start()
		self.addCleanup(patcher.stop)

	def request(self, user="Administrator", kind="client_script", surface="Desk", **args):
		args.setdefault("name", "t-script")
		args.setdefault("value", "ok")
		frappe.set_user(user)
		try:
			return ctools.request(kind, args, _ctx(user, surface), self.run.name, via=f"customize.request_{kind}")
		finally:
			frappe.set_user("Administrator")


class TestSwitchesRolesSurfaces(CustomizationTestCase):
	def test_every_switch_is_off_by_default_in_the_doctype(self):
		path = frappe.get_app_path("ai_agent", "ai_agent", "doctype", "ai_settings", "ai_settings.json")
		with open(path) as f:
			fields = {d["fieldname"]: d for d in json.load(f)["fields"]}
		for name in ("allow_customization_actions", "allow_customize_fields", "allow_customize_client_scripts",
				"allow_customize_server_scripts", "allow_customize_reports", "customization_allow_mcp",
				"customization_require_four_eyes"):
			self.assertEqual(fields[name]["default"], "0", name)
		self.assertEqual(fields["customization_max_requests_per_hour"]["default"], "20")

	def test_master_switch_and_kind_switches(self):
		fx.set_settings(allow_customization_actions=0)
		self.assertFalse(cpolicy.available("Administrator", "Desk"))
		self.assertFalse(cpolicy.tool_available("customize.inspect", "Administrator", "Desk"))
		fx.set_settings(allow_customization_actions=1, allow_customize_client_scripts=0)
		self.assertTrue(cpolicy.available("Administrator", "Desk"))
		self.assertFalse(cpolicy.tool_available("customize.request_client_script", "Administrator", "Desk"))
		self.assertTrue(cpolicy.tool_available("customize.request_field", "Administrator", "Desk"))
		fx.set_settings(allow_customize_fields=0, allow_customize_server_scripts=0, allow_customize_reports=0)
		self.assertFalse(cpolicy.available("Administrator", "Desk"), "no kind on: no group")

	def test_surfaces(self):
		self.assertTrue(cpolicy.available("Administrator", "Desk"))
		for surface in ("Chatbot", "WhatsApp", "API", None, ""):
			self.assertFalse(cpolicy.available("Administrator", surface), surface)
		self.assertFalse(cpolicy.available("Administrator", "MCP"), "MCP is off by default")
		fx.set_settings(customization_allow_mcp=1)
		self.assertTrue(cpolicy.available("Administrator", "MCP"))

	def test_frappe_roles_decide(self):
		self.assertTrue(cpolicy.available(fx.SM_USER, "Desk"))
		self.assertFalse(cpolicy.available(fx.PLAIN_USER, "Desk"))
		self.assertTrue(cpolicy.kind_available("client_script", fx.SM_USER, "Desk"))
		self.assertFalse(cpolicy.kind_available("server_script", fx.SM_USER, "Desk"), "needs Script Manager")
		self.assertTrue(cpolicy.kind_available("server_script", fx.SCRIPT_USER, "Desk"))
		with self.assertRaises(PermissionDenied):
			self.request(user=fx.SM_USER, kind="server_script")
		with self.assertRaises(PermissionDenied):
			self.request(user=fx.PLAIN_USER)

	def test_engine_offers_the_group_only_where_allowed(self):
		self.assertEqual(TOOL_GROUPS["customization"]["tools"][0], "customize.inspect")
		agent = acting_agent()
		with patch.object(ToolExecutor, "_session_surface", return_value="Desk"):
			ex = ToolExecutor(PermissionContext("Administrator", agent.name), agent, self.run.name)
		self.assertTrue(ex.allowed("customize.request_client_script"))
		self.assertIn("customization", [g["name"] for g in ex.groups_for_prompt()])
		with patch.object(ToolExecutor, "_session_surface", return_value="Chatbot"):
			bubble = ToolExecutor(PermissionContext("Administrator", agent.name), agent, self.run.name)
		self.assertFalse(bubble.group_available("customization"), "never in the support bubble")
		self.assertFalse(bubble.allowed("customize.inspect"))
		no_session = ToolExecutor(PermissionContext("Administrator", agent.name), agent, self.run.name)
		self.assertFalse(no_session.allowed("customize.inspect"), "no conversation, no surface: refused")
		read_only = acting_agent(allow_actions=0)
		with patch.object(ToolExecutor, "_session_surface", return_value="Desk"):
			ro = ToolExecutor(PermissionContext("Administrator", agent.name), read_only, self.run.name)
		self.assertFalse(ro.allowed("customize.inspect"), "an agent without actions has no customization")
		fx.set_settings(allow_customization_actions=0)
		self.assertFalse(ex.allowed("customize.inspect"))

	def test_gateway_refuses_without_an_allowed_surface(self):
		registry = ToolRegistry()
		for surface in (None, "Chatbot", "WhatsApp"):
			gateway = ToolGateway(_ctx("Administrator", surface), registry)
			with self.assertRaises(ToolDenied):
				gateway.authorize_and_execute("customize.inspect", {"doctype": "ToDo"}, run_name=self.run.name)
			with self.assertRaises(ToolDenied):
				gateway.create_approval("customize.request_client_script", {"operation": "create"}, self.run.name, None, None)
		result = ToolGateway(_ctx("Administrator"), registry).authorize_and_execute("customize.inspect", {"doctype": "ToDo"}, run_name=self.run.name)
		self.assertIn("customizable", result)

	def test_mcp_does_not_see_the_tools_unless_allowed(self):
		from ai_agent.mcp.service import MCPService

		svc = MCPService.__new__(MCPService)
		svc.user, svc.agent, svc.registry = "Administrator", acting_agent(), ToolRegistry()
		fx.set_settings(mcp_enable_action_requests=1)
		svc.settings = frappe.get_cached_doc("AI Settings")
		inspect, request = svc.registry.get_tool("customize.inspect"), svc.registry.get_tool("customize.request_client_script")
		self.assertFalse(svc._is_exposed(inspect), "customization_allow_mcp is off")
		self.assertFalse(svc._is_exposed(request))
		fx.set_settings(customization_allow_mcp=1)
		self.assertTrue(svc._is_exposed(inspect))
		self.assertTrue(svc._is_exposed(request))
		fx.set_settings(mcp_enable_action_requests=0)
		svc.settings = frappe.get_cached_doc("AI Settings")
		self.assertTrue(svc._is_exposed(inspect))
		self.assertFalse(svc._is_exposed(request), "requests also need MCP action requests")
		fx.set_settings(mcp_enable_action_requests=1, allow_customization_actions=0)
		self.assertFalse(svc._is_exposed(inspect))


class TestNeverWithoutAPerson(CustomizationTestCase):
	def setUp(self):
		self.need_customize_option()  # before setUp: a skip in setUp skips tearDown too
		super().setUp()

	def _pending(self, **kw):
		out = self.request(**kw)
		self.assertTrue(out.get("approval_id"), out)
		return frappe.get_doc("AI Approval Request", out["approval_id"])

	def test_policy_lists_customize_as_always_human(self):
		self.assertTrue(policy.requires_human("Customize"))
		self.assertTrue(policy.requires_human("Approved Method"))
		fx.set_settings(allow_auto_approval=1, require_four_eyes=0)
		self.assertFalse(policy.auto_approves("Auto", "Customize"))

	def test_execute_direct_refuses(self):
		approval = self._pending()
		with self.assertRaises(PermissionDenied):
			ApprovalEngine(PermissionContext("Administrator")).execute_direct(approval.name, "Administrator")
		approval.reload()
		self.assertEqual(approval.status, "Pending")
		self.assertEqual(self.client.store, {})

	def test_approve_always_refuses(self):
		from ai_agent.chat import api

		approval = self._pending()
		with self.assertRaises(frappe.ValidationError):
			api.decide_approval(approval.name, "approve_always")
		approval.reload()
		self.assertEqual(approval.status, "Pending")

	def test_auto_mode_conversation_still_waits(self):
		fx.set_settings(allow_auto_approval=1, require_four_eyes=0)
		agent = acting_agent()
		with patch.object(ToolExecutor, "_session_surface", return_value="Desk"):
			ex = ToolExecutor(PermissionContext("Administrator", agent.name), agent, self.run.name)
		with patch.object(ToolExecutor, "_auto_mode", return_value=True):
			outcome = ex.execute(Block(kind=TOOL_USE, id="c1", name="customize__request_client_script",
				data={"operation": "create", "name": "auto-script", "value": "ok"}))
		self.assertTrue(outcome.approval, outcome.result and outcome.result.text)
		self.assertEqual(frappe.db.get_value("AI Approval Request", outcome.approval, "status"), "Pending")
		self.assertEqual(self.client.store, {}, "nothing ran")
		self.assertEqual(outcome.approval_block.data["action_type"], "Customize")

	def test_mcp_without_require_approval_still_waits(self):
		from ai_agent.mcp.service import MCPService

		fx.set_settings(customization_allow_mcp=1, mcp_enable_action_requests=1, mcp_require_approval=0)
		svc = MCPService.__new__(MCPService)
		svc.user, svc.agent, svc.registry, svc.company = "Administrator", acting_agent(), ToolRegistry(), None
		svc.settings = frappe.get_cached_doc("AI Settings")
		with patch("ai_agent.mcp.approvals.approval_required", return_value=False):
			out = svc.call_tool("customize__request_client_script", {"operation": "create", "name": "mcp-script", "value": "ok"})
		self.assertFalse(out.get("isError"), out)
		rows = frappe.get_all("AI Approval Request", filters={"action_type": "Customize", "document_name": "mcp-script"},
			fields=["name", "status", "payload_json"])
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].status, "Pending")
		context = json.loads(rows[0].payload_json)["context"]
		self.assertEqual((context["surface"], context["tainted"]), ("MCP", True), "every MCP request is tainted")
		self.assertEqual(self.client.store, {})
		# and an external model cannot approve it; rejecting is fine
		approve = svc._call_synthetic("approvals__decide", {"approval_id": rows[0].name, "decision": "approve"})
		self.assertTrue(approve.get("isError"))
		self.assertEqual(frappe.db.get_value("AI Approval Request", rows[0].name, "status"), "Pending")
		self.assertEqual(self.client.store, {})

	def test_four_eyes_of_its_own(self):
		fx.set_settings(customization_require_four_eyes=1, require_four_eyes=0)
		approval = self._pending()
		engine = ApprovalEngine(PermissionContext("Administrator"))
		with self.assertRaises(PermissionDenied):
			engine.approve(approval.name, user="Administrator")
		frappe.set_user(fx.SM_USER)
		try:
			ApprovalEngine(PermissionContext(fx.SM_USER)).approve(approval.name, user=fx.SM_USER)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("AI Approval Request", approval.name, "status"), "Approved")

	def test_approver_without_script_manager_is_refused(self):
		approval = self._pending(kind="server_script", name="srv-script")
		with self.assertRaises(PermissionDenied):
			ApprovalEngine(PermissionContext(fx.SM_USER)).approve(approval.name, user=fx.SM_USER)
		# even if it got approved by someone without the role, execution re-checks the approver
		frappe.db.set_value("AI Approval Request", approval.name, {"status": "Approved", "approved_by": fx.SM_USER})
		frappe.set_user(fx.SM_USER)
		try:
			with self.assertRaises(PermissionDenied):
				ApprovalEngine(PermissionContext(fx.SM_USER)).execute_approved_action(approval.name)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self.server.store, {})

	def test_switch_turned_off_after_the_request_stops_execution(self):
		approval = self._pending()
		ApprovalEngine(PermissionContext("Administrator")).approve(approval.name, user="Administrator")
		fx.set_settings(allow_customize_client_scripts=0)
		with self.assertRaises(PermissionDenied):
			ApprovalEngine(PermissionContext("Administrator")).execute_approved_action(approval.name)
		self.assertEqual(self.client.store, {})


class TestLimits(CustomizationTestCase):
	def setUp(self):
		self.need_customize_option()  # before setUp: a skip in setUp skips tearDown too
		super().setUp()

	def test_one_pending_request_per_target(self):
		self.assertTrue(self.request(name="same").get("approval_id"))
		with self.assertRaises(PermissionDenied):
			self.request(name="same", value="other")

	def test_pending_per_user(self):
		for i in range(cpolicy.MAX_PENDING_PER_USER):
			self.assertTrue(self.request(name=f"p-{i}").get("approval_id"))
		with self.assertRaises(PermissionDenied):
			self.request(name="p-extra")

	def test_requests_per_hour(self):
		# Real requests made on this site in the last hour count too: allow exactly two more.
		since = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-1)
		already = frappe.db.count("AI Approval Request", {"action_type": "Customize", "requested_by": "Administrator", "creation": (">", since)})
		fx.set_settings(customization_max_requests_per_hour=already + 2)
		self.request(name="r-1")
		self.request(name="r-2")
		with self.assertRaises(PermissionDenied):
			self.request(name="r-3")

	def test_size_limit(self):
		with self.assertRaises(PermissionDenied):
			ctools.request("client_script", {"name": "big", "script": "x" * (cpolicy.MAX_CODE_CHARS + 1)}, _ctx("Administrator"), self.run.name)

	def test_ttl_is_capped_at_an_hour(self):
		fx.set_settings(default_approval_ttl_minutes=600)
		approval = frappe.get_doc("AI Approval Request", self.request(name="ttl")["approval_id"])
		self.assertLessEqual(approval.expires_at - approval.requested_at, timedelta(minutes=cpolicy.MAX_TTL_MINUTES, seconds=1))

	def test_surface_is_checked_at_request(self):
		with self.assertRaises(PermissionDenied):
			self.request(surface="Chatbot")
		with self.assertRaises(PermissionDenied):
			self.request(surface="MCP")
