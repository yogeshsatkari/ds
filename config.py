import os
from dotenv import load_dotenv

load_dotenv()

# Gemini AI Model Configuration
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# Gemini Thinking Budgets (Tokens allocated for internal reasoning)
# OCR: 1024 preserves 100% handwriting recognition while preventing excessive thought token spend
GEMINI_OCR_THINKING_BUDGET = int(os.environ.get("GEMINI_OCR_THINKING_BUDGET", "1024"))

# Synthesis: 2048 provides ample reasoning headroom for multi-day inpatient hospital records
GEMINI_SYNTHESIS_THINKING_BUDGET = int(os.environ.get("GEMINI_SYNTHESIS_THINKING_BUDGET", "2048"))

# Stage 1 OCR Safeguards
# Max tokens per page: prevents runaway repetition loops (a dense page is ~1,000-1,500 tokens)
GEMINI_OCR_MAX_OUTPUT_TOKENS = int(os.environ.get("GEMINI_OCR_MAX_OUTPUT_TOKENS", "3000"))

# Per-page HTTP timeout in milliseconds (60 seconds)
GEMINI_OCR_PAGE_TIMEOUT_MS = int(os.environ.get("GEMINI_OCR_PAGE_TIMEOUT_MS", "60000"))

# Gemini Cost & Currency Settings
GEMINI_INPUT_COST_PER_MILLION = float(os.environ.get("GEMINI_INPUT_COST_PER_MILLION", "0.30"))
GEMINI_OUTPUT_COST_PER_MILLION = float(os.environ.get("GEMINI_OUTPUT_COST_PER_MILLION", "2.50"))

# Currency Conversion (defaulting to current ~86.50 INR / USD)
USD_TO_INR_RATE = float(os.environ.get("USD_TO_INR_RATE", "86.5"))


def calculate_token_cost(prompt_tokens: int, billed_output_tokens: int) -> tuple[float, float]:
    """Calculates USD and INR costs based on current pricing configuration."""
    cost_usd = (prompt_tokens * (GEMINI_INPUT_COST_PER_MILLION / 1e6)) + (
        billed_output_tokens * (GEMINI_OUTPUT_COST_PER_MILLION / 1e6)
    )
    cost_inr = cost_usd * USD_TO_INR_RATE
    return cost_usd, cost_inr
