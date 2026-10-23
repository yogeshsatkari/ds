import time
from dataclasses import dataclass
from typing import Optional

import gemini_service
import metrics as metrics_lib
from context_to_discharge.extract import synthesize_discharge_summary
from context_to_discharge.render import convert_markdown_to_docx_bytes, parse_patient_metadata_from_md
from context_to_discharge.template_parser import parse_template_to_markdown


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
    summary_md: str
    patient_name: str = ""
    metadata: Optional[dict] = None
    metrics: Optional[dict] = None
    template_md: Optional[str] = None


def run_extraction_pipeline(
    image_items: list[tuple[str, bytes]],
    template_items: Optional[list[tuple[str, bytes]]] = None,
    discharge_type: str = "standard",
    preparsed_template_md: Optional[str] = None,
) -> ExtractionResult:
    """
    Executes clinical OCR and synthesizes a discharge summary DOCX.
    - If preparsed_template_md is provided: uses pre-parsed doctor profile template Markdown.
    - If template_items is provided: extracts the custom template into Markdown and guides synthesis.
    - If template_items is empty/None: uses the institutional standardized discharge template.
    """
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

    # 2. Ingest Template (Stage 1b - if provided)
    template_md: Optional[str] = None
    template_metrics: Optional[dict] = None

    if preparsed_template_md:
        template_md = preparsed_template_md
        discharge_type_norm = "custom"
        template_metrics = {
            "format": "Pre-parsed Doctor Profile Template",
            "method": "R2 Cached Markdown",
            "status": "reused",
            "duration_seconds": 0.0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "cost_inr": 0.0,
        }
    elif template_items:
        try:
            template_md, template_metrics = parse_template_to_markdown(template_items)
            discharge_type_norm = "custom"
        except Exception as tmpl_exc:
            # If template parsing encounters an issue, log and fall back to standard
            template_md = None
            template_metrics = {"status": "failed", "error": str(tmpl_exc)}
            discharge_type_norm = "standard"
    elif discharge_type_norm == "custom":
        # Doctor selected custom but provided no template files -> gracefully fall back to standard
        discharge_type_norm = "standard"

    # 3. Stage 2: Synthesis & Direct Word Document Rendering
    try:
        summary_md, synthesis_metrics = synthesize_discharge_summary(
            clinical_text=context_md,
            template_md=template_md,
        )
        docx_bytes = convert_markdown_to_docx_bytes(summary_md)
        try:
            meta = parse_patient_metadata_from_md(summary_md, fallback_context_md=context_md)
        except Exception:
            meta = {}
        patient_name = (meta.get("patient_name") or "").strip()

        metadata = {
            "patient_name": patient_name,
            "age": str(meta.get("age") or "").strip(),
            "sex": str(meta.get("sex") or "").strip(),
            "uhid_no": str(meta.get("uhid_no") or "").strip(),
            "date_of_admission": str(meta.get("date_of_admission") or "").strip(),
            "date_of_discharge": str(meta.get("date_of_discharge") or "").strip(),
            "discharge_type": discharge_type_norm,
        }
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
            stage_template_extraction=template_metrics,
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

    # 4. Overall Pipeline Success
    pipeline_duration = time.time() - pipeline_start
    metrics = metrics_lib.build_pipeline_metrics(
        discharge_type=discharge_type_norm,
        stage_1_ocr=ocr_metrics,
        stage_2_synthesis=synthesis_metrics,
        stage_template_extraction=template_metrics,
        total_duration_seconds=pipeline_duration,
        overall_status="completed",
        model=gemini_service.model_name(),
    )

    return ExtractionResult(
        context_md=context_md,
        docx_bytes=docx_bytes,
        discharge_type=discharge_type_norm,
        summary_md=summary_md,
        patient_name=patient_name,
        metadata=metadata,
        metrics=metrics,
        template_md=template_md,
    )
