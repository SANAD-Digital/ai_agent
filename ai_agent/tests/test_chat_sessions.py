import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.api import get_chat_messages, list_chat_sessions
from ai_agent.patches.v0_1.backfill_ai_run_root import execute as backfill_root
from ai_agent.runtime.session_context import SessionContextService
from ai_agent.tests.utils import TEST_AGENT, delete_docs, ensure_test_agent


class TestChatSessions(IntegrationTestCase):
	"""Chat sessions are identified by the indexed root_run field instead of
	walking parent_run chains (which split sessions and capped context)."""

	def setUp(self):
		ensure_test_agent()
		self._runs = []

	def tearDown(self):
		delete_docs("AI Run", list(reversed(self._runs)))
		frappe.db.commit()

	def _make_run(self, prompt, parent=None):
		run = frappe.get_doc({
			"doctype": "AI Run",
			"agent": TEST_AGENT,
			"user": "Administrator",
			"prompt": prompt,
			"status": "Completed",
			"final_answer": f"جواب: {prompt}",
			"parent_run": parent,
		})
		run.insert(ignore_permissions=True)
		self._runs.append(run.name)
		return run

	def _make_chain(self):
		r1 = self._make_run("سؤال الجلسة الأول")
		r2 = self._make_run("متابعة ثانية", parent=r1.name)
		r3 = self._make_run("متابعة ثالثة", parent=r2.name)
		return r1, r2, r3

	def test_root_is_stamped_on_insert_and_inherited(self):
		r1, r2, r3 = self._make_chain()
		self.assertEqual(frappe.db.get_value("AI Run", r1.name, "root_run"), r1.name)
		self.assertEqual(frappe.db.get_value("AI Run", r2.name, "root_run"), r1.name)
		self.assertEqual(frappe.db.get_value("AI Run", r3.name, "root_run"), r1.name)

	def test_get_chat_messages_returns_whole_session_from_any_member(self):
		r1, r2, r3 = self._make_chain()
		result = get_chat_messages(r3.name)
		run_names = {m.get("run_name") for m in result["messages"]}
		self.assertTrue({r1.name, r2.name, r3.name}.issubset(run_names))

	def test_list_chat_sessions_groups_by_root(self):
		r1, r2, r3 = self._make_chain()
		sessions = list_chat_sessions(limit=10)["sessions"]
		mine = next((s for s in sessions if s["root_run"] == r1.name), None)
		self.assertIsNotNone(mine)
		self.assertEqual(mine["message_count"], 3)
		self.assertEqual(mine["latest_run"], r3.name)
		self.assertIn("سؤال الجلسة الأول", mine["title"])

	def test_session_context_uses_root_and_orders_by_creation(self):
		r1, r2, r3 = self._make_chain()
		previous = SessionContextService().get_previous_runs(frappe.get_doc("AI Run", r3.name))
		self.assertEqual([row.name for row in previous], [r1.name, r2.name])

	def test_backfill_stamps_legacy_chains(self):
		r1, r2, r3 = self._make_chain()
		for name in (r1.name, r2.name, r3.name):
			frappe.db.set_value("AI Run", name, "root_run", "", update_modified=False)
		backfill_root()
		self.assertEqual(frappe.db.get_value("AI Run", r2.name, "root_run"), r1.name)
		self.assertEqual(frappe.db.get_value("AI Run", r3.name, "root_run"), r1.name)
		orphan = self._make_run("يتيم بلا أب")
		frappe.db.set_value("AI Run", orphan.name, "root_run", "", update_modified=False)
		frappe.db.set_value("AI Run", orphan.name, "parent_run", "AI-RUN-GONE", update_modified=False)
		backfill_root()
		self.assertEqual(frappe.db.get_value("AI Run", orphan.name, "root_run"), orphan.name)
