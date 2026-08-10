#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Standalone OCR worker for the dedicated Python 3.13 environment.

Important:
- Does not import Frappe.
- Does not save rendered PDF pages.
- Does not save enhanced/threshold images.
- Does not save JSON/TXT output files.
- Returns one JSON object on stdout.
- Writes progress/debug messages to stderr.
- Converts OCR bounding boxes back to the original image dimensions.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

# Limit CPU thread pools before importing inference libraries.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

SUPPORTED_IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
SUPPORTED_INPUTS = SUPPORTED_IMAGES | {".pdf"}


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
    error: str | None = None


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def import_dependencies():
    try:
        import cv2
        import numpy as np
        import pymupdf
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise RuntimeError(f"Missing OCR dependency: {exc}") from exc

    return np, cv2, pymupdf, PaddleOCR


def read_image(path: Path, cv2, np):
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Cannot read image: {path}")
    return image


def pixmap_to_bgr(pixmap, cv2, np):
    """Convert a PyMuPDF Pixmap directly to an OpenCV BGR array."""
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
    """High-quality preprocessing matching the successful Windows test."""
    image = normalize_image_size(image, cv2)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
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
    image = normalize_image_size(image, cv2)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
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

    if len(points) == 4 and all(
        isinstance(value, (int, float)) for value in points
    ):
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


def scale_bbox_to_original(
    bbox: list[int],
    *,
    original_width: int,
    original_height: int,
    processed_width: int,
    processed_height: int,
) -> list[int]:
    """Convert a processed-image bbox to coordinates of the original image."""
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

    # Clip coordinates to the original image boundaries.
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
    scaled: list[OCRBlock] = []
    for block in blocks:
        scaled.append(
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
        )
    return sort_blocks_arabic(scaled)


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


def create_ocr(PaddleOCR, engine: str = "onnxruntime"):
    kwargs: dict[str, Any] = {
        "lang": "ar",
        "ocr_version": "PP-OCRv5",
        "use_doc_orientation_classify": True,
        "use_doc_unwarping": False,
        "use_textline_orientation": True,
    }

    if engine:
        kwargs["engine"] = engine

    log("[INFO] Loading PaddleOCR Arabic model...")
    return PaddleOCR(**kwargs)


def run_ocr_on_array(
    ocr,
    image,
    page_no: int,
    cv2,
    retry_threshold: float,
) -> PageResult:
    original_height, original_width = image.shape[:2]

    enhanced = enhance_for_ocr(image, cv2)
    processed_height, processed_width = enhanced.shape[:2]

    first_prediction = ocr.predict(enhanced)
    first_blocks = extract_blocks_from_prediction(first_prediction)
    first_avg = average_confidence(first_blocks)

    chosen_blocks = first_blocks
    retry_used = False

    if first_avg < retry_threshold:
        retry_used = True
        binary = threshold_variant(image, cv2)
        second_prediction = ocr.predict(binary)
        second_blocks = extract_blocks_from_prediction(second_prediction)
        chosen_blocks = choose_better_result(first_blocks, second_blocks)

        # Both preprocessing variants use the same normalize_image_size rules.
        processed_height, processed_width = binary.shape[:2]

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
    )


def build_document_result(
    input_path: Path,
    page_results: list[PageResult],
    elapsed_seconds: float,
    min_confidence: float,
) -> dict[str, Any]:
    low_confidence_items: list[dict[str, Any]] = []
    combined_text_parts: list[str] = []

    for page in page_results:
        combined_text_parts.append(
            f"{'=' * 24}\nPAGE {page.page_no}\n{'=' * 24}\n{page.full_text}"
        )

        for block in page.blocks:
            if block.confidence < min_confidence:
                low_confidence_items.append(
                    {
                        "page_no": page.page_no,
                        "text": block.text,
                        "confidence": block.confidence,
                        "bbox": block.bbox,
                    }
                )

    document_avg = (
        round(
            sum(page.average_confidence for page in page_results)
            / len(page_results),
            6,
        )
        if page_results
        else 0.0
    )

    return {
        "success": True,
        "source_file": str(input_path),
        "pages_count": len(page_results),
        "document_average_confidence": document_avg,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "low_confidence_threshold": min_confidence,
        "low_confidence_blocks_count": len(low_confidence_items),
        "full_text": "\n\n".join(combined_text_parts).strip(),
        "low_confidence_blocks": low_confidence_items,
        "pages": [asdict(page) for page in page_results],
    }


