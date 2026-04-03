"""
Core trading bot — order placement, cancellation, and position tracking.
Adapted from discountry/polymarket-trading-bot/src/bot.py
"""

import asyncio
import base64
import hashlib
import hmac as hmac_mod
import json
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import httpx
from loguru import logger

import config
from core.signer import (
    sign_order,
    sign_auth_message,
    get_address,
    CTF_EXCHANGE_ADDRESS,
)
from core.utils import retry_async


@dataclass
class ApiCredentials:
    """L2 API credentials for authenticated CLOB endpoints."""

    api_key: str
    secret: str
    passphrase: str

    def is_valid(self) -> bool:
        return bool(self.api_key and self.secret and self.passphrase)


class TradingBot:
    """
    Low-level interface to the Polymarket CLOB API.
    Handles order placement, cancellation, and open order queries.
    Uses HMAC-SHA256 L2 authentication for signed requests.
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
        self.clob_url = clob_url

        self.address = get_address(self.private_key) if self.private_key else ""
        self._client: Optional[httpx.AsyncClient] = None

        # L2 API credentials (derived via EIP-712 auth or supplied directly)
        self.api_creds: Optional[ApiCredentials] = None
        if api_key or config.POLY_API_KEY:
            self.api_creds = ApiCredentials(
                api_key=api_key or config.POLY_API_KEY,
                secret=api_secret or config.POLY_API_SECRET,
                passphrase=api_passphrase or config.POLY_API_PASSPHRASE,
            )

        # Builder credentials (for gasless/attributed orders)
        self.builder_api_key = config.POLY_BUILDER_API_KEY
        self.builder_api_secret = config.POLY_BUILDER_API_SECRET
        self.builder_api_passphrase = config.POLY_BUILDER_API_PASSPHRASE

    async def derive_api_credentials(self) -> ApiCredentials:
        """
        Derive L2 API credentials from the wallet private key.
        Calls /auth/derive-api-key (or /auth/api-key to create).
        Stores them on self.api_creds for subsequent requests.
        """
        ts = str(int(time.time()))
        sig = sign_auth_message(self.private_key, timestamp=ts, nonce=0)

        headers = {
            "POLY_ADDRESS": self.address,
            "POLY_SIGNATURE": sig,
            "POLY_TIMESTAMP": ts,
            "POLY_NONCE": "0",
        }

        client = await self._get_client()

        # Try derive first (existing key), fall back to create
        for endpoint in ("/auth/derive-api-key", "/auth/api-key"):
            method = "GET" if "derive" in endpoint else "POST"
            try:
                if method == "GET":
                    resp = await client.get(endpoint, headers=headers)
                else:
                    resp = await client.post(endpoint, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                self.api_creds = ApiCredentials(
                    api_key=data["apiKey"],
                    secret=data["secret"],
                    passphrase=data["passphrase"],
                )
                logger.info("L2 API credentials derived successfully")
                return self.api_creds
            except httpx.HTTPStatusError:
                continue

        raise RuntimeError("Failed to derive L2 API credentials")

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.clob_url,
                timeout=30.0,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    def _build_hmac_headers(
        self, method: str, path: str, body: str = ""
    ) -> dict:
        """
        Build HMAC-SHA256 authentication headers for L2 API requests.
        Message format: timestamp + method + path + body
        Secret is base64-decoded before use.
        """
        headers: dict[str, str] = {}
        # Single timestamp for both HMAC sections to ensure consistency
        ts = str(int(time.time()))

        # Builder HMAC (for gasless/attributed orders)
        if self.builder_api_key and self.builder_api_secret:
            message = f"{ts}{method}{path}{body}"
            try:
                decoded_secret = base64.urlsafe_b64decode(self.builder_api_secret)
                h = hmac_mod.new(decoded_secret, message.encode("utf-8"), hashlib.sha256)
                sig = base64.urlsafe_b64encode(h.digest()).decode("utf-8")
            except Exception:
                logger.warning(
                    "Builder API secret is not valid base64 — falling back to raw key"
                )
                sig = hmac_mod.new(
                    self.builder_api_secret.encode(),
                    message.encode(),
                    hashlib.sha256,
                ).hexdigest()
            headers.update({
                "POLY_BUILDER_API_KEY": self.builder_api_key,
                "POLY_BUILDER_TIMESTAMP": ts,
                "POLY_BUILDER_PASSPHRASE": self.builder_api_passphrase,
                "POLY_BUILDER_SIGNATURE": sig,
            })

        # L2 user HMAC (for authenticated data endpoints)
        if self.api_creds and self.api_creds.is_valid():
            message = f"{ts}{method}{path}"
            if body:
                message += body

            try:
                decoded_secret = base64.urlsafe_b64decode(self.api_creds.secret)
                h = hmac_mod.new(decoded_secret, message.encode("utf-8"), hashlib.sha256)
                sig = base64.urlsafe_b64encode(h.digest()).decode("utf-8")
            except Exception as exc:
                logger.warning(
                    f"L2 API secret is not valid base64 ({exc!r}) — "
                    "falling back to raw key; authenticated requests may fail"
                )
                sig = hmac_mod.new(
                    self.api_creds.secret.encode(),
                    message.encode(),
                    hashlib.sha256,
                ).hexdigest()

            headers.update({
                "POLY_ADDRESS": self.address,
                "POLY_API_KEY": self.api_creds.api_key,
                "POLY_TIMESTAMP": ts,
                "POLY_PASSPHRASE": self.api_creds.passphrase,
                "POLY_SIGNATURE": sig,
            })

        return headers

    def _build_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: int,
        expiration: int = 0,
    ) -> Optional[dict]:
        """Build an order dict for EIP-712 signing."""
        # Validate token_id is numeric
        try:
            token_id_int = int(token_id)
        except (ValueError, OverflowError):
            logger.error(f"Invalid token_id (not numeric): {token_id[:40]}")
            return None

        # Convert price/size to maker/taker amounts (6 decimal USDC)
        price_raw = round(price * 1e6)
        size_raw = round(size * 1e6)

        if side == self.SIDE_BUY:
            maker_amount = size_raw  # USDC to spend
            taker_amount = (size_raw * 1_000_000) // price_raw if price_raw > 0 else 0
        else:
            maker_amount = (size_raw * 1_000_000) // price_raw if price_raw > 0 else 0
            taker_amount = size_raw

        order = {
            "salt": uuid.uuid4().int,
            "maker": self.address,
            "signer": self.address,
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": str(token_id_int),
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
        order_type: str = "GTC",
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
                "orderType": order_type,
            }

        side_int = self.SIDE_BUY if side.upper() == "BUY" else self.SIDE_SELL
        order = self._build_order(token_id, price, size, side_int, expiration)
        if order is None:
            return None
        signature = sign_order(order, self.private_key)

        payload = {
            "order": order,
            "signature": signature,
            "owner": self.address,
            "orderType": order_type,
        }

        body_json = json.dumps(payload, separators=(",", ":"))
        hmac_headers = self._build_hmac_headers("POST", "/order", body_json)

        client = await self._get_client()
        try:
            resp = await client.post(
                "/order", content=body_json, headers=hmac_headers
            )
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

    async def get_order_scoring(self, order_id: str) -> dict:
        """Check whether an order is currently scoring for liquidity rewards."""
        if config.DRY_RUN:
            return {"scoring": True}

        endpoint = "/order-scoring"
        hmac_headers = self._build_hmac_headers("GET", endpoint)
        client = await self._get_client()
        try:
            resp = await client.get(
                endpoint,
                params={"orderId": order_id},
                headers=hmac_headers,
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {"scoring": bool(data)}
        except httpx.HTTPError as e:
            logger.debug(f"Failed to get order scoring for {order_id}: {e}")
            return {"scoring": False}

    async def get_orders_scoring(self, order_ids: list[str]) -> dict[str, bool]:
        """Check whether multiple orders are scoring for rewards."""
        if not order_ids:
            return {}
        if config.DRY_RUN:
            return {order_id: True for order_id in order_ids}

        endpoint = "/orders-scoring"
        body = {"orderIds": order_ids}
        body_json = json.dumps(body, separators=(",", ":"))
        hmac_headers = self._build_hmac_headers("POST", endpoint, body_json)
        client = await self._get_client()
        try:
            resp = await client.post(
                endpoint,
                content=body_json,
                headers=hmac_headers,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                return {str(k): bool(v) for k, v in data.items()}
        except httpx.HTTPError as e:
            logger.debug(f"Failed to get order scoring batch: {e}")
        return {order_id: False for order_id in order_ids}

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by ID."""
        if config.DRY_RUN:
            logger.info(f"[DRY RUN] Would cancel order {order_id}")
            return True

        body = {"orderID": order_id}
        body_json = json.dumps(body, separators=(",", ":"))
        hmac_headers = self._build_hmac_headers("DELETE", "/order", body_json)

        client = await self._get_client()
        try:
            resp = await client.request(
                "DELETE", "/order", content=body_json, headers=hmac_headers
            )
            resp.raise_for_status()
            logger.info(f"Order cancelled: {order_id}")
            return True
        except httpx.HTTPError as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    async def cancel_all_orders(self) -> int:
        """Cancel all open orders. Returns number cancelled."""
        if config.DRY_RUN:
            logger.info("[DRY RUN] Would cancel all open orders")
            return 0

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

        hmac_headers = self._build_hmac_headers("GET", "/data/orders")
        client = await self._get_client()
        params = {}
        if market:
            params["market"] = market

        try:
            resp = await client.get("/data/orders", params=params, headers=hmac_headers)
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", []) if isinstance(data, dict) else data
        except httpx.HTTPError as e:
            logger.error(f"Failed to get open orders: {e}")
            return []

    async def get_order(self, order_id: str) -> Optional[dict]:
        """Get details of a specific order."""
        if config.DRY_RUN:
            return None

        endpoint = f"/data/order/{order_id}"
        hmac_headers = self._build_hmac_headers("GET", endpoint)
        client = await self._get_client()
        try:
            resp = await client.get(endpoint, headers=hmac_headers)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error(f"Failed to get order {order_id}: {e}")
            return None

    async def get_balance(self) -> float:
        """Get USDC balance on Polymarket."""
        if config.DRY_RUN:
            return 500.0  # Simulated balance

        hmac_headers = self._build_hmac_headers("GET", "/balance")
        client = await self._get_client()
        try:
            resp = await client.get("/balance", headers=hmac_headers)
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
