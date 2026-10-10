import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

from PIL import Image
import pypdfium2 as pdfium
from google import genai
from google.genai import types

ALLOWED_DOCUMENT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".pdf"}


def gemini_configured() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def gemini_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable is missing.")
    return genai.Client(api_key=api_key)


def validate_document_filename(filename: str) -> None:
    if not filename:
        raise ValueError("Each uploaded file must have a filename.")
    ext = os.path.splitext(filename.lower())[1]
    if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {filename}")


def pdf_to_page_images(
    pdf_bytes: bytes,
    base_filename: str = "document",
    dpi: int = 200,
    jpeg_quality: int = 90,
) -> list[tuple[str, bytes]]:
    """Unpacks each page of a PDF into high-quality JPEG image bytes in memory."""
    pdf = pdfium.PdfDocument(pdf_bytes)
    page_images: list[tuple[str, bytes]] = []
    total_pages = len(pdf)
    scale = dpi / 72.0
    stem = Path(base_filename).stem or "doc"

    for page_idx in range(total_pages):
        page = pdf[page_idx]
        bitmap = page.render(scale=scale)  # pyright: ignore[reportArgumentType]
        pil_image = bitmap.to_pil()

        if pil_image.mode in ("RGBA", "P"):
            pil_image = pil_image.convert("RGB")

        buffer = BytesIO()
        pil_image.save(buffer, format="JPEG", quality=jpeg_quality)
        jpeg_bytes = buffer.getvalue()

        page_name = f"{stem}_page_{page_idx + 1:03d}.jpg"
        page_images.append((page_name, jpeg_bytes))

    return page_images


import config
import metrics as metrics_lib


def model_name() -> str:
    return config.GEMINI_MODEL


def default_ocr_thinking_budget() -> int:
    return config.GEMINI_OCR_THINKING_BUDGET


def calculate_cost(prompt_tokens: int, billed_output_tokens: int) -> tuple[float, float]:
    return config.calculate_token_cost(prompt_tokens, billed_output_tokens)


def extract_page_context(
    client: genai.Client,
    image: Image.Image,
    filename: str,
    page_num: int,
    total_pages: int,
    max_retries: int = 4,
    thinking_budget: Optional[int] = None,
) -> tuple[str, dict]:
    if thinking_budget is None:
        thinking_budget = default_ocr_thinking_budget()

    prompt = f"""
You are a Medical Record Digitization Specialist. Your task is to extract ALL information from this patient record image ({filename}, Page {page_num} of {total_pages}) with strict factual accuracy.

CORE EXTRACTION & SAFETY RULES:
1. Handling Ambiguity:
   - If ANY word, number, dosage, lab unit, or date is illegible, blurry, or ambiguous, write '[verify]' (e.g., 'Levetiracetam 1g BD [verify]', '15/10/2026 [verify]').
   - Never invent, extrapolate, or guess unreadable content.

2. Chronological & Date Sanity:
   - Identify the primary admission timeline from printed forms, lab timestamps, and registration stamps.
   - For handwritten notes and bedside charts, ensure transcribed dates match the active admission timeframe rather than misinterpreting cursive digits as unrelated years.
   - If a handwritten date is ambiguous, transcribe your best reading followed by '[verify]' (e.g., '29/09/2026 [verify]').

3. Medical Terminology Grounding:
   - Transcribe clinical terms, drugs, and diagnostic findings accurately (e.g., Fahr's disease, NCSE, GTCS, CBNAAT, Dengue, Scrub typhus).
   - Carefully distinguish imaging signal characteristics (e.g., T2/SWI hypointensity vs. hyperintensity).

4. Structure:
   - Output structured Markdown.
   - Format vitals, labs, and medication administration sheets as clear Markdown tables.
   - Explicitly preserve checkbox states as '[X]' or '[ ]'.

Return ONLY the structured Markdown for this page.
"""

    last_error: BaseException = RuntimeError("Extraction failed without raising a specific exception")
    start_time = time.time()
    for attempt in range(1, max_retries + 1):
        try:
            config = types.GenerateContentConfig(
                temperature=0.1,
                thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
            )
            response = client.models.generate_content(
                model=model_name(),
                contents=[image, prompt],
                config=config,
            )
            duration = time.time() - start_time
            extracted_text = (response.text or "").strip()
            page_text = (
                f"# PAGE {page_num}: {filename}\n\n"
                f"{extracted_text}\n\n"
                "---\n"
            )

            usage = getattr(response, "usage_metadata", None)
            prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
            visible_tokens = getattr(usage, "candidates_token_count", 0) or 0
            thought_tokens = getattr(usage, "thoughts_token_count", 0) or 0

            page_metrics = metrics_lib.make_successful_page_metrics(
                page_num=page_num,
                filename=filename,
                prompt_tokens=prompt_tokens,
                visible_tokens=visible_tokens,
                thought_tokens=thought_tokens,
                duration_seconds=duration,
                attempts=attempt,
            )
            return page_text, page_metrics
        except Exception as exc:
            last_error = exc
            err_str = str(exc)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "getaddrinfo" in err_str:
                time.sleep(6 * attempt)
            elif attempt < max_retries:
                time.sleep(3)

    raise last_error


