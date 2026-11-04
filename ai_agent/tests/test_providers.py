
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.chat.api import available_providers
from ai_agent.tests.utils import delete_docs, ensure_test_agent

PROVIDER = "Unit Test Provider"


class TestProviders(IntegrationTestCase):
	def setUp(self):
		ensure_test_agent()
		self._created = []
		if not frappe.db.exists("AI Provider", PROVIDER):
			frappe.get_doc({
				"doctype": "AI Provider",
				"provider_name": PROVIDER,
				"enabled": 1,
				"is_default": 0,
				"provider_type": "OpenAI Compatible",
				"base_url": "http://localhost:9/v1",
				"api_key": "unit-secret-key-1234",
				"default_model": "unit-model",
				"models": [
					{"model_name": "unit-model", "is_default": 1},
					{"model_name": "unit-vision", "supports_vision": 1, "context_window_tokens": 64000},
				],
			}).insert(ignore_permissions=True)
		self._runs = []

	def tearDown(self):
		delete_docs("AI Run", self._runs)
		delete_docs("AI Provider", self._created)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		if frappe.db.exists("AI Provider", PROVIDER):
			frappe.delete_doc("AI Provider", PROVIDER, ignore_permissions=True, force=True)
			frappe.db.commit()
		super().tearDownClass()

	def test_available_providers_exposes_models_not_secrets(self):
		from ai_agent.runtime import policy

		# the test provider is OpenAI-compatible: listed whatever the site's provider policy (managed mode allows Anthropic only)
		with patch.object(policy, "provider_allowed", return_value=True):
			providers = available_providers()
		mine = next((p for p in providers if p["name"] == PROVIDER), None)
		self.assertIsNotNone(mine)
		self.assertEqual([m["model_name"] for m in mine["models"]], ["unit-model", "unit-vision"])
		self.assertNotIn("api_key", mine)

	def test_only_one_default_provider(self):
		original = frappe.db.get_value("AI Provider", {"is_default": 1}, "name")
		try:
			first = frappe.get_doc({
				"doctype": "AI Provider", "provider_name": "Unit Default A", "enabled": 1, "is_default": 1,
				"provider_type": "OpenAI", "default_model": "a-model",
			}).insert(ignore_permissions=True)
			self._created.append(first.name)
			second = frappe.get_doc({
				"doctype": "AI Provider", "provider_name": "Unit Default B", "enabled": 1, "is_default": 1,
				"provider_type": "OpenAI", "default_model": "b-model",
			}).insert(ignore_permissions=True)
			self._created.append(second.name)
			self.assertEqual(frappe.db.get_value("AI Provider", first.name, "is_default"), 0)
			self.assertEqual(frappe.db.count("AI Provider", {"is_default": 1}), 1)
		finally:
			if original:
				frappe.db.set_value("AI Provider", original, "is_default", 1, update_modified=False)
				for name in self._created:
					frappe.db.set_value("AI Provider", name, "is_default", 0, update_modified=False)
				frappe.clear_document_cache("AI Provider", original)


class TestFetchModels(IntegrationTestCase):
	def test_openai_compatible_list_is_mapped_and_filtered(self):
		from types import SimpleNamespace
		from unittest.mock import MagicMock, patch as mock_patch

		from ai_agent.ai_agent.doctype.ai_provider import ai_provider

		def model(**raw):
			return SimpleNamespace(id=raw["id"], model_dump=lambda: raw)

		listed = [
			model(id="deepseek-flash", name="DeepSeek-V4.1-Flash", context_window=1048576, max_output_tokens=393216, input_modalities=["text", "image"], effort={"supported_levels": ["low"]}),
			model(id="text-embedding-3-small"),
			model(id="vendor/model", context_length=200000, architecture={"input_modalities": ["text"]}, supported_parameters=["tools"], top_provider={"max_completion_tokens": 8000}),
		]
		client = MagicMock()
		client.models.list.return_value = listed
		with mock_patch("openai.OpenAI", return_value=client):
			out = {m["model_name"]: m for m in ai_provider._openai_models({"api_key": "k", "base_url": "https://x"})}
		self.assertEqual(out["text-embedding-3-small"]["model_type"], "Embedding")   # kept, but not a chat model
		self.assertEqual(out["deepseek-flash"]["model_type"], "Chat")
		self.assertEqual(out["deepseek-flash"]["context_window_tokens"], 1048576)
		self.assertEqual(out["deepseek-flash"]["supports_vision"], 1)
		self.assertEqual(out["deepseek-flash"]["supports_thinking"], 1)
		self.assertEqual(out["deepseek-flash"]["label"], "DeepSeek-V4.1-Flash")
		self.assertEqual(out["vendor/model"]["max_output_tokens"], 8000)  # OpenRouter shape
		self.assertEqual(out["vendor/model"]["supports_tools"], 1)


