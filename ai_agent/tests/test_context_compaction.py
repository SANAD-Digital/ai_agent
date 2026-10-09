"""Phase B4: smaller schema results, schema summaries and compaction after use."""

import json

from frappe.tests import IntegrationTestCase

from ai_agent.runtime import result_compaction
from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.runtime.skill_registry import SkillRegistry
from ai_agent.tools import schema


class TestDescribeDoctype(IntegrationTestCase):
	def setUp(self):
		self.ctx = PermissionContext("Administrator")

	def test_default_limit_and_most_used_first(self):
		result = schema.handle_describe_doctype({"doctype": "Sales Invoice"}, self.ctx)
		self.assertLessEqual(len(result["fields"]), schema.DEFAULT_FIELD_LIMIT)
		self.assertGreater(result["fields_omitted"], 0)
		first = [f["fieldname"] for f in result["fields"][:10]]
		self.assertIn("customer", first)
		self.assertIn("posting_date", first)

	def test_fields_filter(self):
		result = schema.handle_describe_doctype(
			{"doctype": "Sales Invoice", "fields_filter": ["outstanding", "due"]}, self.ctx
		)
		names = {f["fieldname"] for f in result["fields"]}
		self.assertIn("outstanding_amount", names)
		self.assertIn("due_date", names)
		self.assertTrue(all("outstanding" in n or "due" in n or "Due" in (f.get("label") or "")
							for n, f in ((f["fieldname"], f) for f in result["fields"])))

	def test_filtered_result_is_much_smaller(self):
		full = json.dumps(schema.handle_describe_doctype({"doctype": "Sales Invoice", "limit": 200}, self.ctx))
		small = json.dumps(schema.handle_describe_doctype({"doctype": "Sales Invoice", "fields_filter": ["customer"]}, self.ctx))
		self.assertLess(len(small) * 3, len(full))


class TestSchemaReferences(IntegrationTestCase):
	def test_reference_files_exist_for_top_doctypes(self):
		references = SkillRegistry().list_references("system")
		for name in ("sales-invoice", "payment-entry", "gl-entry", "bin", "item", "customer"):
			self.assertIn(name, references)
		self.assertGreaterEqual(len(references), 15)

	def test_reference_path_traversal_is_blocked(self):
		registry = SkillRegistry()
		self.assertIn("# Sales Invoice", registry.read_reference("system", "sales-invoice"))
		for bad in ("../../hooks", "../SKILL", "/etc/passwd", "..%2f..%2fhooks"):
			self.assertIsNone(registry.read_reference("system", bad), bad)
		self.assertIsNone(registry.read_reference("../..", "hooks"))


class TestCompactionAfterUse(IntegrationTestCase):
	def _messages(self, content):
		return [
			{"role": "system", "content": "s"},
			{"role": "user", "content": "q"},
			{"role": "assistant", "tool_calls": [{"id": "1"}, {"id": "2"}]},
			{"role": "tool", "tool_call_id": "1", "content": content, "_tool": "schema.describe_doctype", "_summary": "Described Sales Invoice"},
			{"role": "tool", "tool_call_id": "2", "content": content, "_tool": "metrics.query", "_summary": "net sales"},
			{"role": "assistant", "tool_calls": [{"id": "3"}]},
			{"role": "tool", "tool_call_id": "3", "content": content, "_tool": "schema.describe_doctype", "_summary": "x"},
		]

	def test_read_exploration_results_are_summarized(self):
		messages = self._messages("x" * 5000)
		self.assertEqual(result_compaction.compact_seen_tool_results(messages), 1)
		self.assertIn("Described Sales Invoice", messages[3]["content"])
		self.assertTrue(messages[3]["_compacted"])

	def test_data_results_and_unread_results_are_kept(self):
		messages = self._messages("x" * 5000)
		result_compaction.compact_seen_tool_results(messages)
		self.assertEqual(len(messages[4]["content"]), 5000)  # metrics.query: numbers stay
		self.assertEqual(len(messages[6]["content"]), 5000)  # not read by a model call yet

	def test_small_results_are_kept(self):
		messages = self._messages("short")
		self.assertEqual(result_compaction.compact_seen_tool_results(messages), 0)

	def test_private_keys_never_reach_openai_compatible_apis(self):
		from ai_agent.runtime.model_provider import _openai_messages

		cleaned = _openai_messages(self._messages("x"))
		self.assertTrue(all(not key.startswith("_") for message in cleaned for key in message))
