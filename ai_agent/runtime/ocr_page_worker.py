#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Standalone one-page OCR worker for the dedicated Python 3.13 environment.

The worker never imports Frappe and never writes rendered pages or OCR result
files. For a PDF it opens and renders only ``--page-number``. The result is
returned as one JSON line on stdout. All logs go to stderr.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("OMP_THREAD_LIMIT", "1")
os.environ.setdefault("PADDLE_NUM_THREADS", "1")
os.environ.setdefault("ONNXRUNTIME_THREAD_NUM", "1")
os.environ.setdefault("ORT_NUM_THREADS", "1")
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

SUPPORTED_IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
SUPPORTED_INPUTS = SUPPORTED_IMAGES | {".pdf"}
RESULT_MARKER = "__AI_AGENT_OCR_RESULT__="
FAST_DET_MODEL = "PP-OCRv6_small_det"
HEAVY_DET_MODEL = "PP-OCRv5_server_det"
ARABIC_REC_MODEL = "arabic_PP-OCRv5_mobile_rec"
WEAK_AVERAGE_CONFIDENCE = 0.84
WEAK_LOW_CONFIDENCE = 0.70
WEAK_LOW_CONFIDENCE_RATIO = 0.20
WEAK_MIN_BLOCKS = 5


@dataclass
class OCRBlock:
    text: str
    confidence: float
    bbox: list[int]


@dataclass
class PageResult:
    page_no: int
    width: int
    height: int
    average_confidence: float
    blocks: list[OCRBlock]
    full_text: str
    retry_used: bool
    elapsed_seconds: float = 0.0
    error: str | None = None


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def emit(payload: dict[str, Any]) -> None:
    print(
        RESULT_MARKER + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        flush=True,
    )


def import_inspection_dependency():
    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError(f"Missing inspection dependency: {exc}") from exc
    return pymupdf


def import_ocr_dependencies():
    try:
        import cv2
        import numpy as np
        import pymupdf
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise RuntimeError(f"Missing OCR dependency: {exc}") from exc

    return np, cv2, pymupdf, PaddleOCR


def inspect_source(input_path: Path) -> dict[str, Any]:
    if not input_path.is_file():
        raise FileNotFoundError(f"Input file does not exist: {input_path}")

    suffix = input_path.suffix.lower()
    if suffix not in SUPPORTED_INPUTS:
        raise ValueError(
            f"Unsupported file type: {suffix}. "
            f"Supported: {', '.join(sorted(SUPPORTED_INPUTS))}"
        )

    if suffix != ".pdf":
        return {
            "success": True,
            "source_file": str(input_path),
            "source_type": "image",
            "page_count": 1,
        }

    pymupdf = import_inspection_dependency()
    document = pymupdf.open(str(input_path))
    try:
        page_count = int(document.page_count)
    finally:
        document.close()

    if page_count < 1:
        raise ValueError("PDF has no pages")

    return {
        "success": True,
        "source_file": str(input_path),
        "source_type": "pdf",
        "page_count": page_count,
    }


def read_image(path: Path, cv2, np):
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Cannot read image: {path}")
    return image


def pixmap_to_bgr(pixmap, cv2, np):
    samples = np.frombuffer(pixmap.samples, dtype=np.uint8)
    image = samples.reshape(pixmap.height, pixmap.width, pixmap.n)

    if pixmap.n == 4:
        return cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)
    if pixmap.n == 3:
        return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    if pixmap.n == 1:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)

    raise ValueError(f"Unsupported Pixmap channel count: {pixmap.n}")


def normalize_image_size(image, cv2, max_side: int = 2600, min_side: int = 1300):
    height, width = image.shape[:2]
    longest = max(width, height)
    shortest = min(width, height)

    scale = 1.0
    if longest > max_side:
        scale = max_side / longest
    elif shortest < min_side:
        scale = min(2.0, min_side / max(shortest, 1))

    if abs(scale - 1.0) < 0.01:
        return image

    interpolation = cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA
    return cv2.resize(
        image,
        (max(1, int(width * scale)), max(1, int(height * scale))),
        interpolation=interpolation,
    )


