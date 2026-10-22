"""Light local OCR (RapidOCR): routing from the configured engine and the
marking of low-confidence (usually handwritten) lines."""

from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from ai_agent.runtime import gemini_ocr, rapid_ocr


class TestRapidOcrText(IntegrationTestCase):
    def test_uncertain_lines_are_marked_and_explained(self):
        with patch.object(rapid_ocr, "ocr_bytes", return_value={"text": "Receipt Voucher\n[?] كلان الاف", "lines": 2, "uncertain": 1}):
            text = rapid_ocr.ocr_text(b"x")
        self.assertIn("[?] كلان الاف", text)
        self.assertIn("ask them to type those values", text)
        self.assertEqual(rapid_ocr.uncertain_ratio(text), 0.5)

    def test_clean_read_has_no_note(self):
        with patch.object(rapid_ocr, "ocr_bytes", return_value={"text": "No.12743", "lines": 1, "uncertain": 0}):
            self.assertEqual(rapid_ocr.ocr_text(b"x"), "No.12743")


class TestOcrRouting(IntegrationTestCase):
    def setUp(self):
        # routing only: the optional rapidocr package (pyproject extra "ocr") need not be installed
        installed = patch.object(rapid_ocr, "available", return_value=True)
        installed.start()
        self.addCleanup(installed.stop)

    def _settings(self, engine):
        import frappe

        return frappe._dict(enabled=1, ocr_engine=engine, fallback_to_tesseract=0)

    def test_local_engine_without_paddle_uses_rapidocr(self):
        with patch.object(gemini_ocr, "_get_settings_or_none", return_value=self._settings("Local Engine (PaddleOCR / PyMuPDF)")), \
             patch.object(gemini_ocr, "_paddle_runtime_installed", return_value=False), \
             patch.object(rapid_ocr, "ocr_text", return_value="RAPID") as rapid:
            self.assertEqual(gemini_ocr.gemini_ocr_image_bytes(b"img"), "RAPID")
            rapid.assert_called_once()

    def test_cloud_failure_falls_back_to_rapidocr(self):
        class Boom:
            def __init__(self, *_):
                pass

            def extract_text_from_image(self, *a, **k):
                raise Exception("credits are depleted")

        with patch.object(gemini_ocr, "_get_settings_or_none", return_value=self._settings("Cloud API (Gemini OCR)")), \
             patch.object(gemini_ocr, "_cloud_ocr_allowed", return_value=True), \
             patch.object(gemini_ocr, "GeminiOCRClient", Boom), \
             patch.object(rapid_ocr, "ocr_text", return_value="RAPID"):
            self.assertEqual(gemini_ocr.gemini_ocr_image_bytes(b"img"), "RAPID")

    def test_real_engine_reads_printed_text(self):
        """One real pass on a rendered image (skipped when rapidocr is absent)."""
        import importlib.util

        # not rapid_ocr.available(): setUp patches it for the routing tests
        if not importlib.util.find_spec("rapidocr") or not importlib.util.find_spec("cv2"):
            self.skipTest("rapidocr not installed")
        import cv2
        import numpy as np

        img = np.full((120, 700, 3), 255, dtype=np.uint8)
        cv2.putText(img, "Receipt Voucher 12743", (20, 75), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 0, 0), 3)
        ok, png = cv2.imencode(".png", img)
        out = rapid_ocr.ocr_bytes(png.tobytes())
        self.assertIn("12743", out["text"].replace(" ", ""))
