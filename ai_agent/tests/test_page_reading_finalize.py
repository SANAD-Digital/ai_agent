import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.purchase_invoice_ai.api import _finalize_page_reading_if_done
from ai_agent.tests.utils import delete_docs, make_test_run


class TestPageReadingFinalize(IntegrationTestCase):
	"""Regression for FA-SESSION-1387: re-reading one failed page must not mark
	the run Failed while other pages in the session are already Read."""

	def setUp(self):
		self.run = make_test_run()
		self._sessions = []
		self.file = frappe.get_doc({
			"doctype": "File",
			"file_name": "finalize-test.txt",
			"content": "test",
			"is_private": 1,
		}).insert(ignore_permissions=True)

	def tearDown(self):
		delete_docs("Financial Attachment Session", self._sessions)
		delete_docs("AI Run", [self.run.name])
		delete_docs("File", [self.file.name])
		frappe.db.commit()

	def _make_session(self, page_states):
		session = frappe.get_doc({
			"doctype": "Financial Attachment Session",
			"source_run": self.run.name,
			"user": "Administrator",
			"status": "Reading",
			"grouping_mode": "auto",
		})
		for index, (status, selected) in enumerate(page_states, start=1):
			session.append("pages", {
				"page_id": f"P-{index}",
				"file": self.file.name,
				"file_name": "test.pdf",
				"page_number": index,
				"status": status,
				"selected": selected,
				"text_preview": "نص مقروء" if status == "Read" else "",
			})
		session.insert(ignore_permissions=True)
		self._sessions.append(session.name)
		return session

	def test_partial_failure_still_completes_with_read_pages(self):
		self._make_session([("Read", 1), ("Read", 1), ("Failed", 1)])
		_finalize_page_reading_if_done(self.run.name)
		self.assertEqual(frappe.db.get_value("AI Run", self.run.name, "status"), "Completed")

	def test_reread_of_single_failed_page_keeps_earlier_reads(self):
		# Only the failed page is selected for the retry; the earlier Read
		# pages are deselected — the old logic saw "no Read among selected"
		# and failed the whole run.
		self._make_session([("Read", 0), ("Read", 0), ("Failed", 1)])
		_finalize_page_reading_if_done(self.run.name)
		self.assertEqual(frappe.db.get_value("AI Run", self.run.name, "status"), "Completed")

	def test_total_failure_still_fails_the_run(self):
		self._make_session([("Failed", 1), ("Failed", 1)])
		_finalize_page_reading_if_done(self.run.name)
		row = frappe.db.get_value("AI Run", self.run.name, ["status", "error_summary"], as_dict=True)
		self.assertEqual(row.status, "Failed")
		self.assertIn("لم تتم قراءة", row.error_summary)
