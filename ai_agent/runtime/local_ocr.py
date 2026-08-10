# -*- coding: utf-8 -*-
"""Local PP-OCRv5 bridge used by Financial Attachment Session page reading.

Frappe stays on its normal Python environment. Every OCR call launches the
isolated Python 3.13 runtime, reads exactly one existing session page, returns
JSON in memory, and exits. This module does not split PDFs, create session page
rows, or save rendered/processed images.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import frappe
from frappe import _
from frappe.utils import cint
from frappe.utils.file_manager import get_file_path

RESULT_MARKER = "__AI_AGENT_OCR_RESULT__="
DEFAULT_DPI = 170
DEFAULT_MIN_CONFIDENCE = 0.70
DEFAULT_RETRY_THRESHOLD = 0.74
DEFAULT_PAGE_TIMEOUT = 300
PDF_TEXT_MIN_CHARS = 20


class LocalOCRExecutionError(RuntimeError):
    pass


def _app_package_path() -> Path:
    return Path(frappe.get_app_path("ai_agent")).resolve()


def _bench_path() -> Path:
    return _app_package_path().parents[2]


def _ocr_python() -> Path:
    configured = frappe.conf.get("ai_agent_ocr_python")
    if configured:
        return Path(configured).expanduser().resolve()
    return _bench_path() / ".venvs" / "ai_agent-ocr" / "bin" / "python"


def _ocr_worker() -> Path:
    configured = frappe.conf.get("ai_agent_ocr_worker")
    if configured:
        return Path(configured).expanduser().resolve()
    return _app_package_path() / "runtime" / "ocr_page_worker.py"


def _runtime_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "PYTHONUNBUFFERED": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "OMP_THREAD_LIMIT": "1",
        "PADDLE_NUM_THREADS": "1",
        "ONNXRUNTIME_THREAD_NUM": "1",
        "ORT_NUM_THREADS": "1",
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
    })
    return env


def _validate_runtime() -> tuple[Path, Path]:
    python_path = _ocr_python()
    worker_path = _ocr_worker()
    if not python_path.is_file():
        raise LocalOCRExecutionError(
            f"OCR Python environment was not found: {python_path}"
        )
    if not os.access(python_path, os.X_OK):
        raise LocalOCRExecutionError(f"OCR Python is not executable: {python_path}")
    if not worker_path.is_file():
        raise LocalOCRExecutionError(f"OCR worker was not found: {worker_path}")
    return python_path, worker_path


def _assert_background_ocr_context() -> None:
    if frappe.conf.get("ai_agent_allow_web_ocr"):
        return
    if getattr(frappe.local, "request", None):
        raise LocalOCRExecutionError(
            "Local OCR is disabled inside web requests. Use the background page-reading job."
        )


def _import_pymupdf():
    try:
        import pymupdf

        return pymupdf
    except ImportError:
        try:
            import fitz

            return fitz
        except ImportError as exc:
            raise LocalOCRExecutionError(f"PyMuPDF is not available: {exc}") from exc


def _pdf_text_payload(source_path: Path, page_number: int) -> dict[str, Any] | None:
    if source_path.suffix.lower() != ".pdf":
        return None
    started = time.monotonic()
    pymupdf = _import_pymupdf()
    document = pymupdf.open(str(source_path))
    try:
        page_count = int(document.page_count)
        if page_count < 1 or page_number < 1 or page_number > page_count:
            return None
        page = document.load_page(page_number - 1)
        try:
            text = str(page.get_text("text") or "").strip()
            real_chars = sum(1 for char in text if char.isalnum())
            if real_chars < PDF_TEXT_MIN_CHARS:
                return None
            rect = page.rect
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            blocks = [
                {
                    "text": line,
                    "confidence": 1.0,
                    "bbox": [0, 0, int(rect.width or 0), int(rect.height or 0)],
                }
                for line in lines
            ]
            return {
                "success": True,
                "source_file": str(source_path),
                "source_type": "pdf",
                "source_pages_count": page_count,
                "requested_page_number": page_number,
                "extraction_method": "pdf_text",
                "low_confidence_threshold": DEFAULT_MIN_CONFIDENCE,
                "low_confidence_blocks_count": 0,
                "low_confidence_blocks": [],
                "page": {
                    "page_no": page_number,
                    "width": int(rect.width or 0),
                    "height": int(rect.height or 0),
                    "average_confidence": 1.0,
                    "blocks": blocks,
                    "full_text": text,
                    "retry_used": False,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "error": None,
                },
            }
        finally:
            del page
    finally:
        document.close()


def _resource_limited_command(command: list[str]) -> list[str]:
    wrapped = list(command)
    ionice = shutil.which("ionice")
    if ionice:
        wrapped = [ionice, "-c", "3"] + wrapped
    nice = shutil.which("nice")
    if nice:
        wrapped = [nice, "-n", str(cint(frappe.conf.get("ai_agent_ocr_nice") or 10))] + wrapped
    return wrapped


def _parse_result(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    result_text = ""
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_MARKER):
            result_text = line[len(RESULT_MARKER):]
            break

    if not result_text:
        if completed.returncode in (-9, 137):
            raise LocalOCRExecutionError(
                "OCR was killed by the operating system. Check available RAM and swap."
            )
        raise LocalOCRExecutionError(
            "OCR worker returned no result marker.\n"
            f"Return code: {completed.returncode}\n"
            f"STDOUT tail:\n{stdout[-3000:]}\n"
            f"STDERR tail:\n{stderr[-5000:]}"
        )

    try:
        payload = json.loads(result_text)
    except json.JSONDecodeError as exc:
        raise LocalOCRExecutionError(
            f"OCR worker returned invalid JSON: {result_text[-3000:]}"
        ) from exc

    if completed.returncode != 0 or not payload.get("success"):
        raise LocalOCRExecutionError(
            payload.get("traceback")
            or payload.get("error")
            or stderr[-5000:]
            or f"OCR worker exited with code {completed.returncode}."
        )
    return payload


def _stop_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except Exception:
        try:
            process.terminate()
        except Exception:
            pass
    try:
        process.wait(timeout=5)
    except Exception:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def _run_ocr_subprocess(
    command: list[str],
    *,
    timeout: int,
    cancel_check: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=str(_bench_path()),
        env=_runtime_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    def stop_for_signal(signum, _frame):
        _stop_process_tree(process)
        raise LocalOCRExecutionError(f"OCR worker stopped by signal {signum}.")

    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)
    handlers_installed = False
    try:
        signal.signal(signal.SIGTERM, stop_for_signal)
        signal.signal(signal.SIGINT, stop_for_signal)
        handlers_installed = True
    except ValueError:
        handlers_installed = False
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=1)
                return subprocess.CompletedProcess(
                    command,
                    process.returncode,
                    stdout,
                    stderr,
                )
            except subprocess.TimeoutExpired:
                if cancel_check and cancel_check():
                    _stop_process_tree(process)
                    raise LocalOCRExecutionError(_("تم إيقاف OCR بناءً على طلب الإلغاء."))
                if timeout and (time.monotonic() - started) > timeout:
                    _stop_process_tree(process)
                    raise LocalOCRExecutionError(
                        f"OCR page exceeded {timeout} seconds."
                    )
    finally:
        if handlers_installed:
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)


def run_local_ocr_page(
    file_path: str | Path,
    page_number: int = 1,
    *,
    dpi: int = DEFAULT_DPI,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    retry_threshold: float = DEFAULT_RETRY_THRESHOLD,
    timeout: int | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run high-quality OCR for exactly one page/image and return in-memory JSON."""
    _assert_background_ocr_context()
    python_path, worker_path = _validate_runtime()
    source_path = Path(file_path).expanduser().resolve()
    if not source_path.is_file():
        raise LocalOCRExecutionError(f"OCR source file was not found: {source_path}")

    page_number = max(1, cint(page_number))
    pdf_text = _pdf_text_payload(source_path, page_number)
    if pdf_text:
        return pdf_text
    page_timeout = cint(
        timeout
        or frappe.conf.get("ai_agent_ocr_page_timeout")
        or DEFAULT_PAGE_TIMEOUT
    )
    command = _resource_limited_command([
        str(python_path),
        str(worker_path),
        "--input", str(source_path),
        "--page-number", str(page_number),
        "--engine", "onnxruntime",
        "--dpi", str(cint(dpi)),
        "--heavy-dpi", str(cint(frappe.conf.get("ai_agent_ocr_heavy_dpi") or 220)),
        "--min-confidence", str(float(min_confidence)),
        "--retry-threshold", str(float(retry_threshold)),
        "--mode", "adaptive",
    ])

    completed = _run_ocr_subprocess(
        command,
        timeout=page_timeout,
        cancel_check=cancel_check,
    )

    return _parse_result(completed)


