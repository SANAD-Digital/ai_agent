"""One default provider: making another the default clears the old one (in the database and in the document cache),
keeps AI Settings > Default Provider the same, and tells the user which one stopped being the default."""

import json

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.tests.utils import delete_docs


class TestDefaultProvider(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.before = (frappe.get_all("AI Provider", filters={"is_default": 1}, pluck="name"),
			frappe.db.get_single_value("AI Settings", "default_provider"))
		self.names = []
		for label in ("A", "B"):
			name = f"Default Test {label} {frappe.generate_hash(length=5)}"
			frappe.get_doc({"doctype": "AI Provider", "provider_name": name, "provider_type": "OpenAI Compatible",
				"base_url": "http://localhost:9/v1", "enabled": 1}).insert(ignore_permissions=True)
			self.names.append(name)
		self.addCleanup(self.restore)

	def restore(self):
		frappe.db.set_value("AI Provider", {"name": ("in", self.names)}, "is_default", 0)
		for name in self.before[0]:
			frappe.db.set_value("AI Provider", name, "is_default", 1)
			frappe.clear_document_cache("AI Provider", name)
		frappe.db.set_single_value("AI Settings", "default_provider", self.before[1])
		delete_docs("AI Provider", self.names)
		frappe.db.commit()

	def make_default(self, name):
		frappe.local.message_log = []
		doc = frappe.get_doc("AI Provider", name)
		doc.is_default = 1
		doc.save(ignore_permissions=True)
		return [json.loads(m).get("message", "") if isinstance(m, str) else m.get("message", "") for m in frappe.local.message_log]

	def test_the_old_default_is_cleared_and_the_user_told(self):
		first, second = self.names
		self.make_default(first)
		messages = self.make_default(second)
		self.assertEqual(frappe.get_all("AI Provider", filters={"is_default": 1}, pluck="name"), [second])
		self.assertEqual(frappe.get_cached_doc("AI Provider", first).is_default, 0, "the cached old default is cleared too")
		self.assertEqual(frappe.db.get_single_value("AI Settings", "default_provider"), second)
		self.assertTrue(any(first in m and second in m for m in messages), messages)

	def test_saving_the_default_again_says_nothing(self):
		self.make_default(self.names[0])
		self.assertEqual(self.make_default(self.names[0]), [])

	def test_choosing_it_in_ai_settings_does_the_same(self):
		first, second = self.names
		self.make_default(first)
		settings = frappe.get_single("AI Settings")
		settings.default_provider = second
		frappe.local.message_log = []
		settings.save(ignore_permissions=True)
		self.assertEqual(frappe.get_all("AI Provider", filters={"is_default": 1}, pluck="name"), [second])
		self.assertTrue(frappe.local.message_log, "the user is told")
