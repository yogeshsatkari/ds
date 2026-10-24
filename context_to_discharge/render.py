import re
from io import BytesIO
from pathlib import Path
from typing import Optional
from docx import Document
from docx.document import Document as DocumentType
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Inches, Pt, RGBColor

FONT_NAME = "Calibri"
BLACK_COLOR = RGBColor(0, 0, 0)


def apply_cell_padding(cell, top=100, bottom=100, left=150, right=150):
    """Sets inner margins (padding) for a table cell in twips."""
    tcPr = cell._tc.get_or_add_tcPr()
    tcMar = OxmlElement('w:tcMar')
    for margin_name, val in (('top', top), ('bottom', bottom), ('left', left), ('right', right)):
        node = OxmlElement(f'w:{margin_name}')
        node.set(qn('w:w'), str(val))
        node.set(qn('w:type'), 'dxa')
        tcMar.append(node)
    tcPr.append(tcMar)


def apply_inline_formatting(paragraph, text: str, default_size_pt: float = 11):
    """Parses inline **bold** and *italic* formatting."""
    tokens = re.split(r'(\*\*.*?\*\*|\*.*?\*)', text)
    for token in tokens:
        if not token:
            continue
        if token.startswith("**") and token.endswith("**") and len(token) > 4:
            run = paragraph.add_run(token[2:-2])
            run.bold = True
        elif token.startswith("*") and token.endswith("*") and len(token) > 2:
            run = paragraph.add_run(token[1:-1])
            run.italic = True
        else:
            run = paragraph.add_run(token)

        run.font.name = FONT_NAME
        run.font.size = Pt(default_size_pt)
        run.font.color.rgb = BLACK_COLOR


def render_markdown_table(doc: DocumentType, table_lines: list[str]):
    """Parses and renders a GitHub-flavored Markdown table with styling."""
    rows = []
    for line in table_lines:
        clean = line.strip()
        # Skip markdown table divider line (| --- | :---: |)
        if not clean or all(c in "-: |" for c in clean):
            continue
        # Split cells
        cells = [c.strip() for c in clean.strip("|").split("|")]
        rows.append(cells)

    if not rows:
        return

    num_cols = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=num_cols)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"
    table.autofit = True

    flat_table_text = " ".join(" ".join(r) for r in rows).lower()
    is_demographics = any(kw in flat_table_text for kw in (
        "patient", "uhid", "admission", "discharge", "doctor", "consultant", "age / sex", "address", "ipd"
    ))

    for r_idx, row in enumerate(rows):
        padded_row = row + [""] * (num_cols - len(row))

        for c_idx in range(num_cols):
            cell_value = padded_row[c_idx]
            cell = table.cell(r_idx, c_idx)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            apply_cell_padding(cell, top=100, bottom=100, left=130, right=130)

            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.space_before = Pt(0)

            if is_demographics:
                # In demographics table: even columns (0, 2, 4) are key labels, odd are values
                if c_idx % 2 == 0 and cell_value.strip():
                    apply_inline_formatting(p, cell_value, default_size_pt=10)
                    for r in p.runs:
                        r.bold = True
                else:
                    apply_inline_formatting(p, cell_value, default_size_pt=10)
            elif r_idx == 0:
                # Standard data table header row (e.g. Investigations / Lab panels)
                shading_elm = parse_xml(r'<w:shd {} w:fill="F2F4F7"/>'.format(nsdecls('w')))
                cell._tc.get_or_add_tcPr().append(shading_elm)
                apply_inline_formatting(p, cell_value, default_size_pt=10.5)
                for r in p.runs:
                    r.bold = True
            else:
                apply_inline_formatting(p, cell_value, default_size_pt=10)

        # Merge trailing empty cells for single key-value rows like Address: [Patient's Address] | | | | |
        if is_demographics and num_cols > 2:
            non_empty_indices = [i for i, v in enumerate(padded_row) if v.strip()]
            if len(non_empty_indices) == 2 and non_empty_indices == [0, 1]:
                try:
                    table.cell(r_idx, 1).merge(table.cell(r_idx, num_cols - 1))
                except Exception:
                    pass

    # Add small spacing after table
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_before = Pt(3)
    spacer.paragraph_format.space_after = Pt(3)


