"""A scanned PDF is read page by page within a time budget per pass, every page cached; the model is told which pages
are still unread and reads them with files.read `pages`. No fixed page count stops it (the old 10 pages)."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.runtime.file_context import FileContextService, _ranges, parse_pages
from ai_agent.tools import chat_files

PAGES = 6


def _scanned_pdf():
	"""Pages with no text layer, as a scan has."""
	import fitz

	document = fitz.open()
	for _ in range(PAGES):
		document.new_page()
	data = document.tobytes()
	document.close()
	return data


class TestPdfPages(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.file = frappe.get_doc({"doctype": "File", "file_name": "scan-pages.pdf", "content": _scanned_pdf(), "decode": False,
			"is_private": 1}).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("File", self.file.name, ignore_permissions=True, force=True))
		self.addCleanup(frappe.cache.delete_keys, f"ai_agent:ocr_page:{self.file.name}")
		self.addCleanup(frappe.cache.delete_keys, f"ai_agent:file_summary:{self.file.name}")
		self.calls = []

	def ocr(self):
		"""Each page's OCR answers "text of page N" (pages are rendered in order)."""
		def read(service, data, mime_type="image/png", allow_claude=True):
			self.calls.append(1)
			return f"text of page {len(self.calls)}"

		return patch.object(FileContextService, "_ocr_image_bytes", read)

	def test_page_lists(self):
		self.assertEqual(parse_pages("1-3, 7,9-10"), [1, 2, 3, 7, 9, 10])
		self.assertEqual(_ranges([1, 2, 3, 7, 9, 10]), "1-3,7,9-10")
		for bad in ("", "a", "3-1", "0", "1-x"):
			self.assertIsNone(parse_pages(bad), bad)

	def test_every_page_is_read_when_time_allows(self):
		with self.ocr():
			texts, read, total = FileContextService().ocr_pdf_pages(self.file)
		self.assertEqual((read, total), (list(range(1, PAGES + 1)), PAGES))

	def test_a_pass_stops_at_its_time_limit_and_the_next_goes_on_from_the_cache(self):
		with self.ocr():
			_, first, _ = FileContextService().ocr_pdf_pages(self.file, seconds=1e-9)
			self.assertEqual(first, [1], "at least one page per pass")
			_, second, _ = FileContextService().ocr_pdf_pages(self.file, seconds=1e-9)
		self.assertEqual(second, [1, 2], "page 1 from the cache, one new page")
		self.assertEqual(len(self.calls), 2, "no page is read twice")

	def test_the_summary_names_the_unread_pages_and_is_not_cached_while_some_are(self):
		with self.ocr(), patch("ai_agent.runtime.file_context.OCR_PDF_SECONDS", 1e-9), \
				patch("ai_agent.runtime.file_context.FileContextService._claude_pdf", return_value=""):
			summary = FileContextService().summarize_file(self.file)
		self.assertIn("- ocr_pages: 1 of 6", summary)
		self.assertIn('pages="2-6"', summary)
		self.assertFalse(frappe.cache.get_value(f"ai_agent:file_summary:{self.file.name}:{self.file.modified}"))

	def test_files_read_reads_the_pages_asked(self):
		with self.ocr():
			out = chat_files._read_pages(self.file, "4-6")
		self.assertEqual(out["pages_read"], "4-6")
		self.assertEqual(out["page_count"], PAGES)
		self.assertIn("[page 6]", out["content"])
		self.assertNotIn("pages_not_read", out)

	def test_files_read_refuses_a_bad_page_list(self):
		self.assertIn("error", chat_files._read_pages(self.file, "six"))
