"""A scanned PDF read by the conversation's own model when it sees images but takes no PDF (DeepSeek flash): page by
page as images, the texts put together; the configured OCR when that model fails; separate caches per reader."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.runtime import vision_ocr
from ai_agent.runtime.file_context import FileContextService
from ai_agent.tests.test_pdf_pages import _scanned_pdf

VISION = ("Test Vision", "vision-model")


class TestVisionPages(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.file = frappe.get_doc({"doctype": "File", "file_name": "scan-vision.pdf", "content": _scanned_pdf(), "decode": False,
			"is_private": 1}).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("File", self.file.name, ignore_permissions=True, force=True))
		self.addCleanup(frappe.cache.delete_keys, f"ai_agent:ocr_page:{self.file.name}")
		self.addCleanup(frappe.cache.delete_keys, f"ai_agent:file_summary:{self.file.name}")

	def test_each_page_goes_to_the_model_and_the_texts_are_put_together(self):
		pages = iter(range(1, 100))
		with patch.object(vision_ocr, "read_image", side_effect=lambda *a, **k: f"model text of page {next(pages)}") as model, \
				patch("ai_agent.runtime.gemini_ocr.gemini_ocr_image_bytes") as ocr:
			texts, read, total = FileContextService(VISION).ocr_pdf_pages(self.file)
		self.assertEqual(model.call_count, total)
		self.assertFalse(ocr.called, "the OCR engine is only the fallback")
		self.assertEqual(texts[total], f"model text of page {total}")

	def test_a_page_the_model_fails_on_falls_back_to_the_ocr_engine(self):
		with patch.object(vision_ocr, "read_image", side_effect=RuntimeError("provider down")), \
				patch("ai_agent.runtime.gemini_ocr.gemini_ocr_image_bytes", return_value="ocr text"):
			texts, read, _ = FileContextService(VISION).ocr_pdf_pages(self.file, pages=[1])
		self.assertEqual(texts[1], "ocr text")

	def test_each_reader_has_its_own_cache(self):
		with patch("ai_agent.runtime.gemini_ocr.gemini_ocr_image_bytes", return_value="ocr text"):
			FileContextService().ocr_pdf_pages(self.file, pages=[1])
		with patch.object(vision_ocr, "read_image", return_value="model text"):
			texts, _, _ = FileContextService(VISION).ocr_pdf_pages(self.file, pages=[1])
		self.assertEqual(texts[1], "model text", "the local reading is not served to the model's reader")

	def test_only_a_model_that_sees_images_and_takes_no_pdf_reads(self):
		from ai_agent.engine.types import ProviderCaps

		class Provider:
			name = "openai"

			def __init__(self, caps):
				self._caps = caps

			def caps(self, model):
				return self._caps

		for caps, expected in ((ProviderCaps(supports_vision=True), VISION), (ProviderCaps(supports_vision=False), None),
				(ProviderCaps(supports_vision=True, supports_pdf=True), None)):
			with patch("ai_agent.engine.providers.factory.provider_for", return_value=(Provider(caps), *VISION)):
				self.assertEqual(vision_ocr.reader(*VISION), expected)
