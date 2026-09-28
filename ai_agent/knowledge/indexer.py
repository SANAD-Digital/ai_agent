"""Split AI Knowledge Documents into searchable chunks (no vector store).

PDFs are read page by page (pypdf) so every chunk keeps its page number;
Markdown and text are split on headings and paragraphs. Chunks store a
normalized copy of their text for Arabic-insensitive matching.
"""

import io
import re

import frappe
from frappe.utils import now_datetime

from ai_agent.runtime.constants import ARABIC_NORMALIZATION_MAP

CHUNK_CHARS = 1200
_DIACRITICS = re.compile(r"[ً-ْـ]")  # tashkeel + tatweel


def normalize(text):
	text = _DIACRITICS.sub("", str(text or "").lower())
	for source, target in ARABIC_NORMALIZATION_MAP:
		text = text.replace(source, target)
	return " ".join(text.split())


def index_document(document):
	doc = frappe.get_doc("AI Knowledge Document", document)
	doc.db_set("indexing_status", "Indexing", update_modified=False)
	frappe.db.commit()
	try:
		content, extension = _read_file(doc.file)
		chunks = split(content, extension)
		frappe.db.delete("AI Knowledge Chunk", {"document": doc.name})
		for index, chunk in enumerate(chunks):
			frappe.get_doc({
				"doctype": "AI Knowledge Chunk",
				"document": doc.name,
				"page": chunk["page"],
				"chunk_index": index,
				"heading": chunk["heading"][:140],
				"content": chunk["text"],
				"search_text": normalize(f"{chunk['heading']} {chunk['text']}"),
			}).insert(ignore_permissions=True)
		doc.db_set({
			"indexing_status": "Indexed",
			"chunk_count": len(chunks),
			"indexed_on": now_datetime(),
			"index_error": "",
		}, update_modified=False)
	except Exception as exc:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), f"AI Knowledge Indexing Failed: {document}")
		doc.db_set({"indexing_status": "Failed", "index_error": str(exc)[:500]}, update_modified=False)
	frappe.db.commit()


def _read_file(file_url):
	file_doc = frappe.get_doc("File", {"file_url": file_url})
	extension = (file_doc.file_name or file_url).rsplit(".", 1)[-1].lower()
	return file_doc.get_content(), extension


def split(content, extension):
	"""[{page, heading, text}] chunks of at most ~CHUNK_CHARS characters."""
	if extension == "pdf":
		from pypdf import PdfReader

		reader = PdfReader(io.BytesIO(content if isinstance(content, bytes) else content.encode()))
		pages = [(number, page.extract_text() or "") for number, page in enumerate(reader.pages, start=1)]
	else:
		text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)
		pages = [(None, text)]

	chunks = []
	for page, text in pages:
		heading = ""
		buffer = []
		for block in re.split(r"\n\s*\n", text):
			block = block.strip()
			if not block:
				continue
			first_line = block.splitlines()[0]
			if first_line.startswith("#"):
				_flush(chunks, page, heading, buffer)
				heading = first_line.lstrip("#").strip()
				block = "\n".join(block.splitlines()[1:]).strip()
				if not block:
					continue
			if sum(len(b) for b in buffer) + len(block) > CHUNK_CHARS:
				_flush(chunks, page, heading, buffer)
			while len(block) > CHUNK_CHARS:
				buffer.append(block[:CHUNK_CHARS])
				_flush(chunks, page, heading, buffer)
				block = block[CHUNK_CHARS:]
			buffer.append(block)
		_flush(chunks, page, heading, buffer)
	return chunks


def _flush(chunks, page, heading, buffer):
	text = "\n\n".join(buffer).strip()
	if text:
		chunks.append({"page": page, "heading": heading, "text": text})
	buffer.clear()
