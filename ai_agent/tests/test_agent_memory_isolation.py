import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.runtime.memory_service import MemoryService
from ai_agent.tests.utils import TEST_AGENT, delete_docs, ensure_test_agent, make_test_run


class TestAgentMemoryIsolation(IntegrationTestCase):
	"""disable_memory on an agent must cut every memory pathway — regression
	for the WhatsApp shared-identity leak where one support_user serves all
	customers and agent-scope episodes crossed between conversations."""

	def setUp(self):
		ensure_test_agent()
		self._previous_enable_memory = frappe.db.get_single_value("AI Settings", "enable_memory")
		frappe.db.set_single_value("AI Settings", "enable_memory", 1)
		frappe.clear_document_cache("AI Settings", "AI Settings")
		self.marker = f"leak-check-{frappe.generate_hash(length=8)}"
		memory = MemoryService().store(
			user="Administrator",
			scope="Agent",
			memory_type="Summary",
			content=f"Customer A balance details {self.marker}",
			agent=TEST_AGENT,
		)
		self._memories = [memory.name]
		self.run = make_test_run()
		self._runs = [self.run.name]

	def tearDown(self):
		delete_docs("AI Memory", self._memories)
		delete_docs("AI Run", self._runs)
		frappe.db.set_single_value("AI Settings", "enable_memory", self._previous_enable_memory or 0)
		frappe.clear_document_cache("AI Settings", "AI Settings")
		frappe.db.commit()

	def _agent(self, disable_memory):
		agent = frappe.get_doc("AI Agent", TEST_AGENT)
		agent.disable_memory = disable_memory
		return agent
