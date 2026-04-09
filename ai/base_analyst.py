"""Shared analyst protocol for AI market analysis providers."""

from typing import Protocol


class MarketAnalyst(Protocol):
    """Common interface for AI providers used by the strategy."""

    api_key: str
    model: str
    provider_name: str
    required_api_key_env: str

    async def analyse_market(self, market_data: dict, recent_news: list[str]) -> dict:
        """Estimate market probability with structured JSON output."""

    async def classify_market(self, market_data: dict, recent_news: list[str]) -> dict:
        """Classify market direction (BULLISH/BEARISH/NEUTRAL) + materiality score."""

