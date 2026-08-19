import json

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.runtime.memory_learning import learn_from_executed_approval
from ai_agent.runtime.memory_service import MemoryService, normalize_entity_key
from ai_agent.tests.utils import TEST_AGENT, delete_docs, ensure_test_agent, make_test_run
from ai_agent.tools.memory import handle_search


class MemoryTestBase(IntegrationTestCase):
	def setUp(self):
		ensure_test_agent()
		self.service = MemoryService()
		self._previous_enable_memory = frappe.db.get_single_value("AI Settings", "enable_memory")
		frappe.db.set_single_value("AI Settings", "enable_memory", 1)
		frappe.clear_document_cache("AI Settings", "AI Settings")
		self._created = []
		self._runs = []

	def tearDown(self):
		delete_docs("AI Memory", self._created)
		delete_docs("AI Run", self._runs)
		frappe.db.set_single_value("AI Settings", "enable_memory", self._previous_enable_memory or 0)
		frappe.clear_document_cache("AI Settings", "AI Settings")
		frappe.db.commit()

	def _track(self, memory):
		if memory:
			self._created.append(memory.name)
		return memory


class TestMappingUpsert(MemoryTestBase):
	def test_same_entity_updates_one_row(self):
		marker = f"upsert-{frappe.generate_hash(length=8)}"
		first = self._track(self.service.store(
			user="Administrator", scope="Agent", memory_type="Mapping",
			content=f"{marker} v1", agent=TEST_AGENT,
			entity_type="Supplier", entity_key=f"شركة {marker}",
			mapping={"account": "OLD"},
		))
		second = self.service.store(
			user="Administrator", scope="Agent", memory_type="Mapping",
			content=f"{marker} v2", agent=TEST_AGENT,
			entity_type="Supplier", entity_key=f"شركه {marker}",
			mapping={"account": "NEW"},
		)
		self.assertEqual(first.name, second.name)
		row = frappe.db.get_value(
			"AI Memory", first.name,
			["content", "mapping_json", "usage_count", "entity_key"], as_dict=True,
		)
		self.assertIn("v2", row.content)
		self.assertIn("NEW", row.mapping_json)
		self.assertEqual(row.usage_count, 2)
		self.assertEqual(row.entity_key, normalize_entity_key(f"شركة {marker}"))


class TestApprovalLearning(MemoryTestBase):
	def test_learns_supplier_account_mapping_from_je_payload(self):
		run = make_test_run()
		self._runs.append(run.name)
		company = frappe.get_all("Company", pluck="name", limit_page_length=1)
		if not company:
			self.skipTest("No Company on this site")
		marker = f"مورد التعلم {frappe.generate_hash(length=6)}"
		payload = {
			"doctype": "Journal Entry",
			"company": company[0],
			"accounts": [
				{"account": "5110 - مشتريات", "custom_cash_supplier": marker,
				 "cost_center": "الرئيسي", "debit": 100},
				{"account": "1110 - الصندوق", "credit": 100},
			],
		}
		approval = frappe._dict(
			name="TEST-APR",
			run=run.name,
			action_type="Create",
			doctype_name="Journal Entry",
			requested_by="Administrator",
			payload_json=json.dumps(payload, ensure_ascii=False),
		)
		learned = learn_from_executed_approval(approval)
		self.assertEqual(learned, 1)
		row = frappe.get_all(
			"AI Memory",
			filters={"entity_key": normalize_entity_key(marker)},
			fields=["name", "memory_type", "mapping_json", "scope", "company"],
		)
		self.assertEqual(len(row), 1)
		self._created.append(row[0].name)
		self.assertEqual(row[0].memory_type, "Mapping")
		self.assertEqual(row[0].scope, "Company")
		self.assertIn("5110", row[0].mapping_json)

	def test_disabled_memory_agent_learns_nothing(self):
		run = make_test_run()
		self._runs.append(run.name)
		frappe.db.set_value("AI Agent", TEST_AGENT, "disable_memory", 1)
		try:
			approval = frappe._dict(
				name="TEST-APR-2", run=run.name, action_type="Create",
				doctype_name="Journal Entry", requested_by="Administrator",
				payload_json=json.dumps({
					"doctype": "Journal Entry", "company": "",
					"accounts": [{"account": "X", "custom_cash_supplier": "Y"}],
				}),
			)
			self.assertEqual(learn_from_executed_approval(approval), 0)
		finally:
			frappe.db.set_value("AI Agent", TEST_AGENT, "disable_memory", 0)


class TestRelevanceRetrieval(MemoryTestBase):
	def test_entity_match_beats_recency(self):
		older = self._track(self.service.store(
			user="Administrator", scope="Agent", memory_type="Mapping",
			content="مؤسسة الوسام → حساب 5120", agent=TEST_AGENT,
			entity_type="Supplier", entity_key="مؤسسة الوسام",
			mapping={"account": "5120"},
		))
		self._track(self.service.store(
			user="Administrator", scope="Agent", memory_type="Mapping",
			content="شركة النخيل → حساب 5130", agent=TEST_AGENT,
			entity_type="Supplier", entity_key="شركة النخيل",
			mapping={"account": "5130"},
		))
		context = self.service.build_context(
			"Administrator", agent=TEST_AGENT, limit=1,
			query_text="سجل فاتورة جديدة من مؤسسه الوسام",
		)
		self.assertIn("5120", context)
		self.assertNotIn("5130", context)
		self.assertTrue(older)

	def test_search_tool_matches_normalized_entity(self):
		marker = f"مطبع-{frappe.generate_hash(length=6)}"
		self._track(self.service.store(
			user="Administrator", scope="Agent", memory_type="Mapping",
			content=f"شركة المروة {marker} → حساب 5140", agent=TEST_AGENT,
			entity_type="Supplier", entity_key=f"شركة المروة {marker}",
			mapping={"account": "5140"},
		))
		ctx = frappe._dict(user="Administrator", agent_name=TEST_AGENT, company=None)
		result = handle_search({"query": f"شركه المروه {marker}"}, ctx)
		self.assertGreaterEqual(result["row_count"], 1)
		top = result["rows"][0]
		self.assertEqual(top["entity_key"], normalize_entity_key(f"شركة المروة {marker}"))
		self.assertIn("5140", top["mapping"])
