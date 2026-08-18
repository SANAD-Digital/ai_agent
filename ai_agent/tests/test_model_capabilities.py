import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.runtime.model_provider import (
	DEFAULT_CONTEXT_WINDOW_TOKENS,
	estimate_context_window_tokens,
	supports_vision_model,
)


class TestModelCapabilities(IntegrationTestCase):
	def setUp(self):
		self._set_capabilities([])

	def tearDown(self):
		self._set_capabilities([])
		frappe.db.commit()

	def _set_capabilities(self, rows):
		settings = frappe.get_doc("AI Settings")
		settings.set("model_capabilities", rows)
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache("AI Settings", "AI Settings")

	def test_configured_row_overrides_context_window(self):
		self._set_capabilities([
			{"model_pattern": "my-custom-model", "context_window_tokens": 32000, "supports_vision": 0},
		])
		self.assertEqual(estimate_context_window_tokens("my-custom-model-v2"), 32000)

	def test_configured_row_is_authoritative_for_vision(self):
		self._set_capabilities([
			{"model_pattern": "text-only-model", "context_window_tokens": 0, "supports_vision": 0},
			{"model_pattern": "my-vision-model", "context_window_tokens": 0, "supports_vision": 1},
		])
		self.assertTrue(supports_vision_model("my-vision-model-v3"))
		self.assertFalse(supports_vision_model("text-only-model"))

	def test_zero_context_window_falls_back_to_heuristics(self):
		self._set_capabilities([
			{"model_pattern": "gemini", "context_window_tokens": 0, "supports_vision": 1},
		])
		self.assertEqual(estimate_context_window_tokens("gemini-3.5-flash"), 1000000)

	def test_unmatched_model_uses_heuristics(self):
		self._set_capabilities([
			{"model_pattern": "my-custom-model", "context_window_tokens": 32000, "supports_vision": 0},
		])
		self.assertEqual(estimate_context_window_tokens("claude-fable-5"), 200000)
		self.assertTrue(supports_vision_model("gpt-4o"))

	def test_no_rows_keeps_default(self):
		self.assertEqual(
			estimate_context_window_tokens("totally-unknown-model"), DEFAULT_CONTEXT_WINDOW_TOKENS
		)
