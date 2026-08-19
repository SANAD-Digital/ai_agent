import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.api import get_chat_messages, list_chat_sessions
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

	def test_run_creates_and_updates_chat_session_doc(self):
		r1, r2, r3 = self._make_chain()
		session_name = frappe.db.get_value("AI Run", r1.name, "chat_session")
		self.assertTrue(session_name)
		self.assertEqual(frappe.db.get_value("AI Run", r3.name, "chat_session"), session_name)
		session = frappe.db.get_value(
			"AI Chat Session", session_name,
			["title", "root_run", "last_run", "message_count", "status", "user"],
			as_dict=True,
		)
		self.assertIn("سؤال الجلسة الأول", session.title)
		self.assertEqual(session.root_run, r1.name)
		self.assertEqual(session.last_run, r3.name)
		self.assertEqual(session.message_count, 3)
		self.assertEqual(session.status, "Active")

	def test_get_chat_messages_accepts_session_name(self):
		r1, r2, r3 = self._make_chain()
		session_name = frappe.db.get_value("AI Run", r1.name, "chat_session")
		result = get_chat_messages(session_name)
		self.assertEqual(result["session"], session_name)
		run_names = {m.get("run_name") for m in result["messages"]}
		self.assertTrue({r1.name, r2.name, r3.name}.issubset(run_names))

	def test_rename_and_archive_session(self):
		from ai_agent.api import archive_chat_session, rename_chat_session

		r1, _, _ = self._make_chain()
		session_name = frappe.db.get_value("AI Run", r1.name, "chat_session")
		rename_chat_session(session_name, "عنوان جديد للمحادثة")
		self.assertEqual(
			frappe.db.get_value("AI Chat Session", session_name, "title"),
			"عنوان جديد للمحادثة",
		)
		archive_chat_session(session_name)
		self.assertEqual(
			frappe.db.get_value("AI Chat Session", session_name, "status"), "Archived"
		)
		active = [s["name"] for s in list_chat_sessions(limit=100)["sessions"]]
		self.assertNotIn(session_name, active)
