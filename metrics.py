from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import config


@dataclass
class PageMetrics:
    page_num: int
    filename: str
    status: str = "success"  # "success" | "failed"
    attempts: int = 1
    retries: int = 0
    duration_seconds: float = 0.0
    prompt_tokens: int = 0
    visible_tokens: int = 0
    thought_tokens: int = 0
    billed_output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cost_inr: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Stage1OCRMetrics:
    status: str = "completed"  # "completed" | "failed" | "partial_success"
    total_pages: int = 0
    successful_pages: int = 0
    failed_pages: int = 0
    total_retries: int = 0
    duration_seconds: float = 0.0
    prompt_tokens: int = 0
    visible_tokens: int = 0
    thought_tokens: int = 0
    billed_output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cost_inr: float = 0.0
    pages: list[dict] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Stage2SynthesisMetrics:
    status: str = "success"  # "success" | "failed"
    attempts: int = 1
    retries: int = 0
    duration_seconds: float = 0.0
    prompt_tokens: int = 0
    visible_tokens: int = 0
    thought_tokens: int = 0
    billed_output_tokens: int = 0
    total_tokens: int = 0
    output_char_count: int = 0
    cost_usd: float = 0.0
    cost_inr: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SummaryMetrics:
    status: str = "completed"  # "completed" | "failed"
    total_duration_seconds: float = 0.0
    total_tokens: int = 0
    prompt_tokens: int = 0
    visible_tokens: int = 0
    thought_tokens: int = 0
    billed_output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_cost_inr: float = 0.0
    model: str = "gemini-2.5-flash"
    successful_stages: list[str] = field(default_factory=list)
    failed_stage: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================================
# Reusable Templates & Factory Functions
# ============================================================================

def make_failed_page_metrics(
    page_num: int,
    filename: str,
    error: str,
    attempts: int = 4,
    duration_seconds: float = 0.0,
) -> dict:
    """Template for recording a page OCR failure."""
    return PageMetrics(
        page_num=page_num,
        filename=filename,
        status="failed",
        attempts=attempts,
        retries=max(0, attempts - 1),
        duration_seconds=round(duration_seconds, 2),
        error=str(error),
    ).to_dict()


def make_successful_page_metrics(
    page_num: int,
    filename: str,
    prompt_tokens: int,
    visible_tokens: int,
    thought_tokens: int,
    duration_seconds: float,
    attempts: int = 1,
) -> dict:
    """Template for recording a successful page OCR run."""
    billed_output = visible_tokens + thought_tokens
    total_tokens = prompt_tokens + billed_output
    cost_usd, cost_inr = config.calculate_token_cost(prompt_tokens, billed_output)

    return PageMetrics(
        page_num=page_num,
        filename=filename,
        status="success",
        attempts=attempts,
        retries=max(0, attempts - 1),
        duration_seconds=round(duration_seconds, 2),
        prompt_tokens=prompt_tokens,
        visible_tokens=visible_tokens,
        thought_tokens=thought_tokens,
        billed_output_tokens=billed_output,
        total_tokens=total_tokens,
        cost_usd=round(cost_usd, 6),
        cost_inr=round(cost_inr, 4),
    ).to_dict()


def make_failed_synthesis_metrics(
    error: str,
    duration_seconds: float = 0.0,
    attempts: int = 4,
) -> dict:
    """Template for recording a Stage 2 synthesis failure."""
    return Stage2SynthesisMetrics(
        status="failed",
        attempts=attempts,
        retries=max(0, attempts - 1),
        duration_seconds=round(duration_seconds, 2),
        error=str(error),
    ).to_dict()


def make_successful_synthesis_metrics(
    prompt_tokens: int,
    visible_tokens: int,
    thought_tokens: int,
    duration_seconds: float,
    output_char_count: int,
    attempts: int = 1,
) -> dict:
    """Template for recording a successful Stage 2 synthesis run."""
    billed_output = visible_tokens + thought_tokens
    total_tokens = prompt_tokens + billed_output
    cost_usd, cost_inr = config.calculate_token_cost(prompt_tokens, billed_output)

    return Stage2SynthesisMetrics(
        status="success",
        attempts=attempts,
        retries=max(0, attempts - 1),
        duration_seconds=round(duration_seconds, 2),
        prompt_tokens=prompt_tokens,
        visible_tokens=visible_tokens,
        thought_tokens=thought_tokens,
        billed_output_tokens=billed_output,
        total_tokens=total_tokens,
        output_char_count=output_char_count,
        cost_usd=round(cost_usd, 6),
        cost_inr=round(cost_inr, 4),
    ).to_dict()


def build_pipeline_metrics(
    discharge_type: str,
    stage_1_ocr: dict,
    stage_2_synthesis: Optional[dict] = None,
    total_duration_seconds: float = 0.0,
    overall_status: str = "completed",
    error: Optional[str] = None,
    failed_stage: Optional[str] = None,
    model: str = "gemini-2.5-flash",
) -> dict:
    """Reusable template assembling overall pipeline metrics from stage metrics."""
    synthesis = stage_2_synthesis or {}

    total_prompt = stage_1_ocr.get("prompt_tokens", 0) + synthesis.get("prompt_tokens", 0)
    total_visible = stage_1_ocr.get("visible_tokens", 0) + synthesis.get("visible_tokens", 0)
    total_thought = stage_1_ocr.get("thought_tokens", 0) + synthesis.get("thought_tokens", 0)
    total_billed = stage_1_ocr.get("billed_output_tokens", 0) + synthesis.get("billed_output_tokens", 0)
    total_tokens = stage_1_ocr.get("total_tokens", 0) + synthesis.get("total_tokens", 0)
    total_usd = round(stage_1_ocr.get("cost_usd", 0.0) + synthesis.get("cost_usd", 0.0), 6)
    total_inr = round(stage_1_ocr.get("cost_inr", 0.0) + synthesis.get("cost_inr", 0.0), 4)

    successful_stages = []
    if stage_1_ocr.get("status") in ("completed", "partial_success"):
        successful_stages.append("stage_1_ocr")
    if synthesis.get("status") == "success":
        successful_stages.append("stage_2_synthesis")

    summary = SummaryMetrics(
        status=overall_status,
        total_duration_seconds=round(total_duration_seconds, 2),
        total_tokens=total_tokens,
        prompt_tokens=total_prompt,
        visible_tokens=total_visible,
        thought_tokens=total_thought,
        billed_output_tokens=total_billed,
        total_cost_usd=total_usd,
        total_cost_inr=total_inr,
        model=model,
        successful_stages=successful_stages,
        failed_stage=failed_stage,
    )

    stages_dict = {"stage_1_ocr": stage_1_ocr}
    if stage_2_synthesis is not None:
        stages_dict["stage_2_synthesis"] = stage_2_synthesis

    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "discharge_type": discharge_type,
        "status": overall_status,
        "error": error,
        "summary": summary.to_dict(),
        "stages": stages_dict,
    }
