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

	def test_pdf_pages_emit_live_progress_steps(self):
		import io

		from PIL import Image

		from ai_agent.tests.utils import delete_docs, make_test_run

		run = make_test_run()
		pdf_buffer = io.BytesIO()
		page_image = Image.new("RGB", (80, 100), "white")
		page_image.save(pdf_buffer, format="PDF", save_all=True, append_images=[page_image])
		pdf_file = frappe.get_doc({
			"doctype": "File",
			"file_name": "progress-scan.pdf",
			"content": pdf_buffer.getvalue(),
			"decode": False,
			"is_private": 1,
		}).insert(ignore_permissions=True)
		try:
			with patch(
				"ai_agent.runtime.gemini_ocr.gemini_ocr_image_bytes",
				return_value="نص الصفحة المستخرج",
			):
				self.service.summarize_file(pdf_file, progress_run=run.name)
			steps = frappe.get_all(
				"AI Run Step",
				filters={"run": run.name},
				fields=["title", "status", "summary"],
				order_by="sequence asc",
			)
			self.assertEqual(len(steps), 2)
			self.assertIn("قراءة صفحة 1/2", steps[0].title)
			self.assertIn("قراءة صفحة 2/2", steps[1].title)
			self.assertTrue(all(step.status == "Completed" for step in steps))
			self.assertIn("نص الصفحة المستخرج", steps[0].summary)
		finally:
			delete_docs("AI Run Step", [s.name for s in frappe.get_all("AI Run Step", filters={"run": run.name})])
			frappe.delete_doc("File", pdf_file.name, ignore_permissions=True, force=True)
			delete_docs("AI Run", [run.name])
			frappe.db.commit()
