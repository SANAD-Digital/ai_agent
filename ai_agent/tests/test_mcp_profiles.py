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


class TestMcpCompanyLock(IntegrationTestCase):
	"""A connection limited to one company: lists, documents and reports keep to it, as the connection's user."""

	def setUp(self):
		from ai_agent.mcp import company_lock

		self.lock = company_lock
		self.companies = frappe.get_all("Company", pluck="name", order_by="creation asc", limit=2)
		if len(self.companies) < 2:
			self.skipTest("needs two companies")
		frappe.set_user("Administrator")
		if not frappe.db.exists("User", USER):
			frappe.get_doc({"doctype": "User", "email": USER, "first_name": "MCP Profile", "send_welcome_email": 0}).insert(ignore_permissions=True)
		frappe.get_doc("User", USER).add_roles("Accounts User")
		self.addCleanup(lambda: (frappe.set_user("Administrator"), frappe.get_doc("User", USER).remove_roles("Accounts User")))
		self.addCleanup(self.lock.set_lock, None)
		self.mine, self.other = self.companies

	def test_lists_keep_to_the_company(self):
		frappe.set_user(USER)
		self.lock.set_lock(self.mine)
		self.assertEqual(frappe.get_list("Company", pluck="name"), [self.mine])
		accounts = frappe.get_list("Account", fields=["company"], limit=200)
		self.assertTrue(accounts)
		self.assertEqual({a.company for a in accounts}, {self.mine})
		self.lock.set_lock(None)
		self.assertGreater(len(frappe.get_list("Company", pluck="name")), 1, "no lock: every readable company")

	def test_documents_of_another_company_are_refused(self):
		other_account = frappe.db.get_value("Account", {"company": self.other}, "name")
		frappe.set_user(USER)
		self.lock.set_lock(self.mine)
		self.assertFalse(frappe.has_permission("Company", "read", self.other))
		self.assertTrue(frappe.has_permission("Company", "read", self.mine))
		if other_account:
			self.assertFalse(frappe.has_permission("Account", "read", other_account))
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc("Account", other_account).check_permission("read")
		new = frappe.new_doc("Cost Center")
		new.company = self.other
		self.assertFalse(frappe.has_permission("Cost Center", "create", new))

	def test_reports_run_for_the_company(self):
		from ai_agent.tools import reports

		report = frappe._dict(name="X", report_type="Script Report")
		self.lock.set_lock(self.mine)
		self.assertEqual(reports._locked_company(report, []), self.mine)
		self.assertEqual(reports._locked_company(report, [{"fieldname": "company"}]), self.mine)
		with self.assertRaises(frappe.ValidationError):
			reports._locked_company(report, [{"fieldname": "from_date"}])
		self.assertIsNone(reports._locked_company(frappe._dict(name="Y", report_type="Report Builder"), []))

	def test_the_service_locks_the_request(self):
		frappe.set_user("Administrator")
		mcp_on(self)
		for name in frappe.get_all("AI MCP", filters={"user": USER}, pluck="name"):
			frappe.delete_doc("AI MCP", name, force=True, ignore_permissions=True)
		profiles.clear_cache()
		doc = frappe.get_doc({"doctype": "AI MCP", "title": "locked", "user": USER, "agent": ensure_test_agent().name,
			"company": self.mine, "enabled": 1}).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("AI MCP", doc.name, force=True, ignore_permissions=True))
		with patch.object(events, "oauth_client", return_value=None):
			svc = MCPService(USER)
		self.assertEqual((svc.company, self.lock.current()), (self.mine, self.mine))

	# -- what the model sends on a locked connection ----------------------------------------------
	def test_write_requests_are_bound_to_the_company(self):
		from ai_agent.runtime.exceptions import PermissionDenied

		self.lock.set_lock(self.mine)
		bound = self.lock.bind_payload("Create", "Cost Center", {"cost_center_name": "x"})
		self.assertEqual(bound["company"], self.mine, "a new document goes to the locked company")
		with self.assertRaises(PermissionDenied):
			self.lock.bind_payload("Create", "Cost Center", {"cost_center_name": "x", "company": self.other})
		with self.assertRaises(PermissionDenied):
			self.lock.bind_payload("Update", "Cost Center", {"company": self.other})
		self.assertNotIn("company", self.lock.bind_payload("Update", "Cost Center", {"disabled": 1}))
		bulk = self.lock.bind_payload("Bulk Create", None, {"documents": [
			{"doctype": "Cost Center", "payload": {"cost_center_name": "a"}}, {"doctype": "Customer", "payload": {"customer_name": "b"}}]})
		self.assertEqual(bulk["documents"][0]["payload"]["company"], self.mine)
		self.assertNotIn("company", bulk["documents"][1]["payload"], "Customer has no company field")
		with self.assertRaises(PermissionDenied):
			self.lock.bind_payload("Bulk Create", None, {"documents": [{"doctype": "Cost Center", "payload": {"company": self.other}}]})
		self.lock.set_lock(None)
		self.assertEqual(self.lock.bind_payload("Create", "Cost Center", {"company": self.other})["company"], self.other)

	def test_statement_and_artifacts_keep_to_the_company(self):
		from ai_agent.tools import exports
		from ai_agent.tests.utils import make_test_run

		customer = frappe.db.get_value("Customer", {}, "name")
		self.lock.set_lock(self.mine)
		if customer:
			out = exports.handle_statement({"party_type": "Customer", "party": customer, "company": self.other},
				frappe._dict(user="Administrator"))
			self.assertIn("works on", out.get("error", ""))
		run = make_test_run()
		frappe.db.set_value("AI Run", run.name, "company", self.other)
		art = frappe.get_doc({"doctype": "AI Artifact", "parent": run.name, "parenttype": "AI Run", "parentfield": "artifacts",
			"run": run.name, "artifact_type": "Table", "title": "t",
			"content_json": '{"rows": [{"a": 1}], "columns": ["a"]}'}).insert(ignore_permissions=True)
		_c, _r, error = exports._source({"artifact": art.name}, frappe._dict(user="Administrator"))
		self.assertTrue(error, "a table computed for another company is not exported on this connection")
		frappe.db.set_value("AI Run", run.name, "company", self.mine)
		_c, rows, error = exports._source({"artifact": art.name}, frappe._dict(user="Administrator"))
		self.assertFalse(error)
		self.assertEqual(rows, [{"a": 1}])
