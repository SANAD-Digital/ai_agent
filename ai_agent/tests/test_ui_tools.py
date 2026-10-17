"""ui__ask / ui__suggest: chat-only tools that draw a question card or follow-up chips.
The reply ends at the call; the cards never reach the model's history."""

from dataclasses import dataclass, field
from unittest import TestCase

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.engine.context.history import to_messages
from ai_agent.engine.loop import Loop, LoopConfig
from ai_agent.engine.context.compactor import Compactor
from ai_agent.engine.providers.base import CancelToken
from ai_agent.engine.providers.fake import FakeProvider
from ai_agent.engine.tools import ToolExecutor
from ai_agent.engine.types import QUESTION, SUGGESTIONS, TEXT, TOOL_RESULT, TOOL_USE, Block, Message, ToolSpec
from ai_agent.runtime.permission_context import PermissionContext


@dataclass
class Outcome:
	result: object = None
	extras: list = field(default_factory=list)
	approval: str = ""
	approval_block: object = None
	duration_ms: int = 0
	tool_call: str = ""
	end_turn: bool = False


class AskExecutor:
	def specs(self):
		return [ToolSpec(name="ui__ask", description="ask", parameters={"type": "object", "properties": {}})]

	def is_read_only(self, name):
		return False

	def execute(self, call):
		return Outcome(result=Block(kind=TOOL_RESULT, id=call.id, name=call.name, text="shown"),
			extras=[Block(kind=QUESTION, id=call.id, name=call.name, data={"question": "Which?", "options": []})], end_turn=True)


class TestAskEndsTheTurn(TestCase):
	def test_the_reply_stops_after_the_question(self):
		provider = FakeProvider([[("text", "Let me ask."), ("tool", "ui__ask", {"question": "Which?"})], [("text", "never")]])
		loop = Loop(provider, AskExecutor(), Compactor(200_000), CancelToken(), LoopConfig(model="fake", retries=1, backoff_seconds=0), "S", "D", sleep=lambda s: None)
		events = list(loop.run([Message(role="user", blocks=[Block(kind=TEXT, text="hi")])]))
		self.assertEqual(len(provider.requests), 1, "no second model call: the user answers next")
		self.assertIn("question.asked", [e.type for e in events])
		self.assertEqual(events[-1].type, "message.done")
		self.assertEqual(events[-1].status, "completed")

	def test_cards_stay_out_of_the_models_history(self):
		blocks = [Block(kind=TOOL_USE, id="t1", name="ui__ask", data={"question": "q"}), Block(kind=QUESTION, id="t1", data={"question": "q"}),
			Block(kind=TOOL_RESULT, id="t1", name="ui__ask", text="shown"), Block(kind=SUGGESTIONS, data={"items": ["x"]})]
		messages = to_messages([("assistant", 1, "m1", blocks)])
		kinds = [b.kind for m in messages for b in m.blocks]
		self.assertNotIn(QUESTION, kinds)
		self.assertNotIn(SUGGESTIONS, kinds)
		self.assertEqual(kinds, [TOOL_USE, TOOL_RESULT], "the call and its result still pair up for the provider")


class TestUiTools(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.agent = frappe.get_doc("AI Agent", "ERP Power Operator")
		self.session = frappe.get_doc({"doctype": "AI Chat Session", "title": "ui tools test", "user": "Administrator",
			"agent": self.agent.name, "surface": "Desk"}).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.db.rollback()

	def _executor(self):
		return ToolExecutor(PermissionContext("Administrator", self.agent.name), self.agent, None, session=self.session.name)

	def test_offered_in_the_chat_not_on_whatsapp(self):
		names = {s.name for s in self._executor().specs()}
		self.assertTrue({"ui__ask", "ui__suggest"} <= names)
		self.session.db_set("surface", "WhatsApp")
		self.assertFalse({"ui__ask", "ui__suggest"} & {s.name for s in self._executor().specs()})

	def test_ask_draws_a_card_and_ends_the_turn(self):
		out = self._executor().execute(Block(kind=TOOL_USE, id="q1", name="ui__ask", data={
			"question": "أي شركة؟", "options": [{"label": "روشن", "description": "المصنع"}, {"label": "طيبة"}]}))
		self.assertTrue(out.end_turn)
		self.assertEqual(out.extras[0].kind, QUESTION)
		self.assertEqual([o["label"] for o in out.extras[0].data["options"]], ["روشن", "طيبة"])

	def test_ask_needs_two_options(self):
		out = self._executor().execute(Block(kind=TOOL_USE, id="q2", name="ui__ask", data={"question": "?", "options": [{"label": "one"}]}))
		self.assertTrue(out.result.is_error)
		self.assertFalse(out.end_turn)

	def test_suggest_keeps_three(self):
		out = self._executor().execute(Block(kind=TOOL_USE, id="s1", name="ui__suggest", data={"suggestions": ["a", "b", "c", "d"]}))
		self.assertEqual(out.extras[0].data["items"], ["a", "b", "c"])
