"""AI Settings > Default Provider is a link to an AI Provider, kept in step with
AI Provider.is_default (what the engine reads) from either side."""

import frappe
from frappe.tests import IntegrationTestCase

NAMES = ("Link Test Provider A", "Link Test Provider B")


class TestDefaultProviderLink(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.previous = frappe.db.get_value("AI Provider", {"is_default": 1}, "name")
		for name in NAMES:
			if not frappe.db.exists("AI Provider", name):
				frappe.get_doc({"doctype": "AI Provider", "provider_name": name, "provider_type": "Ollama",
					"enabled": 1, "default_model": "llama3"}).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.db.rollback()
		for name in NAMES:
			frappe.delete_doc("AI Provider", name, force=True, ignore_permissions=True)
		if self.previous:
			frappe.db.sql("update `tabAI Provider` set is_default = (name = %s)", self.previous)
		frappe.db.set_single_value("AI Settings", "default_provider", self.previous)
		frappe.db.commit()

	def test_picking_it_in_settings_marks_the_provider_default(self):
		settings = frappe.get_single("AI Settings")
		settings.default_provider = NAMES[0]
		settings.save(ignore_permissions=True)
		self.assertEqual(frappe.get_all("AI Provider", filters={"is_default": 1}, pluck="name"), [NAMES[0]])

	def test_marking_a_provider_default_updates_the_link(self):
		doc = frappe.get_doc("AI Provider", NAMES[1])
		doc.is_default = 1
		doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_single_value("AI Settings", "default_provider"), NAMES[1])

	def test_a_disabled_provider_cannot_be_the_default(self):
		frappe.db.set_value("AI Provider", NAMES[0], "enabled", 0)
		settings = frappe.get_single("AI Settings")
		settings.default_provider = NAMES[0]
		with self.assertRaises(frappe.ValidationError):
			settings.save(ignore_permissions=True)
