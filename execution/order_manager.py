"""
Order lifecycle management — tracks open positions and manages exits.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from dateutil.parser import isoparse
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

import config
from core.bot import TradingBot
from core.websocket_client import MarketWebSocket
from execution.executor import ThreePhaseExecutor


@dataclass
class Position:
    """An open trading position."""

    id: str
    token_id: str
    market_question: str
    strategy: str
    side: str
    entry_price: float
    size_usdc: float
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    close_reason: str = ""
    pnl_usdc: float = 0.0
    status: str = "OPEN"  # OPEN, CLOSING, CLOSED, STOPPED

    @property
    def is_open(self) -> bool:
        return self.status == "OPEN"

    def current_pnl(self, current_price: float) -> float:
        """Calculate unrealised P&L at a given price."""
        if self.side == "BUY":
            if self.entry_price <= 0:
                return 0.0
            shares = self.size_usdc / self.entry_price
            return shares * (current_price - self.entry_price)
        else:
            denom = 1 - self.entry_price
            if denom <= 0:
                return 0.0
            shares = self.size_usdc / denom
            return shares * (self.entry_price - current_price)


class OrderManager:
    """
    Manages the full lifecycle of positions:
    - Opens positions via ThreePhaseExecutor
    - Monitors take-profit / stop-loss levels
    - Closes positions when exit conditions are met
    """

    def __init__(
        self,
        bot: TradingBot,
        ws: MarketWebSocket,
        executor: ThreePhaseExecutor,
    ):
        self.bot = bot
        self.ws = ws
        self.executor = executor
        self.positions: dict[str, Position] = {}  # id -> Position
        self._lock = asyncio.Lock()

    def load_open_positions(self, open_trades: list[dict]):
        """Restore open positions from database rows after a restart."""
        for t in open_trades:
            pos = Position(
                id=t["id"],
                token_id=t["token_id"],
                market_question=t.get("market_question", ""),
                strategy=t["strategy"],
                side=t["side"],
                entry_price=t["entry_price"],
                size_usdc=t["size_usdc"],
                take_profit=(
                    float(t["take_profit"])
                    if t.get("take_profit") is not None
                    else None
                ),
                stop_loss=(
                    float(t["stop_loss"])
                    if t.get("stop_loss") is not None
                    else None
                ),
                opened_at=(
                    isoparse(t["opened_at"])
                    if isinstance(t.get("opened_at"), str)
                    else datetime.now(timezone.utc)
                ),
                close_reason=t.get("close_reason", "") or "",
            )
            self.positions[pos.id] = pos
        if open_trades:
            logger.info(f"Restored {len(open_trades)} open positions from database")

    def _next_id(self) -> str:
        return f"pos-{uuid.uuid4().hex[:12]}"

    async def open_position(
        self,
        token_id: str,
        market_question: str,
        strategy: str,
        side: str,
        price: float,
        size_usdc: float,
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
    ) -> Optional[Position]:
        """Open a new position via the three-phase executor.

        The executor call runs outside _lock to avoid blocking exit checks
        for the full 45-95s execution duration. Only the dict mutation is locked.
        """
        result = await self.executor.execute(token_id, price, size_usdc, side)

        if not result.filled and result.fill_size == 0:
            logger.warning(
                f"Failed to open position: {strategy} {side} "
                f"{size_usdc} USDC at {price} for '{market_question[:50]}...'"
            )
            return None

        pos = Position(
            id=self._next_id(),
            token_id=token_id,
            market_question=market_question,
            strategy=strategy,
            side=side,
            entry_price=result.fill_price,
            size_usdc=result.fill_size,
            take_profit=take_profit,
            stop_loss=stop_loss,
        )
        async with self._lock:
            self.positions[pos.id] = pos

        logger.info(
            f"Position opened: {pos.id} — {side} {result.fill_size:.2f} USDC "
            f"at {result.fill_price:.4f} [{strategy}] "
            f"(phases: {result.phases_used})"
        )
        return pos

    async def close_position(
        self, position_id: str, reason: str = ""
    ) -> Optional[Position]:
        """Close an open position."""
        async with self._lock:
            pos = self.positions.get(position_id)
            if not pos or not pos.is_open:
                return None

            # Prevent re-entry: mark as CLOSING before any async work
            pos.status = "CLOSING"

        current_price = self.ws.get_mid_price(pos.token_id)
        if current_price is None:
            current_price = pos.entry_price

        # Place exit order (opposite side)
        exit_side = "SELL" if pos.side == "BUY" else "BUY"
        result = await self.executor.execute(
            pos.token_id, current_price, pos.size_usdc, exit_side
        )

        if not result.filled and result.fill_size == 0:
            logger.error(
                f"Exit FAILED for {pos.id} ({reason or 'manual'}) — "
                f"position remains OPEN. Error: {result.error}"
            )
            async with self._lock:
                pos.status = "OPEN"  # Revert from CLOSING under lock
            return None

        # Partial exit: reduce position size, keep OPEN for remainder
        if result.fill_size < pos.size_usdc * 0.95 and not result.filled:
            # Compute realized P&L on the partial fill
            exit_price = result.fill_price if result.fill_price > 0 else current_price
            if pos.side == "BUY" and pos.entry_price > 0:
                partial_shares = result.fill_size / pos.entry_price
                partial_pnl = partial_shares * (exit_price - pos.entry_price)
            elif pos.side != "BUY" and (1 - pos.entry_price) > 0:
                partial_shares = result.fill_size / (1 - pos.entry_price)
                partial_pnl = partial_shares * (pos.entry_price - exit_price)
            else:
                partial_pnl = 0.0
            logger.warning(
                f"Partial exit for {pos.id}: {result.fill_size:.2f}/{pos.size_usdc:.2f} "
                f"(partial P&L: ${partial_pnl:+.2f}) — position remains OPEN for remainder"
            )
            async with self._lock:
                pos.size_usdc -= result.fill_size
                pos.status = "OPEN"  # Revert from CLOSING under lock
            # Persist reduced size to DB so restarts reflect partial exit
            self._on_partial_exit(pos, result.fill_size, partial_pnl)
            return None

        async with self._lock:
            pos.exit_price = result.fill_price if result.filled else current_price
            pos.close_reason = reason or "manual"
            pos.pnl_usdc = pos.current_pnl(pos.exit_price)
            pos.closed_at = datetime.now(timezone.utc)
            pos.status = "CLOSED"

        logger.info(
            f"Position closed: {pos.id} — P&L: ${pos.pnl_usdc:+.2f} "
            f"({reason or 'manual'})"
        )
        return pos

    def _on_partial_exit(self, pos: Position, fill_size: float, partial_pnl: float = 0.0):
        """Hook for subclasses/callers to persist partial exit size changes."""
        pass

    async def check_exits(self) -> list[Position]:
        """Check all open positions for take-profit / stop-loss conditions."""
        closed = []
        now = datetime.now(timezone.utc)

        # Snapshot candidates under lock to avoid TOCTOU with close_position
        async with self._lock:
            candidates = [p for p in self.positions.values() if p.is_open]

        for pos in candidates:
            # Re-check status: close_position may have set CLOSING between snapshot and here
            if not pos.is_open:
                continue

            current_price = self.ws.get_mid_price(pos.token_id)
            if current_price is None:
                continue

            unrealised = pos.current_pnl(current_price)

            # Take profit check (direction depends on side)
            if pos.take_profit is not None:
                tp_hit = (
                    current_price >= pos.take_profit if pos.side == "BUY"
                    else current_price <= pos.take_profit
                )
                if tp_hit:
                    p = await self.close_position(pos.id, "take-profit")
                    if p:
                        closed.append(p)
                    continue

            # Stop loss check (direction depends on side)
            if pos.stop_loss is not None:
                sl_hit = (
                    current_price <= pos.stop_loss if pos.side == "BUY"
                    else current_price >= pos.stop_loss
                )
                if sl_hit:
                    p = await self.close_position(pos.id, "stop-loss")
                    if p:
                        closed.append(p)
                    continue  # Don't fall through to position-stop check

            # Mispricing positions should not sit indefinitely if the market
            # never converges. Close them after a bounded holding window.
            if (
                pos.strategy == "mispricing"
                and (now - pos.opened_at).total_seconds()
                >= config.MISPRICING_MAX_HOLD_MIN * 60
            ):
                p = await self.close_position(pos.id, "time-stop")
                if p:
                    closed.append(p)
                continue

            # Position-level stop: -20% of invested amount.
            # If the strategy provided an explicit stop, use that instead.
            if pos.stop_loss is None and unrealised <= -(pos.size_usdc * 0.20):
                p = await self.close_position(pos.id, "position-stop-20%")
                if p:
                    closed.append(p)

        # Prune closed positions to prevent unbounded memory growth
        async with self._lock:
            closed_ids = [
                pid for pid, p in self.positions.items()
                if p.status in ("CLOSED", "STOPPED")
            ]
            if len(closed_ids) > 100:
                closed_positions = sorted(
                    [(pid, self.positions[pid]) for pid in closed_ids],
                    key=lambda x: x[1].closed_at or datetime.min.replace(tzinfo=timezone.utc),
                )
                for pid, _ in closed_positions[:-50]:  # Keep last 50
                    del self.positions[pid]

        return closed

    def get_open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.is_open]

    def get_closed_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if not p.is_open]

    def get_total_exposure(self) -> float:
        """Total USDC currently in open positions."""
        return sum(p.size_usdc for p in self.positions.values() if p.is_open)

    def has_position_in_market(self, token_id: str) -> bool:
        """Check if we already have an open or closing position for this token."""
        return any(
            p.token_id == token_id and p.status in ("OPEN", "CLOSING")
            for p in self.positions.values()
        )
