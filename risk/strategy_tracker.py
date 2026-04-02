"""
Strategy Performance Tracker — monitors per-strategy win rate, P&L, and
automatically pauses underperforming strategies or boosts outperforming ones.

Uses a rolling window of the last N trades per strategy to detect drawdowns
early and adapt capital allocation.
"""

from datetime import datetime, timezone, timedelta

from loguru import logger

import config
from storage.database import Database
from strategies.base import BaseStrategy


class StrategyTracker:
    """
    Tracks per-strategy performance and controls strategy lifecycle.

    - Auto-pause: win rate < 35% over last 20 trades → pause 4h
    - Auto-boost: win rate > 65% over last 20 trades → 1.5x Kelly size
    """

    def __init__(self, db: Database, strategies: list[BaseStrategy]):
        self.db = db
        self._strategies = {s.name: s for s in strategies}
        self._paused_until: dict[str, datetime] = {}

    def record_trade(self, strategy_name: str, pnl: float):
        """Record a completed trade for a strategy."""
        won = 1 if pnl > 0 else 0
        self.db.record_strategy_trade(strategy_name, pnl, won)

        # Check auto-pause / auto-boost after recording
        self._evaluate_strategy(strategy_name)

    def get_stats(self, strategy_name: str) -> dict:
        """Get rolling stats for a strategy."""
        return self.db.get_strategy_rolling_stats(
            strategy_name, config.TRACKER_ROLLING_WINDOW
        )

    def get_size_multiplier(self, strategy_name: str) -> float:
        """
        Returns position size multiplier for a strategy:
        - 0.0 if paused
        - 1.5 if boosted (high win rate)
        - 1.0 otherwise
        """
        # Check pause
        paused_until = self._paused_until.get(strategy_name)
        if paused_until:
            now = datetime.now(timezone.utc)
            if now < paused_until:
                return 0.0
            else:
                # Pause expired, resume
                del self._paused_until[strategy_name]
                strategy = self._strategies.get(strategy_name)
                if strategy:
                    strategy.resume()
                    logger.info(f"StrategyTracker: {strategy_name} auto-resumed")

        # Check boost
        stats = self.get_stats(strategy_name)
        total = stats.get("total_trades", 0)
        if total >= config.TRACKER_ROLLING_WINDOW // 2:
            win_rate = stats.get("win_rate", 0.5)
            if win_rate >= config.TRACKER_BOOST_WIN_RATE:
                return config.TRACKER_BOOST_FACTOR

        return 1.0

    def _evaluate_strategy(self, strategy_name: str):
        """Check if a strategy should be paused based on recent performance."""
        stats = self.get_stats(strategy_name)
        total = stats.get("total_trades", 0)

        if total < config.TRACKER_ROLLING_WINDOW // 2:
            return  # Not enough data yet

        win_rate = stats.get("win_rate", 0.5)

        if win_rate < config.TRACKER_AUTO_PAUSE_WIN_RATE:
            strategy = self._strategies.get(strategy_name)
            if strategy and strategy.is_active:
                pause_until = datetime.now(timezone.utc) + timedelta(
                    hours=config.TRACKER_AUTO_PAUSE_HOURS
                )
                self._paused_until[strategy_name] = pause_until
                strategy.pause()
                logger.warning(
                    f"StrategyTracker: AUTO-PAUSED {strategy_name} — "
                    f"win rate {win_rate:.1%} < {config.TRACKER_AUTO_PAUSE_WIN_RATE:.0%} "
                    f"over last {total} trades. Resuming at "
                    f"{pause_until.strftime('%H:%M UTC')}"
                )

    def get_all_stats(self) -> dict[str, dict]:
        """Get rolling stats for all tracked strategies."""
        result = {}
        for name in self._strategies:
            result[name] = self.get_stats(name)
        return result

    def reconcile_states(self):
        """
        Refresh pause/boost state for every tracked strategy.

        This is important before each scan cycle because paused strategies are
        skipped by the scanner; without an explicit reconciliation pass they
        would never get a chance to auto-resume.
        """
        for name in self._strategies:
            self.get_size_multiplier(name)
