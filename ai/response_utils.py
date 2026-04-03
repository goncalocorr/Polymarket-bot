"""Helpers shared by AI analyst providers."""

import json
import re


def parse_analysis_json(text: str) -> dict:
    """Parse a JSON analysis payload, tolerating fenced code blocks."""
    text = (text or "").strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    elif text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()

    result = json.loads(text)
    try:
        probability = max(0.0, min(1.0, float(result.get("probability", 0.5))))
    except (ValueError, TypeError):
        probability = 0.5
    try:
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0.0))))
    except (ValueError, TypeError):
        confidence = 0.0
    return {
        "probability": probability,
        "confidence": confidence,
        "reasoning": str(result.get("reasoning", "")),
    }

