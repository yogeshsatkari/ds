import io
import os
import re
import time
from typing import Any, Optional
from PIL import Image, ImageOps
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from google.genai import types

import gemini_service


def normalize_image(content: bytes, max_dim: int = 2048) -> Image.Image:
    """
    Normalizes mobile/scanner photos:
    1. EXIF auto-orientation (handles upside-down or sideways smartphone camera photos).
    2. Converts color mode to standard RGB (handles RGBA, P, L, CMYK).
    3. Smart downscaling to max_dim (preserves OCR legibility while reducing transfer size and latency).
    """
    img = Image.open(io.BytesIO(content))
    img = ImageOps.exif_transpose(img) or img
    if img.mode in ("RGBA", "P", "L", "CMYK"):
        img = img.convert("RGB")
    if max(img.size) > max_dim:
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
    return img


# ==============================================================================
# 1. LOCAL DOCX PARSER (0 tokens, < 50ms)
# ==============================================================================

def docx_to_markdown(docx_bytes: bytes) -> tuple[str, dict]:
    """
    Parses DOCX bytes into structured Markdown.
    Runs locally on CPU with zero tokens and zero API cost.
    """
    start_time = time.time()
    doc = Document(io.BytesIO(docx_bytes))
    md_lines: list[str] = []

    for block in doc.element.body:
        if block.tag.endswith("p"):
            p = Paragraph(block, doc)
            text = p.text.strip()
            if not text:
                continue

            style_name = p.style.name.lower() if p.style and p.style.name else ""

            if "heading 1" in style_name:
                md_lines.append(f"\n# {text}\n")
            elif "heading 2" in style_name:
                md_lines.append(f"\n## {text}\n")
            elif "heading 3" in style_name:
                md_lines.append(f"\n### {text}\n")
            elif "title" in style_name:
                md_lines.append(f"\n# {text}\n")
            elif "list" in style_name or "bullet" in style_name:
                md_lines.append(f"- {text}")
            else:
                runs_are_bold = bool(p.runs) and all(r.bold for r in p.runs if r.text.strip())
                if runs_are_bold and len(text) < 60 and not text.endswith("."):
                    md_lines.append(f"\n### {text}\n")
                elif text.isupper() and len(text) < 50:
                    md_lines.append(f"\n### {text}\n")
                else:
                    formatted_parts = []
                    for r in p.runs:
                        t = r.text
                        if not t:
                            continue
                        if r.bold and t.strip():
                            stripped = t.strip()
                            l_space = " " if t.startswith(" ") else ""
                            r_space = " " if t.endswith(" ") else ""
                            formatted_parts.append(f"{l_space}**{stripped}**{r_space}")
                        else:
                            formatted_parts.append(t)
                    line_text = "".join(formatted_parts).strip()
                    md_lines.append(line_text)

        elif block.tag.endswith("tbl"):
            table = Table(block, doc)
            table_md = _table_to_markdown(table)
            if table_md:
                md_lines.append(f"\n{table_md}\n")

    duration = time.time() - start_time
    extracted_md = "\n".join(md_lines).strip()

    metrics = {
        "format": "DOCX",
        "method": "python-docx (local CPU)",
        "duration_seconds": round(duration, 3),
        "prompt_tokens": 0,
        "visible_tokens": 0,
        "thought_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "cost_inr": 0.0,
        "char_count": len(extracted_md),
    }
    return extracted_md, metrics


def _table_to_markdown(table: Table) -> str:
    raw_rows: list[list[str]] = []

    for row in table.rows:
        row_values: list[str] = []
        seen_cells = set()
        for cell in row.cells:
            cell_id = id(cell._tc)
            if cell_id in seen_cells:
                continue
            seen_cells.add(cell_id)
            cell_clean = " ".join(cell.text.split()).strip()
            row_values.append(cell_clean)
        if any(row_values):
            raw_rows.append(row_values)

    if not raw_rows:
        return ""

    num_cols = max(len(r) for r in raw_rows)
    for r in raw_rows:
        while len(r) < num_cols:
            r.append("")

    lines: list[str] = []
    lines.append("| " + " | ".join(raw_rows[0]) + " |")
    lines.append("| " + " | ".join(["---"] * num_cols) + " |")
    for row in raw_rows[1:]:
        lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines)


# ==============================================================================
# 2. DOCX AI TEMPLATE ABSTRACTION
# ==============================================================================

