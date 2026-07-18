import csv
import base64
import io
import json
import mimetypes
import os
import shutil

import frappe
from frappe.utils.file_manager import get_file_path


MAX_FILE_CONTEXT_CHARS = 6000
MAX_SESSION_FILE_CONTEXT_CHARS = 8000
MAX_TEXT_CHARS = 4000
MAX_TABLE_ROWS = 20
SUPPORTED_TEXT_EXTENSIONS = {".csv", ".json", ".txt", ".md", ".log", ".xlsx", ".pdf"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_VISION_IMAGES = 4
MAX_VISION_PDF_PAGES = 3
MAX_VISION_FILE_BYTES = 8 * 1024 * 1024


class FileContextService:
    """Build compact model context from files attached to an AI Run."""

    def build_context(self, run_name):
        files = self.get_run_files(run_name)
        if not files:
            return ""

        sections = []
        for file_doc in files[:5]:
            sections.append(self.summarize_file(file_doc))

        text = "\n\n".join(section for section in sections if section)
        if len(text) > MAX_FILE_CONTEXT_CHARS:
            text = text[:MAX_FILE_CONTEXT_CHARS].rstrip() + "\n... [file context truncated]"
        return text

    def build_context_for_runs(self, run_names, title="Attached files context"):
        if not run_names:
            return ""

        sections = []
        seen_files = set()
        for run_name in run_names[-10:]:
            for file_doc in self.get_run_files(run_name):
                if file_doc.name in seen_files:
                    continue
                seen_files.add(file_doc.name)
                sections.append(
                    f"Run: {run_name}\n{self.summarize_file(file_doc)}"
                )
                if len(sections) >= 8:
                    break
            if len(sections) >= 8:
                break

        if not sections:
            return ""

        text = f"{title}:\n" + "\n\n".join(section for section in sections if section)
        if len(text) > MAX_SESSION_FILE_CONTEXT_CHARS:
            text = text[:MAX_SESSION_FILE_CONTEXT_CHARS].rstrip() + "\n... [previous file context truncated]"
        return text

    def has_visual_files(self, run_name):
        return any(self._is_visual_file(file_doc) for file_doc in self.get_run_files(run_name))

    def build_vision_parts(self, run_name):
        parts = []
        for file_doc in self.get_run_files(run_name):
            if len(parts) >= MAX_VISION_IMAGES:
                break
            filename = file_doc.file_name or file_doc.name
            extension = os.path.splitext(filename)[1].lower()
            if extension in IMAGE_EXTENSIONS:
                image_part = self._image_file_to_part(file_doc)
                if image_part:
                    parts.append(image_part)
            elif extension == ".pdf":
                for image_part in self._pdf_to_image_parts(file_doc):
                    if len(parts) >= MAX_VISION_IMAGES:
                        break
                    parts.append(image_part)
        return parts

    def get_run_files(self, run_name):
        names = frappe.get_all(
            "File",
            filters={
                "attached_to_doctype": "AI Run",
                "attached_to_name": run_name,
                "is_folder": 0,
            },
            pluck="name",
            order_by="creation asc",
        )
        return [frappe.get_doc("File", name) for name in names]

    def summarize_file(self, file_doc):
        filename = file_doc.file_name or file_doc.name
        extension = os.path.splitext(filename)[1].lower()
        header = (
            f"File: {filename}\n"
            f"- type: {extension or 'unknown'}\n"
            f"- size_bytes: {file_doc.file_size or 0}"
        )

        if extension in IMAGE_EXTENSIONS:
            ocr_text = self._ocr_image_file(file_doc)
            if ocr_text:
                return (
                    f"{header}\n"
                    "- extraction: OCR\n"
                    f"- text_preview:\n{ocr_text[:MAX_TEXT_CHARS]}"
                )
            return (
                f"{header}\n"
                "- summary: Image file attached. Local OCR is not available or found no text; "
                "it will be provided to the model visually when the selected model supports vision."
            )

        if extension not in SUPPORTED_TEXT_EXTENSIONS:
            return (
                f"{header}\n"
                "- summary: File is attached and available, but automatic text extraction is not enabled for this type yet."
            )

        if extension == ".xlsx":
            return f"{header}\n{self._summarize_xlsx(file_doc)}"
        if extension == ".pdf":
            return f"{header}\n{self._summarize_pdf(file_doc)}"

        content = self._read_text(file_doc)
        if not content:
            return f"{header}\n- summary: Empty or unreadable file."

        if extension == ".csv":
            return f"{header}\n{self._summarize_csv(content)}"
        if extension == ".json":
            return f"{header}\n{self._summarize_json(content)}"
        return f"{header}\n- preview:\n{content[:MAX_TEXT_CHARS]}"

    def _read_text(self, file_doc):
        try:
            path = get_file_path(file_doc.name)
            with open(path, "rb") as file:
                data = file.read(MAX_TEXT_CHARS * 4)
            return data.decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _summarize_csv(self, content):
        reader = csv.DictReader(io.StringIO(content))
        rows = []
        for index, row in enumerate(reader):
            if index >= MAX_TABLE_ROWS:
                break
            rows.append(row)

        columns = reader.fieldnames or []
        return (
            f"- columns: {', '.join(columns[:30])}\n"
            f"- sample_rows: {json.dumps(rows, ensure_ascii=False, default=str)}"
        )

    def _summarize_json(self, content):
        try:
            parsed = json.loads(content)
        except Exception:
            return f"- preview:\n{content[:MAX_TEXT_CHARS]}"

        if isinstance(parsed, list):
            sample = parsed[:10]
            return (
                f"- json_type: list\n"
                f"- item_count_preview: {len(parsed)}\n"
                f"- sample: {json.dumps(sample, ensure_ascii=False, default=str)[:MAX_TEXT_CHARS]}"
            )
        if isinstance(parsed, dict):
            return (
                f"- json_type: object\n"
                f"- keys: {', '.join(list(parsed.keys())[:40])}\n"
                f"- sample: {json.dumps(parsed, ensure_ascii=False, default=str)[:MAX_TEXT_CHARS]}"
            )
        return f"- value: {json.dumps(parsed, ensure_ascii=False, default=str)[:MAX_TEXT_CHARS]}"

    def _summarize_xlsx(self, file_doc):
        try:
            from openpyxl import load_workbook

            path = get_file_path(file_doc.name)
            workbook = load_workbook(path, read_only=True, data_only=True)
            sheets = []
            for worksheet in workbook.worksheets[:5]:
                rows = []
                for index, row in enumerate(worksheet.iter_rows(values_only=True)):
                    if index >= MAX_TABLE_ROWS:
                        break
                    rows.append([cell for cell in row])
                sheets.append({
                    "sheet": worksheet.title,
                    "max_row": worksheet.max_row,
                    "max_column": worksheet.max_column,
                    "sample_rows": rows,
                })
            workbook.close()
            return f"- workbook_sheets: {json.dumps(sheets, ensure_ascii=False, default=str)[:MAX_TEXT_CHARS]}"
        except Exception as error:
            return f"- summary: Could not extract Excel content: {str(error)}"

    def _summarize_pdf(self, file_doc):
        try:
            from pypdf import PdfReader

            path = get_file_path(file_doc.name)
            reader = PdfReader(path)
            pages = []
            for page in reader.pages[:5]:
                pages.append((page.extract_text() or "").strip())
            text = "\n\n".join(page for page in pages if page)
            if not text:
                ocr_text = self._ocr_pdf(file_doc)
                if ocr_text:
                    return (
                        f"- pages: {len(reader.pages)}\n"
                        "- extraction: OCR from scanned/image PDF\n"
                        f"- text_preview:\n{ocr_text[:MAX_TEXT_CHARS]}"
                    )
                return (
                    f"- pages: {len(reader.pages)}\n"
                    "- summary: No extractable text found. This is probably a scanned/image PDF. "
                    "Local OCR is not available or found no text; visual pages will be provided to the model when the selected model supports vision."
                )
            return f"- pages: {len(reader.pages)}\n- text_preview:\n{text[:MAX_TEXT_CHARS]}"
        except Exception as error:
            return f"- summary: Could not extract PDF content: {str(error)}"

    def _ocr_image_file(self, file_doc):
        try:
            path = get_file_path(file_doc.name)
            with open(path, "rb") as file:
                return self._ocr_image_bytes(file.read(MAX_VISION_FILE_BYTES))
        except Exception:
            return ""

    def _ocr_pdf(self, file_doc):
        try:
            import fitz

            path = get_file_path(file_doc.name)
            document = fitz.open(path)
            pages = []
            for page_index in range(min(len(document), MAX_VISION_PDF_PAGES)):
                page = document.load_page(page_index)
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                text = self._ocr_image_bytes(pixmap.tobytes("png"))
                if text:
                    pages.append(f"[page {page_index + 1}]\n{text}")
            document.close()
            return "\n\n".join(pages).strip()
        except Exception:
            return ""

    def _ocr_image_bytes(self, data):
        if not shutil.which("tesseract"):
            return ""
        try:
            from PIL import Image
            import pytesseract

            image = Image.open(io.BytesIO(data))
            for language in ("ara+eng", "eng", None):
                try:
                    text = pytesseract.image_to_string(image, lang=language) if language else pytesseract.image_to_string(image)
                    text = " ".join((text or "").split())
                    if text:
                        return text
                except Exception:
                    continue
        except Exception:
            return ""
        return ""

    def _is_visual_file(self, file_doc):
        filename = file_doc.file_name or file_doc.name
        extension = os.path.splitext(filename)[1].lower()
        return extension in IMAGE_EXTENSIONS or extension == ".pdf"

    def _image_file_to_part(self, file_doc, label=None):
        try:
            if cint_safe(file_doc.file_size) > MAX_VISION_FILE_BYTES:
                return None
            path = get_file_path(file_doc.name)
            with open(path, "rb") as file:
                data = file.read(MAX_VISION_FILE_BYTES + 1)
            if len(data) > MAX_VISION_FILE_BYTES:
                return None
            filename = label or file_doc.file_name or file_doc.name
            mime_type = mimetypes.guess_type(filename)[0] or "image/png"
            return {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}",
                    "detail": "high",
                },
            }
        except Exception:
            return None

    def _pdf_to_image_parts(self, file_doc):
        try:
            import fitz

            path = get_file_path(file_doc.name)
            document = fitz.open(path)
            parts = []
            for page_index in range(min(len(document), MAX_VISION_PDF_PAGES)):
                page = document.load_page(page_index)
                pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                data = pixmap.tobytes("png")
                if len(data) > MAX_VISION_FILE_BYTES:
                    continue
                filename = file_doc.file_name or file_doc.name
                parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}",
                        "detail": "high",
                    },
                })
            document.close()
            return parts
        except Exception:
            return []


def cint_safe(value):
    try:
        return int(value or 0)
    except Exception:
        return 0
