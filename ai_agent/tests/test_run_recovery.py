import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.runtime.run_control import mark_interrupted_run


class TestRunRecovery(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		run = frappe.get_doc({
			"doctype": "AI Run",
			"agent": frappe.db.get_value("AI Agent", {}, "name"),
			"user": "Administrator",
			"prompt": "recovery test",
			"status": "Queued",
			# Links to records that no longer exist, as after a session is deleted mid-run.
			"chat_session": "CHAT-DELETED-0",
			"message": "deleted-message",
			"parent_run": "AI-RUN-DELETED-0",
		})
		run.flags.ignore_links = True
		run.insert(ignore_permissions=True)
		self.run_name = run.name

	def tearDown(self):
		frappe.delete_doc("AI Run", self.run_name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_run_with_deleted_links_is_closed(self):
		mark_interrupted_run(self.run_name, "interrupted")
		self.assertEqual(frappe.db.get_value("AI Run", self.run_name, "status"), "Failed")
