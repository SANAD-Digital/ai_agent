#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Local Arabic/English OCR tester for Windows.

What it does:
- Accepts PDF, PNG, JPG, JPEG, WEBP, BMP, TIFF.
- Converts PDF pages to images locally.
- Improves page contrast locally.
- Runs PaddleOCR locally using the Arabic PP-OCRv5 model.
- Saves JSON and TXT output per page and for the whole document.
- Does not call Gemini, Azure, DeepSeek, or any paid API.

First run:
- PaddleOCR downloads its OCR model files once.
- After that, inference is local.

Recommended Python version:
- Python 3.10 or 3.11 (64-bit).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import time
import traceback
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable

SUPPORTED_IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
SUPPORTED_INPUTS = SUPPORTED_IMAGES | {".pdf"}


@dataclass
class OCRBlock:
    text: str
    confidence: float
    bbox: list[int]  # [x0, y0, x1, y1]


@dataclass
class PageResult:
    page_no: int
    source_image: str
    processed_image: str
    width: int
    height: int
    average_confidence: float
    blocks: list[OCRBlock]
    full_text: str
    retry_used: bool
    error: str | None = None


def dependency_error(package: str, install_hint: str) -> None:
    print(f"\n[ERROR] Missing dependency: {package}")
    print(f"Install it with:\n  {install_hint}\n")
    raise SystemExit(2)


def import_dependencies():
    try:
        import numpy as np
    except ImportError:
        dependency_error("numpy", "python -m pip install numpy")

    try:
        import cv2
    except ImportError:
        dependency_error(
            "opencv-python-headless",
            "python -m pip install opencv-python-headless",
        )

    try:
        import pymupdf
    except ImportError:
        dependency_error("pymupdf", "python -m pip install pymupdf")

    try:
        from paddleocr import PaddleOCR
    except ImportError:
        dependency_error(
            "paddleocr",
            "python -m pip install paddleocr onnxruntime",
        )

    return np, cv2, pymupdf, PaddleOCR


def read_image_unicode(path: Path, cv2, np):
    """Read an image reliably from Windows paths containing Arabic characters."""
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Cannot read image: {path}")
    return image


