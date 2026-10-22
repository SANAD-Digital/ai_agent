"""engine/approvals: a decision on a chat card updates the card, gives the waiting tool call its result and resumes
the reply; a reply that no longer waits keeps its card without resuming; expiry settles the waiting reply."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.chat import api
from ai_agent.engine import approvals, runner
from ai_agent.engine.tools import ToolExecutor
from ai_agent.engine.types import TOOL_USE, Block
from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.tests.utils import ensure_test_agent

TITLE = "engine-approvals-test note"


class TestEngineApprovals(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		ensure_test_agent()
		self.saved = {f: frappe.db.get_single_value("AI Settings", f) for f in ("require_four_eyes",)}
		frappe.db.set_single_value("AI Settings", "require_four_eyes", 0)
		frappe.clear_document_cache("AI Settings", "AI Settings")
		with patch.object(runner, "enqueue"):
			out = api.send_message(text="اختبار قرار الموافقة", agent="ERP Power Operator")
		self.session = out["session"]["name"]
		self.run = out["assistant_message"]["run"]
		self.message = out["assistant_message"]["name"]

	def tearDown(self):
		frappe.set_user("Administrator")
		for field, value in self.saved.items():
			frappe.db.set_single_value("AI Settings", field, value or 0)
		frappe.clear_document_cache("AI Settings", "AI Settings")
		for name in frappe.get_all("Note", filters={"title": ["like", f"{TITLE}%"]}, pluck="name"):
			frappe.delete_doc("Note", name, force=True, ignore_permissions=True)
		for run in frappe.get_all("AI Run", filters={"chat_session": self.session}, pluck="name"):
			frappe.delete_doc("AI Run", run, ignore_permissions=True, force=True)
		frappe.delete_doc("AI Chat Session", self.session, ignore_permissions=True, force=True)
		frappe.db.commit()

	def _waiting_card(self, suffix, status="awaiting_approval"):
		"""A reply stopped at an approval card, as the runner leaves it."""
		agent = frappe.get_cached_doc("AI Agent", "ERP Power Operator")
		ex = ToolExecutor(PermissionContext("Administrator", agent.name, None), agent, self.run, session=self.session)
		outcome = ex.execute(Block(kind=TOOL_USE, id="toolu_" + suffix, name="actions__request_create_draft",
			data={"doctype": "Note", "payload": {"title": f"{TITLE} {suffix}", "public": 1}}))
		frappe.get_doc({"doctype": "AI Message Block", "message": self.message, "seq": 10, "block_type": "approval",
			"approval": outcome.approval, "status": "completed", "tool_call_id": "toolu_" + suffix,
			"tool_name": "actions__request_create_draft"}).insert(ignore_permissions=True)
		frappe.db.set_value("AI Chat Message", self.message, "status", status, update_modified=False)
		return outcome.approval

	def _result_block(self):
		return frappe.db.get_value("AI Message Block", {"message": self.message, "block_type": "tool_result"},
			["tool_call_id", "tool_name", "status", "content", "is_user_visible"], as_dict=True)

	def test_approve_executes_answers_the_tool_call_and_resumes(self):
		approval = self._waiting_card("ok")
		with patch.object(runner, "enqueue") as enqueue:
			snapshot, message = approvals.decide(approval, "approve")
		self.assertEqual(message, self.message)
		self.assertEqual(snapshot["status"], "Executed")
		self.assertTrue(frappe.db.exists("Note", {"title": f"{TITLE} ok"}))
		result = self._result_block()
		self.assertEqual((result.tool_call_id, result.tool_name, result.status), ("toolu_ok", "actions__request_create_draft", "completed"))
		self.assertFalse(result.is_user_visible)
		self.assertIn("UNTRUSTED", result.content, "the tool result reaches the model as data")
		self.assertEqual(frappe.db.get_value("AI Chat Message", self.message, "status"), "pending")
		enqueue.assert_called_once_with(self.message, "Administrator", resume=True)
		card = frappe.db.get_value("AI Message Block", {"approval": approval}, ["status", "content_json"], as_dict=True)
		self.assertEqual(card.status, "completed")
		self.assertEqual(json.loads(card.content_json)["decided_by"], "Administrator")

	def test_reject_answers_with_a_failed_result_and_does_not_execute(self):
		approval = self._waiting_card("no")
		with patch.object(runner, "enqueue"):
			snapshot, _ = approvals.decide(approval, "reject", reason="not now")
		self.assertEqual(snapshot["status"], "Rejected")
		self.assertEqual(snapshot["reason"], "not now")
		self.assertFalse(frappe.db.exists("Note", {"title": f"{TITLE} no"}))
		self.assertEqual(self._result_block().status, "failed")
		self.assertIn("rejected", self._result_block().content)
		self.assertEqual(frappe.db.get_value("AI Message Block", {"approval": approval}, "status"), "denied")

	def test_a_reply_that_no_longer_waits_keeps_its_card_and_is_not_resumed(self):
		approval = self._waiting_card("late", status="completed")
		with patch.object(runner, "enqueue") as enqueue:
			snapshot, _ = approvals.decide(approval, "approve")
		self.assertEqual(snapshot["status"], "Executed")
		self.assertIsNone(self._result_block(), "a resumed reply would land above the newer turns")
		enqueue.assert_not_called()
		self.assertEqual(frappe.db.get_value("AI Chat Message", self.message, "status"), "completed")

	def test_settle_waiting_closes_only_a_waiting_reply(self):
		self._waiting_card("settle")
		self.assertTrue(approvals.settle_waiting(self.message, "superseded"))
		msg = frappe.db.get_value("AI Chat Message", self.message, ["status", "stop_reason"], as_dict=True)
		self.assertEqual((msg.status, msg.stop_reason), ("completed", "superseded"))
		self.assertFalse(approvals.settle_waiting(self.message, "superseded"), "already settled")

	def test_expiry_marks_the_card_and_settles_the_reply(self):
		approval = self._waiting_card("exp")
		frappe.db.set_value("AI Approval Request", approval, "status", "Expired")
		approvals.on_approval_expired(approval)
		card = frappe.db.get_value("AI Message Block", {"approval": approval}, ["status", "content_json"], as_dict=True)
		self.assertEqual(card.status, "failed")
		self.assertEqual(json.loads(card.content_json)["status"], "Expired")
		self.assertEqual(frappe.db.get_value("AI Chat Message", self.message, "stop_reason"), "approval_expired")
