import base64
from contextlib import contextmanager
import io
import json
import signal
import threading
import time
from urllib.parse import quote

import frappe
import requests
from frappe import _
from frappe.utils import cint


SETTINGS_DOCTYPE = "Gemini OCR Settings"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.5-flash"
DEFAULT_PROMPT = (
    "Read all visible Arabic and English text from this image/PDF page exactly as written. "
    "Preserve all visible numbers, currencies, dates, tax IDs, phone numbers, table rows, totals, and handwritten text accurately in clean Markdown. "
    "Do NOT analyze, make accounting decisions, or infer missing values. "
    "Simply extract and format the visible text from the image accurately in clean Markdown without code fences."
)


class GeminiOCRError(Exception):
    pass


@contextmanager
def _request_deadline(timeout):
    """Apply a real wall-clock deadline around requests in worker processes."""
    timeout = max(cint(timeout), 1)
    if (
        not hasattr(signal, "setitimer")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_delay, previous_interval = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def deadline_exceeded(_signum, _frame):
        raise requests.Timeout(f"Gemini OCR request exceeded {timeout}s")

    signal.signal(signal.SIGALRM, deadline_exceeded)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_delay > 0:
            remaining = max(previous_delay - (time.monotonic() - started), 0.001)
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_interval)


class GeminiOCRClient:
    def __init__(self, settings=None):
        self.settings = settings or _get_settings()
        self.base_url = (self.settings.base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = self.settings.model or DEFAULT_MODEL
        self.api_key = self.settings.get_password("api_key")
        self.timeout = cint(self.settings.request_timeout) or 60
        self.max_inline_image_bytes = cint(self.settings.max_inline_image_bytes) or 8000000
        self.max_output_chars = cint(self.settings.max_output_chars) or 4000
        self.prompt = self.settings.prompt or DEFAULT_PROMPT

    def extract_text_from_image(self, data, mime_type="image/png", prompt=None):
        if not self.api_key:
            raise GeminiOCRError(_("Gemini OCR API Key is not configured."))
        if not data:
            return ""

        # Optimize image size & resolution for fast OCR processing before Base64 encoding
        data, mime_type = _optimize_image_for_ocr(data, mime_type)

        if len(data) > self.max_inline_image_bytes:
            raise GeminiOCRError(_("Image is larger than Gemini OCR inline limit."))

        prompt_text = prompt or self.prompt
        encoded_image = base64.b64encode(data).decode("ascii")
        response = self._post(
            f"{self.base_url}/models/{quote(self.model, safe='')}:generateContent",
            {
                "contents": [{
                    "role": "user",
                    "parts": [
                        {"text": prompt_text},
                        {
                            "inline_data": {
                                "mime_type": mime_type or "image/png",
                                "data": encoded_image,
                            },
                        },
                    ],
                }],
                "generationConfig": {
                    "response_mime_type": "text/plain",
                    "maxOutputTokens": min(max(self.max_output_chars, 256), 8192),
                },
            },
        )
        text = _extract_output_text(response.json())
        if not text:
            raise GeminiOCRError(_("Gemini OCR returned an empty response."))
        return _clean_ocr_text(text)[:self.max_output_chars]

    def _post(self, url, payload):
        max_attempts = 3
        last_exception = None
        for attempt in range(1, max_attempts + 1):
            try:
                with _request_deadline(self.timeout):
                    response = requests.post(
                        url,
                        headers={
                            "x-goog-api-key": self.api_key,
                            "Content-Type": "application/json",
                        },
                        data=json.dumps(payload),
                        timeout=(min(self.timeout, 10), self.timeout),
                    )
                if response.status_code in (500, 502, 503, 504, 429) and attempt < max_attempts:
                    import time
                    time.sleep(attempt * 1.5)
                    continue
                if response.status_code >= 400:
                    raise GeminiOCRError(_format_error(response))
                return response
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exception = exc
                if attempt < max_attempts:
                    import time
                    time.sleep(attempt * 2)
                    continue
                raise GeminiOCRError(_("Gemini OCR request timed out after {0} attempts (timeout={1}s). Check internet connection.").format(max_attempts, self.timeout)) from exc
            except requests.RequestException as exc:
                raise GeminiOCRError(_("Gemini OCR request failed: {0}").format(str(exc))) from exc
        if last_exception:
            raise GeminiOCRError(_("Gemini OCR request failed: {0}").format(str(last_exception))) from last_exception


def _cloud_ocr_allowed():
    """Gemini is a third-party provider: Own Key mode with consent only."""
    from ai_agent.runtime import policy

    return policy.provider_allowed("Gemini OCR")


def _uses_cloud_engine(settings):
    return (getattr(settings, "ocr_engine", None) or "Cloud API (Gemini OCR)") != "Local Engine (PaddleOCR / PyMuPDF)"


def is_gemini_ocr_enabled():
    settings = _get_settings_or_none()
    if not settings or not settings.enabled:
        return False
    return not _uses_cloud_engine(settings) or _cloud_ocr_allowed()


def gemini_ocr_image_bytes(data, mime_type="image/png", prompt=None):
    settings = _get_settings_or_none()
    if not settings or not settings.enabled:
        return ""

    ocr_engine = getattr(settings, "ocr_engine", None) or "Cloud API (Gemini OCR)"
    if _uses_cloud_engine(settings) and not _cloud_ocr_allowed():
        _set_last_error("Gemini OCR is disabled by the provider policy (Managed mode or no third-party consent).")
        return ""

    if ocr_engine == "Local Engine (PaddleOCR / PyMuPDF)":
        if not _paddle_runtime_installed():
            # The heavy PaddleOCR environment is optional; RapidOCR runs the same
            # models on onnxruntime in-process and is what most servers use.
            return _rapid_ocr(data)
        try:
            from ai_agent.runtime.local_ocr import run_local_ocr_bytes
            # Same downscale/compress pass the cloud path gets: raw high-res
            # chat attachments made onnxruntime request >500MB buffers and die
            # under the worker memory cap.
            data, mime_type = _optimize_image_for_ocr(data, mime_type)
            return run_local_ocr_bytes(data)
        except Exception as exc:
            _set_last_error(str(exc))
            frappe.log_error(frappe.get_traceback(), "Local OCR Engine Failed")
            return _rapid_ocr(data)

    try:
        _set_last_error("")
        text = GeminiOCRClient(settings).extract_text_from_image(data, mime_type=mime_type, prompt=prompt)
        if text and text.strip():
            return text
    except Exception as exc:
        _set_last_error(str(exc))
        frappe.log_error(frappe.get_traceback(), "Gemini OCR Failed")
    # Cloud failed (no credits, network, quota): a light local read beats nothing.
    return _rapid_ocr(data)


def _paddle_runtime_installed():
    try:
        from ai_agent.runtime.local_ocr import _validate_runtime

        _validate_runtime()
        return True
    except Exception:
        return False


def _rapid_ocr(data):
    from ai_agent.runtime import rapid_ocr

    if not rapid_ocr.available():
        _set_last_error((get_last_gemini_ocr_error() + " | " if get_last_gemini_ocr_error() else "") + "No local OCR engine is installed (rapidocr).")
        return ""
    text = rapid_ocr.ocr_text(data)
    if not text:
        _set_last_error("Local OCR (RapidOCR) found no text in the image.")
    return text


def get_last_gemini_ocr_error():
    return getattr(frappe.local, "gemini_ocr_last_error", "")


def test_gemini_ocr_connection():
    settings = _get_settings()
    if not settings.enabled:
        return {"ok": False, "message": _("OCR is disabled. Enable it before testing.")}

    ocr_engine = getattr(settings, "ocr_engine", None) or "Cloud API (Gemini OCR)"
    if ocr_engine == "Local Engine (PaddleOCR / PyMuPDF)":
        try:
            from ai_agent.runtime.local_ocr import run_local_ocr_bytes
            test_img = _build_test_image()
            text = run_local_ocr_bytes(test_img)
            return {
                "ok": True,
                "message": _("Local OCR Engine (PaddleOCR) tested successfully. Text extracted: {0}").format((text or "OK")[:120]),
            }
        except Exception as exc:
            return {"ok": False, "message": _("Local OCR Engine test failed: {0}").format(str(exc))}

    if not settings.get_password("api_key"):
        return {"ok": False, "message": _("Gemini API Key is missing.")}

    try:
        text = GeminiOCRClient(settings).extract_text_from_image(_build_test_image(), mime_type="image/png")
    except GeminiOCRError as exc:
        frappe.log_error(frappe.get_traceback(), "Gemini OCR Test Failed")
        return {
            "ok": False,
            "message": _("Gemini OCR test failed: {0}").format(str(exc)),
        }
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "Gemini OCR Test Failed")
        return {
            "ok": False,
            "message": _("Gemini OCR test failed unexpectedly: {0}").format(str(exc)),
        }

    preview = (text or "").strip()
    if not preview:
        return {
            "ok": False,
            "message": _("Gemini OCR connected, but returned no text for the test image."),
        }

    return {
        "ok": True,
        "message": _("Connected to Gemini OCR. Test text extracted: {0}").format(preview[:120]),
    }


