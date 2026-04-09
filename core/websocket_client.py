"""
WebSocket client for real-time Polymarket order book data.
Adapted from discountry/polymarket-trading-bot/src/websocket_client.py
"""

import asyncio
import json
import time
from collections import defaultdict, deque
from typing import Callable, Optional

import websockets
from loguru import logger

import config


class MarketWebSocket:
    """
    WebSocket client that subscribes to Polymarket market updates.
    Tracks order book snapshots and mid-prices over time.
    """

    def __init__(self, url: str = config.POLYMARKET_WS_URL):
        self.url = url
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._subscriptions: set[str] = set()

        # Price history: token_id -> deque of (timestamp, mid_price)
        self._price_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=1000)
        )

        # Current order book: token_id -> {"bids": [...], "asks": [...]}
        self._order_books: dict[str, dict] = {}

        # Callbacks
        self._on_book_callbacks: list[Callable] = []
        self._on_price_callbacks: list[Callable] = []

    def on_book(self, callback: Callable):
        """Register a callback for order book updates: callback(token_id, book)."""
        self._on_book_callbacks.append(callback)

    def on_price(self, callback: Callable):
        """Register a callback for price updates: callback(token_id, price, timestamp)."""
        self._on_price_callbacks.append(callback)

    async def connect(self):
        """Establish WebSocket connection."""
        try:
            self._ws = await websockets.connect(
                self.url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            )
            self._running = True
            logger.info(f"WebSocket connected to {self.url}")
        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
            raise

    async def subscribe(self, token_id: str, force: bool = False):
        """Subscribe to order book updates for a token."""
        if not self._ws:
            raise RuntimeError("WebSocket not connected")
        if token_id in self._subscriptions and not force:
            return

        msg = json.dumps({
            "assets_ids": [token_id],
            "operation": "subscribe",
        })
        await self._ws.send(msg)
        self._subscriptions.add(token_id)
        logger.debug(f"Subscribed to {token_id}")

    async def unsubscribe(self, token_id: str):
        """Unsubscribe from a token's order book and clean up local data."""
        if not self._ws:
            return

        msg = json.dumps({
            "assets_ids": [token_id],
            "operation": "unsubscribe",
        })
        await self._ws.send(msg)
        self._subscriptions.discard(token_id)
        self._order_books.pop(token_id, None)
        self._price_history.pop(token_id, None)
        logger.debug(f"Unsubscribed from {token_id}")

    def get_mid_price(self, token_id: str) -> Optional[float]:
        """Get the current mid-price for a token."""
        book = self._order_books.get(token_id)
        if not book:
            return None

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        if not bids or not asks:
            return None

        try:
            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
        except (KeyError, TypeError, ValueError, IndexError):
            return None
        return (best_bid + best_ask) / 2

    def get_best_bid_ask(self, token_id: str) -> tuple[Optional[float], Optional[float]]:
        """Return the best bid and ask for a token, when available."""
        book = self._order_books.get(token_id)
        if not book:
            return None, None

        bids = book.get("bids", [])
        asks = book.get("asks", [])
        try:
            best_bid = float(bids[0]["price"]) if bids else None
            best_ask = float(asks[0]["price"]) if asks else None
        except (KeyError, TypeError, ValueError, IndexError):
            return None, None
        return best_bid, best_ask

    def get_book_age_seconds(self, token_id: str) -> Optional[float]:
        """Return the age of the most recent book snapshot for a token."""
        book = self._order_books.get(token_id)
        if not book:
            return None
        try:
            ts = float(book.get("timestamp", 0))
        except (TypeError, ValueError):
            return None
        if ts <= 0:
            return None
        return max(0.0, time.time() - ts)

    def get_price_history(
        self, token_id: str, lookback_seconds: float = 12.0
    ) -> list[tuple[float, float]]:
        """
        Get price history for a token within the lookback window.
        Returns list of (timestamp, price) tuples.
        """
        now = time.time()
        cutoff = now - lookback_seconds
        history = self._price_history.get(token_id, deque())
        return [(ts, price) for ts, price in history if ts >= cutoff]

    def get_price_drop(
        self, token_id: str, lookback_seconds: float = 12.0
    ) -> Optional[float]:
        """
        Calculate the maximum price drop within the lookback window.
        Returns the drop amount (positive = price fell), or None if insufficient data.
        """
        history = self.get_price_history(token_id, lookback_seconds)
        if len(history) < 2:
            return None

        max_price = max(price for _, price in history)
        current_price = history[-1][1]
        return max_price - current_price

    def get_order_book(self, token_id: str) -> Optional[dict]:
        """Get the current order book for a token."""
        return self._order_books.get(token_id)

    def get_spread(self, token_id: str) -> Optional[float]:
        """Get the bid-ask spread for a token (in price units)."""
        book = self._order_books.get(token_id)
        if not book:
            return None
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if not bids or not asks:
            return None
        try:
            return float(asks[0]["price"]) - float(bids[0]["price"])
        except (KeyError, TypeError, ValueError, IndexError):
            return None

    def get_depth(self, token_id: str, levels: int = 5) -> Optional[dict]:
        """
        Get total bid/ask depth (volume) up to N levels.
        Returns {"bid_depth": float, "ask_depth": float} or None.
        """
        book = self._order_books.get(token_id)
        if not book:
            return None
        bids = book.get("bids", [])[:levels]
        asks = book.get("asks", [])[:levels]
        bid_depth = sum(float(b.get("size", 0)) for b in bids)
        ask_depth = sum(float(a.get("size", 0)) for a in asks)
        return {"bid_depth": bid_depth, "ask_depth": ask_depth}

    def get_imbalance(self, token_id: str, levels: int = 5) -> Optional[float]:
        """
        Order book imbalance: bid_depth / (bid_depth + ask_depth).
        Returns 0.0-1.0 where >0.5 = more buying pressure.
        """
        depth = self.get_depth(token_id, levels)
        if not depth:
            return None
        total = depth["bid_depth"] + depth["ask_depth"]
        if total == 0:
            return None
        return depth["bid_depth"] / total

    def get_volatility(
        self, token_id: str, lookback_seconds: float = 300.0
    ) -> Optional[float]:
        """
        Price volatility (max - min) over a lookback window.
        Useful for market quality scoring.
        """
        history = self.get_price_history(token_id, lookback_seconds)
        if len(history) < 2:
            return None
        prices = [p for _, p in history]
        return max(prices) - min(prices)

    async def _process_message(self, raw: str):
        """Process an incoming WebSocket message."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raw_text = raw.strip()
            if raw_text == "NO NEW ASSETS":
                logger.debug(f"WebSocket server notice: {raw_text}")
            else:
                logger.warning(f"Invalid JSON from WebSocket: {raw[:100]}")
            return

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    await self._handle_message(item)
            return

        if not isinstance(data, dict):
            logger.debug(
                f"Ignoring unexpected WebSocket payload type: {type(data).__name__}"
            )
            return

        await self._handle_message(data)

    async def _handle_message(self, data: dict):
        """Handle a single decoded WebSocket payload."""
        msg_type = data.get("event_type") or data.get("type", "")

        if msg_type == "book":
            token_id = data.get("asset_id", "")
            if not token_id:
                return

            try:
                book = {
                    "bids": sorted(
                        data.get("bids", []),
                        key=lambda x: float(x.get("price", 0)),
                        reverse=True,
                    ),
                    "asks": sorted(
                        data.get("asks", []),
                        key=lambda x: float(x.get("price", 0)),
                    ),
                    "timestamp": data.get("timestamp", time.time()),
                }
            except (TypeError, ValueError) as e:
                logger.warning(
                    f"Malformed order book data for {token_id}: {e}"
                )
                return

            self._order_books[token_id] = book

            # Calculate and record mid price
            mid = self.get_mid_price(token_id)
            if mid is not None:
                now = time.time()
                self._price_history[token_id].append((now, mid))

                for cb in self._on_price_callbacks:
                    try:
                        cb(token_id, mid, now)
                    except Exception as e:
                        logger.error(f"Price callback error: {e}")

            for cb in self._on_book_callbacks:
                try:
                    cb(token_id, book)
                except Exception as e:
                    logger.error(f"Book callback error: {e}")

        elif msg_type == "error":
            logger.error(f"WebSocket error message: {data}")

    async def listen(self):
        """Main listen loop — processes messages until stopped."""
        if not self._ws:
            raise RuntimeError("WebSocket not connected")

        logger.info("WebSocket listener started")
        try:
            async for message in self._ws:
                if not self._running:
                    break
                await self._process_message(message)
        except websockets.ConnectionClosed as e:
            logger.warning(f"WebSocket connection closed: {e}")
        except Exception as e:
            logger.error(f"WebSocket listener error: {e}")
        finally:
            self._running = False

    async def run_with_reconnect(self, max_retries: int = -1):
        """Run the listener with automatic reconnection."""
        retries = 0
        while max_retries < 0 or retries < max_retries:
            connected_at = None
            did_reconnect = False
            try:
                if not self._ws or not self._running:
                    await self.connect()
                    did_reconnect = True
                connected_at = asyncio.get_running_loop().time()
                # Clear stale book data and re-subscribe after reconnect
                if did_reconnect:
                    self._order_books.clear()
                    self._price_history.clear()
                    for token_id in list(self._subscriptions):
                        await self.subscribe(token_id, force=True)
                await self.listen()
            except Exception as e:
                logger.error(f"WebSocket error (retry {retries}): {e}")

            # Reset backoff if connection was alive for >60s (not a fast-fail)
            if connected_at and (asyncio.get_running_loop().time() - connected_at) > 60:
                retries = 0
            else:
                retries += 1

            if not self._running:
                break

            wait = min(2 ** retries, 60)
            logger.info(f"Reconnecting in {wait}s...")
            await asyncio.sleep(wait)

    async def close(self):
        """Close the WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("WebSocket closed")