def build_docx_from_markdown(md_content: str) -> DocumentType:
    """Builds a python-docx Document object from structured Markdown."""
    doc = Document()

    # Set 0.75 inch page margins
    for section in doc.sections:
        section.top_margin = Inches(0.75)
        section.bottom_margin = Inches(0.75)
        section.left_margin = Inches(0.75)
        section.right_margin = Inches(0.75)

    lines = md_content.splitlines()

    table_buffer = []
    in_table = False
    in_header_block = True  # True only for top institutional letterhead (before table, divider, or ###)

    for line in lines:
        stripped = line.strip()

        # 1. Accumulate Markdown Table Lines
        if stripped.startswith("|") and stripped.endswith("|"):
            in_header_block = False  # The moment a table begins, header block is over
            in_table = True
            table_buffer.append(stripped)
            continue
        elif in_table:
            render_markdown_table(doc, table_buffer)
            table_buffer = []
            in_table = False
            in_header_block = False

        if not stripped:
            continue

        # 2. Horizontal Divider Line (--- or ***) -> Marks end of top header block
        if re.match(r"^(\-{3,}|\_{3,}|\*{3,})$", stripped):
            in_header_block = False  # Everything below this line is clinical body
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(4)
            pBdr = OxmlElement('w:pBdr')
            bottom = OxmlElement('w:bottom')
            bottom.set(qn('w:val'), 'single')
            bottom.set(qn('w:sz'), '6')
            bottom.set(qn('w:space'), '1')
            bottom.set(qn('w:color'), 'D0D5DD')
            pBdr.append(bottom)
            p._element.get_or_add_pPr().append(pBdr)
            continue

        # 3. Level 1 Heading (# Hospital Name / Masthead)
        if stripped.startswith("# "):
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(12)
            p.paragraph_format.space_after = Pt(3)
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER if in_header_block else WD_ALIGN_PARAGRAPH.LEFT
            heading_text = re.sub(r"^\*\*|\*\*$", "", stripped[2:].strip())
            run = p.add_run(heading_text)
            run.font.name = FONT_NAME
            run.font.size = Pt(16)
            run.font.color.rgb = BLACK_COLOR
            run.bold = True

        # 4. Level 2 Heading (## Subtitle / DISCHARGE SUMMARY)
        elif stripped.startswith("## "):
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(8)
            p.paragraph_format.space_after = Pt(4)
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER if in_header_block else WD_ALIGN_PARAGRAPH.LEFT
            heading_text = re.sub(r"^\*\*|\*\*$", "", stripped[3:].strip())
            run = p.add_run(heading_text)
            run.font.name = FONT_NAME
            run.font.size = Pt(14)
            run.font.color.rgb = BLACK_COLOR
            run.bold = True

        # 5. Level 3 Heading (### Section Heading) - Always LEFT aligned
        elif stripped.startswith("### "):
            in_header_block = False  # Section heading reached, header block is over
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(10)
            p.paragraph_format.space_after = Pt(3)
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            heading_text = re.sub(r"^\*\*|\*\*$", "", stripped[4:].strip())
            run = p.add_run(heading_text)
            run.font.name = FONT_NAME
            run.font.size = Pt(12.5)
            run.font.color.rgb = BLACK_COLOR
            run.bold = True

        # Level 4 Heading (#### Sub-heading)
        elif stripped.startswith("#### "):
            in_header_block = False
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(6)
            p.paragraph_format.space_after = Pt(2)
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            heading_text = re.sub(r"^\*\*|\*\*$", "", stripped[5:].strip())
            run = p.add_run(heading_text)
            run.font.name = FONT_NAME
            run.font.size = Pt(11.5)
            run.font.color.rgb = BLACK_COLOR
            run.bold = True

        # 6. Bullet Points (* item, - item, • item)
        elif stripped.startswith(("- ", "* ", "• ")):
            in_header_block = False
            content = re.sub(r"^[-*•]\s+", "", stripped)
            p = doc.add_paragraph(style="List Bullet")
            p.paragraph_format.space_before = Pt(1)
            p.paragraph_format.space_after = Pt(2)
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            apply_inline_formatting(p, content)

        # 7. Numbered Lists (1. item)
        elif re.match(r"^\d+\.\s+", stripped):
            in_header_block = False
            content = re.sub(r"^\d+\.\s+", "", stripped)
            p = doc.add_paragraph(style="List Number")
            p.paragraph_format.space_before = Pt(1)
            p.paragraph_format.space_after = Pt(2)
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            apply_inline_formatting(p, content)

        # 8. Regular Paragraph (Address/Contact if in header block, else Left-aligned Clinical Body)
        else:
            # If in header block and it's a placeholder, empty string, or missing value, drop completely with ZERO space
            if in_header_block and (
                not stripped
                or re.search(r"\[.*(address|phone|hospital|not recorded|to be filled).*\]", stripped, re.IGNORECASE)
                or stripped.lower() in ("none", "n/a", "not available", "not recorded", "_____")
            ):
                continue
            p = doc.add_paragraph()
            if in_header_block:
                p.paragraph_format.space_before = Pt(1)
                p.paragraph_format.space_after = Pt(2)
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                apply_inline_formatting(p, stripped, default_size_pt=11)
            else:
                p.paragraph_format.space_before = Pt(1)
                p.paragraph_format.space_after = Pt(3)
                p.alignment = WD_ALIGN_PARAGRAPH.LEFT
                apply_inline_formatting(p, stripped)

    if in_table:
        render_markdown_table(doc, table_buffer)

    return doc


