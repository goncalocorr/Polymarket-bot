"""
External data sources: Kalshi, Metaculus, Manifold Markets, and NewsAPI.
Used for cross-market mispricing detection and AI agent context.
"""

import asyncio
import base64
import re
import time
from difflib import SequenceMatcher
from typing import Optional

import httpx
from loguru import logger

import config

# Module-level cache for Metaculus results: question -> (timestamp, result)
_metaculus_cache: dict[str, tuple[float, Optional[float]]] = {}
_METACULUS_CACHE_TTL_S = 3600  # 60 minutes
_METACULUS_MIN_INTERVAL_S = 2.0
_METACULUS_FORBIDDEN_COOLDOWN_S = 3600  # 60 minutes
_metaculus_last_call_ts = 0.0
_metaculus_disabled_until = 0.0
_metaculus_rate_lock = asyncio.Lock()
_kalshi_skip_logged = False


def _metaculus_cache_get(question: str) -> tuple[bool, Optional[float]]:
    """Check Metaculus cache. Returns (hit, value)."""
    key = question[:100].lower().strip()
    entry = _metaculus_cache.get(key)
    if entry is None:
        return False, None
    ts, value = entry
    if time.time() - ts > _METACULUS_CACHE_TTL_S:
        del _metaculus_cache[key]
        return False, None
    return True, value


def _metaculus_cache_set(question: str, value: Optional[float]):
    """Store a Metaculus result in the cache."""
    key = question[:100].lower().strip()
    _metaculus_cache[key] = (time.time(), value)
    # Prune cache if too large (keep last 500 entries)
    if len(_metaculus_cache) > 500:
        sorted_keys = sorted(_metaculus_cache, key=lambda k: _metaculus_cache[k][0])
        for old_key in sorted_keys[:250]:
            _metaculus_cache.pop(old_key, None)


async def _metaculus_throttle():
    """Enforce a minimum delay between Metaculus requests globally."""
    global _metaculus_last_call_ts
    async with _metaculus_rate_lock:
        elapsed = time.time() - _metaculus_last_call_ts
        wait_for = _METACULUS_MIN_INTERVAL_S - elapsed
        if wait_for > 0:
            await asyncio.sleep(wait_for)
        _metaculus_last_call_ts = time.time()


