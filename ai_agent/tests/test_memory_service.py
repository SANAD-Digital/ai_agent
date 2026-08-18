import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.runtime.memory_service import MemoryService
from ai_agent.tests.utils import TEST_AGENT, delete_docs, ensure_test_agent


class TestMemoryService(IntegrationTestCase):
	def setUp(self):
		self.service = MemoryService()
		ensure_test_agent()
		self._previous_enable_memory = frappe.db.get_single_value("AI Settings", "enable_memory")
		self._set_enable_memory(1)
		self._created = []

	def tearDown(self):
		# MemoryService commits on store, so cleanup must be explicit.
		delete_docs("AI Memory", self._created)
		self._set_enable_memory(self._previous_enable_memory or 0)
		frappe.db.commit()

	def _set_enable_memory(self, value):
		frappe.db.set_single_value("AI Settings", "enable_memory", value)
		frappe.clear_document_cache("AI Settings", "AI Settings")

	def _store(self, **kwargs):
		memory = self.service.store(**kwargs)
		if memory:
			self._created.append(memory.name)
		return memory

	def test_store_persists_structured_scope_fields(self):
		memory = self._store(
			user="Administrator",
			scope="Agent",
			memory_type="Context",
			content="Test structured scoping content",
			agent=TEST_AGENT,
		)
		self.assertIsNotNone(memory)
		row = frappe.db.get_value("AI Memory", memory.name, ["agent", "scope"], as_dict=True)
		self.assertEqual(row.agent, TEST_AGENT)
		self.assertEqual(row.scope, "Agent")

	def test_agent_memory_included_without_content_prefix(self):
		# Regression for the substring filter: an Agent-scope memory whose content
		# does NOT embed "agent=<name>" must still reach the model context.
		marker = f"prefers-concise-arabic-{frappe.generate_hash(length=8)}"
		self._store(
			user="Administrator",
			scope="Agent",
			memory_type="Preference",
			content=f"User preference marker {marker}",
			agent=TEST_AGENT,
		)
		context = self.service.build_context("Administrator", agent=TEST_AGENT)
		self.assertIn(marker, context)

	def test_user_scope_memory_is_retrievable(self):
		marker = f"user-scope-{frappe.generate_hash(length=8)}"
		self._store(
			user="Administrator",
			scope="User",
			memory_type="Preference",
			content=f"User scope marker {marker}",
		)
		contents = [m.content for m in self.service.retrieve("Administrator", scope="User")]
		self.assertTrue(any(marker in content for content in contents))

	def test_agent_scope_requires_agent(self):
		self.assertEqual(self.service.retrieve("Administrator", scope="Agent", agent=None), [])

	def test_expired_memory_is_excluded(self):
		memory = self._store(
			user="Administrator",
			scope="User",
			memory_type="Context",
			content=f"Expiring memory {frappe.generate_hash(length=8)}",
		)
		frappe.db.set_value(
			"AI Memory",
			memory.name,
			"expires_at",
			frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-5),
			update_modified=False,
		)
		names = [m.name for m in self.service.retrieve("Administrator", scope="User")]
		self.assertNotIn(memory.name, names)

	def test_disabled_memory_stores_nothing(self):
		self._set_enable_memory(0)
		memory = self._store(
			user="Administrator",
			scope="User",
			memory_type="Context",
			content="Should not be stored",
		)
		self.assertIsNone(memory)
