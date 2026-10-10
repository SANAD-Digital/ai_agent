import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.mcp import setup


class TestMCPSetup(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")

	def test_fix_all_makes_every_check_ready(self):
		frappe.db.set_single_value("AI Settings", "enable_mcp", 0)
		frappe.db.set_single_value("OAuth Settings", "enable_dynamic_client_registration", 0)
		frappe.db.set_single_value("OAuth Settings", "allowed_public_client_origins", "https://example.com")
		self.assertFalse(setup.get_status()["checks"]["oauth"])
		checks = setup.fix("all")["checks"]
		self.assertTrue(all(checks.values()), checks)
		origins = frappe.db.get_single_value("OAuth Settings", "allowed_public_client_origins").splitlines()
		self.assertEqual(origins, ["https://example.com", setup.CHATGPT_ORIGIN], "existing origins are kept")

	def test_grant_role_adds_only_the_mcp_role(self):
		user = frappe.get_all("User", filters={"enabled": 1, "user_type": "System User", "name": ["not in", ["Administrator", "Guest"]]}, pluck="name", limit=1)[0]
		before = set(frappe.get_roles(user))
		setup.grant_role(user)
		self.assertEqual(set(frappe.get_roles(user)) - before, {setup.MCP_ROLE} - before)

	def test_revoke_client_revokes_all_its_active_tokens(self):
		client = frappe.get_doc({"doctype": "OAuth Client", "app_name": "Test MCP App", "redirect_uris": "https://example.com/cb",
			"default_redirect_uri": "https://example.com/cb", "scopes": "all"}).insert(ignore_permissions=True)
		for token in ("t-setup-1", "t-setup-2"):
			frappe.get_doc({"doctype": "OAuth Bearer Token", "client": client.name, "user": "Administrator", "access_token": token,
				"status": "Active", "expires_in": 3600, "scopes": "all"}).insert(ignore_permissions=True)
		self.assertIn(client.name, [c["client"] for c in setup.get_status()["clients"]])
		status = setup.revoke_client(client.name)
		self.assertNotIn(client.name, [c["client"] for c in status["clients"]])
		self.assertEqual(frappe.db.count("OAuth Bearer Token", {"client": client.name, "status": "Active"}), 0)

	def test_only_system_managers(self):
		user = frappe.get_all("User", filters={"enabled": 1, "user_type": "System User", "name": ["not in", ["Administrator", "Guest"]]}, pluck="name", limit=50)
		plain = next((u for u in user if "System Manager" not in frappe.get_roles(u)), None)
		if not plain:
			self.skipTest("no non-System-Manager user on this site")
		frappe.set_user(plain)
		with self.assertRaises(frappe.PermissionError):
			setup.fix("oauth")
