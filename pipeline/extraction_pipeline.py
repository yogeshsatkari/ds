from dataclasses import dataclass
from typing import Optional

import gemini_service
from discharge_types.custom_data_to_docx.extract_json import extract_json_context_from_markdown
from discharge_types.custom_data_to_docx.render import render_discharge_summary_from_template
from discharge_types.standard_md_to_docx.extract_md import extract_discharge_summary_md_from_markdown
from discharge_types.standard_md_to_docx.render import convert_markdown_to_docx_bytes, parse_patient_metadata_from_md



@dataclass
class ExtractionResult:
    context_md: str
    docx_bytes: bytes
    discharge_type: str
    context_json: Optional[dict] = None
    summary_md: Optional[str] = None
    patient_name: str = ""
    metadata: Optional[dict] = None


def run_extraction_pipeline(
    image_items: list[tuple[str, bytes]],
    discharge_type: str = "standard",
) -> ExtractionResult:
    """Executes clinical OCR and generates DOCX based on discharge_type ('standard' or 'custom')."""
    # 1. Page-by-page OCR context extraction
    context_md = gemini_service.run_extraction(image_items)

    discharge_type_norm = (discharge_type or "standard").strip().lower()

    if discharge_type_norm == "custom":
        # Custom Flow: Markdown -> Structured JSON -> ds-template.docx
        context_json = extract_json_context_from_markdown(context_md)
        docx_bytes = render_discharge_summary_from_template(context_json)
        patient_name = (context_json.get("patient_name") or "").strip()

        metadata = {
            "patient_name": patient_name,
            "age": str(context_json.get("age") or "").strip(),
            "sex": str(context_json.get("sex") or "").strip(),
            "uhid_no": str(context_json.get("uhid_no") or "").strip(),
            "date_of_admission": str(context_json.get("date_of_admission") or "").strip(),
            "date_of_discharge": str(context_json.get("date_of_discharge") or "").strip(),
            "discharge_type": "custom",
        }

        return ExtractionResult(
            context_md=context_md,
            docx_bytes=docx_bytes,
            discharge_type="custom",
            context_json=context_json,
            summary_md=None,
            patient_name=patient_name,
            metadata=metadata,
        )
    else:
        # Standard Flow: Markdown -> discharge-summary.md -> direct Word document
        summary_md = extract_discharge_summary_md_from_markdown(context_md)
        docx_bytes = convert_markdown_to_docx_bytes(summary_md)
        meta = parse_patient_metadata_from_md(summary_md)
        patient_name = (meta.get("patient_name") or "").strip()

        metadata = {
            "patient_name": patient_name,
            "age": str(meta.get("age") or "").strip(),
            "sex": str(meta.get("sex") or "").strip(),
            "uhid_no": str(meta.get("uhid_no") or "").strip(),
            "date_of_admission": str(meta.get("date_of_admission") or "").strip(),
            "date_of_discharge": str(meta.get("date_of_discharge") or "").strip(),
            "discharge_type": "standard",
        }

        return ExtractionResult(
            context_md=context_md,
            docx_bytes=docx_bytes,
            discharge_type="standard",
            context_json=None,
            summary_md=summary_md,
            patient_name=patient_name,
            metadata=metadata,
        )

