"""
Market scanner — periodically scans for trading opportunities across strategies.
"""

import asyncio

from loguru import logger

import config
from strategies.base import BaseStrategy, Signal


class MarketScanner:
    """
    Orchestrates market scanning across all active strategies.
    Runs on a configurable interval.
    """

    def __init__(
        self,
        strategies: list[BaseStrategy],
        scan_interval_s: float = 30.0,
        strategy_timeout_s: float = config.STRATEGY_EVALUATION_TIMEOUT_S,
    ):
        self.strategies = strategies
        self.scan_interval_s = scan_interval_s
        self.strategy_timeout_s = strategy_timeout_s
        self._running = False

    async def _evaluate_strategy(self, strategy: BaseStrategy) -> list[Signal]:
        try:
            signals = await asyncio.wait_for(
                strategy.evaluate(),
                timeout=self.strategy_timeout_s,
            )
            if signals:
                logger.info(
                    f"Scanner: {strategy.name} generated "
                    f"{len(signals)} signal(s)"
                )
            return signals
        except asyncio.TimeoutError:
            logger.warning(
                f"Scanner: {strategy.name} timed out after "
                f"{self.strategy_timeout_s:.1f}s"
            )
        except Exception as e:
            logger.error(f"Scanner: {strategy.name} evaluation failed: {e}")
        return []

    async def scan_once(self) -> list[Signal]:
        """Run a single scan across all active strategies."""
        active_strategies = [s for s in self.strategies if s.is_active]
        if not active_strategies:
            return []

        results = await asyncio.gather(
            *(self._evaluate_strategy(strategy) for strategy in active_strategies)
        )

        all_signals: list[Signal] = []
        for signals in results:
            all_signals.extend(signals)
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
