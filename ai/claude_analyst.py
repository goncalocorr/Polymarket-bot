"""Market analysis via Anthropic Claude API."""

import asyncio
import json
from typing import Optional

import anthropic
from loguru import logger

import config
from ai.prompt_templates import (
    SYSTEM_CLASSIFIER,
    SYSTEM_SUPERFORECASTER,
    classification_prompt,
    superforecaster_prompt,
)
from ai.response_utils import parse_analysis_json, parse_classification_json

MAX_RETRIES = 3
CLASSIFY_MAX_RETRIES = 2  # Fewer retries for screening calls (fail fast)
RETRY_BASE_DELAY = 2.0


class ClaudeAnalyst:
    """
    Uses Claude to estimate the true probability of a market event
    given current prices, external data, and recent news.
    """

    provider_name = "anthropic"
    required_api_key_env = "ANTHROPIC_API_KEY"

    def __init__(self, api_key: str = "", model: str = ""):
        self.api_key = api_key or config.ANTHROPIC_API_KEY
        self.model = model or config.CLAUDE_MODEL
        self.screening_model = config.SCREENING_MODEL_ANTHROPIC or self.model
        self._client: Optional[anthropic.AsyncAnthropic] = None

    def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
        return self._client

    async def close(self):
        """Close the underlying Anthropic HTTP client."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def analyse_market(
        self, market_data: dict, recent_news: list[str]
    ) -> dict:
        """Ask Claude to estimate the true probability of a market event."""
        question = market_data.get("question", "")
        price = market_data.get("price", 0.5)
        if not question:
            return {"probability": 0.5, "confidence": 0.0, "reasoning": "Missing market question"}

        external_info = ""
        ext_prices = market_data.get("external_prices", {})
        if ext_prices:
            lines = [f"  - {k}: {v:.2f}" for k, v in ext_prices.items() if isinstance(v, (int, float))]
            external_info = "External platform probabilities:\n" + "\n".join(lines)

        news_section = ""
        if recent_news:
            news_section = "Recent relevant news:\n" + "\n".join(
                f"- {n}" for n in recent_news[:5]
            )

        prompt = superforecaster_prompt(
            question=question,
            price=price,
            volume=market_data.get("volume", 0),
            end_date=market_data.get("end_date", "Unknown"),
            external_info=external_info,
            news_section=news_section,
        )

        text = ""
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
                    return {
                        "probability": 0.5,
                        "confidence": 0.0,
                        "reasoning": "Empty response",
                    }

                text = response.content[0].text.strip()
                break

            except anthropic.APIConnectionError as e:
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"Claude API connection error (attempt {attempt + 1}/{MAX_RETRIES}): "
                        f"{e} - retrying in {delay:.0f}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
            except anthropic.RateLimitError as e:
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
                logger.error(f"Claude API error: {e}")
                raise

        try:
            result = parse_analysis_json(text)
            q = question
            q_display = q[:50] + ("..." if len(q) > 50 else "")
            logger.debug(
                f"Claude analysis: prob={result['probability']:.2f}, "
                f"conf={result['confidence']:.2f} for '{q_display}'"
            )
            return result
        except json.JSONDecodeError as e:
            logger.error(f"Claude returned invalid JSON: {e}. Raw: {text[:200]!r}")
            return {"probability": 0.5, "confidence": 0.0, "reasoning": "Parse error"}

    async def classify_market(
        self, market_data: dict, recent_news: list[str]
    ) -> dict:
        """Ask Claude to classify market direction and materiality."""
        question = market_data.get("question", "")
        if not question:
            return {"direction": "NEUTRAL", "materiality": 0.0, "confidence": 0.0, "reasoning": "Missing question"}

        external_info = ""
        ext_prices = market_data.get("external_prices", {})
        if ext_prices:
            lines = [f"  - {k}: {v:.2f}" for k, v in ext_prices.items() if isinstance(v, (int, float))]
            external_info = "External platform probabilities:\n" + "\n".join(lines)

        news_section = ""
        if recent_news:
            news_section = "Recent relevant news:\n" + "\n".join(
                f"- {n}" for n in recent_news[:5]
            )

        prompt = classification_prompt(
            question=question,
            price=market_data.get("price", 0.5),
            volume=market_data.get("volume", 0),
            end_date=market_data.get("end_date", "Unknown"),
            external_info=external_info,
            news_section=news_section,
        )

        text = ""
        for attempt in range(CLASSIFY_MAX_RETRIES):
            try:
                client = self._get_client()
                response = await client.messages.create(
                    model=self.screening_model,
                    max_tokens=300,
                    system=SYSTEM_CLASSIFIER,
                    messages=[{"role": "user", "content": prompt}],
                )

                if not response.content:
                    logger.error("Claude returned empty content for classification")
                    return {"direction": "NEUTRAL", "materiality": 0.0, "confidence": 0.0, "reasoning": "Empty response"}

                text = response.content[0].text.strip()
                break

            except anthropic.APIConnectionError as e:
                if attempt < CLASSIFY_MAX_RETRIES - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(f"Claude classify connection error (attempt {attempt + 1}): {e} - retrying in {delay:.0f}s")
                    await asyncio.sleep(delay)
                    continue
                raise
            except anthropic.RateLimitError as e:
                if attempt < CLASSIFY_MAX_RETRIES - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(f"Claude classify rate limited (attempt {attempt + 1}): retrying in {delay:.0f}s")
                    await asyncio.sleep(delay)
                    continue
                raise
            except anthropic.APIError as e:
                logger.error(f"Claude classify API error: {e}")
                raise

        try:
            result = parse_classification_json(text)
            q_display = question[:50] + ("..." if len(question) > 50 else "")
            logger.debug(
                f"Claude classification: dir={result['direction']}, "
                f"mat={result['materiality']:.2f}, conf={result['confidence']:.2f} "
                f"for '{q_display}'"
            )
            return result
        except json.JSONDecodeError as e:
            logger.error(f"Claude returned invalid classification JSON: {e}. Raw: {text[:200]!r}")
            return {"direction": "NEUTRAL", "materiality": 0.0, "confidence": 0.0, "reasoning": "Parse error"}

