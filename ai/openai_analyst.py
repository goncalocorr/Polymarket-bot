"""Market analysis via the OpenAI Responses API."""

import asyncio
import json
from typing import Optional

import httpx
from loguru import logger

import config
from ai.prompt_templates import SYSTEM_SUPERFORECASTER, superforecaster_prompt
from ai.response_utils import parse_analysis_json

MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0


class OpenAIAnalyst:
    """
    Uses an OpenAI model to estimate the true probability of a market event
    given current prices, external data, and recent news.
    """

    provider_name = "openai"
    required_api_key_env = "OPENAI_API_KEY"

    def __init__(self, api_key: str = "", model: str = "", api_url: str = ""):
        self.api_key = api_key or config.OPENAI_API_KEY
        self.model = model or config.OPENAI_MODEL
        self.api_url = api_url or config.OPENAI_API_URL
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or (
            isinstance(self._client, httpx.AsyncClient) and self._client.is_closed
        ):
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _build_prompt(self, market_data: dict, recent_news: list[str]) -> str:
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

        return superforecaster_prompt(
            question=market_data.get("question", ""),
            price=market_data.get("price", 0.5),
            volume=market_data.get("volume", 0),
            end_date=market_data.get("end_date", "Unknown"),
            external_info=external_info,
            news_section=news_section,
        )

    @staticmethod
    def _extract_output_text(payload: dict) -> str:
        output_text = payload.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        chunks: list[str] = []
        for item in payload.get("output", []) or []:
            for content in item.get("content", []) or []:
                content_type = content.get("type", "")
                if content_type in {"output_text", "text"}:
                    text = content.get("text")
                    if isinstance(text, str) and text.strip():
                        chunks.append(text.strip())
        return "\n".join(chunks).strip()

    async def analyse_market(
        self, market_data: dict, recent_news: list[str]
    ) -> dict:
        """Ask an OpenAI model to estimate the true probability of a market event."""
        question = market_data.get("question", "")
        if not question:
            return {"probability": 0.5, "confidence": 0.0, "reasoning": "Missing market question"}

        prompt = self._build_prompt(market_data, recent_news)
        payload = {
            "model": self.model,
            "instructions": SYSTEM_SUPERFORECASTER,
            "input": prompt,
            "max_output_tokens": 400,
            "text": {
                "format": {
                    "type": "json_object",
                }
            },
        }

        text = ""
        for attempt in range(MAX_RETRIES):
            try:
                response = await self._get_client().post(
                    self.api_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )

                if response.status_code in {429, 500, 502, 503, 504}:
                    raise httpx.HTTPStatusError(
                        "Retryable OpenAI error",
                        request=response.request,
                        response=response,
                    )

                response.raise_for_status()
                text = self._extract_output_text(response.json())
                if not text:
                    logger.error("OpenAI returned empty content")
                    return {
                        "probability": 0.5,
                        "confidence": 0.0,
                        "reasoning": "Empty response",
                    }
                break
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"OpenAI API connection error (attempt {attempt + 1}/{MAX_RETRIES}): "
                        f"{e} - retrying in {delay:.0f}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code if e.response else None
                if status_code in {429, 500, 502, 503, 504} and attempt < MAX_RETRIES - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"OpenAI rate/server error {status_code} "
                        f"(attempt {attempt + 1}/{MAX_RETRIES}) - retrying in {delay:.0f}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error(f"OpenAI API error {status_code}: {e}")
                raise

        try:
            result = parse_analysis_json(text)
            q = question
            q_display = q[:50] + ("..." if len(q) > 50 else "")
            logger.debug(
                f"OpenAI analysis: prob={result['probability']:.2f}, "
                f"conf={result['confidence']:.2f} for '{q_display}'"
            )
            return result
        except json.JSONDecodeError as e:
            logger.error(f"OpenAI returned invalid JSON: {e}. Raw: {text[:200]!r}")
            return {"probability": 0.5, "confidence": 0.0, "reasoning": "Parse error"}

