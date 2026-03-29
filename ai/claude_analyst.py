"""
Market analysis via Anthropic Claude API.
Adapted from Polymarket/agents, replacing OpenAI with Claude.
"""

import json
import os
from typing import Optional

import anthropic
from loguru import logger

import config


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

        prompt = f"""You are a prediction market analyst. Estimate the true probability
of the following event resolving YES.

Market: {market_data['question']}
Current Polymarket price: {market_data['price']} (implies {market_data['price']*100:.1f}% probability)
24h volume: ${market_data.get('volume', 0):,.0f}
Resolution date: {market_data.get('end_date', 'Unknown')}

{external_info}

{news_section}

Respond ONLY with valid JSON, no other text:
{{"probability": 0.65, "confidence": 0.7, "reasoning": "brief explanation"}}

Where:
- probability: your estimate of YES probability (0.0 to 1.0)
- confidence: how confident you are in this estimate (0.0 to 1.0)
- reasoning: one sentence explanation"""

        try:
            client = self._get_client()
            response = await client.messages.create(
                model=self.model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )

            text = response.content[0].text.strip()
            # Extract JSON even if wrapped in markdown code block
            if "```" in text:
                text = text.split("```")[1]
                if text.lower().startswith("json"):
                    text = text[4:]
                text = text.strip()

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
            logger.error(f"Claude returned invalid JSON: {e}")
            return {"probability": 0.5, "confidence": 0.0, "reasoning": "Parse error"}
        except anthropic.APIError as e:
            logger.error(f"Claude API error: {e}")
            raise
