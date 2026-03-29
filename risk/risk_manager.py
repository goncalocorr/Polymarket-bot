"""
Risk management — enforces position limits, daily stop-loss, and circuit breaker.
Singleton checked before every order execution.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from loguru import logger

import config
from strategies.base import Signal


class RiskManager:
    """
    Pre-order validation:
    1. Daily stop-loss triggered? → block all orders
    2. Circuit breaker active? → block until pause expires
    3. Existing position in this market? → skip
    4. Total exposure > 60%? → block
    5. Position size > 10%? → reduce to fit
    """

    _instance: Optional["RiskManager"] = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, total_capital: float = 500.0):
        if hasattr(self, "_initialized"):
            if total_capital != self.total_capital:
                logger.warning(
                    f"RiskManager singleton already initialized with "
                    f"${self.total_capital:.2f}, ignoring new ${total_capital:.2f}"
                )
            return
        self._initialized = True

        self.total_capital = total_capital
        self._daily_start_capital = total_capital
        self.daily_pnl = 0.0
        self._last_reset_date = ""
        self._consecutive_losses = 0
        self._circuit_breaker_until: Optional[datetime] = None
        self._daily_stop_triggered = False

        # Callbacks for alerts
        self._on_stop_loss_callbacks = []
        self._on_circuit_breaker_callbacks = []

    def on_stop_loss(self, callback):
        self._on_stop_loss_callbacks.append(callback)

    def on_circuit_breaker(self, callback):
        self._on_circuit_breaker_callbacks.append(callback)

    def update_capital(self, capital: float):
        """Update total capital (from balance check or P&L)."""
        self.total_capital = capital

    def _reset_daily(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._last_reset_date:
            self.daily_pnl = 0.0
            self._daily_start_capital = self.total_capital
            self._daily_stop_triggered = False
            self._last_reset_date = today

    def record_trade_result(self, pnl: float):
        """Record a trade result and update risk state."""
        self._reset_daily()
        self.daily_pnl += pnl
        self.total_capital += pnl

        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        # Check daily stop-loss (based on start-of-day capital, not reduced)
        daily_loss_limit = self._daily_start_capital * config.DAILY_LOSS_LIMIT_PCT
        if self.daily_pnl <= -daily_loss_limit:
            self._daily_stop_triggered = True
            logger.critical(
                f"DAILY STOP-LOSS TRIGGERED: P&L ${self.daily_pnl:.2f} "
                f"exceeds limit -${daily_loss_limit:.2f}"
            )
            for cb in self._on_stop_loss_callbacks:
                try:
                    cb(self.daily_pnl, self.total_capital)
                except Exception:
                    pass

        # Check circuit breaker
        if self._consecutive_losses >= config.CIRCUIT_BREAKER_CONSECUTIVE_LOSSES:
            self._circuit_breaker_until = datetime.now(timezone.utc) + timedelta(
                hours=config.CIRCUIT_BREAKER_PAUSE_HOURS
            )
            logger.warning(
                f"CIRCUIT BREAKER: {self._consecutive_losses} consecutive losses, "
                f"pausing until {self._circuit_breaker_until.isoformat()}"
            )
            self._consecutive_losses = 0
            for cb in self._on_circuit_breaker_callbacks:
                try:
                    cb(self._circuit_breaker_until)
                except Exception:
                    pass

    def validate_order(
        self,
        signal: Signal,
        current_exposure: float,
        has_existing_position: bool,
    ) -> tuple[bool, float, str]:
        """
        Validate whether an order should proceed.

        Returns:
            (allowed, adjusted_size_usdc, reason)
        """
        self._reset_daily()

        # 1. Daily stop-loss
        if self._daily_stop_triggered:
            return False, 0, "Daily stop-loss triggered"

        # 2. Circuit breaker
        if self._circuit_breaker_until:
            now = datetime.now(timezone.utc)
            if now < self._circuit_breaker_until:
                remaining = (self._circuit_breaker_until - now).total_seconds() / 60
                return False, 0, f"Circuit breaker active ({remaining:.0f}min remaining)"
            else:
                self._circuit_breaker_until = None

        # 3. Existing position
        if has_existing_position:
            return False, 0, "Already have position in this market"

        # 4. Total exposure limit
        max_exposure = self.total_capital * config.MAX_TOTAL_EXPOSURE_PCT
        if current_exposure >= max_exposure:
            return False, 0, (
                f"Total exposure ${current_exposure:.2f} >= "
                f"limit ${max_exposure:.2f}"
            )

        # 5. Position size limit
        max_position = self.total_capital * config.MAX_POSITION_SIZE_PCT
        size = signal.size_usdc if signal.size_usdc > 0 else max_position
        size = min(size, max_position)

        # Also cap to remaining exposure room
        remaining_room = max_exposure - current_exposure
        size = min(size, remaining_room)

        if size < config.MIN_ORDER_SIZE_USDC:
            return False, 0, f"Adjusted size ${size:.2f} below minimum"

        return True, size, "OK"

    @property
    def status(self) -> str:
        """Current risk status string."""
        if self._daily_stop_triggered:
            return "STOPPED"
        if self._circuit_breaker_until and datetime.now(timezone.utc) < self._circuit_breaker_until:
            return "PAUSED"
        return "RUNNING"
