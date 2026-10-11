"""Phase B2: native Anthropic provider (translation, caching, usage, key safety)."""

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.providers import anthropic_provider as ap

SECRET = "sk-ant-test-SECRET-never-logged"


class FakeSettings(dict):
	def get(self, key, default=None):
		return super().get(key, default)

	def get_password(self, fieldname):
		return SECRET if fieldname == "api_key" else None


def _provider(**overrides):
	return ap.AnthropicProvider(FakeSettings(default_model="claude-opus-5", base_url="https://proxy.example", **overrides))


MESSAGES = [
	{"role": "system", "content": f"STATIC RULES\n\n{ap.DYNAMIC_CONTEXT_MARKER}\n\nCurrent user: a@b.c"},
	{"role": "user", "content": "كم المبيعات اليوم؟"},
	{"role": "assistant", "content": None, "tool_calls": [
		{"id": "call_1", "type": "function", "function": {"name": "metrics__query", "arguments": '{"metric": "net_sales"}'}},
	]},
	{"role": "tool", "tool_call_id": "call_1", "content": '{"total": 5}'},
	{"role": "system", "content": "Finalize now."},
]
TOOLS = [
	{"name": "metrics__query", "description": "metrics", "parameters": {"type": "object", "properties": {}}},
	{"name": "tools__request_more", "description": "more", "parameters": {"type": "object", "properties": {}}},
]


