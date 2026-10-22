"""Agents and the support window pick a provider, and their model is one of that
provider's models. With no explicit choice a conversation runs on the agent's provider."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.chat import api
from ai_agent.runtime import policy

P1, P2 = "Agent Test Provider One", "Agent Test Provider Two"
AGENT = "Agent Provider Test Agent"


class TestAgentProvider(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		patcher = patch.object(policy, "assert_provider_allowed", lambda *_: None)
		patcher.start()
		self.addCleanup(patcher.stop)
		for name, models in ((P1, ["one-a", "one-b"]), (P2, ["two-a", "two-b"])):
			frappe.get_doc({"doctype": "AI Provider", "provider_name": name, "provider_type": "Ollama", "enabled": 1,
				"models": [{"model_name": m, "model_type": "Chat", "supports_tools": 1, "is_default": int(m.endswith("-b"))} for m in models],
			}).insert(ignore_permissions=True)
		self.agent = frappe.get_doc({"doctype": "AI Agent", "agent_name": AGENT, "enabled": 1, "provider": P2, "model": "two-a",
			"system_instructions": "test"}).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.db.rollback()

	def test_the_model_must_belong_to_the_provider(self):
		self.agent.model = "one-a"
		with self.assertRaises(frappe.ValidationError):
			self.agent.save(ignore_permissions=True)

	def test_no_choice_runs_on_the_agents_provider_and_model(self):
		self.assertEqual(api._resolve_provider_model(None, None, self.agent.name), (P2, "two-a"))

	def test_another_provider_does_not_take_the_agents_model(self):
		provider, model = api._resolve_provider_model(P1, None, self.agent.name)
		self.assertEqual((provider, model), (P1, "one-b"), "the provider's default model, not the agent's two-a")
