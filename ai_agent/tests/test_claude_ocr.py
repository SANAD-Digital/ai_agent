"""Scanned files are read by Claude through the site's Anthropic provider when Gemini OCR
cannot be used (Managed mode), in one request per PDF."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.runtime import claude_ocr
from ai_agent.runtime.file_context import FileContextService


def response(text, stop="end_turn"):
	return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])


class TestClaudeOCR(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		import io

		from pypdf import PdfWriter

		writer, buf = PdfWriter(), io.BytesIO()
		writer.add_blank_page(width=200, height=200)
		writer.write(buf)
		self.file = frappe.get_doc({"doctype": "File", "file_name": "scan-test.pdf", "content": buf.getvalue(), "is_private": 1}).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.db.rollback()

	def _client(self, reply):
		client = MagicMock()
		client.messages.create.return_value = reply
		return client

	def test_a_pdf_goes_as_one_document_and_its_text_comes_back(self):
		client = self._client(response("فاتورة رقم 4411"))
		with patch.object(claude_ocr, "_provider", return_value=("k", None, "claude-opus-5-5")), patch("anthropic.Anthropic", return_value=client):
			text = claude_ocr.read_pdf(self.file.get_full_path(), "read")
		self.assertEqual(text, "فاتورة رقم 4411")
		sent = client.messages.create.call_args.kwargs["messages"][0]["content"]
		self.assertEqual(sent[0]["type"], "document")
		self.assertEqual(sent[0]["source"]["media_type"], "application/pdf")

	def test_a_refusal_reads_as_nothing(self):
		client = self._client(response("", stop="refusal"))
		with patch.object(claude_ocr, "_provider", return_value=("k", None, "m")), patch("anthropic.Anthropic", return_value=client):
			self.assertEqual(claude_ocr.read_image(b"x", "image/png", "read"), "")

	def test_no_anthropic_provider_no_call(self):
		with patch.object(claude_ocr, "_provider", return_value=None), patch("anthropic.Anthropic") as made:
			self.assertEqual(claude_ocr.read_image(b"x", "image/png", "read"), "")
			made.assert_not_called()

	def test_gemini_first_when_it_can_be_used(self):
		with patch("ai_agent.runtime.gemini_ocr.is_gemini_ocr_enabled", return_value=True), patch.object(claude_ocr, "read_pdf") as read:
			self.assertEqual(FileContextService()._claude_pdf(self.file), "")
			read.assert_not_called()
		with patch("ai_agent.runtime.gemini_ocr.is_gemini_ocr_enabled", return_value=False), \
				patch.object(claude_ocr, "available", return_value=True), patch.object(claude_ocr, "read_pdf", return_value="نص") as read:
			self.assertEqual(FileContextService()._claude_pdf(self.file), "نص")
			read.assert_called_once()
