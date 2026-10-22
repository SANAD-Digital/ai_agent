"""Standard agents, plugins and preset providers ship as files Frappe re-imports on migrate (delete + insert).
The re-import refreshes the app's text and keeps what the site owns."""

import os

import frappe
from frappe.modules.import_file import import_file_by_path
from frappe.tests import IntegrationTestCase

from ai_agent import install

BASE = frappe.get_app_path("ai_agent", "ai_agent")


def path(doctype, name):
	return os.path.join(BASE, frappe.scrub(doctype), frappe.scrub(name), frappe.scrub(name) + ".json")


class TestStandardRecords(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_every_standard_file_is_registered(self):
		self.assertEqual(set(frappe.get_hooks("importable_doctypes", app_name="ai_agent")), {"AI Agent", "AI Plugin", "AI Provider"})
		for doctype in ("AI Agent", "AI Plugin", "AI Provider"):
			folder = os.path.join(BASE, frappe.scrub(doctype))
			for name in os.listdir(folder):
				doc = frappe.parse_json(open(os.path.join(folder, name, name + ".json")).read())
				self.assertEqual((doc.doctype, frappe.scrub(doc.name)), (doctype, name))

	def test_agent_reimport_refreshes_the_text_and_keeps_the_site_choices(self):
		frappe.db.set_value("AI Agent", "ERP Analyst", {"enabled": 0, "system_instructions": "edited", "reasoning_effort": "low"})
		import_file_by_path(path("AI Agent", "ERP Analyst"), force=True)
		agent = frappe.get_doc("AI Agent", "ERP Analyst")
		self.assertEqual(agent.enabled, 0, "the admin's switch stays")
		self.assertIn("ERP data analyst", agent.system_instructions)
		self.assertEqual(agent.reasoning_effort, "medium")

	def test_agent_reimport_keeps_fields_the_file_does_not_carry(self):
		before = frappe.db.get_value("AI Agent", "ERP Power Operator", ["provider", "model"], as_dict=True)
		frappe.db.set_value("AI Agent", "ERP Power Operator", "description", "x")
		import_file_by_path(path("AI Agent", "ERP Power Operator"), force=True)
		after = frappe.db.get_value("AI Agent", "ERP Power Operator", ["provider", "model", "description"], as_dict=True)
		self.assertEqual((after.provider, after.model), (before.provider, before.model))
		self.assertNotEqual(after.description, "x")

	def test_plugin_stays_disabled(self):
		frappe.db.set_value("AI Plugin", "csv", {"enabled": 0, "description": "x"})
		import_file_by_path(path("AI Plugin", "csv"), force=True)
		self.assertEqual(frappe.db.get_value("AI Plugin", "csv", ["enabled", "description"]), (0, "A CSV file of the raw data"))

	def test_preset_provider_keeps_its_key_models_and_switches(self):
		name = frappe.db.exists("AI Provider", "Groq")
		if not name:
			self.skipTest("no Groq preset on this site")
		doc = frappe.get_doc("AI Provider", name)
		doc.api_key = "gsk-test-key"
		doc.description = "mine"
		doc.append("models", {"model_name": "llama-test", "model_type": "Chat"})
		doc.flags.ignore_validate = True
		doc.save(ignore_permissions=True)
		import_file_by_path(path("AI Provider", "Groq"), force=True)
		doc = frappe.get_doc("AI Provider", name)
		self.assertEqual(doc.get_password("api_key"), "gsk-test-key")
		self.assertEqual(doc.description, "mine")
		self.assertIn("llama-test", [m.model_name for m in doc.models])

	def test_preset_providers_are_older_than_any_site_record(self):
		"""A migrate never re-imports a preset over the site's provider: its file is dated before any real record."""
		for name in os.listdir(os.path.join(BASE, "ai_provider")):
			doc = frappe.parse_json(open(path("AI Provider", name)).read())
			self.assertTrue(doc.modified.startswith("2026-01-01"), name)

	def test_a_new_site_stores_the_settings_defaults(self):
		frappe.db.delete("Singles", {"doctype": "Gemini OCR Settings"})
		install.store_single_defaults()
		meta = frappe.get_meta("Gemini OCR Settings")
		for field in ("request_timeout", "max_output_chars", "model"):
			self.assertEqual(str(frappe.db.get_single_value("Gemini OCR Settings", field)), meta.get_field(field).default, field)

	def test_install_imports_the_files_frappe_skips_on_install(self):
		"""On install Frappe syncs the app before it is installed, so importable_doctypes is not read yet."""
		from ai_agent.standard_records import import_all

		frappe.delete_doc("AI Plugin", "csv", force=True, ignore_permissions=True)
		import_all()
		self.assertTrue(frappe.db.exists("AI Plugin", "csv"))
