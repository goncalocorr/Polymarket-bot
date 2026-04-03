"""
Structured prompt templates for Claude market analysis.
Ported from Polymarket/agents superforecaster patterns.
"""


SYSTEM_SUPERFORECASTER = """You are a Superforecaster — someone with a proven track record \
of making accurate predictions about future events. You use systematic analysis, \
avoid cognitive biases, and calibrate probabilities carefully.

Your methodology:
1. Decompose the question into smaller, measurable components.
2. Identify the relevant base rate from historical data or analogous situations.
3. List factors that push the probability up or down, weighting each by evidence strength.
4. Think probabilistically — express uncertainty, avoid binary thinking.
5. Consider the outside view: what would a naive statistical model predict?
6. Check for anchoring bias: is the current market price pulling your estimate?"""


def _sanitize_external(text: str, max_len: int = 200, allow_newlines: bool = False) -> str:
    """Strip control characters, XML tags, and truncate external data to prevent prompt injection."""
    import re
    if allow_newlines:
        cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    else:
        cleaned = text.replace("\r", " ").replace("\n", " ").strip()
    # Remove angle brackets to prevent XML tag injection in structured prompts
    cleaned = re.sub(r"[<>]", "", cleaned)
    return cleaned[:max_len]


def superforecaster_prompt(
    question: str,
    price: float,
    volume: float,
    end_date: str,
    external_info: str,
    news_section: str,
) -> str:
    """
    Build a structured superforecaster analysis prompt.
    Returns the user message; pair with SYSTEM_SUPERFORECASTER as system prompt.
    """
    safe_question = _sanitize_external(question, max_len=200)
    safe_end_date = _sanitize_external(end_date, max_len=30)
    safe_external = _sanitize_external(external_info, max_len=500, allow_newlines=True)
    safe_news = _sanitize_external(news_section, max_len=500, allow_newlines=True)

    return f"""Analyse this prediction market using the superforecaster methodology.

<market_data>
Market question: {safe_question}
Current Polymarket price: {price:.4f} (implies {price * 100:.1f}% probability)
24h volume: ${volume:,.0f}
Resolution date: {safe_end_date}

{safe_external}

{safe_news}
</market_data>

Follow these steps in your reasoning (think step-by-step internally, but only output JSON):

Step 1 — Decompose: What sub-questions determine the outcome?
Step 2 — Base rate: What is the historical base rate for events like this?
Step 3 — Evidence: List the 3-5 most important factors and their directional impact.
Step 4 — Adjustment: Starting from the base rate, adjust for each factor.
Step 5 — Calibration: Is your final estimate pulled too close to the market price?

Respond ONLY with valid JSON, no other text:
{{"probability": 0.65, "confidence": 0.7, "reasoning": "brief explanation"}}

Where:
- probability: your estimate of YES probability (0.0 to 1.0)
- confidence: how confident you are in this estimate (0.0 to 1.0)
- reasoning: one-sentence explanation of the key driver"""