def _image_suffix_from_bytes(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return ".tiff"
    return ".png"


def run_local_ocr_bytes(
    data: bytes,
    *,
    dpi: int = DEFAULT_DPI,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    retry_threshold: float = DEFAULT_RETRY_THRESHOLD,
    timeout: int | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> str:
    """Run local OCR for one in-memory image and return the extracted text."""
    if not data:
        return ""

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=_image_suffix_from_bytes(bytes(data[:16])),
            prefix="ai_agent_ocr_",
            delete=False,
        ) as temp_file:
            temp_file.write(data)
            temp_path = temp_file.name

        result = run_local_ocr_page(
            temp_path,
            page_number=1,
            dpi=dpi,
            min_confidence=min_confidence,
            retry_threshold=retry_threshold,
            timeout=timeout,
            cancel_check=cancel_check,
        )
        page = result.get("page") or {}
        return str(page.get("full_text") or "").strip()
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def extract_financial_attachment_page(file_doc, page_number: int, run_name: str = "") -> dict[str, Any]:
    """Return the page-result contract expected by purchase_invoice_ai.api.

    The Financial Attachment Session and its page rows must already exist.
    This function only reads the requested page and returns its extraction.
    """
    source_path = Path(get_file_path(file_doc.name)).resolve()
    def is_cancelled() -> bool:
        if not run_name:
            return False
        try:
            values = frappe.db.get_value("AI Run", run_name, ["status", "cancel_requested"], as_dict=True)
            return bool(
                values
                and (
                    values.get("cancel_requested")
                    or values.get("status") in {"Cancelled", "Timed Out"}
                )
            )
        except Exception:
            return False

    result = run_local_ocr_page(
        source_path,
        page_number=page_number,
        dpi=cint(frappe.conf.get("ai_agent_ocr_dpi") or DEFAULT_DPI),
        min_confidence=float(frappe.conf.get("ai_agent_ocr_min_confidence") or DEFAULT_MIN_CONFIDENCE),
        retry_threshold=float(frappe.conf.get("ai_agent_ocr_retry_threshold") or DEFAULT_RETRY_THRESHOLD),
        timeout=cint(frappe.conf.get("ai_agent_ocr_page_timeout") or DEFAULT_PAGE_TIMEOUT),
        cancel_check=is_cancelled,
    )
    page = result.get("page") or {}
    blocks = page.get("blocks") or []
    full_text = str(page.get("full_text") or "").strip()
    low_count = cint(result.get("low_confidence_blocks_count"))
    extraction_method = result.get("extraction_method") or "local_ocr_fast"

    warnings: list[str] = []
    if low_count:
        warnings.append(
            _("يوجد {0} جزء منخفض الثقة ويحتاج إلى مراجعة.").format(low_count)
        )
    if not full_text:
        warnings.append(_("لم يتم استخراج نص واضح من الصفحة."))

    return {
        "page": cint(page.get("page_no")) or max(1, cint(page_number)),
        "status": "read" if full_text else "failed",
        "can_reprocess": 1,
        "extraction": extraction_method,
        "text_preview": full_text,
        "rows": [],
        "totals": {},
        "warnings": warnings,
        "extracted_lines_count": len(blocks),
        "metadata": {
            "extraction_method": extraction_method,
            "ocr_engine": "pymupdf" if extraction_method == "pdf_text" else "onnxruntime",
            "ocr_pipeline": "PyMuPDF text" if extraction_method == "pdf_text" else "PP-OCR adaptive",
            "ocr_detection_model": result.get("text_detection_model") or "",
            "ocr_recognition_model": result.get("text_recognition_model") or "",
            "source_pages_count": cint(result.get("source_pages_count")),
            "page_no": cint(page.get("page_no")) or max(1, cint(page_number)),
            "width": cint(page.get("width")),
            "height": cint(page.get("height")),
            "average_confidence": float(page.get("average_confidence") or 0),
            "elapsed_seconds": float(page.get("elapsed_seconds") or 0),
            "retry_used": bool(page.get("retry_used")),
            "low_confidence_blocks_count": low_count,
            "blocks": blocks,
        },
    }
