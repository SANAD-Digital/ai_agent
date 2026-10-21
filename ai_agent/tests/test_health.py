"""AI Agent Health: the report runs, and alerts reach System Managers once per problem."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.runtime import health


class TestHealth(IntegrationTestCase):
	def setUp(self):
		frappe.cache().delete_keys("ai_agent:health_alert:")

	def tearDown(self):
		frappe.cache().delete_keys("ai_agent:health_alert:")
		frappe.db.delete("Notification Log", {"subject": ["like", "%AI assistant%test-stuck%"]})

	def test_report_runs(self):
		from ai_agent.ai_agent.report.ai_agent_health.ai_agent_health import execute

		columns, data, _message, chart = execute({"days": 3})
		self.assertTrue(any(c["fieldname"] == "failed_share" for c in columns))
		self.assertIn("datasets", chart["data"])

	def test_alert_is_sent_once(self):
		stuck = [frappe._dict(name="test-stuck")]
		with patch.object(health, "stuck_messages", return_value=stuck), patch.object(health, "failed_jobs", return_value=[]), \
				patch.object(health, "STUCK_MINUTES", "test-stuck"):
			first = health.check_and_alert()
			before = frappe.db.count("Notification Log", {"subject": ["like", "%AI assistant%test-stuck%"]})
			health.check_and_alert()
			after = frappe.db.count("Notification Log", {"subject": ["like", "%AI assistant%test-stuck%"]})
		self.assertTrue(any(key == "stuck" for key, _text in first))
		self.assertGreater(before, 0)
		self.assertEqual(before, after)
