from types import SimpleNamespace

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.engine.errors import MESSAGES, kind_of, user_message
from ai_agent.engine.providers import openai_compat
from ai_agent.engine.types import TEXT, TOOL_RESULT, TOOL_USE, Block, Message


class TestFriendlyErrors(IntegrationTestCase):
	def test_raw_provider_text_never_reaches_the_user(self):
		raw = "Error code: 400 - [{'error': {'code': 400, 'message': 'Function call is missing a thought_signature in functionCall parts.', 'status': 'INVALID_ARGUMENT'}}]"
		text = user_message("error", raw)
		self.assertNotIn("400", text)
		self.assertNotIn("thought_signature", text)

	def test_specific_causes_are_recognised(self):
		self.assertEqual(kind_of("error", "Function calling is not enabled for models/x"), "no_tools")
		self.assertEqual(kind_of("rate_limit", "You exceeded your current quota"), "quota")
		self.assertEqual(kind_of("error", "models/foo is not found for API version v1beta"), "model")
		self.assertEqual(kind_of("auth", "Incorrect API key"), "auth")
		self.assertEqual(kind_of("weird", "?"), "error")

	def test_message_follows_the_users_language(self):
		user = frappe.get_all("User", filters={"enabled": 1, "user_type": "System User"}, pluck="name", limit=1)[0]
		lang = frappe.db.get_value("User", user, "language")
		try:
			frappe.db.set_value("User", user, "language", "ar")
			self.assertNotEqual(user_message("timeout", user=user), MESSAGES["timeout"])
			frappe.db.set_value("User", user, "language", "en")
			self.assertEqual(user_message("timeout", user=user), MESSAGES["timeout"])
		finally:
			frappe.db.set_value("User", user, "language", lang)


class TestGeminiThoughtSignature(IntegrationTestCase):
	def turn(self, signature=""):
		call = Block(kind=TOOL_USE, id="c1", name="get_time", data={"city": "Riyadh"}, signature=signature)
		second = Block(kind=TOOL_USE, id="c2", name="get_time", data={"city": "Aden"})
		return [
			Message(role="user", blocks=[Block(kind=TEXT, text="time?")]),
			Message(role="assistant", blocks=[call, second]),
			Message(role="user", blocks=[Block(kind=TOOL_RESULT, id="c1", text="14:05"), Block(kind=TOOL_RESULT, id="c2", text="14:05")]),
		]

	def calls(self, messages, gemini):
		out = openai_compat.convert_messages(messages, gemini=gemini)
		return next(m for m in out if m["role"] == "assistant")["tool_calls"]

	def test_signature_is_sent_back_with_its_call(self):
		calls = self.calls(self.turn("SIG"), gemini=True)
		self.assertEqual(calls[0]["extra_content"], {"google": {"thought_signature": "SIG"}})
		self.assertNotIn("extra_content", calls[1], "parallel calls after the first carry none")

	def test_history_without_signature_gets_the_placeholder(self):
		calls = self.calls(self.turn(""), gemini=True)
		self.assertEqual(calls[0]["extra_content"]["google"]["thought_signature"], openai_compat.GEMINI_SIGNATURE_PLACEHOLDER)

	def test_other_providers_get_no_extra_field(self):
		self.assertTrue(all("extra_content" not in c for c in self.calls(self.turn("SIG"), gemini=False)))

	def test_signature_is_read_from_the_stream(self):
		tc = SimpleNamespace(extra_content={"google": {"thought_signature": "ABC"}})
		self.assertEqual(openai_compat.gemini_signature(tc), "ABC")
		self.assertEqual(openai_compat.gemini_signature(SimpleNamespace()), "")


class TestChatCapableModels(IntegrationTestCase):
	def test_agent_speech_and_toolless_models_cannot_chat(self):
		from ai_agent.ai_agent.doctype.ai_provider.ai_provider import chat_capable

		self.assertFalse(chat_capable({"model_name": "antigravity-preview-05-2026"}))
		self.assertFalse(chat_capable({"model_name": "gemini-3.8-flash-tts"}))
		self.assertFalse(chat_capable({"model_name": "some-model", "supports_tools": 0}))
		self.assertTrue(chat_capable({"model_name": "gemini-3.8-flash", "supports_tools": 1}))

	def test_default_and_session_model_skip_models_that_cannot_chat(self):
		from ai_agent.engine.providers.factory import provider_config

		doc = frappe.get_doc({"doctype": "AI Provider", "provider_name": "x", "provider_type": "OpenAI Compatible", "models": [
			{"model_name": "antigravity-preview-05-2026", "supports_tools": 1},
			{"model_name": "gemini-3.8-flash", "supports_tools": 1},
		]})
		self.assertEqual(provider_config(doc)["model"], "gemini-3.8-flash", "first row by name is an agent model")
		self.assertEqual(provider_config(doc, "antigravity-preview-05-2026")["model"], "gemini-3.8-flash", "a session left on it falls back")

	def test_multiturn_error_is_worded_precisely(self):
		self.assertEqual(kind_of("error", "Multiturn chat is not enabled for models/antigravity-preview-05-2026"), "not_chat")
