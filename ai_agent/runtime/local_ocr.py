# -*- coding: utf-8 -*-
"""Local PP-OCRv5 bridge used by chat attachment OCR (gemini_ocr, engine "Local Engine").

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

RESULT_MARKER = "__AI_AGENT_OCR_RESULT__="
DEFAULT_DPI = 170
DEFAULT_MIN_CONFIDENCE = 0.70
DEFAULT_RETRY_THRESHOLD = 0.74
DEFAULT_PAGE_TIMEOUT = 300
PDF_TEXT_MIN_CHARS = 20
# Memory containment (site_config keys):
#   ai_agent_ocr_memory_mb        hard RLIMIT_AS cap on the worker (default 2048)
#   ai_agent_ocr_min_available_mb required MemAvailable before launching (default 1536)
#   ai_agent_ocr_memory_wait_seconds  how long to wait for memory (default 60)
#   ai_agent_ocr_det_limit        detector side limit passed to the worker (default 1600)
#   ai_agent_ocr_allow_heavy_retry  auto (skip heavy under a <3GB cap) | 1 | 0
DEFAULT_WORKER_MEMORY_MB = 2048
DEFAULT_MIN_AVAILABLE_MB = 1536
DEFAULT_MEMORY_WAIT_SECONDS = 60


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
    cpu_threads = str(max(1, cint(frappe.conf.get("ai_agent_ocr_cpu_threads") or 1)))
    env.update({
        "PYTHONUNBUFFERED": "1",
        "AI_AGENT_OCR_CPU_THREADS": cpu_threads,
        "AI_AGENT_OCR_DET_LIMIT": str(cint(frappe.conf.get("ai_agent_ocr_det_limit") or 1600)),
        "AI_AGENT_OCR_MEMORY_MB": str(_worker_memory_mb()),
        "AI_AGENT_OCR_ALLOW_HEAVY": str(frappe.conf.get("ai_agent_ocr_allow_heavy_retry", "auto")),
        "OMP_NUM_THREADS": cpu_threads,
        "MKL_NUM_THREADS": cpu_threads,
        "OPENBLAS_NUM_THREADS": cpu_threads,
        "NUMEXPR_NUM_THREADS": cpu_threads,
        "OMP_THREAD_LIMIT": cpu_threads,
        "PADDLE_NUM_THREADS": cpu_threads,
        "ONNXRUNTIME_THREAD_NUM": cpu_threads,
        "ORT_NUM_THREADS": cpu_threads,
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
    taskset = shutil.which("taskset")
    if taskset:
        configured_affinity = str(frappe.conf.get("ai_agent_ocr_cpu_affinity") or "").strip()
        cpu_count = max(1, os.cpu_count() or 1)
        affinity = configured_affinity or str(cpu_count - 1)
        wrapped = [taskset, "-c", affinity] + wrapped
    flock = shutil.which("flock")
    if flock:
        lock_file = str(
            frappe.conf.get("ai_agent_ocr_lock_file")
            or "/tmp/ai_agent_local_ocr.lock"
        )
        wrapped = [flock, "--exclusive", lock_file] + wrapped
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
                "OCR was killed by the operating system (OOM). The worker is capped at "
                f"{_worker_memory_mb()}MB (ai_agent_ocr_memory_mb); the page failed but the site stayed up."
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
    # Best-effort cleanup: each step may fail because the process already exited.
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


def _worker_memory_mb() -> int:
    return max(512, cint(frappe.conf.get("ai_agent_ocr_memory_mb") or DEFAULT_WORKER_MEMORY_MB))


def _memory_cap_preexec() -> Callable[[], None]:
    """Hard address-space cap so a hungry OCR worker dies alone instead of
    pushing the whole server into swap thrashing."""
    import resource

    limit_bytes = _worker_memory_mb() * 1024 * 1024

    def apply_limit():
        try:
            resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
        except (ValueError, OSError):
            pass

    return apply_limit


def _available_memory_mb() -> int:
    try:
        with open("/proc/meminfo") as meminfo:
            for line in meminfo:
                if line.startswith("MemAvailable:"):
                    return cint(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def _wait_for_available_memory() -> None:
    """Refuse to launch OCR while the box is memory-starved: a failed page is
    recoverable; a swap-thrashing server takes the whole site down."""
    required = max(256, cint(frappe.conf.get("ai_agent_ocr_min_available_mb") or DEFAULT_MIN_AVAILABLE_MB))
    configured_wait = frappe.conf.get("ai_agent_ocr_memory_wait_seconds")
    # `or` would turn an explicit 0 (fail immediately) back into the default.
    wait_seconds = cint(configured_wait) if configured_wait is not None else DEFAULT_MEMORY_WAIT_SECONDS
    deadline = time.monotonic() + max(0, wait_seconds)
    while True:
        available = _available_memory_mb()
        if not available or available >= required:
            return
        if time.monotonic() >= deadline:
            raise LocalOCRExecutionError(
                _(
                    "The server has {0}MB of free memory, less than the {1}MB local OCR needs. "
                    "Page reading was stopped so the site is not affected; try again later or lower ai_agent_ocr_min_available_mb."
                ).format(available, required)
            )
        time.sleep(3)


def _run_ocr_subprocess(
    command: list[str],
    *,
    timeout: int,
    cancel_check: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess[str]:
    _wait_for_available_memory()
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=str(_bench_path()),
        env=_runtime_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        preexec_fn=_memory_cap_preexec(),
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
                    raise LocalOCRExecutionError(_("OCR stopped as requested."))
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