class TestAnthropicTranslation(IntegrationTestCase):
	def test_static_system_prompt_and_tools_are_cached(self):
		request = _provider().build_request({"messages": MESSAGES, "reasoning_effort": "low"}, TOOLS)
		self.assertEqual(request["system"][0]["text"], "STATIC RULES")
		self.assertEqual(request["system"][0]["cache_control"], {"type": "ephemeral"})
		self.assertNotIn("cache_control", request["system"][1])
		self.assertIn("Current user", request["system"][1]["text"])
		self.assertEqual(request["tools"][-1]["cache_control"], {"type": "ephemeral"})
		self.assertNotIn("cache_control", request["tools"][0])
		self.assertEqual(request["tools"][0]["input_schema"], {"type": "object", "properties": {}})
		self.assertEqual(request["output_config"], {"effort": "low"})

	def test_tool_loop_messages(self):
		request = _provider().build_request({"messages": MESSAGES}, TOOLS)
		roles = [turn["role"] for turn in request["messages"]]
		self.assertEqual(roles, ["user", "assistant", "user"])
		tool_use = request["messages"][1]["content"][0]
		self.assertEqual((tool_use["type"], tool_use["id"], tool_use["input"]), ("tool_use", "call_1", {"metric": "net_sales"}))
		result, instruction = request["messages"][2]["content"]
		self.assertEqual((result["type"], result["tool_use_id"]), ("tool_result", "call_1"))
		self.assertEqual(instruction["type"], "text")
		self.assertIn("Finalize now.", instruction["text"])

	def test_provider_blocks_are_replayed_unchanged(self):
		thinking = {"type": "thinking", "thinking": "", "signature": "sig"}
		tool_use = {"type": "tool_use", "id": "t1", "name": "metrics__query", "input": {}}
		messages = [
			{"role": "user", "content": "q"},
			{"role": "assistant", "content": None, "tool_calls": [], "provider_blocks": [thinking, tool_use]},
		]
		_, turns = ap.convert_messages(messages)
		self.assertEqual(turns[1]["content"], [thinking, tool_use])

	def test_images_and_pdfs(self):
		messages = [{"role": "user", "content": [
			{"type": "text", "text": "read"},
			{"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
			{"type": "image_url", "image_url": {"url": "data:application/pdf;base64,JVBERi0xLjQ="}},
		]}]
		_, turns = ap.convert_messages(messages)
		kinds = [(b["type"], b.get("source", {}).get("media_type")) for b in turns[0]["content"]]
		self.assertEqual(kinds, [("text", None), ("image", "image/png"), ("document", "application/pdf")])
		self.assertEqual(ap.pdf_document_block(b"%PDF")["source"]["media_type"], "application/pdf")

	def test_response_and_usage_translation(self):
		message = MagicMock()
		message.to_dict.return_value = {
			"id": "msg_1", "model": "claude-opus-5", "stop_reason": "tool_use",
			"content": [
				{"type": "thinking", "thinking": "", "signature": "s"},
				{"type": "text", "text": "checking"},
				{"type": "tool_use", "id": "t1", "name": "metrics__query", "input": {"metric": "net_sales"}},
			],
			"usage": {"input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 3000},
		}
		response = ap.to_openai_response(message)
		out = response["choices"][0]["message"]
		self.assertEqual(response["choices"][0]["finish_reason"], "tool_calls")
		self.assertEqual(json.loads(out["tool_calls"][0]["function"]["arguments"]), {"metric": "net_sales"})
		self.assertEqual(len(out["provider_blocks"]), 3)
		usage = _provider().get_usage(response)
		self.assertEqual(usage, {
			"input_tokens": 4100, "output_tokens": 50,
			"cache_creation_input_tokens": 1000, "cache_read_input_tokens": 3000,
		})

	def test_create_step_uses_base_url_and_streaming(self):
		final = MagicMock()
		final.to_dict.return_value = {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {}}
		stream = MagicMock()
		stream.__enter__.return_value.get_final_message.return_value = final
		client = MagicMock()
		client.messages.stream.return_value = stream
		with patch("anthropic.Anthropic", return_value=client) as constructor:
			response = _provider().create_step(instructions={"messages": MESSAGES[:2]}, input_items=[], tools=TOOLS, state={})
		self.assertEqual(constructor.call_args.kwargs["base_url"], "https://proxy.example")
		self.assertEqual(response["choices"][0]["message"]["content"], "ok")
		self.assertEqual(client.messages.stream.call_args.kwargs["model"], "claude-opus-5")


class TestAnthropicKeySafety(IntegrationTestCase):
	def test_api_error_never_contains_the_key(self):
		import anthropic
		import httpx2

		request = httpx2.Request("POST", "https://proxy.example/v1/messages", headers={"x-api-key": SECRET})
		response = httpx2.Response(401, request=request, json={"error": {"message": "invalid x-api-key"}})
		error = anthropic.AuthenticationError("invalid x-api-key", response=response, body={"error": {"message": "invalid x-api-key"}})
		client = MagicMock()
		client.messages.stream.side_effect = error
		with patch("anthropic.Anthropic", return_value=client):
			with self.assertRaises(ap.AnthropicProviderError) as ctx:
				_provider().create_step(instructions={"messages": MESSAGES[:2]}, input_items=[], tools=[], state={})
		self.assertIn("401", str(ctx.exception))
		self.assertNotIn(SECRET, str(ctx.exception))
		self.assertNotIn(SECRET, frappe.as_json(frappe.get_all("Error Log", fields=["error"], limit=5)))

	def test_missing_key_is_a_clear_error(self):
		provider = ap.AnthropicProvider(FakeSettings(default_model="claude-opus-5"))
		provider.settings.get_password = lambda fieldname: None
		with self.assertRaises(ap.AnthropicProviderError):
			provider._get_client()

	def test_openai_provider_drops_private_blocks(self):
		from ai_agent.runtime.model_provider import _openai_messages

		cleaned = _openai_messages([{"role": "assistant", "content": "x", "provider_blocks": [{"type": "thinking"}]}])
		self.assertEqual(cleaned, [{"role": "assistant", "content": "x"}])

	def test_factory_returns_anthropic_provider(self):
		from ai_agent.runtime import model_provider

		config = FakeSettings(provider_type="Anthropic", default_model="claude-opus-5")
		with patch.object(model_provider, "resolve_provider_doc", return_value=MagicMock()), \
				patch.object(model_provider, "ProviderConfig", return_value=config):
			self.assertIsInstance(model_provider.get_provider("X"), ap.AnthropicProvider)


class TestAnthropicBaseUrl(IntegrationTestCase):
	def test_v1_suffix_is_dropped(self):
		from ai_agent.engine.providers.factory import anthropic_base_url

		for url in ("https://api.anthropic.com/v1/", "https://api.anthropic.com/v1", "https://api.anthropic.com/v1/messages", " https://api.anthropic.com/ "):
			self.assertEqual(anthropic_base_url(url), "https://api.anthropic.com", url)
		self.assertEqual(anthropic_base_url("https://proxy.example/anthropic"), "https://proxy.example/anthropic")
		self.assertEqual(anthropic_base_url(""), "")
