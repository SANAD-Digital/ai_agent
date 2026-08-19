from frappe.tests import IntegrationTestCase

from ai_agent.runtime.answer_contract import (
	compound_contract_pending,
	extract_json_object,
	fallback_json_final_answer,
	looks_like_tool_markup,
	merge_skill_policies,
	parse_json_text,
	strip_tool_markup,
	structured_failure_reason,
)


class TestAnswerContract(IntegrationTestCase):
	def test_extract_json_object_balanced_extraction(self):
		text = 'prefix {"a": 1, "b": {"c": 2}} trailing prose {"ignored": true}'
		self.assertEqual(extract_json_object(text), {"a": 1, "b": {"c": 2}})

	def test_extract_json_object_skips_invalid_braces(self):
		text = "broken { not json } then {\"ok\": 1}"
		self.assertEqual(extract_json_object(text), {"ok": 1})

	def test_extract_json_object_reads_fenced_block(self):
		text = '```json\n{"status": "done"}\n```'
		self.assertEqual(extract_json_object(text), {"status": "done"})

	def test_extract_json_object_none_for_plain_text(self):
		self.assertIsNone(extract_json_object("مرحبا، لا يوجد JSON هنا"))

	def test_parse_json_text_embedded_object(self):
		self.assertEqual(parse_json_text('note {"route": "answer"} end'), {"route": "answer"})
		self.assertEqual(parse_json_text("plain text"), {})

	def test_tool_markup_detection_and_stripping(self):
		markup = 'line one\n<tool_call invoke name="data__query">\nDSML junk\nline two'
		self.assertTrue(looks_like_tool_markup(markup))
		cleaned = strip_tool_markup(markup)
		self.assertIn("line one", cleaned)
		self.assertIn("line two", cleaned)
		self.assertNotIn("invoke name=", cleaned)
		self.assertFalse(looks_like_tool_markup("نص عادي بلا أي ترميز أدوات"))

	def test_merge_skill_policies(self):
		merged = merge_skill_policies([
			{"response_format": "Markdown", "required_output_keys": ["a"], "finalization_max_tokens": 100},
			{"response_format": "JSON Object", "required_output_keys": ["a", "b"], "finalization_max_tokens": 900},
			{"final_output_instruction": "Return one object."},
		])
		self.assertEqual(merged["response_format"], "JSON Object")
		self.assertEqual(merged["required_output_keys"], ["a", "b"])
		self.assertEqual(merged["finalization_max_tokens"], 900)
		self.assertEqual(merged["final_output_instruction"], "Return one object.")

	def test_compound_contract_pending(self):
		self.assertTrue(compound_contract_pending({
			"response_format": "JSON Object",
			"required_output_keys": ["invoice_groups", "status", "errors"],
		}))
		self.assertFalse(compound_contract_pending({
			"response_format": "JSON Object",
			"required_output_keys": ["status", "errors", "warnings"],
		}))
		self.assertFalse(compound_contract_pending({"response_format": ""}))

	def test_structured_failure_reason(self):
		policy = {"required_output_keys": ["invoice_groups", "status", "errors"]}
		failed = '{"status": "failed", "errors": ["missing pages"]}'
		self.assertEqual(structured_failure_reason(policy, failed), "missing pages")
		empty_substantive = '{"status": "done", "errors": ["boom"], "invoice_groups": []}'
		self.assertEqual(structured_failure_reason(policy, empty_substantive), "boom")
		success = '{"status": "done", "errors": [], "invoice_groups": [{"x": 1}]}'
		self.assertEqual(structured_failure_reason(policy, success), "")
		self.assertEqual(structured_failure_reason(policy, "plain markdown answer"), "")

	def test_fallback_json_final_answer_includes_required_keys(self):
		policy = {"required_output_keys": ["invoice_groups", "status"]}
		fallback = fallback_json_final_answer(policy, "raw model text")
		self.assertEqual(fallback["status"], "Needs Review")
		self.assertEqual(fallback["invoice_groups"], [])
		self.assertIn("raw model text", fallback["client_readable_summary"])
		self.assertTrue(fallback["errors"])
