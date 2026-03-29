"""
External data sources: Kalshi, Metaculus, Manifold Markets, and NewsAPI.
Used for cross-market mispricing detection and AI agent context.
"""

import asyncio
import re
from difflib import SequenceMatcher
from typing import Optional

import httpx
from loguru import logger

import config


class ExternalSources:
    """Fetches prices and news from external prediction markets and news APIs."""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # --- Kalshi ---

    async def get_kalshi_markets(self, limit: int = 100) -> list[dict]:
        client = await self._get_client()
        try:
            resp = await client.get(
                config.KALSHI_API_URL,
                params={"limit": limit, "status": "open"},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("markets", [])
        except httpx.HTTPError as e:
            logger.debug(f"Kalshi API error: {e}")
            return []

    async def get_kalshi_price(self, question: str) -> Optional[float]:
        markets = await self.get_kalshi_markets()
        best_match = self._fuzzy_match(question, markets, key="title")
        if best_match:
            yes_bid = best_match.get("yes_bid")
            last_price = best_match.get("last_price")
            raw = yes_bid if yes_bid is not None and yes_bid > 0 else last_price
            if raw is None or raw <= 0:
                return None
            return raw / 100.0 if raw > 1 else raw
        return None

    # --- Metaculus ---

    async def get_metaculus_price(self, question: str) -> Optional[float]:
        client = await self._get_client()
        try:
            resp = await client.get(
                config.METACULUS_API_URL,
                params={"search": question[:100], "limit": 5, "status": "open"},
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])

            best = self._fuzzy_match(question, results, key="title")
            if best:
                prediction = best.get("community_prediction", {})
                if isinstance(prediction, dict):
                    return prediction.get("full", {}).get("q2")  # median
                return prediction if isinstance(prediction, (int, float)) else None
        except httpx.HTTPError as e:
            logger.debug(f"Metaculus API error: {e}")
        return None

    # --- Manifold ---

    async def get_manifold_price(self, question: str) -> Optional[float]:
        client = await self._get_client()
        try:
            resp = await client.get(
                config.MANIFOLD_API_URL,
                params={"term": question[:100], "limit": 5},
            )
            resp.raise_for_status()
            markets = resp.json()

            best = self._fuzzy_match(question, markets, key="question")
            if best:
                return best.get("probability")
        except httpx.HTTPError as e:
            logger.debug(f"Manifold API error: {e}")
        return None

    # --- Aggregated ---

    async def find_matching_prices(self, question: str) -> dict[str, float]:
        """
        Find matching prices across all external platforms.
        Returns dict: {"kalshi": 0.65, "metaculus": 0.70, ...}
        """
        results = {}

        # Run all lookups in parallel
        kalshi, metaculus, manifold = await asyncio.gather(
            self.get_kalshi_price(question),
            self.get_metaculus_price(question),
            self.get_manifold_price(question),
            return_exceptions=True,
        )

        for name, val in [("kalshi", kalshi), ("metaculus", metaculus), ("manifold", manifold)]:
            if isinstance(val, BaseException):
                logger.debug(f"{name} lookup failed: {type(val).__name__}: {val}")
            elif isinstance(val, (int, float)) and not isinstance(val, bool):
                results[name] = float(val)

        return results

    # --- News ---

    async def get_recent_news(self, query: str, max_results: int = 5) -> list[str]:
        """Fetch recent news headlines related to a market question."""
        if not config.NEWSAPI_KEY:
            return []

        client = await self._get_client()
        # Extract key terms from the question
        clean_query = re.sub(r"Will |be |the |on |in |by |at |to |a ", "", query)
        clean_query = clean_query[:100]

        try:
            resp = await client.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": clean_query,
                    "sortBy": "publishedAt",
                    "pageSize": max_results,
                    "apiKey": config.NEWSAPI_KEY,
                    "language": "en",
                },
            )
            resp.raise_for_status()
            articles = resp.json().get("articles", [])
            return [
                f"{a.get('title', '')} — {a.get('source', {}).get('name', '')}"
                for a in articles
                if a.get("title")
            ]
        except httpx.HTTPError as e:
            logger.debug(f"NewsAPI error: {e}")
            return []

    # --- Helpers ---

    @staticmethod
    def _fuzzy_match(
        question: str,
        candidates: list[dict],
        key: str,
        threshold: float = 0.4,
    ) -> Optional[dict]:
        """Find the best fuzzy match for a question among candidates."""
        best_score = 0
        best_match = None
        question_lower = question.lower()

        for candidate in candidates:
            title = (candidate.get(key, "") or "").lower()
            score = SequenceMatcher(None, question_lower, title).ratio()
            if score > best_score and score >= threshold:
                best_score = score
                best_match = candidate

        return best_match
