from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.runtime.run_control import execute_run_job
from ai_agent.tests.utils import delete_docs, make_test_run


class TestRunControl(IntegrationTestCase):
	def setUp(self):
		self.run = make_test_run()

	def tearDown(self):
		frappe.set_user("Administrator")
		delete_docs("AI Run", [self.run.name])
		frappe.db.commit()

	def test_run_without_owner_user_is_refused(self):
		frappe.db.set_value("AI Run", self.run.name, "user", "", update_modified=False)
		with patch("ai_agent.runtime.orchestrator.execute_run") as mocked:
			execute_run_job(self.run.name)
		mocked.assert_not_called()
		self.assertEqual(frappe.db.get_value("AI Run", self.run.name, "status"), "Failed")

	def test_guest_owner_is_refused(self):
		with patch("ai_agent.runtime.orchestrator.execute_run") as mocked:
			execute_run_job(self.run.name, user="Guest")
		mocked.assert_not_called()
		self.assertEqual(frappe.db.get_value("AI Run", self.run.name, "status"), "Failed")

	def test_valid_owner_executes(self):
		with patch("ai_agent.runtime.orchestrator.execute_run") as mocked:
			execute_run_job(self.run.name, user="Administrator")
		mocked.assert_called_once_with(self.run.name)

	def test_terminal_run_is_skipped(self):
		frappe.db.set_value("AI Run", self.run.name, "status", "Completed", update_modified=False)
		with patch("ai_agent.runtime.orchestrator.execute_run") as mocked:
			execute_run_job(self.run.name)
		mocked.assert_not_called()
