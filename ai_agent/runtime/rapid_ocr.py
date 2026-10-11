"""Light local OCR with RapidOCR (PaddleOCR models on onnxruntime, no Paddle).

Free, in-process, ~230 MB peak and a few seconds per page on CPU. Reads
printed Arabic and English well (rotated scans included); handwriting comes
out with low confidence, so every line under LOW_CONFIDENCE is marked "[?]"
and the model is told to confirm those fields with the user instead of
trusting them.

Tested on a rotated, partly handwritten Arabic receipt voucher: printed lines
scored 0.74–1.00, handwritten gibberish 0.50–0.65.
"""

from __future__ import annotations

import gc
import threading

import frappe

LOW_CONFIDENCE = 0.70
MAX_SIDE = 2200               # downscale very large photos before OCR
_lock = threading.Lock()      # one OCR at a time per process: bounds memory


def available():
    try:
        import rapidocr  # noqa: F401

        return True
    except Exception:
        return False


def _engine():
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

    # Arabic recognition model also covers Latin letters and digits.
    return RapidOCR(params={
        "Rec.lang_type": LangRec.ARABIC,
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.model_type": ModelType.MOBILE,
        "Global.log_level": "error",
    })


def _decode(data):
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    scale = MAX_SIDE / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return img


def ocr_bytes(data):
    """-> {"text": str, "lines": int, "uncertain": int} (text empty on failure)."""
    if not data:
        return {"text": "", "lines": 0, "uncertain": 0}
    img = _decode(data)
    if img is None:
        return {"text": "", "lines": 0, "uncertain": 0}
    with _lock:
        engine = _engine()
        try:
            result = engine(img)
        finally:
            del engine
            gc.collect()
    txts = list(getattr(result, "txts", None) or [])
    scores = list(getattr(result, "scores", None) or [1.0] * len(txts))
    lines, uncertain = [], 0
    for text, score in zip(txts, scores):
        text = " ".join(str(text).split())
        if not text:
            continue
        if score < LOW_CONFIDENCE:
            uncertain += 1
            lines.append(f"[?] {text}")
        else:
            lines.append(text)
    return {"text": "\n".join(lines), "lines": len(lines), "uncertain": uncertain}


def ocr_text(data):
    """Plain text for callers that only take a string, with a note when some
    lines are uncertain (typically handwriting)."""
    try:
        out = ocr_bytes(data)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "RapidOCR failed")
        return ""
    text = out["text"]
    if text and out["uncertain"]:
        text += (
            f"\n\n[OCR note: {out['uncertain']} of {out['lines']} lines are marked [?] — low confidence, usually handwriting. "
            "Do not rely on them: tell the user which fields you could not read (for example party, amount, date) and ask them to type those values.]"
        )
    return text


def uncertain_ratio(text):
    lines = [line for line in (text or "").splitlines() if line.strip() and not line.startswith("[OCR note")]
    if not lines:
        return 1.0
    return sum(1 for line in lines if line.startswith("[?]")) / len(lines)