def _build_test_image():
    try:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (640, 240), "white")
        draw = ImageDraw.Draw(image)
        lines = [
            "Gemini OCR connection test",
            "Invoice No: OCR-12345",
            "Total: 678.90 SAR",
        ]
        y = 40
        for line in lines:
            draw.text((40, y), line, fill="black")
            y += 58
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        return base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAoAAAAEwCAIAAAD9nS1aAAAE5klEQVR4nO3ZwQ3CMBAFQeUO+9+Z6BWpCqzYhFQm2ZgJwE9mIu+UAAAAAAAAAAAAAAAAAAAAAgH8d2wEAAABgK2YBAAAAYCsGAAAAwFYMAGDr3mP3b7e9d9c7XgAAAPyABQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsxcfr1n6/7wMAAIDfBAwAAACwFQMAAABsBQMAAABgKwYAAADAVgwAAACArRgAAAAAWzEAAAAAtmIAAAAAbMUAAAAA2IoBAAAAsBUDAAAAYCsGAAAAwFYMAGD7A0iiBOT6Xm2uAAAAAElFTkSuQmCC"
        )


def _get_settings_or_none():
    if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
        return None
    if not frappe.db.exists(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE):
        return None
    return frappe.get_doc(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)


def _get_settings():
    settings = _get_settings_or_none()
    if not settings:
        frappe.throw(_("{0} is not configured. Run migrate or open the settings once.").format(SETTINGS_DOCTYPE))
    return settings