class ExternalSources:
    """Fetches prices and news from external prediction markets and news APIs."""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=20.0,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "polymarket-bot/1.0",
                },
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # --- Kalshi ---

    async def get_kalshi_markets(self, limit: int = 100) -> list[dict]:
        """Fetch markets from Kalshi. Requires API credentials for v2 API."""
        global _kalshi_skip_logged
        if not config.KALSHI_API_KEY or not config.KALSHI_API_SECRET:
            if not _kalshi_skip_logged:
                logger.debug("Kalshi: No API credentials configured, skipping")
                _kalshi_skip_logged = True
            return []

        auth_raw = f"{config.KALSHI_API_KEY}:{config.KALSHI_API_SECRET}"
        auth_b64 = base64.b64encode(auth_raw.encode("utf-8")).decode("utf-8")

        client = await self._get_client()
        try:
            resp = await client.get(
                config.KALSHI_API_URL,
                params={"limit": limit, "status": "open"},
                headers={
                    "Authorization": f"Basic {auth_b64}",
                    "Accept": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("markets", [])
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                logger.debug(
                    "Kalshi: Authentication failed (401) — check KALSHI_API_KEY/SECRET"
                )
            else:
                logger.debug(f"Kalshi API error: {e.response.status_code}")
            return []
        except httpx.HTTPError as e:
            logger.debug(f"Kalshi API connection error: {type(e).__name__}")
            return []

    async def get_kalshi_price(self, question: str) -> Optional[float]:
        try:
            markets = await self.get_kalshi_markets()
            best_match = self._fuzzy_match(question, markets, key="title")
            if best_match:
                yes_bid = best_match.get("yes_bid")
                last_price = best_match.get("last_price")
                raw = yes_bid if yes_bid is not None and yes_bid > 0 else last_price
                if raw is None or raw <= 0:
                    return None
                return raw / 100.0 if raw > 1 else raw
        except Exception as e:
            logger.debug(f"Kalshi price lookup error: {type(e).__name__}: {e}")
        return None

    # --- Metaculus ---

    async def get_metaculus_price(self, question: str) -> Optional[float]:
        """Fetch probability from Metaculus with caching and rate limiting."""
        global _metaculus_disabled_until

        if time.time() < _metaculus_disabled_until:
            return None

        # Check cache first
        hit, cached_value = _metaculus_cache_get(question)
        if hit:
            logger.debug(f"Metaculus: Cache hit for '{question[:40]}...'")
            return cached_value

        client = await self._get_client()
        try:
            # Rate limit: keep at least 2s between requests globally.
            await _metaculus_throttle()

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
                    value = prediction.get("full", {}).get("q2")  # median
                elif isinstance(prediction, (int, float)):
                    value = prediction
                else:
                    value = None
                _metaculus_cache_set(question, value)
                return value

            _metaculus_cache_set(question, None)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                logger.debug(
                    "Metaculus: Rate limited (429) — result will be retried next cycle"
                )
            elif e.response.status_code == 403:
                _metaculus_disabled_until = (
                    time.time() + _METACULUS_FORBIDDEN_COOLDOWN_S
                )
                logger.debug(
                    "Metaculus: Access forbidden (403) - disabling lookups for 60 minutes"
                )
            else:
                logger.debug(f"Metaculus API error: {e.response.status_code}")
        except httpx.HTTPError as e:
            logger.debug(f"Metaculus API connection error: {type(e).__name__}")
        except Exception as e:
            logger.debug(f"Metaculus lookup error: {type(e).__name__}: {e}")
        return None

    # --- Manifold ---

    async def get_manifold_price(self, question: str) -> Optional[float]:
        """Fetch probability from Manifold Markets using search endpoint."""
        client = await self._get_client()
        try:
            resp = await client.get(
                config.MANIFOLD_API_URL,
                params={"term": question, "limit": 5},
            )
            resp.raise_for_status()
            markets = resp.json()

            # Handle both list and dict responses
            if isinstance(markets, dict):
                markets = markets.get("markets", markets.get("data", []))
            if not isinstance(markets, list):
                logger.debug(f"Manifold: Unexpected response type: {type(markets).__name__}")
                return None

            best = self._fuzzy_match(question, markets, key="question")
            if best:
                return best.get("probability")
        except httpx.HTTPStatusError as e:
            logger.debug(f"Manifold API error: {e.response.status_code}")
        except httpx.HTTPError as e:
            logger.debug(f"Manifold API connection error: {type(e).__name__}")
        except Exception as e:
            logger.debug(f"Manifold lookup error: {type(e).__name__}: {e}")
        return None

    # --- Aggregated ---

    async def find_matching_prices(self, question: str) -> dict[str, float]:
        """
        Find matching prices across all external platforms.
        Returns dict: {"kalshi": 0.65, "metaculus": 0.70, ...}

        Each source is independent — one failure never blocks the others.
        """
        async def _safe_fetch(name: str, fetch_coro):
            try:
                value = await fetch_coro
            except Exception as e:
                logger.debug(f"{name} lookup failed: {type(e).__name__}: {e}")
                return None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            return None

        # Run all lookups in parallel; each one is fully isolated.
        kalshi, metaculus, manifold = await asyncio.gather(
            _safe_fetch("kalshi", self.get_kalshi_price(question)),
            _safe_fetch("metaculus", self.get_metaculus_price(question)),
            _safe_fetch("manifold", self.get_manifold_price(question)),
        )

        results: dict[str, float] = {}
        if kalshi is not None:
            results["kalshi"] = kalshi
        if metaculus is not None:
            results["metaculus"] = metaculus
        if manifold is not None:
            results["manifold"] = manifold

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
        threshold: float = 0.6,
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

