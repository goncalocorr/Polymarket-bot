"""
Data indexer — collects and stores historical trade data.
Adapted from Jon-Becker/prediction-market-analysis/src/indexers/polymarket/.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx
import pandas as pd
from loguru import logger

import config
from storage.parquet_store import ParquetStore


class PolymarketIndexer:
    """Indexes historical Polymarket trade data for backtesting."""

    TRADES_API = f"{config.POLYMARKET_GAMMA_URL}/trades"

    def __init__(self, store: ParquetStore):
        self.store = store
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def fetch_trades(
        self,
        market_id: str = "",
        limit: int = 500,
        before_id: str = "",
    ) -> list[dict]:
        """Fetch trades from Polymarket API."""
        client = await self._get_client()
        params = {"limit": limit}
        if market_id:
            params["market"] = market_id
        if before_id:
            params["before"] = before_id

        try:
            resp = await client.get(self.TRADES_API, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error(f"Indexer: Failed to fetch trades: {e}")
            return []

    async def index_market(self, market_id: str, max_trades: int = 10000) -> pd.DataFrame:
        """Index all trades for a specific market."""
        all_trades = []
        before_id = ""

        while len(all_trades) < max_trades:
            batch = await self.fetch_trades(
                market_id=market_id, limit=500, before_id=before_id
            )
            if not batch:
                break

            all_trades.extend(batch)
            new_before_id = batch[-1].get("id", "")
            if not new_before_id or new_before_id == before_id:
                break  # No valid cursor — stop pagination
            before_id = new_before_id

            if len(batch) < 500:
                break

            await asyncio.sleep(0.5)  # Rate limit

        if all_trades:
            df = pd.DataFrame(all_trades)
            self.store.save_trades(df, f"trades_{market_id[:16]}.parquet")
            logger.info(f"Indexed {len(all_trades)} trades for market {market_id[:16]}")
            return df

        return pd.DataFrame()

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