DOCX_TEMPLATE_TRANSFORMATION_PROMPT = """You are analyzing an extracted hospital discharge summary template/sample originally parsed from a DOCX document.
Transform this document into a standardized, reusable discharge summary template by replacing patient-specific variable values with INFORMATIVE, DESCRIPTIVE PLACEHOLDERS while strictly preserving its visual layout, headers, and section hierarchy:

1. **Headers & Branding**: Keep hospital name, institutional branding text, and department headings verbatim. Do NOT include image tags, logo placeholders, or markdown image syntax (such as `![Hospital Logo]` or `![Logo]`). Focus purely on textual branding.
2. **Headings**: Preserve all section headings (##, ###).
3. **Tables**: Preserve all markdown tables (| Col 1 | Col 2 |) with exact columns and minimal separators (| --- | --- |).
4. **Short Metadata Fields**: Replace patient demographics with semantic placeholders:
   - e.g. `| Patient's Name: | [Full Patient Name] | IPD ID: | [Inpatient ID / Registration No.] |`
   - e.g. `| Age / Sex: | [Age in years / Gender] | Admission Date: | [DD/MM/YYYY HH:MM] |`
5. **Physical Exam / Vitals**: Preserve parameter names with expected clinical units and attributes:
   - e.g. `Airway: [Patent / Compromised]`
   - e.g. `Breathing: [SpO2 % on RA/O2, Respiratory Rate /min, Bilateral air entry]`
   - e.g. `Circulation: [BP mmHg, Pulse Rate bpm, CRT]`
   - e.g. `Disability: [GCS score E_V_M_, Pupils, Focal neurological deficit]`
6. **Large Narrative Sections (CRITICAL - DO NOT USE SINGLE-WORD OR BLANK PLACEHOLDERS)**:
   For major narrative blocks like "Brief History", "Clinical Summary", "Course in Hospital", or "Condition at Discharge":
   Provide an informative structural guide detailing the exact clinical attributes, chronology, and components to cover based on the sample's contents.
   - e.g. `[Comprehensive narrative outlining: (1) Patient demographic profile, known comorbidities & baseline status; (2) Presenting complaints with duration and onset; (3) Initial emergency triage vitals and physical findings; (4) Diagnostic investigation highlights (key lab values, imaging, culture reports); (5) Inpatient clinical management, multidisciplinary consultations, and procedural interventions; (6) Clinical progression, complication management, and final resolution/stability at discharge]`
7. **Treatment & Medication Lists**:
   - Inpatient: `[Numbered list of administered medications specifying: Drug Name, Dose, Route, Frequency, and Duration; followed by procedural interventions (e.g. nebulization, oxygen therapy, hemodialysis)]`
   - Discharge / Follow-up: `[Discharge medications with explicit Dosage, Route, Frequency, Duration, and Food timing; followed by specific home care/catheter directives, dietary and fluid restrictions]`
8. **Static Boilerplate**: Keep hospital warning instructions, emergency return signs, and standard advisories verbatim as they appear.
9. **No Repetition Loops**: Do NOT output runaway repeating characters, dashes, or words.

Output ONLY the clean Markdown template. Do NOT wrap output in ```markdown code blocks.
"""

