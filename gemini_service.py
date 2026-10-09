import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from io import BytesIO


from PIL import Image
from google import genai
from google.genai import types

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"}


def gemini_configured() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def gemini_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable is missing.")
    return genai.Client(api_key=api_key)


def validate_image_filename(filename: str) -> None:
    if not filename:
        raise ValueError("Each image must have a filename.")
    ext = os.path.splitext(filename.lower())[1]
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError(f"Unsupported image type: {filename}")


def model_name() -> str:
    return os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def extract_page_context(
    client: genai.Client,
    image: Image.Image,
    filename: str,
    page_num: int,
    total_pages: int,
    max_retries: int = 4,
) -> str:
    prompt = f"""
You are an expert medical record digitizer. Analyze this single patient document image ({filename}, Page {page_num} of {total_pages}) and extract ALL information on the page with 100% fidelity.

Extraction Guidelines:
1. Lossless Extraction:
   - Extract every printed word, label, section header, patient ID, hospital details, and form text.
   - Read all handwritten text (doctor notes, prescriptions, complaints, physical exam findings, diagnoses, order lines).
   - Do NOT omit any numbers, dates, timestamps, dosage amounts, frequencies, or units.
   - Do NOT summarize or condense; capture exact names, values, and details.
2. Structure & Organization:
   - Organize the extracted content logically using Markdown headers, lists, and tables.
   - Represent forms, physical examination checklists, and medication tables clearly.
   - Explicitly note any checked boxes or selected fields.
3. Medical & Handwriting Precision:
   - Carefully transcribe doctor handwriting for drug names, dosages, and routes.
   - Maintain the semantic context of every clinical observation.

Output Format:
Return ONLY the structured Markdown text for this page.
"""

    last_error: BaseException = RuntimeError("Extraction failed without raising a specific exception")
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model_name(),
                contents=[image, prompt],
                config=types.GenerateContentConfig(temperature=0.1),
            )
            extracted_text = (response.text or "").strip()
            return (
                f"# PAGE {page_num}: {filename}\n\n"
                f"{extracted_text}\n\n"
                "---\n"
            )
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
) -> tuple[int, str]:
    validate_image_filename(filename)
    image = Image.open(BytesIO(content))
    try:
        page_text = extract_page_context(client, image, filename, idx, total_pages)
        return (idx, page_text)
    except Exception as exc:
        fallback_text = (
            f"# PAGE {idx}: {filename}\n\n"
            f"> Error processing page '{filename}': {exc}\n\n"
            "---\n"
        )
        return (idx, fallback_text)


def run_extraction(image_items: list[tuple[str, bytes]], max_workers: int = 5) -> str:
    client = gemini_client()
    total_pages = len(image_items)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    document = "# CONSOLIDATED PATIENT CLINICAL CONTEXT\n"
    document += f"**Generated Date**: {timestamp}\n"
    document += f"**Total Pages**: {total_pages}\n\n"
    document += "=" * 60 + "\n\n"

    if total_pages == 0:
        return document

    worker_count = min(max_workers, total_pages)
    results: dict[int, str] = {}

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_idx = {
            executor.submit(_process_single_page, client, content, filename, idx, total_pages): idx
            for idx, (filename, content) in enumerate(image_items, start=1)
        }

        for future in as_completed(future_to_idx):
            idx, page_text = future.result()
            results[idx] = page_text

    # Assemble pages in strict original chronological order (Page 1 -> Page N)
    for idx in range(1, total_pages + 1):
        if idx in results:
            document += results[idx]
            document += "\n"

    return document



def generate_json(prompt: str, schema: dict, max_retries: int = 4) -> dict:
    client = gemini_client()
    last_error: BaseException = RuntimeError("Gemini JSON request failed.")

    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model_name(),
                contents=[prompt],
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )
            text = (response.text or "").strip()
            if not text:
                raise RuntimeError("Gemini returned empty JSON.")
            return json.loads(text)
        except Exception as exc:
            last_error = exc
            err = str(exc)
            if "429" in err or "RESOURCE_EXHAUSTED" in err or "getaddrinfo" in err:
                time.sleep(6 * attempt)
            elif attempt < max_retries:
                time.sleep(3)

    raise last_error
