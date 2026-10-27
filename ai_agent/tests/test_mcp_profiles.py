"""AI MCP records: each user's MCP connection has its own agent, company and switch; the record for the request's
connection wins over the user's general one; Desk views are not subject to them."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.mcp import events, profiles
from ai_agent.mcp.protocol import MCPProtocolError
from ai_agent.mcp.service import MCPService
from ai_agent.tests.utils import ensure_test_agent, mcp_on

USER = "ai-mcp-profile@example.com"
CLIENT = "aimcptestclient"


class TestMcpProfiles(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.agent = ensure_test_agent().name
		mcp_on(self)
		if not frappe.db.exists("User", USER):
			frappe.get_doc({"doctype": "User", "email": USER, "first_name": "MCP Profile", "send_welcome_email": 0}).insert(ignore_permissions=True)
		frappe.get_doc("User", USER).add_roles("AI MCP User")
		self.required = frappe.db.get_single_value("AI Settings", "mcp_require_profile")
		self.addCleanup(self.cleanup)

	def cleanup(self):
		frappe.set_user("Administrator")
		for name in frappe.get_all("AI MCP", filters={"user": USER}, pluck="name"):
			frappe.delete_doc("AI MCP", name, force=True, ignore_permissions=True)
		self.require(self.required)
		profiles.clear_cache()

	def require(self, value):
		frappe.db.set_single_value("AI Settings", "mcp_require_profile", value or 0)
		frappe.clear_document_cache("AI Settings", "AI Settings")

	def record(self, title, agent=None, connection=None, enabled=1, company=None):
		doc = frappe.get_doc({"doctype": "AI MCP", "title": title, "user": USER, "agent": agent or self.agent,
			"connection": connection, "enabled": enabled, "company": company or ""})
		doc.flags.ignore_links = True  # the test connection is not a real OAuth client
		return doc.insert(ignore_permissions=True)

	def service(self, connection=None):
		with patch.object(events, "oauth_client", return_value=connection):
			return MCPService(USER)

	def test_no_record_keeps_the_site_wide_agent(self):
		self.assertEqual(self.service().agent.name, frappe.db.get_single_value("AI Settings", "mcp_agent") or "ERP Analyst")

	def test_the_users_record_sets_agent_and_company(self):
		frappe.get_doc("User", USER).add_roles("Accounts User")  # a role that reads Company
		self.addCleanup(lambda: frappe.get_doc("User", USER).remove_roles("Accounts User"))
		company = frappe.get_all("Company", pluck="name", limit=1)
		self.record("general", company=company[0] if company else None)
		svc = self.service()
		self.assertEqual(svc.agent.name, self.agent)
		if company:
			self.assertEqual(svc.company, company[0])

	def test_the_connections_record_wins_over_the_general_one(self):
		self.record("general", agent="ERP Analyst")
		self.record("claude", connection=CLIENT)
		self.assertEqual(self.service(CLIENT).agent.name, self.agent)
		self.assertEqual(self.service("another-client").agent.name, "ERP Analyst")

	def test_a_disabled_record_refuses(self):
		self.record("off", enabled=0)
		with self.assertRaises(MCPProtocolError) as cm:
			self.service()
		self.assertIn("turned off", str(cm.exception))

	def test_a_disabled_connection_does_not_fall_back_to_the_general_record(self):
		self.record("general")
		self.record("claude off", connection=CLIENT, enabled=0)
		with self.assertRaises(MCPProtocolError):
			self.service(CLIENT)
		self.assertTrue(self.service().agent)

	def test_a_record_grants_access_without_the_roles(self):
		frappe.get_doc("User", USER).remove_roles("AI MCP User")
		with self.assertRaises(MCPProtocolError):
			self.service()
		self.record("granted")
		self.assertEqual(self.service().agent.name, self.agent)

	def test_require_a_record(self):
		self.require(1)
		with self.assertRaises(MCPProtocolError) as cm:
			self.service()
		self.assertIn("no AI MCP record", str(cm.exception))
		self.record("allowed")
		self.assertEqual(self.service().agent.name, self.agent)

	def test_one_enabled_record_per_connection(self):
		self.record("first")
		with self.assertRaises(frappe.ValidationError):
			self.record("second")
		self.record("second, disabled", enabled=0)
		self.record("per connection", connection=CLIENT)
		with self.assertRaises(frappe.ValidationError):
			self.record("per connection again", connection=CLIENT)

	def test_a_disabled_agent_is_refused(self):
		frappe.db.set_value("AI Agent", self.agent, "enabled", 0)
		try:
			with self.assertRaises(frappe.ValidationError):
				self.record("disabled agent")
		finally:
			frappe.db.set_value("AI Agent", self.agent, "enabled", 1)

	def test_desk_views_ignore_the_mcp_rules(self):
		self.record("off", enabled=0)
		before = frappe.db.get_single_value("AI Settings", "enable_mcp")
		self.addCleanup(lambda: (frappe.db.set_single_value("AI Settings", "enable_mcp", before),
			frappe.clear_document_cache("AI Settings", "AI Settings")))
		frappe.db.set_single_value("AI Settings", "enable_mcp", 0)
		frappe.clear_document_cache("AI Settings", "AI Settings")
		svc = MCPService(USER, desk=True)
		self.assertIsNone(svc.agent)  # the Desk chat sets its own agent
