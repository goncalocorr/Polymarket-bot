"""
Three-Phase Order Executor — progressively adjusts price to get fills.
Ported from zydomus219/Polymarket-betting-bot (TypeScript → Python).
"""

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Optional

from loguru import logger

import config
from core.bot import TradingBot


@dataclass
class ExecutionResult:
    """Result of a three-phase execution attempt."""

    filled: bool
    fill_price: float
    fill_size: float
    phases_used: int
    order_id: str = ""
    error: str = ""


class ThreePhaseExecutor:
    """
    Attempts to fill an order across up to 3 phases with progressive price adjustments.

    Phase 1: place at current price, wait PHASE1_TIMEOUT
    Phase 2: adjust price +PHASE2_INCREMENT cents, reduce size 5%, wait PHASE2_TIMEOUT
    Phase 3: adjust price +PHASE3_INCREMENT cents, reduce size another 5%, final attempt
    """

    def __init__(self, bot: TradingBot):
        self.bot = bot

    async def _simulate_dry_run_fill(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        phase: int,
        order_id: str,
    ) -> ExecutionResult:
        """
        Simulate a non-perfect dry-run fill.

        Dry run still assumes eventual execution, but adds a small latency and
        adverse price movement so backtests are not unrealistically optimistic.
        """
        delay_s = max(config.DRY_RUN_SIMULATED_DELAY_MS, 0) / 1000
        if delay_s:
            await asyncio.sleep(delay_s)

        seed = hashlib.sha256(
            f"{token_id}:{side}:{phase}:{price:.6f}:{size:.4f}".encode()
        ).digest()
        slip_scale = int.from_bytes(seed[:2], "big") / 65535
        slip_bps = max(config.DRY_RUN_SLIPPAGE_BPS, 0) * (0.4 + 0.6 * slip_scale)
        slip = max(0.001, price * slip_bps / 10_000)
        fill_price = min(price + slip, 0.99) if side == "BUY" else max(price - slip, 0.01)

        return ExecutionResult(
            filled=True,
            fill_price=fill_price,
            fill_size=size,
            phases_used=phase,
            order_id=order_id,
        )

    async def execute(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
    ) -> ExecutionResult:
        """Execute an order using the three-phase approach."""
        remaining_size = size
        total_filled = 0.0
        total_cost = 0.0  # For VWAP fill price

        for phase in range(1, 4):
            if remaining_size < config.MIN_ORDER_SIZE_USDC:
                logger.warning(
                    f"Phase {phase}: Remaining size {remaining_size:.2f} "
                    f"below minimum {config.MIN_ORDER_SIZE_USDC}"
                )
                break

            # Calculate phase-specific parameters
            # For BUY: increase price to be more aggressive
            # For SELL: decrease price to be more aggressive
            if phase == 1:
                phase_price = price
                timeout = config.PHASE1_TIMEOUT_S
            elif phase == 2:
                increment = config.PHASE2_INCREMENT_CENTS / 100
                if side == "BUY":
                    phase_price = min(price + increment, 0.99)
                else:
                    phase_price = max(price - increment, 0.01)
                remaining_size *= 0.95
                timeout = config.PHASE2_TIMEOUT_S
            else:
                increment = (config.PHASE2_INCREMENT_CENTS + config.PHASE3_INCREMENT_CENTS) / 100
                if side == "BUY":
                    phase_price = min(price + increment, 0.99)
                else:
                    phase_price = max(price - increment, 0.01)
                remaining_size *= 0.95
                timeout = config.PHASE3_TIMEOUT_S

            logger.info(
                f"Phase {phase}: {side} {remaining_size:.2f} USDC "
                f"at {phase_price:.4f} (timeout: {timeout}s)"
            )

            # Place order
            result = await self.bot.place_order(
                token_id=token_id,
                price=phase_price,
                size=remaining_size,
                side=side,
            )

            if not result:
                return ExecutionResult(
                    filled=False,
                    fill_price=0,
                    fill_size=0,
                    phases_used=phase,
                    error="Order placement failed",
                )

            order_id = result.get("id", "")

            if config.DRY_RUN:
                return await self._simulate_dry_run_fill(
                    token_id=token_id,
                    price=phase_price,
                    size=remaining_size,
                    side=side,
                    phase=phase,
                    order_id=order_id,
                )

            # Wait for fill
            filled_size = await self._wait_for_fill(order_id, timeout)

            if filled_size < 0:
                # API unreachable — don't cancel (order may be filled on-chain)
                logger.warning(
                    f"Phase {phase}: Cannot confirm fill status for {order_id}, "
                    f"skipping cancel to avoid double-execution"
                )
                return ExecutionResult(
                    filled=False,
                    fill_price=0,
                    fill_size=0,
                    phases_used=phase,
                    order_id=order_id,
                    error="API unreachable — order status unknown",
                )

            if filled_size >= remaining_size * 0.95:
                # Fully filled (within 5% tolerance)
                actual_price = await self._get_fill_price(order_id, phase_price)
                total_filled += filled_size
                total_cost += filled_size * actual_price
                return ExecutionResult(
                    filled=True,
                    fill_price=total_cost / total_filled if total_filled > 0 else actual_price,
                    fill_size=total_filled,
                    phases_used=phase,
                    order_id=order_id,
                )

            # Partially filled or unfilled — cancel remaining
            if filled_size > 0:
                actual_price = await self._get_fill_price(order_id, phase_price)
                total_filled += filled_size
                total_cost += filled_size * actual_price
                remaining_size -= filled_size
                logger.info(
                    f"Phase {phase}: Partially filled {filled_size:.2f} USDC, "
                    f"{remaining_size:.2f} remaining"
                )

            cancelled = await self.bot.cancel_order(order_id)
            if not cancelled:
                logger.error(
                    f"Phase {phase}: Failed to cancel order {order_id} — "
                    f"aborting to avoid double exposure"
                )
                return ExecutionResult(
                    filled=False,
                    fill_price=total_cost / total_filled if total_filled > 0 else price,
                    fill_size=total_filled,
                    phases_used=phase,
                    order_id=order_id,
                    error="Cancel failed — manual intervention needed",
                )

        avg_price = total_cost / total_filled if total_filled > 0 else price
        return ExecutionResult(
            filled=False,
            fill_price=avg_price,
            fill_size=total_filled,
            phases_used=3,
            error="All phases exhausted",
        )

    async def _get_fill_price(self, order_id: str, fallback: float) -> float:
        """Fetch the actual fill price from order details, falling back to limit price."""
        try:
            details = await self.bot.get_order(order_id)
            if details:
                for key in ("avgPrice", "price"):
                    try:
                        val = float(details.get(key, 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    if val > 0:
                        return val
        except Exception as e:
            logger.debug(f"Could not fetch fill price for {order_id}: {e}")
        return fallback

    async def _wait_for_fill(self, order_id: str, timeout: float) -> float:
        """
        Poll order status until filled or timeout.
        Returns the filled amount in USDC, or -1.0 on transient API failure.
        Uses wall-clock time so HTTP latency counts towards the timeout.
        """
        poll_interval = 2.0
        consecutive_errors = 0
        deadline = asyncio.get_running_loop().time() + timeout

        while asyncio.get_running_loop().time() < deadline:
            order = await self.bot.get_order(order_id)
            if not order:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    logger.warning(f"Order {order_id}: API unreachable after 3 retries")
                    return -1.0
                await asyncio.sleep(poll_interval)
                continue

            consecutive_errors = 0
            status = order.get("status", "")
            filled = float(order.get("filledSize", 0) or order.get("size_matched", 0))

            if status in ("MATCHED", "FILLED", "filled"):
                return filled

            if status in ("CANCELLED", "EXPIRED", "cancelled"):
                return filled

            await asyncio.sleep(poll_interval)

        return 0.0
