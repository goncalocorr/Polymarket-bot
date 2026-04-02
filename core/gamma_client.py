"""
Gamma API client for discovering Polymarket markets.
Adapted from discountry/polymarket-trading-bot/src/gamma_client.py
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx
from loguru import logger

import config


class GammaClient:
    """Client for the Polymarket Gamma API (market metadata)."""

    def __init__(self, base_url: str = config.POLYMARKET_GAMMA_URL):
        self.base_url = base_url
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=30.0,
                headers={"Accept": "application/json"},
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_markets(
        self,
        active: bool = True,
        closed: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Fetch markets from the Gamma API."""
        client = await self._get_client()
        params = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
            "offset": offset,
        }
        try:
            resp = await client.get("/markets", params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error(f"Gamma API error fetching markets: {e}")
            return []

    async def get_market(self, condition_id: str) -> Optional[dict]:
        """Fetch a single market by condition ID."""
        client = await self._get_client()
        try:
            resp = await client.get(f"/markets/{condition_id}")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error(f"Gamma API error fetching market {condition_id}: {e}")
            return None

    async def get_events(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """Fetch events (groups of related markets)."""
        client = await self._get_client()
        try:
            resp = await client.get(
                "/events", params={"limit": limit, "offset": offset}
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error(f"Gamma API error fetching events: {e}")
            return []

    async def get_event_markets(self, event_id: str) -> list[dict]:
        """Fetch all markets belonging to a specific event."""
        client = await self._get_client()
        try:
            resp = await client.get(
                "/markets",
                params={"event_id": event_id, "active": "true", "limit": 50},
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error(f"Gamma API error fetching event {event_id} markets: {e}")
            return []

    async def get_arbitrage_events(self, min_outcomes: int = 3) -> list[dict]:
        """
        Find multi-outcome events suitable for arbitrage scanning.
        Returns events that have at least `min_outcomes` active markets.
        """
        events = await self.get_events(limit=100)
        candidates = []

        for event in events:
            # Check if event has enough markets
            market_count = event.get("market_count", 0) or len(
                event.get("markets", [])
            )
            if market_count >= min_outcomes:
                candidates.append(event)

            # Rate limit
            if len(candidates) >= 20:
                break

        logger.info(
            f"Found {len(candidates)} multi-outcome events "
            f"(≥{min_outcomes} outcomes) from {len(events)} total"
        )
        return candidates

    async def get_15min_crypto_markets(self) -> list[dict]:
        """
        Find active 15-minute Up/Down markets for crypto (BTC/ETH/SOL/XRP).
        These are the primary targets for the Flash Crash strategy.
        """
        markets = await self.get_markets(active=True, limit=200)
        crypto_keywords = ["BTC", "ETH", "SOL", "XRP", "Bitcoin", "Ethereum", "Solana"]
        direction_keywords = ["Up", "Down", "above", "below"]

        filtered = []
        for m in markets:
            question = m.get("question", "")
            has_crypto = any(kw.lower() in question.lower() for kw in crypto_keywords)
            has_direction = any(kw.lower() in question.lower() for kw in direction_keywords)

            # Check if it's a short-duration market
            end_date_str = m.get("endDate") or m.get("end_date_iso", "")
            if end_date_str and has_crypto and has_direction:
                try:
                    end_date = datetime.fromisoformat(
                        end_date_str.replace("Z", "+00:00")
                    )
                    now = datetime.now(timezone.utc)
                    minutes_to_resolution = (end_date - now).total_seconds() / 60
                    if 0 < minutes_to_resolution <= 30:
                        filtered.append(m)
                except (ValueError, TypeError):
                    pass

        logger.info(f"Found {len(filtered)} active 15-min crypto markets")
        return filtered

    async def get_markets_for_mispricing(self) -> list[dict]:
        """
        Get markets suitable for the cross-market mispricing strategy.
        Filters by volume, time to resolution, and blacklisted categories.
        """
        all_markets = []
        offset = 0
        max_pages = 10  # Safety cap to avoid runaway pagination
        page = 0
        while page < max_pages:
            batch = await self.get_markets(active=True, limit=100, offset=offset)
            if not batch:
                break
            all_markets.extend(batch)
            offset += len(batch)
            page += 1
            if len(batch) < 100:
                break
            # Rate-limit: don't hammer the API
            await asyncio.sleep(0.5)

        filtered = []
        now = datetime.now(timezone.utc)

        for m in all_markets:
            # Volume filter
            volume = float(m.get("volume", 0) or 0)
            volume_24h = float(m.get("volume24hr", 0) or m.get("volume_num", 0) or 0)
            if volume_24h < config.MIN_VOLUME_24H_USDC:
                continue

            # Category filter
            category = (m.get("category", "") or "").lower().replace(" ", "-")
            if category in config.BLACKLISTED_CATEGORIES:
                continue

            # Time to resolution filter
            end_date_str = m.get("endDate") or m.get("end_date_iso", "")
            if not end_date_str:
                continue
            try:
                end_date = datetime.fromisoformat(
                    end_date_str.replace("Z", "+00:00")
                )
                days_to_resolution = (end_date - now).total_seconds() / 86400
                if not (
                    config.MIN_DAYS_TO_RESOLUTION
                    <= days_to_resolution
                    <= config.MAX_DAYS_TO_RESOLUTION
                ):
                    continue
            except (ValueError, TypeError):
                continue

            # Spread filter
            best_bid = float(m.get("bestBid", 0) or 0)
            best_ask = float(m.get("bestAsk", 1) or 1)
            spread_cents = (best_ask - best_bid) * 100
            if spread_cents > config.MAX_SPREAD_CENTS:
                continue

            filtered.append(m)

        logger.info(
            f"Found {len(filtered)} markets suitable for mispricing strategy "
            f"(from {len(all_markets)} total)"
        )
        return filtered