def enhance_for_ocr(image, cv2):
    """Same high-quality preprocessing used by the successful Windows test."""
    normalized = normalize_image_size(image, cv2)
    gray = cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY)
    denoised = cv2.fastNlMeansDenoising(
        gray,
        None,
        h=7,
        templateWindowSize=7,
        searchWindowSize=21,
    )
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(denoised)
    return cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)


def threshold_variant(image, cv2):
    normalized = normalize_image_size(image, cv2)
    gray = cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    binary = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        41,
        13,
    )
    return cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)


def result_to_dict(result: Any) -> dict[str, Any]:
    candidates: list[Any] = []
    for attr_name in ("json", "to_dict", "dict"):
        attr = getattr(result, attr_name, None)
        if attr is not None:
            try:
                candidates.append(attr() if callable(attr) else attr)
            except Exception:
                pass

    for candidate in candidates:
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except json.JSONDecodeError:
                continue
        if isinstance(candidate, dict):
            return candidate

    try:
        return dict(result)
    except Exception as exc:
        raise TypeError("Unsupported PaddleOCR result object") from exc


def polygon_to_bbox(poly: Any) -> list[int]:
    if poly is None:
        return [0, 0, 0, 0]

    try:
        points = list(poly)
    except TypeError:
        return [0, 0, 0, 0]

    if len(points) == 4 and all(isinstance(value, (int, float)) for value in points):
        x0, y0, x1, y1 = points
        return [int(x0), int(y0), int(x1), int(y1)]

    xs: list[float] = []
    ys: list[float] = []
    for point in points:
        try:
            xs.append(float(point[0]))
            ys.append(float(point[1]))
        except Exception:
            continue

    if not xs or not ys:
        return [0, 0, 0, 0]

    return [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]


def extract_blocks_from_prediction(prediction: Iterable[Any]) -> list[OCRBlock]:
    blocks: list[OCRBlock] = []
    for result in prediction:
        payload = result_to_dict(result)
        data = payload.get("res", payload)

        texts = list(data.get("rec_texts") or [])
        scores = list(data.get("rec_scores") or [])
        boxes = data.get("rec_boxes")
        polygons = data.get("rec_polys")
        geometry = boxes if boxes is not None else polygons
        geometry_list = list(geometry) if geometry is not None else []

        for index, raw_text in enumerate(texts):
            text = str(raw_text or "").strip()
            if not text:
                continue

            try:
                score = float(scores[index]) if index < len(scores) else 0.0
            except Exception:
                score = 0.0

            poly = geometry_list[index] if index < len(geometry_list) else None
            blocks.append(
                OCRBlock(
                    text=text,
                    confidence=round(score, 6),
                    bbox=polygon_to_bbox(poly),
                )
            )

    return sort_blocks_arabic(blocks)


def sort_blocks_arabic(blocks: list[OCRBlock]) -> list[OCRBlock]:
    def sort_key(block: OCRBlock):
        x0, y0, x1, y1 = block.bbox
        y_center = (y0 + y1) / 2
        x_center = (x0 + x1) / 2
        return (round(y_center / 24), -x_center)

    return sorted(blocks, key=sort_key)


def average_confidence(blocks: list[OCRBlock]) -> float:
    if not blocks:
        return 0.0
    return round(sum(block.confidence for block in blocks) / len(blocks), 6)


def blocks_to_text(blocks: list[OCRBlock]) -> str:
    return "\n".join(block.text for block in blocks)


def choose_better_result(first: list[OCRBlock], second: list[OCRBlock]) -> list[OCRBlock]:
    def quality(blocks: list[OCRBlock]) -> float:
        if not blocks:
            return 0.0
        chars = sum(len(block.text) for block in blocks)
        return average_confidence(blocks) * 1000 + min(chars, 5000) / 10

    return second if quality(second) > quality(first) else first


