import re
import time
from typing import Optional
from google.genai import types

import config
import gemini_service
import metrics as metrics_lib
from context_to_discharge.prompts import build_synthesis_prompt


def default_synthesis_thinking_budget() -> int:
    return getattr(config, "GEMINI_SYNTHESIS_THINKING_BUDGET", 2048)


def synthesize_discharge_summary(
    clinical_text: str,
    template_md: Optional[str] = None,
    max_retries: int = 4,
    thinking_budget: Optional[int] = None,
) -> tuple[str, dict]:
    """
    Synthesizes a discharge summary from clinical text:
    - If template_md is provided, mirrors that target hospital template.
    - If template_md is None, uses the standardized institutional prompt.
    Returns (summary_markdown, synthesis_metrics).
    """
    clinical_text = clinical_text.strip()
    if not clinical_text:
        raise ValueError("Clinical context markdown is empty.")

    if thinking_budget is None:
        thinking_budget = default_synthesis_thinking_budget()

    client = gemini_service.gemini_client()
    prompt = build_synthesis_prompt(clinical_text, template_md=template_md)

    last_error: BaseException = RuntimeError("Gemini discharge summary synthesis failed.")
    start_time = time.time()

    for attempt in range(1, max_retries + 1):
        try:
            gen_config = types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=4096,
                thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
                safety_settings=gemini_service.clinical_safety_settings(),
            )
            response = client.models.generate_content(
                model=gemini_service.model_name(),
                contents=[prompt],
                config=gen_config,
            )
            duration = time.time() - start_time
            result = (response.text or "").strip()

            if result.startswith("```"):
                result = re.sub(r"^```(?:markdown)?\s*", "", result)
                result = re.sub(r"\s*```$", "", result)

            if not result:
                raise RuntimeError("Gemini returned empty discharge summary markdown.")

            usage = getattr(response, "usage_metadata", None)
            prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
            visible_tokens = getattr(usage, "candidates_token_count", 0) or 0
            thought_tokens = getattr(usage, "thoughts_token_count", 0) or 0

            synthesis_metrics = metrics_lib.make_successful_synthesis_metrics(
                prompt_tokens=prompt_tokens,
                visible_tokens=visible_tokens,
                thought_tokens=thought_tokens,
                duration_seconds=duration,
                output_char_count=len(result),
                attempts=attempt,
            )
            return result, synthesis_metrics

        except Exception as exc:
            last_error = exc
            err = str(exc)
            if "429" in err or "RESOURCE_EXHAUSTED" in err or "getaddrinfo" in err:
                time.sleep(6 * attempt)
            elif attempt < max_retries:
                time.sleep(3)

    raise last_error
