import frappe
from frappe import _
from frappe.model.document import Document


class GeminiOCRSettings(Document):
    def validate(self):
        if self.enabled and not self.get_password("api_key"):
            frappe.throw(_("API Key is required when Gemini OCR is enabled."))
        if not self.model:
            self.model = "gemini-3.5-flash"
        if not self.base_url:
            self.base_url = "https://generativelanguage.googleapis.com/v1beta"
        if not self.request_timeout or self.request_timeout < 1:
            self.request_timeout = 25
        if not self.max_inline_image_bytes or self.max_inline_image_bytes < 1:
            self.max_inline_image_bytes = 8000000
        if not self.max_output_chars or self.max_output_chars < 1:
            self.max_output_chars = 8000


@frappe.whitelist()
def test_connection():
    # Spends the stored API key on an outbound call: settings managers only.
    frappe.only_for("System Manager")
    from ai_agent.runtime.gemini_ocr import test_gemini_ocr_connection

    return test_gemini_ocr_connection()
