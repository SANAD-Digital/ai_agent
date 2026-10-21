import csv
import base64
import io
import json
import mimetypes
import os
import re

import frappe
from frappe.utils.file_manager import get_file_path


MAX_FILE_CONTEXT_CHARS = 40000
MAX_SESSION_FILE_CONTEXT_CHARS = 24000
MAX_TEXT_CHARS = 4000
MAX_OCR_TEXT_CHARS = 24000
MAX_OCR_PDF_PAGES = 10
FILE_SUMMARY_CACHE_SECONDS = 24 * 3600
MAX_TABLE_ROWS = 20
MAX_DETECTED_INVOICE_ROWS = 40
MAX_PDF_LAYOUT_PAGES = 25
MAX_PDF_OCR_PAGES = MAX_PDF_LAYOUT_PAGES
MIN_PDF_NUMERIC_CHARS = 6
SUPPORTED_TEXT_EXTENSIONS = {".csv", ".json", ".txt", ".md", ".log", ".xlsx", ".pdf"}
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

    def build_context(self, run_name, progress_run=None):
        files = self.get_run_files(run_name)
        if not files:
            return ""

        sections = []
        for file_doc in files[:5]:
            sections.append(self.summarize_file(file_doc, progress_run=progress_run))

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

    def summarize_file(self, file_doc, progress_run=None):
        cache_key = f"ai_agent:file_summary:{file_doc.name}:{file_doc.modified}"
        try:
            cached = frappe.cache().get_value(cache_key)
        except Exception:
            cached = None
        if cached:
            return cached
        summary = self._summarize_file_uncached(file_doc, progress_run=progress_run)
        if summary and ("extraction: OCR" in summary or "text_preview" in summary):
            try:
                frappe.cache().set_value(cache_key, summary, expires_in_sec=FILE_SUMMARY_CACHE_SECONDS)
            except Exception:
                pass
        return summary

    def _summarize_file_uncached(self, file_doc, progress_run=None):
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
                    f"- text_preview:\n{ocr_text[:MAX_OCR_TEXT_CHARS]}"
                )
            diagnostics = self._ocr_diagnostics()
            return (
                f"{header}\n"
                "- summary: Image file attached. Local OCR is not available or found no text; "
                "it will be provided to the model visually when the selected model supports vision."
                + (f"\n- ocr_diagnostics: {'; '.join(diagnostics)}" if diagnostics else "")
            )

        if extension in (".docx", ".xls"):
            text = self._read_docx_text(file_doc) if extension == ".docx" else self._read_xls_text(file_doc)
            if not text:
                return f"{header}\n- summary: Empty or unreadable file."
            return f"{header}\n- preview:\n{text[:MAX_TEXT_CHARS]}"

        if extension not in SUPPORTED_TEXT_EXTENSIONS:
            return (
                f"{header}\n"
                "- summary: File is attached and available, but automatic text extraction is not enabled for this type yet."
            )

        if extension == ".xlsx":
            return f"{header}\n{self._summarize_xlsx(file_doc)}"
        if extension == ".pdf":
            return f"{header}\n{self._summarize_pdf(file_doc, progress_run=progress_run)}"

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

    def _summarize_pdf(self, file_doc, progress_run=None):
        try:
            from pypdf import PdfReader

            path = get_file_path(file_doc.name)
            reader = PdfReader(path)
            pages = []
            for page in reader.pages[:5]:
                pages.append((page.extract_text() or "").strip())
            text = "\n\n".join(page for page in pages if page)
            if not text:
                ocr_text = self._ocr_pdf(file_doc, progress_run=progress_run)
                if ocr_text:
                    return (
                        f"- pages: {len(reader.pages)}\n"
                        "- extraction: OCR from scanned/image PDF\n"
                        f"- ocr_pages: {self._ocr_pages_read(len(reader.pages))} of {len(reader.pages)}\n"
                        f"- text_preview:\n{ocr_text[:MAX_OCR_TEXT_CHARS]}"
                    )
                diagnostics = self._ocr_diagnostics()
                return (
                    f"- pages: {len(reader.pages)}\n"
                    "- summary: No extractable text found. This is probably a scanned/image PDF. "
                    "Local OCR is not available or found no text; visual pages will be provided to the model when the selected model supports vision."
                    + (f"\n- ocr_diagnostics: {'; '.join(diagnostics)}" if diagnostics else "")
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
                ocr_text = self._ocr_pdf(file_doc, progress_run=progress_run)
                if ocr_text:
                    return (
                        f"- pages: {len(reader.pages)}\n"
                        "- extraction: PDF text plus OCR numeric fallback\n"
                        f"- text_preview:\n{text[:MAX_OCR_TEXT_CHARS]}\n"
                        "- ocr_preview_for_numbers:\n"
                        f"{ocr_text[:MAX_OCR_TEXT_CHARS]}"
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
            filename = file_doc.file_name or file_doc.name
            with open(path, "rb") as file:
                data = file.read(MAX_VISION_FILE_BYTES + 1)
            if len(data) > MAX_VISION_FILE_BYTES:
                # A truncated image is corrupt input, not a smaller image.
                return ""
            mime_type = mimetypes.guess_type(filename)[0] or "image/png"
            return self._ocr_image_bytes(data, mime_type=mime_type)
        except Exception:
            return ""

    def _ocr_pdf(self, file_doc, progress_run=None):
        claude_text = self._claude_pdf(file_doc, progress_run)
        if claude_text:
            return claude_text
        try:
            import fitz

            path = get_file_path(file_doc.name)
            filename = file_doc.file_name or file_doc.name
            document = fitz.open(path)
            total = min(len(document), MAX_OCR_PDF_PAGES)
            pages = []
            for page_index in range(total):
                page = document.load_page(page_index)
                pixmap = page.get_pixmap(matrix=fitz.Matrix(150 / 72, 150 / 72), alpha=False)
                text = self._ocr_image_bytes(pixmap.tobytes("png"), mime_type="image/png", allow_claude=False)
                if text:
                    pages.append(f"[page {page_index + 1}]\n{text}")
            document.close()
            return "\n\n".join(pages).strip()
        except Exception:
            return ""

    def _ocr_image_bytes(self, data, mime_type="image/png", allow_claude=True):
        # Route through the configured OCR engine only. A failed cloud request
        # should fail fast instead of silently starting a heavy local OCR pass.
        try:
            from ai_agent.runtime.gemini_ocr import gemini_ocr_image_bytes

            ocr_text = gemini_ocr_image_bytes(data, mime_type=mime_type, prompt=FINANCIAL_ATTACHMENT_OCR_PROMPT)
            if ocr_text and ocr_text.strip():
                return ocr_text.strip()
        except Exception:
            pass
        if allow_claude:   # a single image; PDF pages go to Claude as one document (_claude_pdf)
            try:
                from ai_agent.runtime import claude_ocr

                return claude_ocr.read_image(data, mime_type, FINANCIAL_ATTACHMENT_OCR_PROMPT)
            except Exception:
                frappe.log_error(frappe.get_traceback(), "AI Claude OCR")
        return ""

    def _ocr_pages_read(self, total):
        # Claude reads the whole document; page-by-page OCR stops at MAX_OCR_PDF_PAGES
        return total if getattr(self, "_read_by_claude", False) else min(total, MAX_OCR_PDF_PAGES)

    def _claude_pdf(self, file_doc, progress_run=None):
        """A scanned PDF read by Claude in one request, when Gemini OCR cannot be used
        (Managed mode or off) and an Anthropic provider is set up."""
        try:
            from ai_agent.runtime import claude_ocr
            from ai_agent.runtime.gemini_ocr import is_gemini_ocr_enabled

            if is_gemini_ocr_enabled() or not claude_ocr.available():
                return ""
            filename = file_doc.file_name or file_doc.name
            text = claude_ocr.read_pdf(get_file_path(file_doc.name), FINANCIAL_ATTACHMENT_OCR_PROMPT)
            self._read_by_claude = bool(text)
            return text
        except Exception:
            frappe.log_error(frappe.get_traceback(), "AI Claude OCR")
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
