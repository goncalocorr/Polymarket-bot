"""
Risk management — enforces position limits, daily stop-loss, and circuit breaker.
Checked before every order execution.
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

    def __init__(self, total_capital: float = config.TOTAL_CAPITAL):
        self.total_capital = total_capital
        self._daily_start_capital = total_capital
        self.daily_pnl = 0.0
        self._last_reset_date = ""
        self._consecutive_losses = 0
        self._circuit_breaker_until: Optional[datetime] = None
        self._daily_stop_triggered = False

        # Async lock to serialise state mutations (record_trade_result,
        # validate_order, update_capital) across concurrent coroutines.
        self._lock = asyncio.Lock()

        # Callbacks for alerts
        self._on_stop_loss_callbacks = []
        self._on_circuit_breaker_callbacks = []

    def reset(self, total_capital: float = 500.0):
        """Reset all state — useful for tests and clean restarts."""
        self.total_capital = total_capital
        self._daily_start_capital = total_capital
        self.daily_pnl = 0.0
        self._last_reset_date = ""
        self._consecutive_losses = 0
        self._circuit_breaker_until = None
        self._daily_stop_triggered = False

    def on_stop_loss(self, callback):
        self._on_stop_loss_callbacks.append(callback)

    def on_circuit_breaker(self, callback):
        self._on_circuit_breaker_callbacks.append(callback)

    async def update_capital(self, capital: float):
        """Update total capital (from balance check or P&L)."""
        async with self._lock:
            self.total_capital = capital

    async def restore_state(
        self,
        total_capital: float,
        daily_pnl: float,
        *,
        circuit_breaker_until: Optional[datetime] = None,
    ):
        """Restore risk state after a restart from persisted trade history."""
        async with self._lock:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self.total_capital = total_capital
            self.daily_pnl = daily_pnl
            self._last_reset_date = today
            self._daily_start_capital = max(total_capital - daily_pnl, 0.01)
            self._circuit_breaker_until = circuit_breaker_until
            daily_loss_limit = self._daily_start_capital * config.DAILY_LOSS_LIMIT_PCT
            self._daily_stop_triggered = daily_pnl <= -daily_loss_limit

    def _reset_daily(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._last_reset_date:
            self.daily_pnl = 0.0
            self._daily_start_capital = self.total_capital
            self._daily_stop_triggered = False
            self._last_reset_date = today

    def _counts_toward_circuit_breaker(self, pnl: float, close_reason: str = "") -> bool:
        """Whether a closed trade should count as a consecutive loss."""
        if pnl >= 0:
            return False
        if config.DRY_RUN and close_reason == "time-stop":
            return False
        return True

    async def record_trade_result(self, pnl: float, close_reason: str = ""):
        """Record a trade result and update risk state."""
        async with self._lock:
            self._reset_daily()
            self.daily_pnl += pnl
            self.total_capital += pnl

            if self._counts_toward_circuit_breaker(pnl, close_reason):
                self._consecutive_losses += 1
            elif pnl > 0:
                # Only actual wins reset the streak; breakeven (pnl == 0)
                # leaves the counter unchanged — matches restore logic in main.py.
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

    def kelly_size(
        self,
        estimated_prob: float,
        market_price: float,
        confidence: float = 1.0,
    ) -> float:
        """
        Calculate position size using the Kelly Criterion.

        Kelly formula for binary outcomes:
            f* = (b*p - q) / b
        where p = estimated probability, q = 1-p, b = net odds (payout/risk).

        For prediction markets at price `market_price`:
            b = (1 - market_price) / market_price  (buy YES at market_price, win pays $1)

        Returns the fraction of bankroll to wager (0.0 to 1.0), already
        multiplied by KELLY_FRACTION (Half-Kelly) and confidence.
        """
        if market_price <= 0.01 or market_price >= 0.99:
            return 0.0

        p = max(0.0, min(1.0, estimated_prob))
        q = 1.0 - p

        # Net odds: profit if win / cost to enter
        b = (1.0 - market_price) / market_price

        # Kelly fraction
        f_star = (b * p - q) / b if b > 0 else 0.0

        # Negative Kelly = no edge, don't bet
        if f_star <= 0:
            return 0.0

        # Apply fractional Kelly and confidence scaling
        f_adjusted = f_star * config.KELLY_FRACTION * confidence

        # Cap at MAX_POSITION_SIZE_PCT to respect existing risk limits
        return min(f_adjusted, config.MAX_POSITION_SIZE_PCT)

    async def validate_order(
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
        async with self._lock:
            return self._validate_order_inner(
                signal, current_exposure, has_existing_position
            )

    def _validate_order_inner(
        self,
        signal: Signal,
        current_exposure: float,
        has_existing_position: bool,
    ) -> tuple[bool, float, str]:
        """Inner validation logic — must be called under self._lock."""
        self._reset_daily()

        # 0. Sanity check on signal price
        if signal.price <= 0 or signal.price >= 1:
            return False, 0, f"Invalid signal price {signal.price}"

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

        estimated_prob = signal.estimated_prob

        # 5. Position sizing ? Kelly or fixed
        if signal.estimated_prob is not None and signal.price > 0:
            edge = abs(estimated_prob - signal.price)
            if edge < config.MIN_KELLY_EDGE:
                return False, 0, (
                    f"Edge {edge:.3f} below minimum {config.MIN_KELLY_EDGE}"
                )

            kelly_frac = self.kelly_size(
                estimated_prob=estimated_prob,
                market_price=signal.price,
                confidence=max(signal.confidence, 0.0),
            )

            if kelly_frac <= 0:
                return False, 0, "Kelly says no edge"

            size = self.total_capital * kelly_frac
            sizing_method = "kelly-estimated"
        elif (
            estimated_prob is None
            and signal.confidence >= config.KELLY_MIN_CONFIDENCE
            and signal.price > 0
        ):
            # No explicit estimated_prob — use confidence as a rough proxy.
            # This is an approximation: confidence ≠ probability, but it's the
            # best signal available when strategies don't provide estimated_prob.
            proxy_prob = signal.confidence
            edge = abs(proxy_prob - signal.price)
            if edge < config.MIN_KELLY_EDGE:
                return False, 0, (
                    f"Edge {edge:.3f} below minimum {config.MIN_KELLY_EDGE}"
                )

            kelly_frac = self.kelly_size(
                estimated_prob=proxy_prob,
                market_price=signal.price,
                confidence=1.0,  # scaling handled by KELLY_FRACTION alone
            )

            if kelly_frac <= 0:
                return False, 0, "Kelly says no edge"

            size = self.total_capital * kelly_frac
            sizing_method = "kelly"
        else:
            # Fallback: fixed percentage
            max_position = self.total_capital * config.MAX_POSITION_SIZE_PCT
            size = signal.size_usdc if signal.size_usdc > 0 else max_position
            size = min(size, max_position)
            sizing_method = "fixed"

        # Also cap to remaining exposure room
        remaining_room = max_exposure - current_exposure
        size = min(size, remaining_room)

        if size < config.MIN_ORDER_SIZE_USDC:
            return False, 0, f"Adjusted size ${size:.2f} below minimum ({sizing_method})"

        return True, size, f"OK ({sizing_method})"

    def _snapshot_state(self) -> tuple[bool, Optional[datetime]]:
        """Snapshot volatile fields together to avoid torn reads."""
        return self._daily_stop_triggered, self._circuit_breaker_until

    @property
    def status(self) -> str:
        """Current risk status string."""
        stopped, cb_until = self._snapshot_state()
        if stopped:
            return "STOPPED"
        if cb_until and datetime.now(timezone.utc) < cb_until:
            return "PAUSED"
        return "RUNNING"

    @property
    def stop_reason(self) -> str:
        """Human-readable reason for the current non-running status."""
        stopped, cb_until = self._snapshot_state()
        if stopped:
            return "Daily stop-loss triggered"
        if cb_until and datetime.now(timezone.utc) < cb_until:
            return "Circuit breaker active"
        return ""

    @property
    def resume_at(self) -> str:
        """UTC resume timestamp when a temporary pause is active."""
        _, cb_until = self._snapshot_state()
        if cb_until and datetime.now(timezone.utc) < cb_until:
            return cb_until.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        return ""
