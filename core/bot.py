"""
Core trading bot — order placement, cancellation, and position tracking.
Adapted from discountry/polymarket-trading-bot/src/bot.py
"""

import asyncio
import os
import time
import uuid
from typing import Optional

import httpx
from loguru import logger

import config
from core.signer import sign_order, get_address, CTF_EXCHANGE_ADDRESS


class TradingBot:
    """
    Low-level interface to the Polymarket CLOB API.
    Handles order placement, cancellation, and open order queries.
    """

    SIDE_BUY = 0
    SIDE_SELL = 1

    def __init__(
        self,
        private_key: str = "",
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        clob_url: str = config.POLYMARKET_CLOB_URL,
    ):
        self.private_key = private_key or config.POLYGON_WALLET_PRIVATE_KEY
        self.api_key = api_key or config.POLY_BUILDER_API_KEY
        self.api_secret = api_secret or config.POLY_BUILDER_API_SECRET
        self.api_passphrase = api_passphrase or config.POLY_BUILDER_API_PASSPHRASE
        self.clob_url = clob_url

        self.address = get_address(self.private_key) if self.private_key else ""
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.clob_url,
                timeout=30.0,
                headers=self._auth_headers(),
            )
        return self._client

    def _auth_headers(self) -> dict:
        """Build authentication headers for the CLOB API."""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["POLY_API_KEY"] = self.api_key
            headers["POLY_API_SECRET"] = self.api_secret
            headers["POLY_API_PASSPHRASE"] = self.api_passphrase
        return headers

    def _build_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: int,
        expiration: int = 0,
    ) -> dict:
        """Build an order dict for EIP-712 signing."""
        # Convert price/size to maker/taker amounts (6 decimal USDC)
        price_raw = int(price * 1e6)
        size_raw = int(size * 1e6)

        if side == self.SIDE_BUY:
            maker_amount = size_raw  # USDC to spend
            taker_amount = (size_raw * 1_000_000) // price_raw if price_raw > 0 else 0
        else:
            maker_amount = (size_raw * 1_000_000) // price_raw if price_raw > 0 else 0
            taker_amount = size_raw

        order = {
            "salt": int(uuid.uuid4().int >> 128),
            "maker": self.address,
            "signer": self.address,
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": int(token_id),
            "makerAmount": maker_amount,
            "takerAmount": taker_amount,
            "expiration": expiration,
            "nonce": 0,
            "feeRateBps": 0,
            "side": side,
            "signatureType": 0,
        }
        return order

    async def place_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        expiration: int = 0,
    ) -> Optional[dict]:
        """
        Place an order on the Polymarket CLOB.

        Args:
            token_id: The token/outcome to trade.
            price: Price per share (0.0 to 1.0).
            size: Size in USDC.
            side: "BUY" or "SELL".
            expiration: Unix timestamp for order expiry (0 = no expiry).

        Returns:
            Order response dict, or None on failure.
        """
        if config.DRY_RUN:
            logger.info(
                f"[DRY RUN] Would place {side} order: "
                f"token={token_id}, price={price}, size={size}"
            )
            return {
                "id": f"dry-run-{uuid.uuid4().hex[:8]}",
                "status": "DRY_RUN",
                "side": side,
                "price": price,
                "size": size,
                "token_id": token_id,
            }

        side_int = self.SIDE_BUY if side.upper() == "BUY" else self.SIDE_SELL
        order = self._build_order(token_id, price, size, side_int, expiration)
        signature = sign_order(order, self.private_key)

        payload = {
            "order": order,
            "signature": signature,
            "owner": self.address,
            "orderType": "GTC",
        }

        client = await self._get_client()
        try:
            resp = await client.post("/order", json=payload)
            resp.raise_for_status()
            result = resp.json()
            logger.info(
                f"Order placed: {side} {size} USDC at {price} for {token_id} "
                f"-> {result.get('id', 'unknown')}"
            )
            return result
        except httpx.HTTPError as e:
            logger.error(f"Failed to place order: {e}")
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by ID."""
        if config.DRY_RUN:
            logger.info(f"[DRY RUN] Would cancel order {order_id}")
            return True

        client = await self._get_client()
        try:
            resp = await client.delete(f"/order/{order_id}")
            resp.raise_for_status()
            logger.info(f"Order cancelled: {order_id}")
            return True
        except httpx.HTTPError as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    async def cancel_all_orders(self) -> int:
        """Cancel all open orders. Returns number cancelled."""
        orders = await self.get_open_orders()
        cancelled = 0
        for order in orders:
            if await self.cancel_order(order["id"]):
                cancelled += 1
        return cancelled

    async def get_open_orders(self, market: str = "") -> list[dict]:
        """Get all open orders, optionally filtered by market."""
        if config.DRY_RUN:
            return []

        client = await self._get_client()
        params = {}
        if market:
            params["market"] = market

        try:
            resp = await client.get("/orders", params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error(f"Failed to get open orders: {e}")
            return []

    async def get_order(self, order_id: str) -> Optional[dict]:
        """Get details of a specific order."""
        if config.DRY_RUN:
            return None

        client = await self._get_client()
        try:
            resp = await client.get(f"/order/{order_id}")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error(f"Failed to get order {order_id}: {e}")
            return None

    async def get_balance(self) -> float:
        """Get USDC balance on Polymarket."""
        if config.DRY_RUN:
            return 500.0  # Simulated balance

        client = await self._get_client()
        try:
            resp = await client.get(f"/balance")
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("balance", 0))
        except httpx.HTTPError as e:
            logger.error(f"Failed to get balance: {e}")
            return 0.0

    async def close(self):
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
