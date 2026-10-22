"""One role per surface, nobody by default: the full chat page needs AI Chat User,
the support window AI Chatbot User; "All" no longer opens either."""

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent import roles
from ai_agent.chat import api
from ai_agent.chatbot import settings as bot_settings

USER = "ai-roles-test@example.com"


class TestAppRoles(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		if not frappe.db.exists("User", USER):
			frappe.get_doc({"doctype": "User", "email": USER, "first_name": "Roles Test", "send_welcome_email": 0}).insert(ignore_permissions=True)
		user = frappe.get_doc("User", USER)
		user.remove_roles(*roles.APP_ROLES, "System Manager")
		self.saved_rows = [r.role for r in frappe.get_single("AI Chatbot Settings").allowed_roles]
		self._bubble_roles([])

	def tearDown(self):
		frappe.set_user("Administrator")
		self._bubble_roles(self.saved_rows)
		frappe.get_doc("User", USER).remove_roles(*roles.APP_ROLES)
		frappe.db.commit()

	def _bubble_roles(self, names):
		doc = frappe.get_single("AI Chatbot Settings")
		doc.set("allowed_roles", [{"role": r} for r in names])
		doc.flags.ignore_validate = True
		doc.save(ignore_permissions=True)

	def _as(self, *grant):
		frappe.set_user("Administrator")
		frappe.get_doc("User", USER).add_roles(*grant) if grant else None
		frappe.set_user(USER)

	def test_no_role_no_access(self):
		self._as()
		with self.assertRaises(frappe.PermissionError):
			api.bootstrap()
		self.assertFalse(bot_settings.can_access(USER), "an empty Allowed Roles no longer means everyone")
		self.assertFalse(frappe.has_permission("AI Chat Session", "create"), '"All" no longer grants the conversations')

	def test_chat_role_opens_the_page_only(self):
		self._as(roles.CHAT_ROLE)
		self.assertIn("agents", api.bootstrap())
		self.assertFalse(bot_settings.can_access(USER))

	def test_bubble_role_opens_the_window_only(self):
		self._as(roles.BUBBLE_ROLE)
		self.assertTrue(bot_settings.can_access(USER))
		with self.assertRaises(frappe.PermissionError):
			api.bootstrap()

	def test_the_page_lists_its_roles(self):
		page_roles = {r.role for r in frappe.get_doc("Page", "agent-workspace").roles}
		self.assertEqual(page_roles, {roles.CHAT_ROLE, "System Manager"})
