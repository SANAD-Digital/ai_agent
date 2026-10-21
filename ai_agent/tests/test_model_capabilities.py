import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.engine.providers.base import caps_from_config, guess_context_window, guess_max_output, model_limits


class TestModelCapabilities(IntegrationTestCase):
	"""One source for every path: the provider's model row, else the family estimates."""

	def test_families(self):
		cases = {
			"claude-opus-5-5": (200_000, 32_000),
			"gemini-3.8-flash": (1_048_576, 65_536),
			"gemini-3.8-flash-tts": (8_192, 16_384),
			"google/gemma-4-31b-it:free": (262_144, 32_768),
			"deepseek-v4-flash": (1_048_576, 393_216),
			"deepseek-chat": (128_000, 8_192),
			"gpt-5.6": (1_050_000, 128_000),
			"gpt-5-mini": (400_000, 128_000),
			"gpt-4o-mini": (128_000, 16_384),
			"o4-mini": (200_000, 100_000),
			"openai/gpt-oss-120b": (131_072, 32_768),
			"llama-3.3-70b-versatile": (131_072, 8_192),
			"moonshotai/kimi-k2-instruct": (131_072, 16_384),
			"totally-unknown-model": (128_000, 8_192),
		}
		for model, (context, output) in cases.items():
			self.assertEqual(model_limits(model)[:2], (context, output), model)

	def test_local_ollama_stays_small(self):
		self.assertEqual(guess_context_window("llama3.1:8b", local=True), 8_192)
		self.assertLessEqual(guess_max_output("qwen2.5:7b", local=True), 4_096)
		caps = caps_from_config({"type": "OpenAI Compatible", "base_url": "http://localhost:11434/v1", "models": {}}, "llama3.1:8b")
		self.assertEqual(caps.context_window, 8_192)

	def test_provider_row_wins_everywhere(self):
		row = {"context_window_tokens": 32_000, "max_output_tokens": 2_000}
		caps = caps_from_config({"type": "OpenAI Compatible", "base_url": "", "models": {"gemini-3.8-flash": row}}, "gemini-3.8-flash")
		self.assertEqual((caps.context_window, caps.max_output_tokens), (32_000, 2_000))

		provider = frappe.get_all("AI Provider", pluck="name", limit=1)
		if not provider:
			self.skipTest("no AI Provider on this site")
		doc = frappe.get_doc("AI Provider", provider[0])
		doc.append("models", {"model_name": "test-capability-model", "context_window_tokens": 32_000, "supports_vision": 1})
		doc.save(ignore_permissions=True)
		frappe.db.rollback()


class TestPromptBudget(IntegrationTestCase):
	def test_tool_schemas_count_toward_the_prompt(self):
		from types import SimpleNamespace

		from ai_agent.engine.loop import Loop
		from ai_agent.engine.types import ToolSpec

		schema = {"type": "object", "properties": {f"field_{i}": {"type": "string", "description": "x" * 60} for i in range(20)}}
		bare = Loop.base_tokens(SimpleNamespace(system_static="", system_dynamic=""), [ToolSpec(name="t", description="short", parameters={})])
		full = Loop.base_tokens(SimpleNamespace(system_static="", system_dynamic=""), [ToolSpec(name="t", description="short", parameters=schema)])
		self.assertGreater(full - bare, 500, "a large parameter schema must raise the estimate")
