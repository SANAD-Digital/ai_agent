import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.ai_agent.doctype.gemini_ocr_settings.gemini_ocr_settings import LOCAL_ENGINE


class TestGeminiOCRSettings(IntegrationTestCase):
	"""The API key is the cloud engine's; with no key saved yet the form says so in words, not "Password not found"."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(frappe.db.rollback)
		frappe.db.delete("__Auth", {"doctype": "Gemini OCR Settings", "fieldname": "api_key"})  # no key saved yet

	def settings(self, engine):
		doc = frappe.get_doc("Gemini OCR Settings")
		doc.update({"enabled": 1, "ocr_engine": engine, "api_key": ""})
		return doc

	def test_the_local_engine_needs_no_key(self):
		self.settings(LOCAL_ENGINE).validate()

	def test_the_cloud_engine_without_a_key_is_explained(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			self.settings("Cloud API (Gemini OCR)").validate()
		self.assertIn("API Key is required", str(cm.exception))