def scale_bbox_to_original(
    bbox: list[int],
    *,
    original_width: int,
    original_height: int,
    processed_width: int,
    processed_height: int,
) -> list[int]:
    if processed_width <= 0 or processed_height <= 0:
        return [0, 0, 0, 0]

    x_scale = original_width / processed_width
    y_scale = original_height / processed_height
    x0, y0, x1, y1 = bbox

    scaled = [
        round(x0 * x_scale),
        round(y0 * y_scale),
        round(x1 * x_scale),
        round(y1 * y_scale),
    ]
    scaled[0] = max(0, min(original_width, scaled[0]))
    scaled[1] = max(0, min(original_height, scaled[1]))
    scaled[2] = max(scaled[0], min(original_width, scaled[2]))
    scaled[3] = max(scaled[1], min(original_height, scaled[3]))
    return scaled


def scale_blocks_to_original(
    blocks: list[OCRBlock],
    *,
    original_width: int,
    original_height: int,
    processed_width: int,
    processed_height: int,
) -> list[OCRBlock]:
    return sort_blocks_arabic(
        [
            OCRBlock(
                text=block.text,
                confidence=block.confidence,
                bbox=scale_bbox_to_original(
                    block.bbox,
                    original_width=original_width,
                    original_height=original_height,
                    processed_width=processed_width,
                    processed_height=processed_height,
                ),
            )
            for block in blocks
        ]
    )


def heavy_retry_allowed() -> bool:
    """The heavy profile (server_det + orientation models) needs roughly 3GB;
    under a tighter worker memory cap it cannot load and only kills the page."""
    flag = os.environ.get("AI_AGENT_OCR_ALLOW_HEAVY", "auto").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on", "force"):
        return True
    try:
        cap_mb = int(os.environ.get("AI_AGENT_OCR_MEMORY_MB", "0"))
    except ValueError:
        cap_mb = 0
    return cap_mb == 0 or cap_mb >= 3072


def create_ocr(PaddleOCR, engine: str = "onnxruntime", profile: str = "fast"):
    is_heavy = profile == "heavy"
    cpu_threads = max(1, int(os.environ.get("AI_AGENT_OCR_CPU_THREADS", "1")))
    kwargs: dict[str, Any] = {
        "lang": "ar",
        "ocr_version": "PP-OCRv5",
        "text_detection_model_name": HEAVY_DET_MODEL if is_heavy else FAST_DET_MODEL,
        "text_recognition_model_name": ARABIC_REC_MODEL,
        "use_doc_orientation_classify": is_heavy,
        "use_doc_unwarping": False,
        "use_textline_orientation": is_heavy,
        "text_recognition_batch_size": 1,
        "cpu_threads": cpu_threads,
        # Cap the detector input on BOTH profiles: the heavy server_det had no
        # limit and requested >500MB buffers on small servers.
        "text_det_limit_side_len": max(
            640, int(os.environ.get("AI_AGENT_OCR_DET_LIMIT", "1600"))
        ),
        "text_det_limit_type": "max",
    }
    if not is_heavy:
        kwargs.update({
            "textline_orientation_batch_size": 1,
        })
    if engine:
        kwargs["engine"] = engine
    if engine == "onnxruntime":
        kwargs["engine_config"] = {
            "device_type": "cpu",
            "intra_op_num_threads": cpu_threads,
            "inter_op_num_threads": 1,
            "execution_mode": "sequential",
            "enable_mem_pattern": False,
        }

    log(f"[INFO] Loading PaddleOCR Arabic model ({profile})...")
    return PaddleOCR(**kwargs)


def is_weak_result(blocks: list[OCRBlock], min_confidence: float = WEAK_LOW_CONFIDENCE) -> bool:
    if not blocks:
        return True
    low_count = sum(1 for block in blocks if block.confidence < min_confidence)
    low_ratio = low_count / max(len(blocks), 1)
    text = blocks_to_text(blocks).strip()
    return (
        average_confidence(blocks) < WEAK_AVERAGE_CONFIDENCE
        or not text
        or len(blocks) < WEAK_MIN_BLOCKS
        or low_ratio > WEAK_LOW_CONFIDENCE_RATIO
    )