def process_input(
    input_path: Path,
    ocr,
    np,
    cv2,
    pymupdf,
    dpi: int,
    min_confidence: float,
    retry_threshold: float,
) -> dict[str, Any]:
    if not input_path.exists():
        raise FileNotFoundError(f"Input file does not exist: {input_path}")

    suffix = input_path.suffix.lower()
    if suffix not in SUPPORTED_INPUTS:
        raise ValueError(
            f"Unsupported file type: {suffix}. "
            f"Supported: {', '.join(sorted(SUPPORTED_INPUTS))}"
        )

    started = time.perf_counter()
    results: list[PageResult] = []

    if suffix == ".pdf":
        document = pymupdf.open(str(input_path))
        try:
            total_pages = document.page_count
            if total_pages == 0:
                raise ValueError("PDF has no pages")

            log(f"[INFO] Processing: {input_path}")
            log(f"[INFO] Pages/images: {total_pages}")

            for index, page in enumerate(document, start=1):
                log(f"[OCR] Page {index}/{total_pages} ...")
                try:
                    pixmap = page.get_pixmap(
                        dpi=dpi,
                        alpha=False,
                        colorspace=pymupdf.csRGB,
                    )
                    image = pixmap_to_bgr(pixmap, cv2, np)
                    result = run_ocr_on_array(
                        ocr=ocr,
                        image=image,
                        page_no=index,
                        cv2=cv2,
                        retry_threshold=retry_threshold,
                    )
                except Exception as exc:
                    traceback.print_exc(file=sys.stderr)
                    result = PageResult(
                        page_no=index,
                        width=0,
                        height=0,
                        average_confidence=0.0,
                        blocks=[],
                        full_text="",
                        retry_used=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )

                results.append(result)
                log(
                    f"[DONE] Page {index}: {len(result.blocks)} blocks, "
                    f"confidence={result.average_confidence:.3f}"
                )
        finally:
            document.close()
    else:
        log(f"[INFO] Processing: {input_path}")
        log("[INFO] Pages/images: 1")
        log("[OCR] Page 1/1 ...")
        image = read_image(input_path, cv2, np)
        result = run_ocr_on_array(
            ocr=ocr,
            image=image,
            page_no=1,
            cv2=cv2,
            retry_threshold=retry_threshold,
        )
        results.append(result)
        log(
            f"[DONE] Page 1: {len(result.blocks)} blocks, "
            f"confidence={result.average_confidence:.3f}"
        )

    elapsed = time.perf_counter() - started
    log(f"[INFO] Total time: {elapsed:.1f} seconds")

    return build_document_result(
        input_path=input_path,
        page_results=results,
        elapsed_seconds=elapsed,
        min_confidence=min_confidence,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="In-memory Arabic/English OCR worker"
    )
    parser.add_argument("--input", required=True, help="PDF or image path")
    parser.add_argument("--dpi", type=int, default=220)
    parser.add_argument("--min-confidence", type=float, default=0.70)
    parser.add_argument("--retry-threshold", type=float, default=0.74)
    parser.add_argument("--engine", default="onnxruntime")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        np, cv2, pymupdf, PaddleOCR = import_dependencies()
        ocr = create_ocr(PaddleOCR, args.engine)
        result = process_input(
            input_path=Path(args.input).expanduser().resolve(),
            ocr=ocr,
            np=np,
            cv2=cv2,
            pymupdf=pymupdf,
            dpi=args.dpi,
            min_confidence=args.min_confidence,
            retry_threshold=args.retry_threshold,
        )
        print(
            "__AI_AGENT_OCR_RESULT__="
            + json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            flush=True,
        )
        return 0
    except Exception as exc:
        error = {
            "success": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        print(
            "__AI_AGENT_OCR_RESULT__="
            + json.dumps(error, ensure_ascii=False, separators=(",", ":")),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())