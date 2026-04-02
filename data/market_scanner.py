"""
Market scanner — periodically scans for trading opportunities across strategies.
"""

import asyncio

from loguru import logger

from strategies.base import BaseStrategy, Signal


class MarketScanner:
    """
    Orchestrates market scanning across all active strategies.
    Runs on a configurable interval.
    """

    def __init__(self, strategies: list[BaseStrategy], scan_interval_s: float = 30.0):
        self.strategies = strategies
        self.scan_interval_s = scan_interval_s
        self._running = False

    async def scan_once(self) -> list[Signal]:
        """Run a single scan across all active strategies."""
        all_signals = []

        for strategy in self.strategies:
            if not strategy.is_active:
                continue

            try:
                signals = await strategy.evaluate()
                if signals:
                    logger.info(
                        f"Scanner: {strategy.name} generated "
                        f"{len(signals)} signal(s)"
                    )
                    all_signals.extend(signals)
            except Exception as e:
                logger.error(f"Scanner: {strategy.name} evaluation failed: {e}")

        return all_signals

    async def run(self):
        """Continuous scanning loop."""
        self._running = True
        logger.info(
            f"Market scanner started ({len(self.strategies)} strategies, "
            f"interval: {self.scan_interval_s}s)"
        )

        while self._running:
            try:
                await self.scan_once()
            except Exception as e:
                logger.error(f"Scanner loop error: {e}")

            await asyncio.sleep(self.scan_interval_s)

    def stop(self):
        self._running = False