def convert_markdown_to_docx_bytes(md_content: str) -> bytes:
    """Renders Markdown to in-memory DOCX bytes."""
    doc = build_docx_from_markdown(md_content)
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def is_valid_patient_name(name: str) -> bool:
    """Validates that an extracted string is a plausible patient name, not a doctor, attendant, or hospital label."""
    if not name or len(name) < 2:
        return False
    lower = name.lower()
    disallowed_prefixes = (
        "dr.", "dr ", "doctor", "prof.", "nurse", "sister", "staff",
        "attendant", "relative", "guardian", "father", "mother", "spouse",
    )
    if any(lower.startswith(p) for p in disallowed_prefixes):
        return False
    disallowed_keywords = (
        "hospital", "clinic", "nursing home", "department", "signature",
        "relative", "attendant", "consultant", "physician", "surgeon",
        "resident", "anesthetist", "radiologist", "pathologist", "fellow",
    )
    if any(k in lower for k in disallowed_keywords):
        return False
    # Must contain at least one alphabetic character and not be purely numbers/symbols
    if not re.search(r"[a-zA-Z]", name):
        return False
    if re.fullmatch(r"[\d\W_]+", name):
        return False
    return True


def clean_val(raw: str) -> str:
    """Strips markdown formatting, placeholders, and null indicators."""
    val = (raw or "").strip()
    val = val.strip("*_ \t")
    # Drop placeholders like '[Not recorded]', '[to be filled]', '<Name>'
    if (val.startswith("[") and val.endswith("]")) or (val.startswith("<") and val.endswith(">")):
        return ""
    if val.lower() in ("n/a", "na", "nil", "none", "not recorded", "unknown", "-", "--", "not specified"):
        return ""
    return val


def extract_clean_date(raw: str) -> str:
    """Extracts a clean date token (e.g., DD/MM/YYYY, DD-MM-YYYY, DD Mon YYYY) from raw text."""
    val = clean_val(raw)
    if not val:
        return ""
    m = re.search(r"\b(\d{1,2}[-/. ](?:[A-Za-z]{3,9}|\d{1,2})[-/. ]\d{2,4})\b", val)
    if m:
        return m.group(1).strip()
    return val


