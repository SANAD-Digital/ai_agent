from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.runtime.file_context import FileContextService

# 1x1 transparent PNG
TINY_PNG = (
	b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
	b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
	b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class TestFileContextOCR(IntegrationTestCase):
	"""Chat attachments (📎) must read PDF/image text through the configured
	OCR engine and surface the failure reason when extraction fails."""

	def setUp(self):
		self.file = frappe.get_doc({
			"doctype": "File",
			"file_name": "chat-invoice.png",
			"content": TINY_PNG,
			"decode": False,
			"is_private": 1,
		}).insert(ignore_permissions=True)
		self.service = FileContextService()

	def tearDown(self):
		frappe.delete_doc("File", self.file.name, ignore_permissions=True, force=True)
		frappe.db.commit()

	def test_image_summary_uses_ocr_text(self):
		with patch(
			"ai_agent.runtime.gemini_ocr.gemini_ocr_image_bytes",
			return_value="فاتورة ضريبية رقم 123 بمبلغ 4,500",
		) as mocked:
			summary = self.service.summarize_file(self.file)
		self.assertTrue(mocked.called)
		self.assertIn("extraction: OCR", summary)
		self.assertIn("فاتورة ضريبية رقم 123", summary)

	def test_image_ocr_failure_surfaces_diagnostics(self):
		def failing(*args, **kwargs):
			frappe.local.gemini_ocr_last_error = "OCR page exceeded 300 seconds."
			return ""

		frappe.local.gemini_ocr_last_error = ""
		with patch("ai_agent.runtime.gemini_ocr.gemini_ocr_image_bytes", side_effect=failing):
			summary = self.service.summarize_file(self.file)
		self.assertIn("ocr_diagnostics", summary)
		self.assertIn("300 seconds", summary)
		frappe.local.gemini_ocr_last_error = ""

	def test_ocr_summary_is_cached_per_file(self):
		with patch(
			"ai_agent.runtime.gemini_ocr.gemini_ocr_image_bytes",
			return_value="نص مخبأ للاختبار",
		) as mocked:
			first = self.service.summarize_file(self.file)
			second = self.service.summarize_file(self.file)
		self.assertEqual(first, second)
		self.assertEqual(mocked.call_count, 1)