def docx_to_template_markdown(docx_bytes: bytes, ai_abstraction: bool = True) -> tuple[str, dict]:
    """
    Parses a DOCX template:
    1. Extracts raw markdown from DOCX via local CPU (python-docx).
    2. If ai_abstraction is True and the document contains filled patient data,
       uses Gemini text model to abstract patient data into descriptive placeholders,
       achieving parity with Image and PDF templates.
    """
    start_time = time.time()
    raw_md, local_metrics = docx_to_markdown(docx_bytes)

    # Check if raw_md is already an unfilled blank template (e.g. contains placeholders and lacks filled clinical text)
    has_filled_data = len(raw_md) > 300 and not any(blank in raw_md for blank in ("_____", "[Full Patient Name]", "[Age in years]"))

    if not ai_abstraction or not has_filled_data or not gemini_service.gemini_configured():
        return raw_md, local_metrics

    client = gemini_service.gemini_client()
    config = types.GenerateContentConfig(
        temperature=0.2,
        max_output_tokens=4096,
        thinking_config=types.ThinkingConfig(thinking_budget=1024),
    )
    prompt = f"{DOCX_TEMPLATE_TRANSFORMATION_PROMPT}\n\nRAW EXTRACTED DOCX CONTENT:\n{raw_md}"
    response = client.models.generate_content(
        model=gemini_service.model_name(),
        contents=[prompt],
        config=config,
    )
    duration = time.time() - start_time

    extracted_md = (response.text or "").strip()
    if extracted_md.startswith("```"):
        extracted_md = re.sub(r"^```(?:markdown)?\s*", "", extracted_md)
        extracted_md = re.sub(r"\s*```$", "", extracted_md)

    extracted_md = re.sub(r"-{6,}", "---", extracted_md)
    extracted_md = re.sub(r"_{6,}", "_____", extracted_md)
    extracted_md = re.sub(r"!\[.*?\](?:\(.*?\))?\n?", "", extracted_md).strip()

    usage = getattr(response, "usage_metadata", None)
    prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
    visible_tokens = getattr(usage, "candidates_token_count", 0) or 0
    thought_tokens = getattr(usage, "thoughts_token_count", 0) or 0
    billed_output_tokens = visible_tokens + thought_tokens
    total_tokens = prompt_tokens + billed_output_tokens

    cost_usd, cost_inr = gemini_service.calculate_cost(prompt_tokens, billed_output_tokens)

    metrics = {
        "format": "DOCX (AI Template Abstraction)",
        "method": f"python-docx + Gemini ({gemini_service.model_name()})",
        "duration_seconds": round(duration, 2),
        "prompt_tokens": prompt_tokens,
        "visible_tokens": visible_tokens,
        "thought_tokens": thought_tokens,
        "output_tokens": billed_output_tokens,
        "total_tokens": total_tokens,
        "cost_usd": round(cost_usd, 6),
        "cost_inr": round(cost_inr, 4),
        "char_count": len(extracted_md),
    }
    return extracted_md, metrics


# ==============================================================================
# 3. IMAGE / PDF TEMPLATE PARSER (Via Gemini Vision)
# ==============================================================================

TEMPLATE_STRUCTURAL_EXTRACTION_PROMPT = """You are analyzing a target hospital discharge summary template/sample.
Transcribe its EXACT visual layout, structural hierarchy, and sections into clean Markdown, replacing patient-specific variable values with INFORMATIVE, DESCRIPTIVE PLACEHOLDERS:

1. **Headers & Branding**: Capture hospital name, institutional branding text, and department headings verbatim. Do NOT include image tags, logo placeholders, or markdown image syntax (such as `![Hospital Logo]` or `![Logo]`). Focus purely on textual branding.
2. **Headings**: Structure section titles as Markdown headings (##, ###).
3. **Tables**: Preserve all tables exactly using clean Markdown table syntax (| Col 1 | Col 2 |). The separator row MUST contain exactly three hyphens per column: `| --- | --- |`. Never output more than three hyphens in a cell.
4. **Short Metadata Fields**: Use clear semantic placeholders with expected format:
   - e.g. `| Patient's Name: | [Full Patient Name] | IPD ID: | [Inpatient ID / Registration No.] |`
   - e.g. `| Age / Sex: | [Age in years / Gender] | Admission Date: | [DD/MM/YYYY HH:MM] |`
5. **Physical Exam / Vitals**: Preserve parameter names with expected clinical units and attributes:
   - e.g. `Airway: [Patent / Compromised]`
   - e.g. `Breathing: [SpO2 % on RA/O2, Respiratory Rate /min, Bilateral air entry]`
   - e.g. `Circulation: [BP mmHg, Pulse Rate bpm, CRT]`
   - e.g. `Disability: [GCS score E_V_M_, Pupils, Focal neurological deficit]`
6. **Large Narrative Sections (CRITICAL - DO NOT USE SINGLE-WORD OR BLANK PLACEHOLDERS)**:
   For major text blocks like "Brief History", "Clinical Summary", "Course in Hospital", or "Condition at Discharge":
   Provide an informative structural guide inside the placeholder detailing the exact clinical attributes, chronology, and components to cover based on what this hospital format expects.
   - e.g. `[Comprehensive narrative outlining: (1) Patient demographic profile, known comorbidities & baseline status; (2) Presenting complaints with duration and onset; (3) Initial emergency triage vitals and physical findings; (4) Diagnostic investigation highlights (key lab values, imaging, culture reports); (5) Inpatient clinical management, multidisciplinary consultations, and procedural interventions; (6) Clinical progression, complication management, and final resolution/stability at discharge]`
7. **Treatment & Medication Lists**:
   - Inpatient: `[Numbered list of administered medications specifying: Drug Name, Dose, Route, Frequency, and Duration; followed by procedural interventions (e.g. nebulization, oxygen therapy, hemodialysis)]`
   - Discharge / Follow-up: `[Discharge medications with explicit Dosage, Route, Frequency, Duration, and Food timing; followed by specific home care/catheter directives, dietary and fluid restrictions]`
8. **Static Boilerplate**: Keep hospital warning instructions, emergency return signs, and standard advisories verbatim as they appear.
9. **No Repetition Loops**: Do NOT output runaway repeating characters (such as hyphens, dashes, underscores, spaces, or words).

Output ONLY the structured Markdown template. Do NOT wrap output in ```markdown code blocks.
"""

