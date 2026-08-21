from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.api import create_run, list_available_providers
from ai_agent.runtime.model_provider import (
	estimate_context_window_tokens,
	get_provider,
	resolve_run_model,
	supports_vision_model,
)
from ai_agent.tests.utils import TEST_AGENT, delete_docs, ensure_test_agent

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

	def test_provider_record_drives_the_client(self):
		client = get_provider(PROVIDER)
		self.assertEqual(client.base_url, "http://localhost:9/v1")
		self.assertEqual(client.model, "unit-model")
		self.assertEqual(client.api_key, "unit-secret-key-1234")

	def test_disabled_or_unknown_provider_is_refused(self):
		from ai_agent.runtime.exceptions import ModelProviderError

		with self.assertRaises(ModelProviderError):
			get_provider("No Such Provider")

	def test_capabilities_come_from_provider_models(self):
		self.assertTrue(supports_vision_model("unit-vision"))
		self.assertEqual(estimate_context_window_tokens("unit-vision"), 64000)

	def test_model_precedence(self):
		agent = frappe._dict(model="agent-model")
		self.assertEqual(resolve_run_model(frappe._dict(model="run-model", provider=PROVIDER), agent), "run-model")
		self.assertEqual(resolve_run_model(frappe._dict(model="", provider=PROVIDER), agent), "unit-model")
		self.assertEqual(resolve_run_model(frappe._dict(model="", provider=""), agent), "agent-model")

	def test_list_available_providers_exposes_models_not_secrets(self):
		providers = list_available_providers()["providers"]
		mine = next((p for p in providers if p["name"] == PROVIDER), None)
		self.assertIsNotNone(mine)
		self.assertEqual([m["model_name"] for m in mine["models"]], ["unit-model", "unit-vision"])
		self.assertNotIn("api_key", mine)

	def test_create_run_stores_provider_and_model_and_inherits(self):
		with patch("ai_agent.api.enqueue_run"):
			first = create_run(
				agent=TEST_AGENT, prompt="قارن مبيعات الأشهر الثلاثة الماضية حسب العميل",
				provider=PROVIDER, model="unit-vision",
			)
			self._runs.append(first["run_name"])
			row = frappe.db.get_value("AI Run", first["run_name"], ["provider", "model"], as_dict=True)
			self.assertEqual((row.provider, row.model), (PROVIDER, "unit-vision"))
			follow_up = create_run(
				agent=TEST_AGENT, prompt="وما نسبة النمو بينها؟", parent_run=first["run_name"],
			)
			self._runs.append(follow_up["run_name"])
			row = frappe.db.get_value("AI Run", follow_up["run_name"], ["provider", "model"], as_dict=True)
			self.assertEqual((row.provider, row.model), (PROVIDER, "unit-vision"))

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
