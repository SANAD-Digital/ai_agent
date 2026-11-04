import json

import frappe
from frappe.tests import IntegrationTestCase

SIDEBAR = frappe.get_app_path("ai_agent", "ai_agent", "sidebar", "ai", "ai.json")


class TestModuleSidebar(IntegrationTestCase):
	"""The AI module sidebar the app ships (Frappe 16.53+ reads ``<module>/sidebar/``)."""

	def setUp(self):
		with open(SIDEBAR) as f:
			self.sidebar = json.load(f)
		self.links = {i["link_to"]: i for i in self.sidebar["items"] if i.get("type") == "Link"}

	def test_every_link_opens_something_that_exists(self):
		for target, item in self.links.items():
			doctype = {"DocType": "DocType", "Page": "Page", "Workspace": "Workspace"}[item["link_type"]]
			self.assertTrue(frappe.db.exists(doctype, target), f"{item['label']} -> {doctype} {target}")

	def test_the_per_user_pages_are_linked_and_the_retired_ones_are_not(self):
		self.assertIn("AI MCP", self.links)
		self.assertIn("AI Chatbot Assignment", self.links)
		# WhatsApp support was removed from the app; the chatbot's site defaults open from the assignment list
		self.assertNotIn("AI WhatsApp Support Settings", self.links)
		self.assertNotIn("AI Chatbot Settings", self.links)

	def test_it_is_the_app_sidebar_of_the_module(self):
		self.assertEqual(
			(self.sidebar["module"], self.sidebar["app"], self.sidebar["standard"]), ("AI Agent", "ai_agent", 1)
		)