def prepare_image_for_profile(image, cv2, profile: str):
    if profile == "heavy":
        return enhance_for_ocr(image, cv2)
    return normalize_image_size(image, cv2, max_side=2000, min_side=900)


def load_one_page_or_image(
    input_path: Path,
    page_number: int,
    *,
    np,
    cv2,
    pymupdf,
    dpi: int,
):
    suffix = input_path.suffix.lower()
    if suffix != ".pdf":
        if page_number != 1:
            raise ValueError("Image sources only support page number 1")
        return read_image(input_path, cv2, np), 1, 1

    document = pymupdf.open(str(input_path))
    page = None
    pixmap = None
    try:
        total_pages = int(document.page_count)
        if total_pages < 1:
            raise ValueError("PDF has no pages")
        if page_number < 1 or page_number > total_pages:
            raise ValueError(
                f"Requested page {page_number} is outside PDF range 1..{total_pages}"
            )

        page = document.load_page(page_number - 1)
        pixmap = page.get_pixmap(
            dpi=dpi,
            alpha=False,
            colorspace=pymupdf.csRGB,
        )
        image = pixmap_to_bgr(pixmap, cv2, np).copy()
        return image, page_number, total_pages
    finally:
        if pixmap is not None:
            del pixmap
        if page is not None:
            del page
        document.close()
        gc.collect()


def run_ocr_on_array(
    ocr,
    image,
    page_no: int,
    *,
    cv2,
    retry_threshold: float,
    profile: str = "fast",
) -> PageResult:
    started = time.perf_counter()
    original_height, original_width = image.shape[:2]

    enhanced = None
    binary = None
    first_prediction = None
    second_prediction = None

    try:
        enhanced = prepare_image_for_profile(image, cv2, profile)
        processed_height, processed_width = enhanced.shape[:2]

        first_prediction = ocr.predict(enhanced)
        first_blocks = extract_blocks_from_prediction(first_prediction)
        first_avg = average_confidence(first_blocks)
        del first_prediction
        first_prediction = None

        chosen_blocks = first_blocks
        retry_used = False

        if profile == "heavy" and first_avg < retry_threshold:
            retry_used = True
            binary = threshold_variant(image, cv2)
            processed_height, processed_width = binary.shape[:2]
            second_prediction = ocr.predict(binary)
            second_blocks = extract_blocks_from_prediction(second_prediction)
            del second_prediction
            second_prediction = None
            chosen_blocks = choose_better_result(first_blocks, second_blocks)

        chosen_blocks = scale_blocks_to_original(
            chosen_blocks,
            original_width=original_width,
            original_height=original_height,
            processed_width=processed_width,
            processed_height=processed_height,
        )

        return PageResult(
            page_no=page_no,
            width=original_width,
            height=original_height,
            average_confidence=average_confidence(chosen_blocks),
            blocks=chosen_blocks,
            full_text=blocks_to_text(chosen_blocks),
            retry_used=retry_used,
            elapsed_seconds=round(time.perf_counter() - started, 3),
        )
    finally:
        if first_prediction is not None:
            del first_prediction
        if second_prediction is not None:
            del second_prediction
        if enhanced is not None:
            del enhanced
        if binary is not None:
            del binary
        gc.collect()