def _extract_output_text(payload):
    if isinstance(payload, dict):
        if payload.get("output_text"):
            return payload.get("output_text") or ""
        candidate_parts = []
        for candidate in payload.get("candidates") or []:
            content = candidate.get("content") or {}
            for part in content.get("parts") or []:
                if part.get("text"):
                    candidate_parts.append(part.get("text"))
        if candidate_parts:
            return "\n".join(candidate_parts)
        parts = []
        for step in payload.get("steps") or []:
            for item in step.get("content") or []:
                if item.get("type") == "text" and item.get("text"):
                    parts.append(item.get("text"))
        if parts:
            return "\n".join(parts)
        output = payload.get("output")
        if isinstance(output, str):
            return output
        if isinstance(output, list):
            recursive_parts = [_recursive_text(item) for item in output]
            return "\n".join(part for part in recursive_parts if part)
    return ""


def _recursive_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value for part in [_recursive_text(item)] if part)
    if isinstance(value, dict):
        if value.get("type") == "text" and value.get("text"):
            return value.get("text")
        if value.get("text") and len(value) <= 3:
            return value.get("text")
        return "\n".join(part for item in value.values() for part in [_recursive_text(item)] if part)
    return ""


def _clean_ocr_text(text):
    return "\n".join(line.rstrip() for line in str(text or "").splitlines() if line.strip()).strip()


def _format_error(response):
    try:
        payload = response.json()
        message = payload.get("error", {}).get("message") or payload.get("message")
    except Exception:
        message = response.text[:500]
    if message:
        return _("Gemini OCR request failed: {0}").format(message)
    return _("Gemini OCR request failed: {0} {1}").format(response.status_code, message[:500])


def _optimize_image_for_ocr(data, mime_type="image/png"):
    """Compress and downscale oversized images to drastically reduce OCR API payload and latency."""
    if not data or len(data) < 350000:
        return data, mime_type
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        max_dim = 2048
        if img.width > max_dim or img.height > max_dim:
            img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=85, optimize=True)
        compressed = buffer.getvalue()
        if len(compressed) < len(data):
            return compressed, "image/jpeg"
    except Exception:
        pass
    return data, mime_type


def _set_last_error(message):
    frappe.local.gemini_ocr_last_error = message or ""
