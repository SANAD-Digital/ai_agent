import csv
import base64
import io
import json
import mimetypes
import os
import re
import shutil

import frappe
from frappe.utils.file_manager import get_file_path


MAX_FILE_CONTEXT_CHARS = 6000
MAX_SESSION_FILE_CONTEXT_CHARS = 8000
MAX_TEXT_CHARS = 4000
MAX_TABLE_ROWS = 20
MAX_DETECTED_INVOICE_ROWS = 40
MAX_PDF_LAYOUT_PAGES = 25
MAX_PDF_OCR_PAGES = MAX_PDF_LAYOUT_PAGES
MAX_ATTACHMENT_PREVIEW_PAGES = 120
MAX_ATTACHMENT_PREVIEW_BYTES = 350 * 1024
MIN_PDF_NUMERIC_CHARS = 6
SUPPORTED_TEXT_EXTENSIONS = {".csv", ".json", ".txt", ".md", ".log", ".xlsx", ".pdf"}
TEXT_DOCUMENT_EXTENSIONS = {".xlsx", ".xls", ".docx", ".csv", ".xml", ".json", ".txt", ".md", ".log"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_VISION_IMAGES = 4
MAX_VISION_PDF_PAGES = 3
MAX_VISION_FILE_BYTES = 8 * 1024 * 1024
FINANCIAL_ATTACHMENT_OCR_PROMPT = (
    "Read all visible Arabic and English text from this document image/page exactly as written. "
    "Preserve all visible numbers, currencies, dates, tax IDs, phone numbers, table rows, totals, and handwritten text accurately in clean Markdown. "
    "Do NOT make accounting decisions, analyze, or infer missing values. "
    "Simply extract and format the visible text from the image accurately in clean Markdown without code fences."
)


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
            layout_text = self._extract_pdf_layout_text(file_doc)
            detected_rows = self._extract_invoice_like_rows_from_layout(layout_text)
            detected_totals = self._extract_invoice_like_totals_from_layout(layout_text)
            detected_pages = self.extract_pdf_invoice_pages(file_doc)
            if detected_rows or self._is_better_pdf_table_text(text, layout_text):
                table_text = self._extract_pdf_table_section(layout_text)
                structured_sections = []
                if detected_pages:
                    structured_sections.append(
                        "- detected_attachment_pages:\n"
                        f"{json.dumps(detected_pages, ensure_ascii=False, default=str)}"
                    )
                if detected_rows:
                    structured_sections.append(
                        "- detected_attachment_table_rows:\n"
                        f"{json.dumps(detected_rows, ensure_ascii=False, default=str)}"
                    )
                if detected_totals:
                    structured_sections.append(
                        "- detected_attachment_totals:\n"
                        f"{json.dumps(detected_totals, ensure_ascii=False, default=str)}"
                    )
                return (
                    f"- pages: {len(reader.pages)}\n"
                    "- extraction: PDF text plus layout-aware table extraction\n"
                    f"{chr(10).join(structured_sections)}\n"
                    f"- text_preview:\n{text[:MAX_TEXT_CHARS]}\n"
                    "- table_layout_preview:\n"
                    f"{table_text[:MAX_TEXT_CHARS]}"
                )
            if self._needs_numeric_ocr_fallback(text):
                ocr_text = self._ocr_pdf(file_doc)
                if ocr_text:
                    return (
                        f"- pages: {len(reader.pages)}\n"
                        "- extraction: PDF text plus OCR numeric fallback\n"
                        f"- text_preview:\n{text[:MAX_TEXT_CHARS]}\n"
                        "- ocr_preview_for_numbers:\n"
                        f"{ocr_text[:MAX_TEXT_CHARS]}"
                    )
            return f"- pages: {len(reader.pages)}\n- text_preview:\n{text[:MAX_TEXT_CHARS]}"
        except Exception as error:
            return f"- summary: Could not extract PDF content: {str(error)}"

    def _needs_numeric_ocr_fallback(self, text):
        digits = re.findall(r"[0-9٠-٩۰-۹]", text or "")
        return not text or len(digits) < MIN_PDF_NUMERIC_CHARS

    def _extract_pdf_layout_text(self, file_doc):
        try:
            import fitz

            path = get_file_path(file_doc.name)
            document = fitz.open(path)
            pages = []
            for page_index in range(min(len(document), MAX_PDF_LAYOUT_PAGES)):
                page = document.load_page(page_index)
                text = (page.get_text("text", sort=True) or "").strip()
                if text:
                    pages.append(f"[page {page_index + 1}]\n{text}")
            document.close()
            return "\n\n".join(pages).strip()
        except Exception:
            return ""

    def extract_pdf_invoice_pages(self, file_doc):
        """Return per-page extraction for financial attachment PDF pages."""
        try:
            import fitz

            path = get_file_path(file_doc.name)
            document = fitz.open(path)
            pages = []
            for page_index in range(min(len(document), MAX_PDF_LAYOUT_PAGES)):
                pages.append(self._extract_pdf_page_result(document.load_page(page_index), page_index + 1))
            document.close()
            return pages
        except Exception:
            return []

    def extract_pdf_attachment_page(self, file_doc, page_number):
        """Re-read one PDF page without reprocessing the whole file."""
        page_number = max(1, int(page_number or 1))
        try:
            import fitz

            path = get_file_path(file_doc.name)
            document = fitz.open(path)
            if page_number > len(document):
                document.close()
                return {
                    "page": page_number,
                    "extraction": "pdf_page_not_found",
                    "metadata": {},
                    "rows": [],
                    "totals": {},
                    "text_preview": "",
                    "status": "failed",
                    "can_reprocess": 1,
                    "warnings": ["رقم الصفحة غير موجود في هذا الملف."],
                }
            result = self._extract_pdf_page_result(document.load_page(page_number - 1), page_number)
            document.close()
            return result
        except Exception:
            return {
                "page": page_number,
                "extraction": "pdf_page_ocr_failed",
                "metadata": {},
                "rows": [],
                "totals": {},
                "text_preview": "",
                "status": "failed",
                "can_reprocess": 1,
                "warnings": ["تعذرت إعادة قراءة هذه الصفحة."],
            }

    def render_attachment_pages(self, file_doc):
        """Return lightweight page previews without running OCR."""
        filename = file_doc.file_name or file_doc.name
        extension = os.path.splitext(filename)[1].lower()
        if extension == ".pdf":
            return self._render_pdf_pages(file_doc)
        if extension in IMAGE_EXTENSIONS:
            return [self._render_image_page(file_doc)]
        if extension in TEXT_DOCUMENT_EXTENSIONS:
            return [self._render_text_attachment_page(file_doc)]
        return [{
            "file_id": file_doc.name,
            "file_name": filename,
            "page_id": f"{file_doc.name}::p1",
            "page": 1,
            "page_type": "unsupported_attachment",
            "status": "unsupported",
            "can_read": 0,
            "preview_data_url": "",
            "note": "هذا النوع لا يدعم معاينة الصفحات.",
        }]

    def _render_pdf_pages(self, file_doc):
        try:
            import fitz

            filename = file_doc.file_name or file_doc.name
            path = get_file_path(file_doc.name)
            document = fitz.open(path)
            pages = []
            for page_index in range(min(len(document), MAX_ATTACHMENT_PREVIEW_PAGES)):
                page_number = page_index + 1
                pages.append({
                    "file_id": file_doc.name,
                    "file_name": filename,
                    "page_id": f"{file_doc.name}::p{page_number}",
                    "page": page_number,
                    "page_type": "pdf_page",
                    "status": "ready",
                    "can_read": 1,
                    "preview_data_url": "",
                    "note": "",
                })
            if len(document) > MAX_ATTACHMENT_PREVIEW_PAGES:
                pages.append({
                    "file_id": file_doc.name,
                    "file_name": filename,
                    "page_id": f"{file_doc.name}::overflow",
                    "page": MAX_ATTACHMENT_PREVIEW_PAGES + 1,
                    "page_type": "pdf_page_limit",
                    "status": "skipped",
                    "can_read": 0,
                    "preview_data_url": "",
                    "note": f"تم عرض أول {MAX_ATTACHMENT_PREVIEW_PAGES} صفحة فقط.",
                })
            document.close()
            return pages
        except Exception:
            return [{
                "file_id": file_doc.name,
                "file_name": file_doc.file_name or file_doc.name,
                "page_id": f"{file_doc.name}::p1",
                "page": 1,
                "page_type": "pdf_page",
                "status": "failed",
                "can_read": 0,
                "preview_data_url": "",
                "note": "تعذر تفكيك ملف PDF إلى صفحات.",
            }]

    def _render_image_page(self, file_doc):
        return {
            "file_id": file_doc.name,
            "file_name": file_doc.file_name or file_doc.name,
            "page_id": f"{file_doc.name}::p1",
            "page": 1,
            "page_type": "image",
            "status": "ready",
            "can_read": 1,
            "preview_data_url": "",
            "note": "",
        }

    def _render_text_attachment_page(self, file_doc):
        """Render a single-page preview for text-based files that do not need OCR."""
        extraction = self.extract_text_attachment_page(file_doc)
        text_preview = extraction.get("text_preview") or ""
        return {
            "file_id": file_doc.name,
            "file_name": file_doc.file_name or file_doc.name,
            "page_id": f"{file_doc.name}::p1",
            "page": 1,
            "page_type": "text_document",
            "status": "ready" if text_preview else "failed",
            "can_read": 1,
            "preview_data_url": "",
            "text_preview": text_preview,
            "note": "" if text_preview else "تعذرت قراءة محتوى الملف نصياً.",
        }

    def extract_text_attachment_page(self, file_doc):
        """Read a text-based attachment directly without OCR.

        Returns the same page-result shape as PDF/image extraction so the
        purchase invoice workspace can treat it like any other page.
        """
        filename = file_doc.file_name or file_doc.name
        extension = os.path.splitext(filename)[1].lower()
        warnings = []
        text = ""
        extraction = "direct_text_parse"

        try:
            if extension == ".xlsx":
                text = self._read_xlsx_text(file_doc)
                extraction = "xlsx_sheet_parse"
            elif extension == ".xls":
                text = self._read_xls_text(file_doc)
                extraction = "xls_sheet_parse"
            elif extension == ".docx":
                text = self._read_docx_text(file_doc)
                extraction = "docx_parse"
            elif extension == ".csv":
                text = self._read_text(file_doc)
                extraction = "csv_parse"
            elif extension in {".json", ".txt", ".md", ".log", ".xml"}:
                text = self._read_text(file_doc)
                extraction = "text_file_parse"
            else:
                warnings.append("هذا النوع لا يدعم القراءة المباشرة للنص.")
        except Exception as exc:
            frappe.log_error(frappe.get_traceback(), "Financial Attachment Direct Text Parse Failed")
            warnings.append(f"تعذرت قراءة محتوى الملف نصياً: {exc}")

        text = (text or "").strip()
        rows = self._extract_invoice_like_rows_from_layout(text) if text else []
        totals = self._extract_invoice_like_totals_from_layout(text) if text else {}
        metadata = self._extract_financial_document_metadata(text) if text else {}
        for row in rows:
            row["source_page"] = 1
            for key, value in metadata.items():
                if value and key not in row:
                    row[key] = value
        if text and not rows and not totals:
            warnings.append("تمت قراءة النص مباشرة من الملف وسيتم تحليله في الخطوة التالية.")
        elif not text:
            warnings.append("تعذر استخراج نص من هذا الملف.")
        return {
            "page": 1,
            "extraction": extraction,
            "metadata": metadata,
            "rows": rows,
            "totals": totals,
            "text_preview": text[:6000],
            "status": "read" if text else "failed",
            "can_reprocess": 1,
            "warnings": warnings,
        }

    def _read_xlsx_text(self, file_doc):
        try:
            from openpyxl import load_workbook

            path = get_file_path(file_doc.name)
            workbook = load_workbook(path, read_only=True, data_only=True)
            sections = []
            for worksheet in workbook.worksheets[:20]:
                lines = []
                for row in worksheet.iter_rows(values_only=True):
                    cells = [str(cell).strip() for cell in row if cell is not None and str(cell).strip()]
                    if cells:
                        lines.append(" | ".join(cells))
                if lines:
                    sections.append(f"[sheet: {worksheet.title}]\n" + "\n".join(lines))
            workbook.close()
            return "\n\n".join(sections)
        except Exception:
            return ""

    def _read_xls_text(self, file_doc):
        try:
            import xlrd

            path = get_file_path(file_doc.name)
            workbook = xlrd.open_workbook(path)
            sections = []
            for sheet in workbook.sheets()[:20]:
                lines = []
                for row_index in range(min(sheet.nrows, 500)):
                    cells = []
                    for col_index in range(sheet.ncols):
                        value = sheet.cell_value(row_index, col_index)
                        cell_text = str(value).strip()
                        if cell_text:
                            cells.append(cell_text)
                    if cells:
                        lines.append(" | ".join(cells))
                if lines:
                    sections.append(f"[sheet: {sheet.name}]\n" + "\n".join(lines))
            return "\n\n".join(sections)
        except Exception:
            return ""

    def _read_docx_text(self, file_doc):
        try:
            from docx import Document

            doc = Document(get_file_path(file_doc.name))
            parts = [paragraph.text for paragraph in doc.paragraphs if paragraph.text]
            for table in doc.tables[:20]:
                rows = []
                for row in table.rows[:200]:
                    cells = [cell.text.strip() for cell in row.cells]
                    cells = [cell for cell in cells if cell]
                    if cells:
                        rows.append(" | ".join(cells))
                if rows:
                    parts.append("[table]\n" + "\n".join(rows))
            return "\n".join(parts)
        except Exception:
            pass

        try:
            import zipfile
            import xml.etree.ElementTree as ET

            with zipfile.ZipFile(get_file_path(file_doc.name)) as archive:
                document_xml = archive.read("word/document.xml")
            root = ET.fromstring(document_xml)
            namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            paragraphs = []
            current = []
            for element in root.iter():
                if element.tag == f"{namespace}p":
                    paragraphs.append("".join(current).strip())
                    current = []
                elif element.tag == f"{namespace}t":
                    current.append(element.text or "")
            if current:
                paragraphs.append("".join(current).strip())
            return "\n".join(paragraph for paragraph in paragraphs if paragraph)
        except Exception:
            return ""

    def _page_preview_data_url(self, page):
        try:
            import fitz
            from PIL import Image

            pixmap = page.get_pixmap(matrix=fitz.Matrix(0.6, 0.6), alpha=False)
            image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=65)
            data = output.getvalue()
            return f"data:image/jpeg;base64,{base64.b64encode(data).decode('ascii')}"
        except Exception:
            return ""

    def _image_preview_data_url(self, file_doc):
        try:
            from PIL import Image

            path = get_file_path(file_doc.name)
            image = Image.open(path).convert("RGB")
            image.thumbnail((800, 1000))
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=65)
            data = output.getvalue()
            return f"data:image/jpeg;base64,{base64.b64encode(data).decode('ascii')}"
        except Exception:
            try:
                path = get_file_path(file_doc.name)
                with open(path, "rb") as handle:
                    data = handle.read(MAX_ATTACHMENT_PREVIEW_BYTES)
                mime_type = mimetypes.guess_type(file_doc.file_name or "")[0] or "image/png"
                return f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"
            except Exception:
                return ""

    def _extract_pdf_page_result(self, page, page_number):
        layout_text = (page.get_text("text", sort=True) or "").strip()
        extraction = "pdf_layout_text"
        warnings = []
        if self._needs_numeric_ocr_fallback(layout_text) and page_number <= MAX_PDF_OCR_PAGES:
            ocr_text = self._ocr_pdf_page(page)
            if ocr_text:
                layout_text = f"{layout_text}\n{ocr_text}".strip()
                extraction = "pdf_layout_text_plus_page_ocr"
            else:
                warnings.append("لم يرجع OCR نصًا واضحًا لهذه الصفحة بعد معالجة الصورة.")
                warnings.extend(self._ocr_diagnostics())
        elif self._needs_numeric_ocr_fallback(layout_text):
            warnings.append("هذه الصفحة خارج حد القراءة البصرية الحالي.")

        rows = self._extract_invoice_like_rows_from_layout(layout_text)
        totals = self._extract_invoice_like_totals_from_layout(layout_text)
        metadata = self._extract_financial_document_metadata(layout_text)
        for row in rows:
            row["source_page"] = page_number
            for key, value in metadata.items():
                if value and key not in row:
                    row[key] = value
        if layout_text and not rows and not totals:
            warnings.append("تمت قراءة النص وسيتم تحليله في الخطوة التالية.")
        elif not layout_text:
            warnings.append("لم يرجع OCR نصًا واضحًا لهذه الصفحة.")
            warnings.extend(self._ocr_diagnostics())
        return {
            "page": page_number,
            "extraction": extraction,
            "metadata": metadata,
            "rows": rows,
            "totals": totals,
            "text_preview": layout_text[:6000],
            "status": "read" if layout_text else "failed",
            "can_reprocess": 1,
            "warnings": warnings,
        }

    def extract_image_attachment_page(self, file_doc):
        """Return a page-like extraction record for image attachments."""
        try:
            text = self._ocr_image_file(file_doc)
            rows = self._extract_invoice_like_rows_from_layout(text)
            totals = self._extract_invoice_like_totals_from_layout(text)
            metadata = self._extract_financial_document_metadata(text)
            for row in rows:
                row["source_page"] = 1
                for key, value in metadata.items():
                    if value and key not in row:
                        row[key] = value
            warnings = []
            if not text:
                warnings.append("OCR did not return readable text for this image after preprocessing and rotation attempts.")
                warnings.extend(self._ocr_diagnostics())
            elif not rows and not totals:
                warnings.append("تمت قراءة النص وسيتم تحليله في الخطوة التالية.")
            return [{
                "page": 1,
                "extraction": "image_ocr",
                "metadata": metadata,
                "rows": rows,
                "totals": totals,
                "text_preview": (text or "")[:6000],
                "status": "read" if text else "failed",
                "can_reprocess": 1,
                "warnings": warnings,
            }]
        except Exception:
            return [{
                "page": 1,
                "extraction": "image_ocr",
                "metadata": {},
                "rows": [],
                "totals": {},
                "text_preview": "",
                "status": "failed",
                "can_reprocess": 1,
                "warnings": ["Image OCR failed before returning a result."] + self._ocr_diagnostics(),
            }]

    def _ocr_diagnostics(self):
        try:
            from ai_agent.runtime.gemini_ocr import get_last_gemini_ocr_error, is_gemini_ocr_enabled

            error = get_last_gemini_ocr_error()
            if error:
                frappe.log_error(str(error)[:1000], "Financial Attachment OCR diagnostic")
                return [f"Gemini OCR: {str(error)[:700]}"]
            if not is_gemini_ocr_enabled():
                return ["محرك OCR الاحتياطي غير مفعل في الإعدادات."]
        except Exception:
            pass
        return []

    def _extract_financial_document_metadata(self, text):
        """Best-effort page-level patterns without assigning semantic labels by keywords."""
        value = text or ""
        normalized_value = self._normalize_ocr_digits(value)
        metadata = {}

        tax_match = re.search(r"\b3\d{13,14}\b", normalized_value)
        if tax_match:
            metadata["custom_tax_id"] = tax_match.group(0)

        date_match = (
            re.search(r"\b([0-3]?\d)[/-]([01]?\d)[/-](20\d{2})\b", normalized_value)
            or re.search(r"\b(20\d{2})[/-]([01]?\d)[/-]([0-3]?\d)\b", normalized_value)
        )
        if date_match:
            groups = date_match.groups()
            if len(groups[0]) == 4:
                year, month, day = groups
            else:
                day, month, year = groups
            metadata["custom_bill_date"] = f"{year}-{int(month):02d}-{int(day):02d}"

        number_match = re.search(r"(?:\bNo\.?\s*[:\-]?\s*|\bNO\.?\s*[:\-]?\s*)([A-Za-z0-9\-]{3,})", normalized_value)
        if number_match:
            metadata["custom_bill_no"] = number_match.group(1)

        number_candidates = self._document_number_candidates(normalized_value, tax_match.group(0) if tax_match else "")
        if number_candidates:
            metadata["document_number_candidates"] = number_candidates
            if not metadata.get("custom_bill_no"):
                metadata["custom_bill_no"] = number_candidates[0]

        amount_candidates = []
        for match in re.finditer(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d+(?:\.\d{2})\b", normalized_value):
            number = match.group(0).replace(",", "")
            try:
                value_number = float(number)
            except Exception:
                continue
            if value_number >= 1_000_000_000 or (tax_match and match.group(0) in tax_match.group(0)):
                continue
            amount_candidates.append(value_number)
        if amount_candidates:
            metadata["document_amount"] = max(amount_candidates)

        return metadata

    def _normalize_ocr_digits(self, text):
        return str(text or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))

    def _document_number_candidates(self, text, tax_id=""):
        candidates = []
        tax_id = str(tax_id or "")
        ignored = set()
        for match in re.finditer(r"\b(?:20\d{2})[/-](?:[01]?\d)[/-](?:[0-3]?\d)\b|\b(?:[0-3]?\d)[/-](?:[01]?\d)[/-](?:20\d{2})\b", text):
            ignored.add(match.group(0))
        for match in re.finditer(r"\b[A-Z]{1,8}[-]?\d{3,}\b|\b\d{5,12}\b", text, flags=re.I):
            value = match.group(0).strip(" -:")
            digits = re.sub(r"\D", "", value)
            if not digits:
                continue
            if tax_id and digits in tax_id:
                continue
            if value in ignored or any(value in item for item in ignored):
                continue
            if len(digits) >= 13:
                continue
            score = 0
            if re.search(r"[A-Za-z]", value) and len(digits) >= 5:
                score += 5
            elif re.search(r"[A-Za-z]", value):
                score -= 2
            if 6 <= len(digits) <= 10:
                score += 4
            if digits and digits[0] not in {"0", "1", "3"}:
                score += 2
            if "." in value or "," in value:
                score -= 4
            candidates.append((score, match.start(), value))
        ordered = []
        seen = set()
        for _score, _pos, value in sorted(candidates, key=lambda item: (-item[0], item[1])):
            if value not in seen:
                ordered.append(value)
                seen.add(value)
        return ordered[:8]

    def _ocr_pdf_page(self, page):
        try:
            import fitz

            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            return self._ocr_image_bytes(pixmap.tobytes("png"), mime_type="image/png")
        except Exception:
            return ""

    def _preprocess_image_bytes(self, data):
        try:
            try:
                import cv2
                import numpy as np

                array = np.frombuffer(data, np.uint8)
                image = cv2.imdecode(array, cv2.IMREAD_GRAYSCALE)
                if image is None:
                    return data
                image = cv2.GaussianBlur(image, (3, 3), 0)
                image = cv2.adaptiveThreshold(
                    image, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                    cv2.THRESH_BINARY, 31, 9,
                )
                ok, encoded = cv2.imencode(".png", image)
                return encoded.tobytes() if ok else data
            except Exception:
                from PIL import Image, ImageOps

                image = Image.open(io.BytesIO(data)).convert("L")
                image = ImageOps.autocontrast(image)
                output = io.BytesIO()
                image.save(output, format="PNG")
                return output.getvalue()
        except Exception:
            return data

    def _ocr_image_variants(self, data):
        variants = [data]
        try:
            processed = self._preprocess_image_bytes(data)
            if processed and processed not in variants:
                variants.append(processed)
        except Exception:
            pass
        try:
            import cv2
            import numpy as np

            array = np.frombuffer(data, np.uint8)
            image = cv2.imdecode(array, cv2.IMREAD_GRAYSCALE)
            if image is None:
                return variants

            image = self._crop_dark_content_cv2(image)
            scaled = self._scale_for_ocr_cv2(image)
            deskewed = self._deskew_cv2(scaled)
            for candidate in (image, scaled, deskewed):
                if candidate is None:
                    continue
                ok, encoded = cv2.imencode(".png", candidate)
                if ok:
                    content = encoded.tobytes()
                    if content not in variants:
                        variants.append(content)
        except Exception:
            pass
        return variants[:3]

    def _crop_dark_content_cv2(self, image):
        try:
            import cv2
            import numpy as np

            blurred = cv2.GaussianBlur(image, (3, 3), 0)
            _, binary = cv2.threshold(blurred, 245, 255, cv2.THRESH_BINARY_INV)
            points = cv2.findNonZero(binary)
            if points is None:
                return image
            x, y, width, height = cv2.boundingRect(points)
            margin = max(20, int(min(image.shape[:2]) * 0.03))
            y1 = max(0, y - margin)
            x1 = max(0, x - margin)
            y2 = min(image.shape[0], y + height + margin)
            x2 = min(image.shape[1], x + width + margin)
            cropped = image[y1:y2, x1:x2]
            return cropped if cropped.size else image
        except Exception:
            return image

    def _scale_for_ocr_cv2(self, image):
        try:
            import cv2

            height, width = image.shape[:2]
            longest = max(height, width)
            if longest >= 1800:
                return image
            scale = min(2.0, 1800 / max(longest, 1))
            return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
        except Exception:
            return image

    def _deskew_cv2(self, image):
        try:
            import cv2
            import numpy as np

            edges = cv2.Canny(image, 50, 150, apertureSize=3)
            lines = cv2.HoughLinesP(
                edges,
                1,
                np.pi / 180,
                threshold=80,
                minLineLength=max(80, int(max(image.shape[:2]) * 0.18)),
                maxLineGap=20,
            )
            if lines is None:
                return image
            angles = []
            for line in lines[:, 0]:
                x1, y1, x2, y2 = line
                angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
                if -45 <= angle <= 45:
                    angles.append(angle)
            if not angles:
                return image
            angle = float(np.median(angles))
            if abs(angle) < 0.5 or abs(angle) > 30:
                return image
            height, width = image.shape[:2]
            matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
            return cv2.warpAffine(image, matrix, (width, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        except Exception:
            return image

    def _threshold_for_ocr_cv2(self, image):
        try:
            import cv2

            blurred = cv2.GaussianBlur(image, (3, 3), 0)
            return cv2.adaptiveThreshold(
                blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 31, 9,
            )
        except Exception:
            return image

    def _is_better_pdf_table_text(self, text, layout_text):
        if not layout_text or layout_text == text:
            return False
        table_markers = ("الكمية", "سعر الوحدة", "الإجمالي", "االجمالي", "اإلجمالي", "الصنف")
        if not any(marker in (text or "") for marker in table_markers):
            return False
        return self._money_token_count(layout_text) > self._money_token_count(text)

    def _extract_pdf_table_section(self, layout_text):
        markers = ("الكمية", "سعر الوحدة", "الوحدة سعر", "الصنف", "اإلجمالي", "االجمالي")
        positions = [
            layout_text.find(marker)
            for marker in markers
            if marker in layout_text
        ]
        if not positions:
            return layout_text
        start = max(0, min(positions) - 300)
        return layout_text[start:]

    def _money_token_count(self, text):
        return len(re.findall(r"\b\d[\d,]*[.٫]\d{2}\b", text or ""))

    def _extract_invoice_like_rows_from_layout(self, layout_text):
        rows = []
        for raw_line in (layout_text or "").splitlines():
            line = " ".join(raw_line.split())
            money_tokens = re.findall(r"\b\d[\d,]*[.٫]\d{2}\b", line)
            if len(money_tokens) < 2 or self._looks_like_invoice_total_line(line):
                continue

            line_total = self._parse_decimal_token(money_tokens[0])
            rate = self._parse_decimal_token(money_tokens[1])
            if line_total is None or rate is None:
                continue

            detail_text = line
            for money_token in money_tokens[:2]:
                detail_text = detail_text.replace(money_token, " ", 1)
            detail_text = self._strip_currency_tokens(detail_text)
            tokens = detail_text.split()
            if not tokens:
                continue

            line_no = None
            if self._is_numeric_token(tokens[-1]):
                line_no = int(self._parse_decimal_token(tokens.pop()) or 0) or None

            qty = None
            uom = ""
            description_tokens = tokens[:]
            for index, token in enumerate(tokens):
                if self._is_numeric_token(token):
                    qty = self._parse_decimal_token(token)
                    uom = " ".join(tokens[:index]).strip()
                    description_tokens = tokens[index + 1:]
                    break

            if qty is None and rate:
                qty = round(line_total / rate, 6)

            description = " ".join(description_tokens).strip(" -")
            if not description or qty is None:
                continue

            rows.append({
                "line_no": line_no or len(rows) + 1,
                "description": description,
                "qty": qty,
                "uom": uom,
                "rate": rate,
                "line_total": line_total,
                "raw_line": line,
                "confidence": 0.78,
            })
            if len(rows) >= MAX_DETECTED_INVOICE_ROWS:
                break
        return rows

    def _extract_invoice_like_totals_from_layout(self, layout_text):
        totals = {}
        for raw_line in (layout_text or "").splitlines():
            line = " ".join(raw_line.split())
            money_tokens = re.findall(r"\b\d[\d,]*[.٫]\d{2}\b", line)
            if not money_tokens:
                continue
            amount = self._parse_decimal_token(money_tokens[0])
            if amount is None:
                continue
            normalized_line = self._normalize_arabic_text(line)
            if "المستحق" in normalized_line or "اجمالي الفاتوره" in normalized_line:
                totals["grand_total"] = amount
            elif "الشحن" in normalized_line:
                totals["shipping_amount"] = amount
            elif "قبل" in normalized_line and "ضريب" in normalized_line:
                totals["subtotal"] = amount
            elif "ضريب" in normalized_line:
                totals["tax_amount"] = amount
        return totals

    def _looks_like_invoice_total_line(self, line):
        normalized = self._normalize_arabic_text(line)
        markers = (
            "الاجمالي", "االجمالي", "الاجمالي قبل", "اجمالي الفاتورة", "المستحق",
            "ضريبة", "الشحن", "subtotal", "total", "vat", "tax",
        )
        return any(marker in normalized.lower() for marker in markers)

    def _strip_currency_tokens(self, text):
        cleaned = text or ""
        for token in ("س . ر", "س .ر", "س. ر", "س.ر", "ر . س", "ر .س", "ر. س", "ر.س", "ريال", "SAR", "sar"):
            cleaned = cleaned.replace(token, " ")
        return " ".join(cleaned.split())

    def _is_numeric_token(self, token):
        return bool(re.match(r"^[0-9٠-٩۰-۹][0-9٠-٩۰-۹,٫.]*$", token or ""))

    def _parse_decimal_token(self, token):
        try:
            normalized = str(token or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
            normalized = normalized.replace("٫", ".").replace(",", "")
            return float(normalized)
        except Exception:
            return None

    def _normalize_arabic_text(self, text):
        normalized = str(text or "")
        replacements = {
            "أ": "ا",
            "إ": "ا",
            "آ": "ا",
            "ة": "ه",
            "ى": "ي",
        }
        for source, target in replacements.items():
            normalized = normalized.replace(source, target)
        return normalized

    def _ocr_image_file(self, file_doc):
        try:
            path = get_file_path(file_doc.name)
            with open(path, "rb") as file:
                filename = file_doc.file_name or file_doc.name
                mime_type = mimetypes.guess_type(filename)[0] or "image/png"
                return self._ocr_image_bytes(file.read(MAX_VISION_FILE_BYTES), mime_type=mime_type)
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
                text = self._ocr_image_bytes(pixmap.tobytes("png"), mime_type="image/png")
                if text:
                    pages.append(f"[page {page_index + 1}]\n{text}")
            document.close()
            return "\n\n".join(pages).strip()
        except Exception:
            return ""

    def _ocr_image_bytes(self, data, mime_type="image/png"):
        # Route through the configured OCR engine only. A failed cloud request
        # should fail fast instead of silently starting a heavy local OCR pass.
        try:
            from ai_agent.runtime.gemini_ocr import gemini_ocr_image_bytes

            ocr_text = gemini_ocr_image_bytes(data, mime_type=mime_type, prompt=FINANCIAL_ATTACHMENT_OCR_PROMPT)
            if ocr_text and ocr_text.strip():
                return ocr_text.strip()
        except Exception:
            pass

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
