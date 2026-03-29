"""
Flash Crash Strategy — detects sudden probability drops on 15-min crypto markets.
Adapted from discountry/polymarket-trading-bot/strategies/flash_crash_strategy.py
"""

import asyncio
from datetime import datetime, timezone

from loguru import logger

import config
from core.gamma_client import GammaClient
from core.websocket_client import MarketWebSocket
from strategies.base import BaseStrategy, Signal


class FlashCrashStrategy(BaseStrategy):
    """
    Monitors 15-minute Up/Down crypto markets via WebSocket.
    If probability drops >= threshold within lookback window, generates a BUY signal.
    """

    def __init__(
        self,
        ws: MarketWebSocket,
        gamma: GammaClient,
        drop_threshold: float = config.FLASH_CRASH_DROP_THRESHOLD,
        lookback_seconds: float = config.FLASH_CRASH_LOOKBACK_SECONDS,
        size_usdc: float = config.FLASH_CRASH_SIZE_USDC,
        take_profit: float = config.FLASH_CRASH_TAKE_PROFIT,
        stop_loss: float = config.FLASH_CRASH_STOP_LOSS,
    ):
        super().__init__("flash_crash")
        self.ws = ws
        self.gamma = gamma
        self.drop_threshold = drop_threshold
        self.lookback_seconds = lookback_seconds
        self.size_usdc = size_usdc
        self.take_profit = take_profit
        self.stop_loss = stop_loss

        # token_id -> market metadata
        self._tracked_markets: dict[str, dict] = {}
        # token_id -> True if we already signalled (avoid duplicates per market)
        self._signalled: dict[str, bool] = {}

    async def initialize(self):
        """Discover 15-min crypto markets and subscribe to their order books."""
        logger.info("FlashCrash: Initializing — scanning for 15-min crypto markets")
        markets = await self.gamma.get_15min_crypto_markets()

        for m in markets:
            tokens = m.get("tokens", []) or m.get("clobTokenIds", [])
            question = m.get("question", "Unknown")
            for token in tokens:
                token_id = token if isinstance(token, str) else token.get("token_id", "")
                if token_id:
                    self._tracked_markets[token_id] = {
                        "question": question,
                        "condition_id": m.get("conditionId", ""),
                        "end_date": m.get("endDate", ""),
                    }
                    await self.ws.subscribe(token_id)

        logger.info(
            f"FlashCrash: Tracking {len(self._tracked_markets)} tokens "
            f"across {len(markets)} markets"
        )

    async def evaluate(self) -> list[Signal]:
        """Check all tracked markets for flash crash conditions."""
        if not self.is_active:
            return []

        signals = []

        for token_id, meta in self._tracked_markets.items():
            # Skip if already signalled for this market
            if self._signalled.get(token_id):
                continue

            drop = self.ws.get_price_drop(token_id, self.lookback_seconds)
            if drop is None:
                continue

            current_price = self.ws.get_mid_price(token_id)
            if current_price is None or current_price <= 0:
                continue

            if drop >= self.drop_threshold:
                logger.warning(
                    f"FlashCrash DETECTED: {meta['question']} "
                    f"dropped {drop:.3f} in {self.lookback_seconds}s "
                    f"(current: {current_price:.3f})"
                )

                signal = Signal(
                    strategy=self.name,
                    token_id=token_id,
                    market_question=meta["question"],
                    side="BUY",
                    price=current_price,
                    size_usdc=self.size_usdc,
                    take_profit=min(current_price + self.take_profit, 0.99),
                    stop_loss=max(current_price - self.stop_loss, 0.01),
                    confidence=min(drop / self.drop_threshold, 1.0),
                    reasoning=(
                        f"Price dropped {drop:.3f} in {self.lookback_seconds}s "
                        f"(threshold: {self.drop_threshold})"
                    ),
                )
                signals.append(signal)
                self._signalled[token_id] = True

        return signals

    async def refresh_markets(self):
        """
        Refresh the list of tracked markets.
        Called periodically since 15-min markets expire and new ones appear.
        """
        # Unsubscribe from expired markets
        expired = []
        now = datetime.now(timezone.utc)
        for token_id, meta in self._tracked_markets.items():
            end_str = meta.get("end_date", "")
            if end_str:
                try:
                    end_date = datetime.fromisoformat(
                        end_str.replace("Z", "+00:00")
                    )
                    if now > end_date:
                        expired.append(token_id)
                except (ValueError, TypeError):
                    pass

        for token_id in expired:
            await self.ws.unsubscribe(token_id)
            del self._tracked_markets[token_id]
            self._signalled.pop(token_id, None)

        if expired:
            logger.info(f"FlashCrash: Removed {len(expired)} expired markets")

        # Subscribe to new markets (only ones not already tracked)
        markets = await self.gamma.get_15min_crypto_markets()
        new_count = 0
        for m in markets:
            tokens = m.get("tokens", []) or m.get("clobTokenIds", [])
            question = m.get("question", "Unknown")
            for token in tokens:
                token_id = token if isinstance(token, str) else token.get("token_id", "")
                if token_id and token_id not in self._tracked_markets:
                    self._tracked_markets[token_id] = {
                        "question": question,
                        "condition_id": m.get("conditionId", ""),
                        "end_date": m.get("endDate", ""),
                    }
                    await self.ws.subscribe(token_id)
                    new_count += 1
        if new_count:
            logger.info(f"FlashCrash: Subscribed to {new_count} new markets")

    async def shutdown(self):
        """Unsubscribe from all tracked markets."""
        for token_id in list(self._tracked_markets.keys()):
            await self.ws.unsubscribe(token_id)
        self._tracked_markets.clear()
        self._signalled.clear()
        logger.info("FlashCrash: Shut down")