def image_or_pdf_to_markdown(template_items: list[tuple[str, bytes]], input_type: str = "Image") -> tuple[str, dict]:
    """
    Extracts structural Markdown from template images or PDF using Gemini.
    Passes all pages of the template into Gemini for a cohesive multi-page structure.
    """
    if not gemini_service.gemini_configured():
        raise RuntimeError("GEMINI_API_KEY is not configured.")

    client = gemini_service.gemini_client()
    pil_images: list[Image.Image] = []
    start_time = time.time()

    for filename, content in template_items:
        ext = os.path.splitext(filename.lower())[1]

        if ext == ".pdf":
            page_images = gemini_service.pdf_to_page_images(content, base_filename=filename)
            for page_name, img_bytes in page_images:
                pil_images.append(normalize_image(img_bytes))
        else:
            pil_images.append(normalize_image(content))

    if not pil_images:
        return "", {"format": input_type, "duration_seconds": 0, "total_tokens": 0, "cost_usd": 0.0, "cost_inr": 0.0, "char_count": 0}

    contents: list[Any] = [*pil_images, TEMPLATE_STRUCTURAL_EXTRACTION_PROMPT]

    config = types.GenerateContentConfig(
        temperature=0.2,
        max_output_tokens=4096,
        thinking_config=types.ThinkingConfig(thinking_budget=1024),
    )
    response = client.models.generate_content(
        model=gemini_service.model_name(),
        contents=contents,
        config=config,
    )
    duration = time.time() - start_time

    extracted_md = (response.text or "").strip()
    if extracted_md.startswith("```"):
        extracted_md = re.sub(r"^```(?:markdown)?\s*", "", extracted_md)
        extracted_md = re.sub(r"\s*```$", "", extracted_md)

    extracted_md = re.sub(r"-{6,}", "---", extracted_md)
    extracted_md = re.sub(r"_{6,}", "_____", extracted_md)
    extracted_md = re.sub(r"!\[.*?\](?:\(.*?\))?\n?", "", extracted_md).strip()

    usage = getattr(response, "usage_metadata", None)
    prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
    visible_tokens = getattr(usage, "candidates_token_count", 0) or 0
    thought_tokens = getattr(usage, "thoughts_token_count", 0) or 0
    billed_output_tokens = visible_tokens + thought_tokens
    total_tokens = prompt_tokens + billed_output_tokens

    cost_usd, cost_inr = gemini_service.calculate_cost(prompt_tokens, billed_output_tokens)

    metrics = {
        "format": input_type,
        "method": f"Gemini ({gemini_service.model_name()})",
        "duration_seconds": round(duration, 2),
        "pages_processed": len(pil_images),
        "prompt_tokens": prompt_tokens,
        "visible_tokens": visible_tokens,
        "thought_tokens": thought_tokens,
        "output_tokens": billed_output_tokens,
        "total_tokens": total_tokens,
        "cost_usd": round(cost_usd, 6),
        "cost_inr": round(cost_inr, 4),
        "char_count": len(extracted_md),
    }
    return extracted_md, metrics


# ==============================================================================
# 4. UNIFIED PARSER ENTRYPOINT
# ==============================================================================

def parse_template_to_markdown(
    template_items: list[tuple[str, bytes]],
    ai_docx_abstraction: bool = True,
) -> tuple[str, dict]:
    """
    Unified entry point: accepts any list of uploaded template files
    (DOCX, PDF, or Images) and returns the inferred structural Markdown.
    """
    if not template_items:
        return "", {"format": "None", "duration_seconds": 0, "total_tokens": 0, "cost_usd": 0.0, "cost_inr": 0.0, "char_count": 0}

    docx_files = [item for item in template_items if item[0].lower().endswith(".docx")]
    if docx_files:
        return docx_to_template_markdown(docx_files[0][1], ai_abstraction=ai_docx_abstraction)

    pdf_files = [item for item in template_items if item[0].lower().endswith(".pdf")]
    if pdf_files:
        return image_or_pdf_to_markdown(template_items, input_type="PDF")

    return image_or_pdf_to_markdown(template_items, input_type="Images (JPEG/PNG)")
