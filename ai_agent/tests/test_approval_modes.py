"""Per-conversation approval modes: Ask (every action waits) and Auto (drafts and edits
run at once). Submit, cancel, workflow and email always wait; four-eyes or the admin
setting turn Auto off entirely."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.chat import api
from ai_agent.engine import runner
from ai_agent.engine.tools import ToolExecutor
from ai_agent.engine.types import APPROVAL, TOOL_USE, Block
from ai_agent.runtime import policy
from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.tests.utils import ensure_test_agent

TITLE = "approval-mode-test note"


class TestApprovalModes(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		ensure_test_agent()
		self.saved = {f: frappe.db.get_single_value("AI Settings", f) for f in ("allow_auto_approval", "require_four_eyes")}
		self._settings(allow_auto_approval=1, require_four_eyes=0)
		with patch.object(runner, "enqueue"):
			out = api.send_message(text="اختبار وضع الموافقة", agent="ERP Power Operator")
		self.session = out["session"]["name"]
		self.run = out["assistant_message"]["run"]

	def tearDown(self):
		frappe.set_user("Administrator")
		self._settings(**self.saved)
		for name in frappe.get_all("Note", filters={"title": ["like", f"{TITLE}%"]}, pluck="name"):
			frappe.delete_doc("Note", name, force=True, ignore_permissions=True)
		for run in frappe.get_all("AI Run", filters={"session": self.session}, pluck="name"):
			frappe.delete_doc("AI Run", run, ignore_permissions=True, force=True)
		frappe.delete_doc("AI Chat Session", self.session, ignore_permissions=True, force=True)
		frappe.db.commit()

	def _settings(self, **values):
		for field, value in values.items():
			frappe.db.set_single_value("AI Settings", field, value or 0)
		frappe.clear_document_cache("AI Settings", "AI Settings")

	def _create_note(self, title):
		agent = frappe.get_cached_doc("AI Agent", "ERP Power Operator")
		ex = ToolExecutor(PermissionContext("Administrator", agent.name, None), agent, self.run, session=self.session)
		return ex.execute(Block(kind=TOOL_USE, id="t-" + title[-4:], name="actions__request_create_draft",
			data={"doctype": "Note", "payload": {"title": title, "public": 1}}))

	def test_policy_only_drafts_and_edits_and_only_when_allowed(self):
		self.assertTrue(policy.auto_approves("Auto", "Create"))
		self.assertTrue(policy.auto_approves("Auto", "Update"))
		for kind in ("Submit", "Cancel", "Workflow", "Approved Method"):
			self.assertFalse(policy.auto_approves("Auto", kind), kind)
		self.assertFalse(policy.auto_approves("Ask", "Create"))
		self._settings(require_four_eyes=1)
		self.assertFalse(policy.auto_approves("Auto", "Create"), "four-eyes forbids self-approval")
		self._settings(require_four_eyes=0, allow_auto_approval=0)
		self.assertFalse(policy.auto_approves("Auto", "Create"), "the admin turned it off")

	def test_ask_mode_pauses_for_approval(self):
		outcome = self._create_note(TITLE + " ask")
		self.assertTrue(outcome.approval, "the reply waits for a decision")
		self.assertFalse(frappe.db.exists("Note", {"title": TITLE + " ask"}))

	def test_auto_mode_creates_at_once_and_keeps_the_card(self):
		api.set_approval_mode(self.session, "Auto")
		outcome = self._create_note(TITLE + " auto")
		self.assertFalse(outcome.approval, "no pause in auto mode")
		self.assertFalse(outcome.result.is_error, outcome.result.text)
		self.assertTrue(frappe.db.exists("Note", {"title": TITLE + " auto"}))
		card = next(b for b in outcome.extras if b.kind == APPROVAL)
		self.assertTrue(card.data["auto"])
		self.assertEqual(card.data["status"], "Executed")

	def test_auto_mode_cannot_be_set_when_turned_off(self):
		self._settings(allow_auto_approval=0)
		with self.assertRaises(frappe.PermissionError):
			api.set_approval_mode(self.session, "Auto")

	def test_switching_off_on_the_site_stops_auto_runs(self):
		api.set_approval_mode(self.session, "Auto")
		self._settings(require_four_eyes=1)
		outcome = self._create_note(TITLE + " fouryes")
		self.assertTrue(outcome.approval, "a stored Auto mode no longer applies once four-eyes is on")

	def test_approve_always_switches_the_conversation(self):
		outcome = self._create_note(TITLE + " always")
		approval = outcome.approval
		# the card's block, as the runner stores it
		msg = frappe.db.get_value("AI Chat Message", {"session": self.session, "role": "assistant"}, "name")
		frappe.get_doc({"doctype": "AI Message Block", "message": msg, "seq": 50, "block_type": "approval",
			"approval": approval, "status": "completed"}).insert(ignore_permissions=True)
		with patch.object(runner, "enqueue"):
			out = api.decide_approval(approval, "approve_always")
		self.assertEqual(out["session"]["approval_mode"], "Auto")
		self.assertEqual(out["approval"]["status"], "Executed")
		self.assertEqual(frappe.db.get_value("AI Chat Session", self.session, "approval_mode"), "Auto")
