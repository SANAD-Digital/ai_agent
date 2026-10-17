"""Memories are whole topics: never cut silently, recalled in full, and a topic stored
again replaces itself instead of piling up duplicates. Company/agent stay in their fields."""

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.engine import memory as engine_memory
from ai_agent.runtime.memory_service import MAX_MEMORY_CONTENT_CHARS
from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.tools.memory import handle_store_context

KEY = "memory-topics-test rules"


class TestMemoryTopics(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.saved = frappe.db.get_single_value("AI Settings", "enable_memory")
		frappe.db.set_single_value("AI Settings", "enable_memory", 1)
		frappe.clear_document_cache("AI Settings", "AI Settings")
		self.company = frappe.get_all("Company", pluck="name", limit=1)[0]
		self.ctx = PermissionContext("Administrator", "ERP Power Operator", self.company)

	def tearDown(self):
		frappe.db.rollback()
		frappe.db.set_single_value("AI Settings", "enable_memory", self.saved or 0)
		frappe.clear_document_cache("AI Settings", "AI Settings")

	def _store(self, content):
		return handle_store_context({"content": content, "scope": "Company", "entity_type": "Procedure", "entity_key": KEY}, self.ctx)

	def test_too_long_is_refused_not_cut(self):
		out = self._store("قاعدة " * (MAX_MEMORY_CONTENT_CHARS // 4))
		self.assertIn("Too long", out.get("error", ""))

	def test_the_same_topic_replaces_itself(self):
		first = self._store("1. الغاز → 312302. 2. الديزل → 312215.")
		second = self._store("1. الغاز → 312302. 2. الديزل → 312215. 3. الصيانة → 312400.")
		self.assertEqual(first["memory"], second["memory"])
		rows = frappe.get_all("AI Memory", filters={"entity_key": ["like", "%memory-topics-test%"]}, pluck="content")
		self.assertEqual(len(rows), 1)
		self.assertIn("312400", rows[0])

	def test_no_tags_in_the_text_and_a_short_title(self):
		out = self._store("قواعد قيود المصاريف اليومية: " + "تفصيل " * 30)
		doc = frappe.get_doc("AI Memory", out["memory"])
		self.assertFalse(doc.content.startswith(("company=", "agent=")))
		self.assertEqual(doc.company, self.company)
		self.assertLessEqual(len(doc.title), 90)

	def test_recall_carries_the_whole_memory(self):
		long_rules = "قواعد: " + " ".join(f"{i}. حساب {i} → 31{i:04d}." for i in range(1, 21))
		self.assertGreater(len(long_rules), 220)
		self._store(long_rules)
		agent = frappe.get_doc("AI Agent", "ERP Power Operator")
		block = engine_memory.recall(self.ctx, agent, "قواعد")
		self.assertIn("310020", block, "the last rule reaches the model, not just the first 220 characters")
