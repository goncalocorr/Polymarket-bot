"""
Market analysis via Anthropic Claude API.
Adapted from Polymarket/agents, replacing OpenAI with Claude.
Uses superforecaster prompt methodology for structured reasoning.
"""

import asyncio
import json
import re
from typing import Optional

import anthropic
from loguru import logger

import config
from ai.prompt_templates import SYSTEM_SUPERFORECASTER, superforecaster_prompt

MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0


class ClaudeAnalyst:
    """
    Uses Claude to estimate the true probability of a market event
    given current prices, external data, and recent news.
    """

    def __init__(self, api_key: str = "", model: str = ""):
        self.api_key = api_key or config.ANTHROPIC_API_KEY
        self.model = model or config.CLAUDE_MODEL
        self._client: Optional[anthropic.AsyncAnthropic] = None

    def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
        return self._client

    async def analyse_market(
        self, market_data: dict, recent_news: list[str]
    ) -> dict:
        """
        Ask Claude to estimate the true probability of a market event.

        Args:
            market_data: Dict with question, price, volume, end_date, external_prices.
            recent_news: List of recent news headlines.

        Returns:
            Dict with probability, confidence, reasoning.
        """
        external_info = ""
        ext_prices = market_data.get("external_prices", {})
        if ext_prices:
            lines = [f"  - {k}: {v:.2f}" for k, v in ext_prices.items()]
            external_info = "External platform probabilities:\n" + "\n".join(lines)

        news_section = ""
        if recent_news:
            news_section = "Recent relevant news:\n" + "\n".join(
                f"- {n}" for n in recent_news[:5]
            )

        prompt = superforecaster_prompt(
            question=market_data["question"],
            price=market_data["price"],
            volume=market_data.get("volume", 0),
            end_date=market_data.get("end_date", "Unknown"),
            external_info=external_info,
            news_section=news_section,
        )

        text = ""
        last_error: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                client = self._get_client()
                response = await client.messages.create(
                    model=self.model,
                    max_tokens=400,
                    system=SYSTEM_SUPERFORECASTER,
                    messages=[{"role": "user", "content": prompt}],
                )

                if not response.content:
                    logger.error("Claude returned empty content")
                    return {"probability": 0.5, "confidence": 0.0, "reasoning": "Empty response"}

                text = response.content[0].text.strip()
                break  # Success — exit retry loop

            except anthropic.APIConnectionError as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"Claude API connection error (attempt {attempt + 1}/{MAX_RETRIES}): "
                        f"{e} — retrying in {delay:.0f}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
            except anthropic.RateLimitError as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"Claude rate limited (attempt {attempt + 1}/{MAX_RETRIES}): "
                        f"retrying in {delay:.0f}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
            except anthropic.APIError as e:
                # Non-retryable API errors (auth, bad request, etc.)
                logger.error(f"Claude API error: {e}")
                raise

        # Parse response
        try:
            # Extract JSON from markdown code blocks (handles multiple blocks)
            match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
            if match:
                text = match.group(1).strip()
            elif text.startswith("```"):
                # Fallback for unclosed code blocks
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text).strip()

            result = json.loads(text)

            # Validate response
            result["probability"] = max(0.0, min(1.0, float(result.get("probability", 0.5))))
            result["confidence"] = max(0.0, min(1.0, float(result.get("confidence", 0.0))))
            result["reasoning"] = str(result.get("reasoning", ""))

            q = market_data['question']
            q_display = q[:50] + ("..." if len(q) > 50 else "")
            logger.debug(
                f"Claude analysis: prob={result['probability']:.2f}, "
                f"conf={result['confidence']:.2f} for '{q_display}'"
            )
            return result

        except json.JSONDecodeError as e:
            logger.error(f"Claude returned invalid JSON: {e}. Raw: {text[:200]!r}")
            return {"probability": 0.5, "confidence": 0.0, "reasoning": "Parse error"}