def extract_clean_id(raw: str) -> str:
    """Cleans up UHID, IPD, or Registration numbers, stripping trailing bed/ward/dated text."""
    val = clean_val(raw)
    if not val:
        return ""
    val = re.split(r"\s+(?:bed|ward|dated|date|time|at)\b", val, flags=re.IGNORECASE)[0].strip()
    return val


def extract_clean_age(raw: str) -> str:
    """Extracts clean age string, verifying reasonable digit bounds."""
    val = clean_val(raw)
    if not val:
        return ""
    m = re.search(r"\b(\d{1,3}\s*(?:years?|yrs?|y|months?|m|days?|d)?)\b", val, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return val


def extract_clean_sex(raw: str) -> str:
    """Extracts and normalizes sex/gender."""
    val = clean_val(raw)
    if not val:
        return ""
    m = re.search(r"\b(Male|Female|Transgender|Other)\b", val, re.IGNORECASE)
    if m:
        return m.group(1).capitalize()
    m_single = re.search(r"\b([MF])\b", val, re.IGNORECASE)
    if m_single:
        return "Male" if m_single.group(1).upper() == "M" else "Female"
    return val


def parse_patient_metadata_from_md(md_content: str, fallback_context_md: Optional[str] = None) -> dict:
    """
    Extracts basic patient info from discharge summary markdown for filenames and dashboard list views.
    If any field is missing and fallback_context_md is provided, performs a strict, conservative
    fallback search against the raw OCR context.
    """
    meta = {
        "patient_name": "",
        "age": "",
        "sex": "",
        "uhid_no": "",
        "date_of_admission": "",
        "date_of_discharge": "",
    }
    if not md_content or not isinstance(md_content, str):
        if not fallback_context_md:
            return meta
        md_content = ""

    try:
        # --- Stage A: Primary extraction from Discharge Summary Markdown ---
        # 1. Patient Name (Table or Key-Value)
        match_name = re.search(r"\|\s*(?:\*\*)?Patient(?:'s)?(?:\s*Name)?\s*:?(?:\*\*)?\s*\|\s*([^|\n]+)\|", md_content, re.IGNORECASE)
        if match_name:
            cand = clean_val(match_name.group(1))
            if is_valid_patient_name(cand):
                meta["patient_name"] = cand
        else:
            match_name_kv = re.search(r"(?:Patient(?:'s)?\s*Name|Name)\s*[:\-]\s*([^\n\r|*]+)", md_content, re.IGNORECASE)
            if match_name_kv:
                cand = clean_val(match_name_kv.group(1))
                if is_valid_patient_name(cand):
                    meta["patient_name"] = cand

        # 2. Age / Sex (Combined or Separate)
        match_age_sex = re.search(r"\|\s*(?:\*\*)?Age\s*/\s*Sex\s*:?(?:\*\*)?\s*\|\s*([^|\n]+)\|", md_content, re.IGNORECASE)
        if match_age_sex:
            val = clean_val(match_age_sex.group(1))
            parts = [p.strip() for p in val.split("/") if p.strip()]
            if len(parts) >= 1:
                meta["age"] = extract_clean_age(parts[0])
            if len(parts) >= 2:
                meta["sex"] = extract_clean_sex(parts[1])
        else:
            # Separate row: | Age: | 74 Years |
            match_age_row = re.search(r"\|\s*(?:\*\*)?Age\s*:?(?:\*\*)?\s*\|\s*([^|\n]+)\|", md_content, re.IGNORECASE)
            if match_age_row:
                meta["age"] = extract_clean_age(match_age_row.group(1))
            else:
                match_age = re.search(r"(?:Age|Age\s*in\s*years)\s*[:\-]\s*([0-9]+(?:\s*(?:years?|yrs?))?)", md_content, re.IGNORECASE)
                if match_age:
                    meta["age"] = extract_clean_age(match_age.group(1))

            # Separate row: | Sex: | Male |
            match_sex_row = re.search(r"\|\s*(?:\*\*)?(?:Sex|Gender)\s*:?(?:\*\*)?\s*\|\s*([^|\n]+)\|", md_content, re.IGNORECASE)
            if match_sex_row:
                meta["sex"] = extract_clean_sex(match_sex_row.group(1))
            else:
                match_sex = re.search(r"(?:Sex|Gender)\s*[:\-]\s*([A-Za-z]+)", md_content, re.IGNORECASE)
                if match_sex:
                    meta["sex"] = extract_clean_sex(match_sex.group(1))

        # 3. UHID / IPD ID (Strict UHID prioritized over secondary IPD ID)
        match_uhid_strict = re.search(r"\|\s*(?:\*\*)?UHID(?:\s*No\.?)?\s*:?(?:\*\*)?\s*\|\s*([^|\n]+)\|", md_content, re.IGNORECASE)
        if match_uhid_strict:
            meta["uhid_no"] = extract_clean_id(match_uhid_strict.group(1))
        else:
            match_uhid_alt = re.search(r"\|\s*(?:\*\*)?(?:IPD\s*ID|CR\s*NO\.?)\s*:?(?:\*\*)?\s*\|\s*([^|\n]+)\|", md_content, re.IGNORECASE)
            if match_uhid_alt:
                meta["uhid_no"] = extract_clean_id(match_uhid_alt.group(1))
            else:
                match_uhid_kv = re.search(r"(?:UHID|IPD\s*ID|CR\s*NO)\s*[:\-]\s*([^\n\r|*]+)", md_content, re.IGNORECASE)
                if match_uhid_kv:
                    meta["uhid_no"] = extract_clean_id(match_uhid_kv.group(1))

        # 4. Date of Admission
        match_doa = re.search(r"\|\s*(?:\*\*)?(?:Date\s*of\s*Admission|Admission\s*Date|DOA)\s*:?(?:\*\*)?\s*\|\s*([^|\n]+)\|", md_content, re.IGNORECASE)
        if match_doa:
            meta["date_of_admission"] = extract_clean_date(match_doa.group(1))
        else:
            match_doa_kv = re.search(r"(?:Date\s*of\s*Admission|DOA|Admission\s*Date)\s*[:\-]\s*([^\n\r|*]+)", md_content, re.IGNORECASE)
            if match_doa_kv:
                meta["date_of_admission"] = extract_clean_date(match_doa_kv.group(1))

        # 5. Date of Discharge
        match_dod = re.search(r"\|\s*(?:\*\*)?(?:Date\s*of\s*Discharge|Discharge\s*Date|DOD)\s*:?(?:\*\*)?\s*\|\s*([^|\n]+)\|", md_content, re.IGNORECASE)
        if match_dod:
            meta["date_of_discharge"] = extract_clean_date(match_dod.group(1))
        else:
            match_dod_kv = re.search(r"(?:Date\s*of\s*Discharge|DOD|Discharge\s*Date)\s*[:\-]\s*([^\n\r|*]+)", md_content, re.IGNORECASE)
            if match_dod_kv:
                meta["date_of_discharge"] = extract_clean_date(match_dod_kv.group(1))

        # --- Stage B: Strict Fallback search in raw OCR context.md for missing values ---
        if fallback_context_md and isinstance(fallback_context_md, str) and fallback_context_md.strip():
            # 1. Fallback Patient Name (Strict: must have 'Patient' or 'Pt', never a generic 'Name')
            if not meta["patient_name"]:
                match_p_name = re.search(
                    r"(?:\|\s*(?:\*\*)?Patient(?:'s)?(?:\s*Name)?\s*:?(?:\*\*)?\s*\|\s*([^|\n]+)\|"
                    r"|(?:Patient(?:'s)?\s*Name|Name\s+of\s+(?:the\s+)?Patient|Pt\.?\s*Name)\s*[:\-]\s*([^\n\r|*]+))",
                    fallback_context_md,
                    re.IGNORECASE,
                )
                if match_p_name:
                    cand = clean_val(match_p_name.group(1) or match_p_name.group(2))
                    if is_valid_patient_name(cand):
                        meta["patient_name"] = cand

            # 2. Fallback Age / Sex
            if not meta["age"] or not meta["sex"]:
                match_p_age_sex = re.search(
                    r"(?:\|\s*(?:\*\*)?Age\s*/\s*Sex\s*:?(?:\*\*)?\s*\|\s*([^|\n]+)\|"
                    r"|(?:Age\s*[/|]\s*(?:Sex|Gender))\s*[:\-]\s*([^\n\r|*]+))",
                    fallback_context_md,
                    re.IGNORECASE,
                )
                if match_p_age_sex:
                    raw_val = match_p_age_sex.group(1) or match_p_age_sex.group(2)
                    parts = [p.strip() for p in raw_val.split("/") if p.strip()]
                    if not meta["age"] and len(parts) >= 1:
                        meta["age"] = extract_clean_age(parts[0])
                    if not meta["sex"] and len(parts) >= 2:
                        meta["sex"] = extract_clean_sex(parts[1])

                if not meta["age"]:
                    match_p_age = re.search(
                        r"(?:Age|Age\s*in\s*years)\s*[:\-]\s*([0-9]+\s*(?:years?|yrs?|y|months?|m|days?|d)?)",
                        fallback_context_md,
                        re.IGNORECASE,
                    )
                    if match_p_age:
                        meta["age"] = extract_clean_age(match_p_age.group(1))

                if not meta["sex"]:
                    match_p_sex = re.search(
                        r"(?:Sex|Gender)\s*[:\-]\s*(Male|Female|M|F|Transgender|Other)\b",
                        fallback_context_md,
                        re.IGNORECASE,
                    )
                    if match_p_sex:
                        meta["sex"] = extract_clean_sex(match_p_sex.group(1))

            # 3. Fallback UHID / IPD / Registration No
            if not meta["uhid_no"]:
                match_p_uhid = re.search(
                    r"(?:\|\s*(?:\*\*)?(?:UHID|IPD|CR\s*NO)\S*\s*:?(?:\*\*)?\s*\|\s*([^|\n]+)\|"
                    r"|(?:UHID(?:\s*No\.?)?|IPD\s*(?:ID|No\.?)|CR\s*NO\.?|MRN|Reg(?:istration)?\.?\s*(?:No\.?|ID))\s*[:\-]\s*([^\n\r|*]+))",
                    fallback_context_md,
                    re.IGNORECASE,
                )
                if match_p_uhid:
                    cand = extract_clean_id(match_p_uhid.group(1) or match_p_uhid.group(2))
                    if cand and len(cand) >= 2:
                        meta["uhid_no"] = cand

            # 4. Fallback Date of Admission
            if not meta["date_of_admission"]:
                match_p_doa = re.search(
                    r"(?:Date\s*of\s*Admission|Admission\s*Date|DOA|Admitted\s*on)\s*[:\-]\s*([^\n\r|*]+)",
                    fallback_context_md,
                    re.IGNORECASE,
                )
                if match_p_doa:
                    cand = extract_clean_date(match_p_doa.group(1))
                    if cand:
                        meta["date_of_admission"] = cand

            # 5. Fallback Date of Discharge
            if not meta["date_of_discharge"]:
                match_p_dod = re.search(
                    r"(?:Date\s*of\s*Discharge|Discharge\s*Date|DOD|Discharged\s*on)\s*[:\-]\s*([^\n\r|*]+)",
                    fallback_context_md,
                    re.IGNORECASE,
                )
                if match_p_dod:
                    cand = extract_clean_date(match_p_dod.group(1))
                    if cand:
                        meta["date_of_discharge"] = cand

    except Exception:
        pass

    return meta