class TestStrictToolSchemas(IntegrationTestCase):
	def test_strict_shape(self):
		from ai_agent.engine.providers.openai_compat import strict_schema

		schema = {"type": "object", "properties": {"q": {"type": "string"}, "n": {"type": "integer", "default": 5},
			"mode": {"type": "string", "enum": ["a", "b"]}, "tags": {"type": "array", "items": {"type": "object", "properties": {"k": {"type": "string"}}}}},
			"required": ["q"]}
		out = strict_schema(schema)
		self.assertEqual(out["required"], ["q", "n", "mode", "tags"])
		self.assertFalse(out["additionalProperties"])
		self.assertEqual(out["properties"]["n"]["type"], ["integer", "null"], "optional fields become nullable")
		self.assertNotIn("default", out["properties"]["n"])
		self.assertIn(None, out["properties"]["mode"]["enum"])
		self.assertFalse(out["properties"]["tags"]["items"]["additionalProperties"], "nested objects too")

	def test_free_form_fields_stay_non_strict(self):
		from ai_agent.engine.providers.openai_compat import OpenAICompatProvider, strict_schema
		from ai_agent.engine.types import ToolSpec

		free = {"type": "object", "properties": {"payload": {"type": "object"}}, "required": ["payload"]}
		self.assertIsNone(strict_schema(free))
		self.assertIsNone(strict_schema({"type": "object", "properties": {"value": {}}}))
		provider = OpenAICompatProvider({"strict_tools": True, "model": "m"})
		self.assertNotIn("strict", provider._tool(ToolSpec(name="t", description="d", parameters=free))["function"])
		self.assertTrue(provider._tool(ToolSpec(name="t", description="d", parameters={"type": "object", "properties": {"a": {"type": "string"}}}))["function"]["strict"])

	def test_null_arguments_are_dropped(self):
		from ai_agent.engine.tools import drop_nulls

		self.assertEqual(drop_nulls({"a": None, "b": 1, "c": {"d": None, "e": [{"f": None, "g": 2}]}}), {"b": 1, "c": {"e": [{"g": 2}]}})


class TestProviderKey(IntegrationTestCase):
	"""No request goes out with a placeholder key: a hosted provider without one fails before calling it."""

	def test_hosted_providers_need_a_key(self):
		from ai_agent.engine.providers.factory import _needs_key

		self.assertTrue(_needs_key({"type": "OpenAI", "base_url": "https://api.openai.com/v1"}))
		self.assertTrue(_needs_key({"type": "Anthropic", "base_url": ""}))
		self.assertTrue(_needs_key({"type": "OpenAI Compatible", "base_url": "https://api.deepseek.com"}))
		self.assertFalse(_needs_key({"type": "Ollama", "base_url": ""}))
		self.assertFalse(_needs_key({"type": "OpenAI Compatible", "base_url": "http://localhost:8000/v1"}))
		self.assertFalse(_needs_key({"type": "OpenAI Compatible", "base_url": "http://127.0.0.1:1234/v1"}))

	def test_provider_without_a_key_is_refused_with_its_name(self):
		from ai_agent.engine.providers import factory

		name = "Keyless Test Provider"
		frappe.get_doc({"doctype": "AI Provider", "provider_name": name, "provider_type": "OpenAI Compatible", "enabled": 1,
			"base_url": "https://api.example.com/v1", "models": [{"model_name": "m", "model_type": "Chat", "supports_tools": 1}]}).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("AI Provider", name, force=True, ignore_permissions=True))
		with patch("ai_agent.runtime.policy.assert_provider_allowed", lambda *_: None):
			with self.assertRaises(frappe.ValidationError) as cm:
				factory.provider_for(name, "m")
		self.assertIn(name, str(cm.exception))