def write_image_unicode(path: Path, image, cv2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower() or ".png"
    ok, encoded = cv2.imencode(ext, image)
    if not ok:
        raise ValueError(f"Cannot encode image: {path}")
    encoded.tofile(str(path))


def normalize_image_size(image, cv2, max_side: int = 2600, min_side: int = 1300):
    """Keep enough resolution for small invoice numbers without sending huge images."""
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
    """
    Conservative enhancement:
    - resize
    - grayscale
    - denoise
    - CLAHE contrast
    Keeps a natural grayscale image because aggressive thresholding can damage Arabic.
    """
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
    """Fallback variant for pale thermal receipts or low-contrast scans."""
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


def render_pdf(pdf_path: Path, pages_dir: Path, dpi: int, pymupdf) -> list[Path]:
    pages_dir.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open(str(pdf_path))
    output: list[Path] = []

    try:
        if document.page_count == 0:
            raise ValueError("PDF has no pages.")

        for index, page in enumerate(document):
            out_path = pages_dir / f"page_{index + 1:04d}.png"
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            pix.save(str(out_path))
            output.append(out_path)
    finally:
        document.close()

    return output


def copy_image_to_workdir(image_path: Path, pages_dir: Path) -> list[Path]:
    pages_dir.mkdir(parents=True, exist_ok=True)
    target = pages_dir / f"page_0001{image_path.suffix.lower()}"
    shutil.copy2(image_path, target)
    return [target]


def result_to_dict(result: Any) -> dict[str, Any]:
    """
    PaddleOCR 3.x Result objects expose JSON data.
    This helper tolerates small API differences between minor releases.
    """
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

    # Last resort for objects with a mapping-like payload.
    try:
        return dict(result)
    except Exception as exc:
        raise TypeError(
            "Unsupported PaddleOCR result object. "
            "Try upgrading paddleocr and onnxruntime."
        ) from exc


def polygon_to_bbox(poly: Any) -> list[int]:
    """Convert a polygon or box to [x0, y0, x1, y1]."""
    if poly is None:
        return [0, 0, 0, 0]

    try:
        points = list(poly)
    except TypeError:
        return [0, 0, 0, 0]

    # Already [x0, y0, x1, y1]
    if len(points) == 4 and all(isinstance(v, (int, float)) for v in points):
        x0, y0, x1, y1 = points
        return [int(x0), int(y0), int(x1), int(y1)]

    xs: list[float] = []
    ys: list[float] = []
    for point in points:
        try:
            x, y = point[0], point[1]
            xs.append(float(x))
            ys.append(float(y))
        except Exception:
            continue

    if not xs or not ys:
        return [0, 0, 0, 0]

    return [
        int(min(xs)),
        int(min(ys)),
        int(max(xs)),
        int(max(ys)),
    ]


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

            score = 0.0
            if index < len(scores):
                try:
                    score = float(scores[index])
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
    """
    Sort approximately top-to-bottom and right-to-left.
    A 24-pixel row bucket keeps words on the same visual line together.
    """

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
    # One detected block per line is safer than trying to reconstruct tables here.
    return "\n".join(block.text for block in blocks)


def choose_better_result(
    first: list[OCRBlock],
    second: list[OCRBlock],
) -> list[OCRBlock]:
    """
    Prefer the result with higher weighted quality:
    confidence + detected character count.
    """

    def quality(blocks: list[OCRBlock]) -> float:
        if not blocks:
            return 0.0
        chars = sum(len(b.text) for b in blocks)
        return average_confidence(blocks) * 1000 + min(chars, 5000) / 10

    return second if quality(second) > quality(first) else first


def create_ocr(PaddleOCR, engine: str):
    print("[INFO] Loading PaddleOCR Arabic model...")
    for eng in [engine, "onnxruntime", "paddle_static", "paddle"]:
        if not eng:
            continue
        try:
            return PaddleOCR(lang="ar", engine=eng)
        except Exception:
            pass
    try:
        return PaddleOCR(lang="ar", engine="onnxruntime")
    except Exception:
        return PaddleOCR()


def run_ocr_on_image(
    ocr,
    source_path: Path,
    processed_dir: Path,
    cv2,
    np,
    retry_threshold: float,
) -> PageResult:
    page_no = int(source_path.stem.split("_")[-1]) if "_" in source_path.stem else 1
    processed_dir.mkdir(parents=True, exist_ok=True)

    image = read_image_unicode(source_path, cv2, np)
    enhanced = enhance_for_ocr(image, cv2)
    enhanced_path = processed_dir / f"page_{page_no:04d}_enhanced.png"
    write_image_unicode(enhanced_path, enhanced, cv2)

    prediction = ocr.predict(str(enhanced_path))
    first_blocks = extract_blocks_from_prediction(prediction)
    first_avg = average_confidence(first_blocks)

    chosen_blocks = first_blocks
    chosen_path = enhanced_path
    retry_used = False

    if first_avg < retry_threshold:
        retry_used = True
        binary = threshold_variant(image, cv2)
        binary_path = processed_dir / f"page_{page_no:04d}_threshold.png"
        write_image_unicode(binary_path, binary, cv2)

        second_prediction = ocr.predict(str(binary_path))
        second_blocks = extract_blocks_from_prediction(second_prediction)
        chosen_blocks = choose_better_result(first_blocks, second_blocks)
        if chosen_blocks is second_blocks:
            chosen_path = binary_path

    height, width = image.shape[:2]
    return PageResult(
        page_no=page_no,
        source_image=str(source_path),
        processed_image=str(chosen_path),
        width=width,
        height=height,
        average_confidence=average_confidence(chosen_blocks),
        blocks=chosen_blocks,
        full_text=blocks_to_text(chosen_blocks),
        retry_used=retry_used,
    )


def safe_json_data(value: Any) -> Any:
    if isinstance(value, OCRBlock):
        return asdict(value)
    if isinstance(value, PageResult):
        data = asdict(value)
        return data
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def save_outputs(
    input_path: Path,
    output_dir: Path,
    page_results: list[PageResult],
    elapsed_seconds: float,
    min_confidence: float,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    pages_output = output_dir / "pages"
    pages_output.mkdir(parents=True, exist_ok=True)

    for page in page_results:
        page_json = pages_output / f"page_{page.page_no:04d}.json"
        page_txt = pages_output / f"page_{page.page_no:04d}.txt"

        page_json.write_text(
            json.dumps(page, default=safe_json_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        page_txt.write_text(page.full_text, encoding="utf-8")

    combined_text_parts = []
    low_confidence_items = []

    for page in page_results:
        combined_text_parts.append(
            f"\n{'=' * 24}\nPAGE {page.page_no}\n{'=' * 24}\n{page.full_text}\n"
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
            sum(page.average_confidence for page in page_results) / len(page_results),
            6,
        )
        if page_results
        else 0.0
    )

    combined = {
        "source_file": str(input_path),
        "pages_count": len(page_results),
        "document_average_confidence": document_avg,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "low_confidence_threshold": min_confidence,
        "low_confidence_blocks_count": len(low_confidence_items),
        "pages": page_results,
    }

    (output_dir / "ocr_result.json").write_text(
        json.dumps(combined, default=safe_json_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "ocr_text.txt").write_text(
        "".join(combined_text_parts).strip() + "\n",
        encoding="utf-8",
    )
    (output_dir / "low_confidence.json").write_text(
        json.dumps(low_confidence_items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def process_input(
    input_path: Path,
    ocr,
    np,
    cv2,
    pymupdf,
    dpi: int,
    min_confidence: float,
    retry_threshold: float,
    output_root: Path | None,
) -> Path:
    if not input_path.exists():
        raise FileNotFoundError(f"Input file does not exist: {input_path}")

    suffix = input_path.suffix.lower()
    if suffix not in SUPPORTED_INPUTS:
        raise ValueError(
            f"Unsupported file type: {suffix}. "
            f"Supported: {', '.join(sorted(SUPPORTED_INPUTS))}"
        )

    output_dir = (
        output_root / f"{input_path.stem}_ocr_output"
        if output_root
        else input_path.parent / f"{input_path.stem}_ocr_output"
    )
    pages_dir = output_dir / "_source_pages"
    processed_dir = output_dir / "_processed_pages"

    if output_dir.exists():
        # Keep the operation deterministic for testing.
        shutil.rmtree(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    if suffix == ".pdf":
        source_pages = render_pdf(input_path, pages_dir, dpi, pymupdf)
    else:
        source_pages = copy_image_to_workdir(input_path, pages_dir)

    print(f"[INFO] Processing: {input_path}")
    print(f"[INFO] Pages/images: {len(source_pages)}")
    started = time.perf_counter()

    results: list[PageResult] = []

    for index, source_page in enumerate(source_pages, start=1):
        print(f"[OCR] Page {index}/{len(source_pages)} ...", flush=True)
        try:
            result = run_ocr_on_image(
                ocr=ocr,
                source_path=source_page,
                processed_dir=processed_dir,
                cv2=cv2,
                np=np,
                retry_threshold=retry_threshold,
            )
        except Exception as exc:
            traceback.print_exc()
            result = PageResult(
                page_no=index,
                source_image=str(source_page),
                processed_image="",
                width=0,
                height=0,
                average_confidence=0.0,
                blocks=[],
                full_text="",
                retry_used=False,
                error=f"{type(exc).__name__}: {exc}",
            )

        results.append(result)
        print(
            f"[DONE] Page {index}: "
            f"{len(result.blocks)} blocks, "
            f"confidence={result.average_confidence:.3f}"
        )

    elapsed = time.perf_counter() - started
    save_outputs(
        input_path=input_path,
        output_dir=output_dir,
        page_results=results,
        elapsed_seconds=elapsed,
        min_confidence=min_confidence,
    )

    print(f"\n[SUCCESS] Output folder:\n{output_dir}")
    print(f"[INFO] Total time: {elapsed:.1f} seconds")
    return output_dir


def choose_files_with_gui() -> list[Path]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return []

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    filenames = filedialog.askopenfilenames(
        title="اختر ملفات PDF أو صور الفواتير",
        filetypes=[
            ("PDF and images", "*.pdf *.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff"),
            ("PDF", "*.pdf"),
            ("Images", "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff"),
            ("All files", "*.*"),
        ],
    )
    root.destroy()
    return [Path(name) for name in filenames]


def show_done_message(output_dirs: list[Path]) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "اكتملت القراءة",
            "تم حفظ النتائج في:\n\n" + "\n".join(str(path) for path in output_dirs),
        )
        root.destroy()
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local Arabic/English OCR for PDF and invoice images.",
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        help="PDF or image paths. If omitted, a Windows file picker opens.",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=220,
        help="PDF rendering DPI. Default: 220.",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.70,
        help="Blocks below this value are listed in low_confidence.json.",
    )
    parser.add_argument(
        "--retry-threshold",
        type=float,
        default=0.74,
        help="Retry a page with adaptive thresholding below this average confidence.",
    )
    parser.add_argument(
        "--engine",
        default="onnxruntime",
        choices=["onnxruntime", "paddle", ""],
        help="PaddleOCR inference engine. Default: onnxruntime.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help="Optional root folder for all output directories.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    input_paths = [Path(value).expanduser().resolve() for value in args.inputs]
    used_gui = False

    if not input_paths:
        used_gui = True
        input_paths = choose_files_with_gui()

    if not input_paths:
        print("No files selected.")
        return 1

    np, cv2, pymupdf, PaddleOCR = import_dependencies()
    ocr = create_ocr(PaddleOCR, args.engine)

    output_dirs: list[Path] = []
    failures = 0

    for input_path in input_paths:
        try:
            output_dir = process_input(
                input_path=input_path,
                ocr=ocr,
                np=np,
                cv2=cv2,
                pymupdf=pymupdf,
                dpi=args.dpi,
                min_confidence=args.min_confidence,
                retry_threshold=args.retry_threshold,
                output_root=args.output_root,
            )
            output_dirs.append(output_dir)
        except Exception as exc:
            failures += 1
            print(f"\n[FAILED] {input_path}")
            print(f"{type(exc).__name__}: {exc}")
            traceback.print_exc()

    if used_gui and output_dirs:
        show_done_message(output_dirs)

    if failures:
        print(f"\nCompleted with {failures} failed input file(s).")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
