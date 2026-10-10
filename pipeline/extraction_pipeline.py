import time
from dataclasses import dataclass
from typing import Optional

import gemini_service
import metrics as metrics_lib
from discharge_types.custom_data_to_docx.extract_json import extract_json_context_from_markdown
from discharge_types.custom_data_to_docx.render import render_discharge_summary_from_template
from discharge_types.standard_md_to_docx.extract_md import extract_discharge_summary_md_from_markdown
from discharge_types.standard_md_to_docx.render import convert_markdown_to_docx_bytes, parse_patient_metadata_from_md



class ExtractionPipelineError(Exception):
    """Raised when an extraction stage fails, carrying partial metrics and context."""

    def __init__(
        self,
        message: str,
        metrics: Optional[dict] = None,
        partial_context_md: Optional[str] = None,
    ):
        super().__init__(message)
        self.metrics = metrics
        self.partial_context_md = partial_context_md


@dataclass
class ExtractionResult:
    context_md: str
    docx_bytes: bytes
    discharge_type: str
    context_json: Optional[dict] = None
    summary_md: Optional[str] = None
    patient_name: str = ""
    metadata: Optional[dict] = None
    metrics: Optional[dict] = None


def run_extraction_pipeline(
    image_items: list[tuple[str, bytes]],
    discharge_type: str = "standard",
) -> ExtractionResult:
    """Executes clinical OCR and generates DOCX based on discharge_type ('standard' or 'custom')."""
    pipeline_start = time.time()
    discharge_type_norm = (discharge_type or "standard").strip().lower()

    # 1. Page-by-page OCR context extraction with metrics (Stage 1)
    context_md, ocr_metrics = gemini_service.run_extraction(image_items)

    # Check if all pages failed OCR
    if ocr_metrics.get("total_pages", 0) > 0 and ocr_metrics.get("successful_pages", 0) == 0:
        pipeline_duration = time.time() - pipeline_start
        failure_metrics = metrics_lib.build_pipeline_metrics(
            discharge_type=discharge_type_norm,
            stage_1_ocr=ocr_metrics,
            total_duration_seconds=pipeline_duration,
            overall_status="failed",
            error="All pages failed OCR extraction.",
            failed_stage="stage_1_ocr",
            model=gemini_service.model_name(),
        )
        raise ExtractionPipelineError(
            message="All uploaded pages failed OCR extraction.",
            metrics=failure_metrics,
            partial_context_md=context_md,
        )

    # 2. Stage 2: Synthesis
    try:
        if discharge_type_norm == "custom":
            # Custom Flow: Markdown -> Structured JSON -> ds-template.docx
            context_json, synthesis_metrics = extract_json_context_from_markdown(context_md)
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
            summary_md = None
        else:
            # Standard Flow: Markdown -> discharge-summary.md -> direct Word document
            summary_md, synthesis_metrics = extract_discharge_summary_md_from_markdown(context_md)
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
            context_json = None
    except Exception as stage2_exc:
        pipeline_duration = time.time() - pipeline_start
        synthesis_failed_metrics = metrics_lib.make_failed_synthesis_metrics(
            error=str(stage2_exc),
            duration_seconds=max(0.0, pipeline_duration - ocr_metrics.get("duration_seconds", 0)),
        )
        failure_metrics = metrics_lib.build_pipeline_metrics(
            discharge_type=discharge_type_norm,
            stage_1_ocr=ocr_metrics,
            stage_2_synthesis=synthesis_failed_metrics,
            total_duration_seconds=pipeline_duration,
            overall_status="failed",
            error=f"Stage 2 synthesis failed: {stage2_exc}",
            failed_stage="stage_2_synthesis",
            model=gemini_service.model_name(),
        )
        raise ExtractionPipelineError(
            message=f"Stage 2 synthesis failed: {stage2_exc}",
            metrics=failure_metrics,
            partial_context_md=context_md,
        ) from stage2_exc

    # 3. Overall Pipeline Success
    pipeline_duration = time.time() - pipeline_start
    metrics = metrics_lib.build_pipeline_metrics(
        discharge_type=discharge_type_norm,
        stage_1_ocr=ocr_metrics,
        stage_2_synthesis=synthesis_metrics,
        total_duration_seconds=pipeline_duration,
        overall_status="completed",
        model=gemini_service.model_name(),
    )

    return ExtractionResult(
        context_md=context_md,
        docx_bytes=docx_bytes,
        discharge_type=discharge_type_norm,
        context_json=context_json,
        summary_md=summary_md,
        patient_name=patient_name,
        metadata=metadata,
        metrics=metrics,
    )

