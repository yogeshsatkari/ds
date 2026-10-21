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


def normalize_template_masthead(md: str) -> str:
    """
    Cleans up the top masthead of extracted template markdown:
    1. Strips OCR glyph/logo artifacts (e.g. 'vAll India...' -> 'All India...').
    2. Deduplicates consecutive repeated hospital title lines (e.g. bilingual OCR duplicates).
    3. Ensures the official hospital name is formatted as a prominent Level 1 Heading ('# <Hospital Name>').
    If any error occurs, safely returns the original markdown to preserve normal flow.
    """
    if not md:
        return md

    try:
        lines = md.splitlines()
        first_idx = None
        for i, line in enumerate(lines):
            if line.strip():
                first_idx = i
                break

        if first_idx is None:
            return md

        # Clean leading single-letter / symbol OCR logo artifacts on line 1 (e.g. 'vAll' -> 'All')
        clean_line1 = re.sub(r"^[a-z\W_]+(?=[A-Z])", "", lines[first_idx].strip())
        raw_title1 = re.sub(r"^[#\s*]+", "", clean_line1).strip()
        raw_title1 = re.sub(r"[*]+$", "", raw_title1).strip()

        # Check next non-empty line
        second_idx = None
        for j in range(first_idx + 1, len(lines)):
            if lines[j].strip():
                second_idx = j
                break

        if second_idx is not None:
            clean_line2 = lines[second_idx].strip()
            raw_title2 = re.sub(r"^[#\s*]+", "", clean_line2).strip()
            raw_title2 = re.sub(r"[*]+$", "", raw_title2).strip()

            # If line 2 is identical to line 1 (e.g. bilingual translation or logo subcaption), drop line 2
            if raw_title1 and raw_title2 and raw_title1.lower() == raw_title2.lower():
                lines.pop(second_idx)

        # Ensure the top hospital line is formatted as '# <Hospital Name>'
        # (unless it's already DISCHARGE SUMMARY, a table, or a divider)
        if raw_title1 and not raw_title1.lower().startswith("discharge summary") and not raw_title1.startswith(("|", "-", "*")):
            lines[first_idx] = f"# {raw_title1}"

        return "\n".join(lines)
    except Exception:
        # Fail-safe: if any unexpected issue arises, return original markdown to preserve normal flow
        return md


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

1. **Hospital Masthead & Branding (CRITICAL)**: 
   - The official hospital name MUST be the very first line of the document, formatted as a Level 1 Heading: `# <Hospital Name>` (e.g. `# All India Institute of Medical Sciences Rishikesh`).
   - **Deduplication**: Hospital letterheads often repeat the institution name (e.g. Hindi on line 1, English on line 2, or repeating under a logo). Strip any OCR logo artifacts (such as leading 'v', '✓', bullets, or icons). Output the clean English hospital name EXACTLY ONCE as `# <Hospital Name>`. Never output duplicate or bilingual repeated lines of the hospital name.
   - **Document Title Normalization**: Hospital samples often include transient administrative stamps, diagonal watermarks, or draft disclaimers (e.g. `(DRAFT - verification pending)`, `(DRAFT)`, `PROVISIONAL`, `DUPLICATE`, `COPY`). Strip all such transient status labels and watermarks. The document title heading must always be standardized cleanly as `## DISCHARGE SUMMARY` (or specific department discharge title).
   - Capture genuine institutional address, contact phone/helpline, website, accreditation badges (e.g. NABH/JCI), and department headings verbatim. Focus purely on clean textual branding (do NOT include image tags, logo placeholders, or markdown image syntax such as `![Hospital Logo]` or `![Logo]`).
2. **Headings**: Preserve all section headings (##, ###).
3. **Tables & Header Boundary**: 
   - Preserve all markdown tables (| Col 1 | Col 2 |) with exact columns and minimal separators (| --- | --- |).
   - Place a horizontal divider rule (`---`) immediately following the demographics table / header block, cleanly separating the institutional header from the clinical sections.
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
10. **Ignore Printout Artifacts**: Ignore physical print artifacts, diagonal background watermarks, barcode numbers, page counters ('Page 1 of 2'), and printer audit timestamps that belong to the sample printout rather than the reusable hospital format.

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
        return normalize_template_masthead(raw_md), local_metrics

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
    extracted_md = normalize_template_masthead(extracted_md)

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

1. **Hospital Masthead & Branding (CRITICAL)**: 
   - The official hospital name MUST be the very first line of the document, formatted as a Level 1 Heading: `# <Hospital Name>` (e.g. `# All India Institute of Medical Sciences Rishikesh`).
   - **Deduplication**: Hospital letterheads often repeat the institution name (e.g. Hindi on line 1, English on line 2, or repeating under a logo). Strip any OCR logo artifacts (such as leading 'v', '✓', bullets, or icons). Output the clean English hospital name EXACTLY ONCE as `# <Hospital Name>`. Never output duplicate or bilingual repeated lines of the hospital name.
   - **Document Title Normalization**: Hospital samples often include transient administrative stamps, diagonal watermarks, or draft disclaimers (e.g. `(DRAFT - verification pending)`, `(DRAFT)`, `PROVISIONAL`, `DUPLICATE`, `COPY`). Strip all such transient status labels and watermarks. The document title heading must always be standardized cleanly as `## DISCHARGE SUMMARY` (or specific department discharge title).
   - Capture genuine institutional address, contact phone/helpline, website, accreditation badges (e.g. NABH/JCI), and department headings verbatim. Focus purely on clean textual branding (do NOT include image tags, logo placeholders, or markdown image syntax such as `![Hospital Logo]` or `![Logo]`).
2. **Headings**: Structure section titles as Markdown headings (##, ###).
3. **Tables & Header Boundary**: 
   - Preserve all tables exactly using clean Markdown table syntax (| Col 1 | Col 2 |). The separator row MUST contain exactly three hyphens per column: `| --- | --- |`. Never output more than three hyphens in a cell.
   - Place a horizontal divider rule (`---`) immediately following the demographics table / header block, cleanly separating the institutional header from the clinical sections.
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
10. **Ignore Printout Artifacts**: Ignore physical print artifacts, diagonal background watermarks, barcode numbers, page counters ('Page 1 of 2'), and printer audit timestamps that belong to the sample printout rather than the reusable hospital format.

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
    extracted_md = normalize_template_masthead(extracted_md)

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
        md, metrics = docx_to_template_markdown(docx_files[0][1], ai_abstraction=ai_docx_abstraction)
    elif any(item[0].lower().endswith(".pdf") for item in template_items):
        md, metrics = image_or_pdf_to_markdown(template_items, input_type="PDF")
    else:
        md, metrics = image_or_pdf_to_markdown(template_items, input_type="Images (JPEG/PNG)")

    md = normalize_template_masthead(md)
    return md, metrics