def _process_single_page(
    client: genai.Client,
    content: bytes,
    filename: str,
    idx: int,
    total_pages: int,
    thinking_budget: Optional[int] = None,
) -> tuple[int, str, dict]:
    validate_document_filename(filename)
    image = Image.open(BytesIO(content))
    if image.mode in ("RGBA", "P"):
        image = image.convert("RGB")
    start_time = time.time()
    try:
        page_text, page_metrics = extract_page_context(
            client, image, filename, idx, total_pages, thinking_budget=thinking_budget
        )
        return (idx, page_text, page_metrics)
    except Exception as exc:
        fallback_text = (
            f"# PAGE {idx}: {filename}\n\n"
            f"> Error processing page '{filename}': {exc}\n\n"
            "---\n"
        )
        fallback_metrics = metrics_lib.make_failed_page_metrics(
            page_num=idx,
            filename=filename,
            error=str(exc),
            attempts=4,
            duration_seconds=time.time() - start_time,
        )
        return (idx, fallback_text, fallback_metrics)


def run_extraction(
    image_items: list[tuple[str, bytes]],
    max_workers: int = 5,
    thinking_budget: Optional[int] = None,
) -> tuple[str, dict]:
    client = gemini_client()
    total_pages = len(image_items)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    start_time = time.time()

    document = "# CONSOLIDATED PATIENT CLINICAL CONTEXT\n"
    document += f"**Generated Date**: {timestamp}\n"
    document += f"**Total Pages**: {total_pages}\n\n"
    document += "=" * 60 + "\n\n"

    if total_pages == 0:
        return document, metrics_lib.Stage1OCRMetrics().to_dict()

    worker_count = min(max_workers, total_pages)
    results: dict[int, str] = {}
    page_metrics_dict: dict[int, dict] = {}

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_idx = {
            executor.submit(_process_single_page, client, content, filename, idx, total_pages, thinking_budget): idx
            for idx, (filename, content) in enumerate(image_items, start=1)
        }

        for future in as_completed(future_to_idx):
            idx, page_text, page_metrics = future.result()
            results[idx] = page_text
            page_metrics_dict[idx] = page_metrics

    # Assemble pages in strict original chronological order (Page 1 -> Page N)
    for idx in range(1, total_pages + 1):
        if idx in results:
            document += results[idx]
            document += "\n"

    ocr_duration = time.time() - start_time
    pages_list = [page_metrics_dict[idx] for idx in range(1, total_pages + 1) if idx in page_metrics_dict]
    total_prompt = sum(p.get("prompt_tokens", 0) for p in pages_list)
    total_visible = sum(p.get("visible_tokens", 0) for p in pages_list)
    total_thought = sum(p.get("thought_tokens", 0) for p in pages_list)
    total_billed_output = sum(p.get("billed_output_tokens", 0) for p in pages_list)
    total_tokens = sum(p.get("total_tokens", 0) for p in pages_list)
    successful_pages = sum(1 for p in pages_list if p.get("status") == "success")
    failed_pages = sum(1 for p in pages_list if p.get("status") == "failed")
    total_retries = sum(p.get("retries", 0) for p in pages_list)
    stage_status = "completed" if failed_pages == 0 else ("failed" if successful_pages == 0 else "partial_success")
    cost_usd, cost_inr = calculate_cost(total_prompt, total_billed_output)

    ocr_metrics = metrics_lib.Stage1OCRMetrics(
        status=stage_status,
        total_pages=total_pages,
        successful_pages=successful_pages,
        failed_pages=failed_pages,
        total_retries=total_retries,
        duration_seconds=round(ocr_duration, 2),
        prompt_tokens=total_prompt,
        visible_tokens=total_visible,
        thought_tokens=total_thought,
        billed_output_tokens=total_billed_output,
        total_tokens=total_tokens,
        cost_usd=round(cost_usd, 6),
        cost_inr=round(cost_inr, 4),
        pages=pages_list,
    ).to_dict()
    return document, ocr_metrics


def generate_json(
    prompt: str,
    schema: dict,
    max_retries: int = 4,
    thinking_budget: Optional[int] = None,
) -> tuple[dict, dict]:
    if thinking_budget is None:
        thinking_budget = default_ocr_thinking_budget()

    client = gemini_client()
    last_error: BaseException = RuntimeError("Gemini JSON request failed.")
    start_time = time.time()

    for attempt in range(1, max_retries + 1):
        try:
            config = types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
                response_schema=schema,
                thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
            )
            response = client.models.generate_content(
                model=model_name(),
                contents=[prompt],
                config=config,
            )
            duration = time.time() - start_time
            text = (response.text or "").strip()
            if not text:
                raise RuntimeError("Gemini returned empty JSON.")
            data = json.loads(text)

            usage = getattr(response, "usage_metadata", None)
            prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
            visible_tokens = getattr(usage, "candidates_token_count", 0) or 0
            thought_tokens = getattr(usage, "thoughts_token_count", 0) or 0
            billed_output_tokens = visible_tokens + thought_tokens
            total_tokens = getattr(usage, "total_token_count", 0) or (prompt_tokens + billed_output_tokens)
            cost_usd, cost_inr = calculate_cost(prompt_tokens, billed_output_tokens)

            metrics = {
                "status": "success",
                "attempts": attempt,
                "retries": attempt - 1,
                "duration_seconds": round(duration, 2),
                "prompt_tokens": prompt_tokens,
                "visible_tokens": visible_tokens,
                "thought_tokens": thought_tokens,
                "billed_output_tokens": billed_output_tokens,
                "total_tokens": total_tokens,
                "cost_usd": round(cost_usd, 6),
                "cost_inr": round(cost_inr, 4),
            }
            return data, metrics
        except Exception as exc:
            last_error = exc
            err = str(exc)
            if "429" in err or "RESOURCE_EXHAUSTED" in err or "getaddrinfo" in err:
                time.sleep(6 * attempt)
            elif attempt < max_retries:
                time.sleep(3)

    raise last_error