def process_one_page(
    input_path: Path,
    *,
    page_number: int,
    engine: str,
    dpi: int,
    heavy_dpi: int,
    min_confidence: float,
    retry_threshold: float,
    mode: str,
) -> dict[str, Any]:
    overall_started = time.perf_counter()
    if not input_path.is_file():
        raise FileNotFoundError(f"Input file does not exist: {input_path}")

    suffix = input_path.suffix.lower()
    if suffix not in SUPPORTED_INPUTS:
        raise ValueError(
            f"Unsupported file type: {suffix}. "
            f"Supported: {', '.join(sorted(SUPPORTED_INPUTS))}"
        )

    np, cv2, pymupdf, PaddleOCR = import_ocr_dependencies()
    image = None
    ocr = None

    try:
        image, actual_page_number, total_pages = load_one_page_or_image(
            input_path,
            page_number,
            np=np,
            cv2=cv2,
            pymupdf=pymupdf,
            dpi=dpi,
        )
        log(
            f"[INFO] Processing page {actual_page_number}/{total_pages}: {input_path}"
        )

        profile = "heavy" if mode == "heavy" else "fast"
        ocr = create_ocr(PaddleOCR, engine, profile=profile)
        page_result = run_ocr_on_array(
            ocr,
            image,
            actual_page_number,
            cv2=cv2,
            retry_threshold=retry_threshold,
            profile=profile,
        )
        extraction_method = "local_ocr_heavy_fallback" if profile == "heavy" else "local_ocr_fast"

        wants_heavy = mode == "adaptive" and is_weak_result(page_result.blocks, min_confidence)
        if wants_heavy and not heavy_retry_allowed():
            log("[INFO] Weak fast result, but heavy retry is disabled under the current memory cap; keeping the fast result.")
        if wants_heavy and heavy_retry_allowed():
            if ocr is not None:
                del ocr
                ocr = None
            if image is not None:
                del image
                image = None
            gc.collect()

            image, actual_page_number, total_pages = load_one_page_or_image(
                input_path,
                page_number,
                np=np,
                cv2=cv2,
                pymupdf=pymupdf,
                dpi=heavy_dpi,
            )
            ocr = create_ocr(PaddleOCR, engine, profile="heavy")
            page_result = run_ocr_on_array(
                ocr,
                image,
                actual_page_number,
                cv2=cv2,
                retry_threshold=retry_threshold,
                profile="heavy",
            )
            page_result.retry_used = True
            extraction_method = "local_ocr_heavy_fallback"

        page_result.elapsed_seconds = round(time.perf_counter() - overall_started, 3)

        low_confidence = [
            asdict(block)
            for block in page_result.blocks
            if block.confidence < min_confidence
        ]
        return {
            "success": True,
            "source_file": str(input_path),
            "source_type": "pdf" if suffix == ".pdf" else "image",
            "source_pages_count": total_pages,
            "requested_page_number": page_number,
            "extraction_method": extraction_method,
            "text_detection_model": HEAVY_DET_MODEL if extraction_method == "local_ocr_heavy_fallback" else FAST_DET_MODEL,
            "text_recognition_model": ARABIC_REC_MODEL,
            "low_confidence_threshold": min_confidence,
            "low_confidence_blocks_count": len(low_confidence),
            "low_confidence_blocks": low_confidence,
            "page": asdict(page_result),
        }
    finally:
        if image is not None:
            del image
        if ocr is not None:
            del ocr
        gc.collect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Single-page Arabic/English OCR worker")
    parser.add_argument("--input", required=True, help="PDF or image path")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--page-number", type=int, default=1)
    parser.add_argument("--dpi", type=int, default=170)
    parser.add_argument("--heavy-dpi", type=int, default=220)
    parser.add_argument("--min-confidence", type=float, default=0.70)
    parser.add_argument("--retry-threshold", type=float, default=0.74)
    parser.add_argument("--engine", default="onnxruntime")
    parser.add_argument("--mode", choices=("adaptive", "fast", "heavy"), default="adaptive")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_path = Path(args.input).expanduser().resolve()

    try:
        if args.inspect:
            emit(inspect_source(input_path))
            return 0

        result = process_one_page(
            input_path,
            page_number=max(1, int(args.page_number)),
            engine=args.engine,
            dpi=max(72, int(args.dpi)),
            heavy_dpi=max(72, int(args.heavy_dpi)),
            min_confidence=float(args.min_confidence),
            retry_threshold=float(args.retry_threshold),
            mode=args.mode,
        )
        emit(result)
        return 0
    except Exception as exc:
        emit(
            {
                "success": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
