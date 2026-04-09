"""Quote lifecycle management for reward-aware market making."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

import config
from core.bot import TradingBot
from storage.database import Database


@dataclass
class DesiredQuote:
    quote_key: str
    market_id: str
    token_id: str
    market_question: str
    outcome: str
    side: str
    price: float
    size_usdc: float
    order_kind: str
    reward_max_spread: float = 0.0
    reward_min_size: float = 0.0
    reward_daily_rate: float = 0.0
    score_estimate: float = 0.0


@dataclass
class ActiveQuote:
    order_id: str
    quote_key: str
    market_id: str
    token_id: str
    market_question: str
    outcome: str
    side: str
    price: float
    size_usdc: float
    order_kind: str
    reward_max_spread: float = 0.0
    reward_min_size: float = 0.0
    reward_daily_rate: float = 0.0
    score_estimate: float = 0.0
    scoring: bool = False
    reconcile_pending: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class InventoryLot:
    token_id: str
    market_id: str
    market_question: str
    outcome: str
    shares: float = 0.0
    avg_entry_price: float = 0.0
    realized_pnl_usdc: float = 0.0
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_flat(self) -> bool:
        return self.shares <= 1e-9

    def current_notional(self, price: float) -> float:
        return self.shares * max(price, 0.0)

    def apply_buy_fill(self, fill_price: float, fill_size_usdc: float):
        shares_bought = fill_size_usdc / max(fill_price, 0.01)
        total_cost = (self.shares * self.avg_entry_price) + fill_size_usdc
        self.shares += shares_bought
        self.avg_entry_price = total_cost / max(self.shares, 1e-9)
        self.updated_at = datetime.now(timezone.utc)

    def apply_sell_fill(self, fill_price: float, fill_size_usdc: float) -> float:
        shares_sold = min(self.shares, fill_size_usdc / max(fill_price, 0.01))
        realized = shares_sold * (fill_price - self.avg_entry_price)
        self.shares = max(0.0, self.shares - shares_sold)
        self.realized_pnl_usdc += realized
        self.updated_at = datetime.now(timezone.utc)
        if self.shares <= 1e-9:
            self.avg_entry_price = 0.0
        return realized


class QuoteManager:
    """Keeps desired maker quotes synchronized with live open orders."""

    def __init__(self, bot: TradingBot, db: Database, ws=None):
        self.bot = bot
        self.db = db
        self.ws = ws
        self.active_quotes: dict[str, ActiveQuote] = {}
        self.inventory: dict[str, InventoryLot] = {}
        self._lock = asyncio.Lock()

    def restore_state(self):
        self.active_quotes.clear()
        for row in self.db.get_open_maker_orders():
            created_at = row.get("created_at")
            updated_at = row.get("updated_at")
            self.active_quotes[row["quote_key"]] = ActiveQuote(
                order_id=row["order_id"],
                quote_key=row["quote_key"],
                market_id=row.get("market_id", ""),
                token_id=row["token_id"],
                market_question=row.get("market_question", ""),
                outcome=row.get("outcome", ""),
                side=row["side"],
                price=float(row["price"]),
                size_usdc=float(row["size_usdc"]),
                order_kind=row.get("order_kind", ""),
                reward_max_spread=float(row.get("reward_max_spread", 0) or 0),
                reward_min_size=float(row.get("reward_min_size", 0) or 0),
                reward_daily_rate=float(row.get("reward_daily_rate", 0) or 0),
                score_estimate=float(row.get("score_estimate", 0) or 0),
                scoring=bool(row.get("scoring", 0)),
                created_at=(
                    datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    if isinstance(created_at, str)
                    else datetime.now(timezone.utc)
                ),
                updated_at=(
                    datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                    if isinstance(updated_at, str)
                    else datetime.now(timezone.utc)
                ),
            )

        self.inventory.clear()
        for row in self.db.get_maker_inventory():
            updated_at = row.get("updated_at")
            self.inventory[row["token_id"]] = InventoryLot(
                token_id=row["token_id"],
                market_id=row.get("market_id", ""),
                market_question=row.get("market_question", ""),
                outcome=row.get("outcome", ""),
                shares=float(row.get("shares", 0) or 0),
                avg_entry_price=float(row.get("avg_entry_price", 0) or 0),
                realized_pnl_usdc=float(row.get("realized_pnl_usdc", 0) or 0),
                updated_at=(
                    datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                    if isinstance(updated_at, str)
                    else datetime.now(timezone.utc)
                ),
            )

    def get_active_quotes(self) -> list[ActiveQuote]:
        return list(self.active_quotes.values())

    def get_inventory(self) -> list[InventoryLot]:
        return [lot for lot in self.inventory.values() if not lot.is_flat]

    def committed_capital(self) -> float:
        return sum(q.size_usdc for q in self.active_quotes.values())

    def pending_buy_capital(self) -> float:
        return sum(
            q.size_usdc for q in self.active_quotes.values()
            if q.side.upper() == "BUY"
        )

    def inventory_exit_notional(self) -> float:
        return sum(
            q.size_usdc
            for q in self.active_quotes.values()
            if q.order_kind == "inventory_exit" and q.side.upper() == "SELL"
        )

    def inventory_notional(self, price_lookup) -> float:
        total = 0.0
        for lot in self.get_inventory():
            total += lot.current_notional(price_lookup(lot.token_id))
        return total

    def _needs_refresh(self, current: ActiveQuote, desired: DesiredQuote) -> bool:
        if current.order_kind != desired.order_kind:
            return True
        if current.side != desired.side:
            return True
        if abs(current.size_usdc - desired.size_usdc) > 0.75:
            return True
        if desired.price <= 0 or current.price <= 0:
            return True
        drift_bps = abs(current.price - desired.price) / current.price * 10_000
        return drift_bps >= config.MAKER_REQUOTE_BPS

    async def sync_quotes(self, desired_quotes: list[DesiredQuote]):
        async with self._lock:
            desired_by_key = {quote.quote_key: quote for quote in desired_quotes}
            active_keys = set(self.active_quotes)

            for quote_key in list(active_keys - set(desired_by_key)):
                current = self.active_quotes.get(quote_key)
                if current and current.reconcile_pending:
                    continue
                await self._cancel_quote_unlocked(quote_key, reason="not-selected")

            for desired in desired_quotes:
                current = self.active_quotes.get(desired.quote_key)
                if current and current.reconcile_pending:
                    continue
                if current and not self._needs_refresh(current, desired):
                    continue
                if current:
                    await self._cancel_quote_unlocked(desired.quote_key, reason="requote")
                await self._place_quote(desired)

    async def cancel_matching_quotes(self, predicate, reason: str):
        async with self._lock:
            for quote_key, active in list(self.active_quotes.items()):
                if active.reconcile_pending:
                    continue
                if predicate(active):
                    await self._cancel_quote_unlocked(quote_key, reason=reason)

    async def _place_quote(self, desired: DesiredQuote) -> Optional[ActiveQuote]:
        result = await self.bot.place_order(
            token_id=desired.token_id,
            price=desired.price,
            size=desired.size_usdc,
            side=desired.side,
            order_type="GTC",
        )
        if not result:
            logger.warning(
                f"QuoteManager: failed to place {desired.order_kind} "
                f"{desired.side} quote on {desired.token_id[:16]}..."
            )
            return None

        now = datetime.now(timezone.utc)
        active = ActiveQuote(
            order_id=result.get("id", ""),
            quote_key=desired.quote_key,
            market_id=desired.market_id,
            token_id=desired.token_id,
            market_question=desired.market_question,
            outcome=desired.outcome,
            side=desired.side,
            price=desired.price,
            size_usdc=desired.size_usdc,
            order_kind=desired.order_kind,
            reward_max_spread=desired.reward_max_spread,
            reward_min_size=desired.reward_min_size,
            reward_daily_rate=desired.reward_daily_rate,
            score_estimate=desired.score_estimate,
            scoring=desired.score_estimate > 0,
            created_at=now,
            updated_at=now,
        )
        self.active_quotes[desired.quote_key] = active
        self.db.save_maker_order({
            "order_id": active.order_id,
            "quote_key": active.quote_key,
            "market_id": active.market_id,
            "token_id": active.token_id,
            "market_question": active.market_question,
            "outcome": active.outcome,
            "side": active.side,
            "price": active.price,
            "size_usdc": active.size_usdc,
            "order_kind": active.order_kind,
            "reward_max_spread": active.reward_max_spread,
            "reward_min_size": active.reward_min_size,
            "reward_daily_rate": active.reward_daily_rate,
            "score_estimate": active.score_estimate,
            "scoring": active.scoring,
            "status": "OPEN",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        })
        return active

    async def cancel_quote(self, quote_key: str, reason: str = "cancelled") -> bool:
        async with self._lock:
            return await self._cancel_quote_unlocked(quote_key, reason=reason)

    async def _cancel_quote_unlocked(self, quote_key: str, reason: str = "cancelled") -> bool:
        """Cancel a quote without acquiring the lock (caller must hold it)."""
        current = self.active_quotes.get(quote_key)
        if not current:
            return False
        # Mark pending before async cancel to prevent concurrent operations
        current.reconcile_pending = True
        cancelled = await self.bot.cancel_order(current.order_id)
        if cancelled:
            self.active_quotes.pop(quote_key, None)
            self.db.update_maker_order(
                current.order_id,
                status="CANCELLED",
                close_reason=reason,
            )
        else:
            logger.warning(
                f"QuoteManager: Failed to cancel order {current.order_id} "
                f"({reason}) for {current.token_id[:16]}..."
            )
            current.reconcile_pending = False
        return cancelled

    async def cancel_all(self, reason: str = "shutdown"):
        async with self._lock:
            for quote_key in list(self.active_quotes):
                await self._cancel_quote_unlocked(quote_key, reason=reason)

    async def reconcile_live_orders(self):
        """Update local quote state when live orders disappear or fill."""
        async with self._lock:
            await self._reconcile_live_orders_unlocked()

    async def _reconcile_live_orders_unlocked(self):
        if config.DRY_RUN:
            await self._simulate_dry_run_reconciliation()
            return

        open_orders = await self.bot.get_open_orders()
        open_ids = {str(order.get("id", "")) for order in open_orders}

        for quote_key, active in list(self.active_quotes.items()):
            if active.order_id in open_ids:
                continue

            details = await self.bot.get_order(active.order_id)
            status = (details or {}).get("status", "")
            filled_size_usdc = self._filled_size_usdc(details, active)
            fill_price = self._fill_price(details, active)

            if not details:
                active.reconcile_pending = True
                active.updated_at = datetime.now(timezone.utc)
                logger.warning(
                    f"QuoteManager: could not confirm state for {active.order_id}; "
                    "keeping quote pending reconciliation"
                )
                continue

            active.reconcile_pending = False

            if str(status).upper() in {"OPEN", "LIVE", "PLACED"}:
                continue

            if str(status).upper() in {"FILLED", "MATCHED"} or filled_size_usdc >= active.size_usdc * 0.95:
                realized_pnl = self._apply_fill(active, fill_price, filled_size_usdc)
                self.db.update_maker_order(
                    active.order_id,
                    status="FILLED",
                    fill_size_usdc=filled_size_usdc,
                    fill_price=fill_price,
                    close_reason="filled",
                )
                self.db.save_maker_fill({
                    "order_id": active.order_id,
                    "quote_key": active.quote_key,
                    "market_id": active.market_id,
                    "token_id": active.token_id,
                    "market_question": active.market_question,
                    "outcome": active.outcome,
                    "side": active.side,
                    "fill_price": fill_price,
                    "fill_size_usdc": filled_size_usdc,
                    "shares": filled_size_usdc / max(fill_price, 0.01),
                    "realized_pnl_usdc": realized_pnl,
                })
                logger.info(
                    f"QuoteManager: {active.order_kind} filled on "
                    f"{active.market_question[:40]}... at {fill_price:.3f}"
                )
            else:
                self.db.update_maker_order(
                    active.order_id,
                    status="CANCELLED",
                    close_reason="no-longer-open",
                )

            self.active_quotes.pop(quote_key, None)

    async def _simulate_dry_run_reconciliation(self):
        if self.ws is None:
            return

        now = datetime.now(timezone.utc)
        for quote_key, active in list(self.active_quotes.items()):
            simulated = self._simulate_dry_run_fill(active, now)
            if not simulated:
                continue

            fill_price, fill_size_usdc, fill_reason = simulated
            realized_pnl = self._apply_fill(active, fill_price, fill_size_usdc)
            self.db.update_maker_order(
                active.order_id,
                status="FILLED",
                fill_size_usdc=fill_size_usdc,
                fill_price=fill_price,
                close_reason=fill_reason,
            )
            self.db.save_maker_fill({
                "order_id": active.order_id,
                "quote_key": active.quote_key,
                "market_id": active.market_id,
                "token_id": active.token_id,
                "market_question": active.market_question,
                "outcome": active.outcome,
                "side": active.side,
                "fill_price": fill_price,
                "fill_size_usdc": fill_size_usdc,
                "shares": fill_size_usdc / max(fill_price, 0.01),
                "realized_pnl_usdc": realized_pnl,
            })
            logger.info(
                f"QuoteManager: [DRY RUN] simulated fill for {active.order_kind} "
                f"on {active.market_question[:40]}... at {fill_price:.3f}"
            )
            self.active_quotes.pop(quote_key, None)

    async def update_scoring(self, scoring_flags: dict[str, bool]):
        async with self._lock:
            for active in self.active_quotes.values():
                scoring = scoring_flags.get(active.order_id, active.score_estimate > 0)
                active.scoring = scoring
                active.updated_at = datetime.now(timezone.utc)
                self.db.update_maker_order(
                    active.order_id,
                    scoring=scoring,
                    score_estimate=active.score_estimate,
                )

    def _apply_fill(
        self,
        active: ActiveQuote,
        fill_price: float,
        fill_size_usdc: float,
    ) -> float:
        lot = self.inventory.get(active.token_id)
        if lot is None:
            lot = InventoryLot(
                token_id=active.token_id,
                market_id=active.market_id,
                market_question=active.market_question,
                outcome=active.outcome,
            )
            self.inventory[active.token_id] = lot

        realized = 0.0
        if active.side.upper() == "BUY":
            lot.apply_buy_fill(fill_price, fill_size_usdc)
        else:
            realized = lot.apply_sell_fill(fill_price, fill_size_usdc)

        if lot.is_flat:
            self.db.delete_maker_inventory(active.token_id)
            self.inventory.pop(active.token_id, None)
        else:
            self.db.upsert_maker_inventory({
                "token_id": lot.token_id,
                "market_id": lot.market_id,
                "market_question": lot.market_question,
                "outcome": lot.outcome,
                "shares": lot.shares,
                "avg_entry_price": lot.avg_entry_price,
                "realized_pnl_usdc": lot.realized_pnl_usdc,
                "updated_at": lot.updated_at.isoformat(),
            })
        return realized

    def _simulate_dry_run_fill(
        self,
        active: ActiveQuote,
        now: datetime,
    ) -> Optional[tuple[float, float, str]]:
        if self.ws is None:
            return None
        if config.DRY_RUN_MAKER_REQUIRE_SCORING and not active.scoring:
            return None

        best_bid, best_ask = self.ws.get_best_bid_ask(active.token_id)
        if best_bid is None or best_ask is None:
            return None

        tick = 0.01
        spread = max(best_ask - best_bid, tick)
        age_s = max((now - active.created_at).total_seconds(), 0.0)
        min_age = max(config.DRY_RUN_MAKER_MIN_FILL_AGE_S, 0.0)
        force_age = max(config.DRY_RUN_MAKER_FORCE_FILL_AGE_S, min_age)

        if active.side.upper() == "BUY":
            marketable = active.price >= (best_ask - tick)
            competitiveness = max(
                0.0,
                min(1.0, 1.0 - max(best_bid - active.price, 0.0) / spread),
            )
        else:
            marketable = active.price <= (best_bid + tick)
            competitiveness = max(
                0.0,
                min(1.0, 1.0 - max(active.price - best_ask, 0.0) / spread),
            )

        if marketable:
            return active.price, active.size_usdc, "dry-run-touch-fill"

        min_competitiveness = max(config.DRY_RUN_MAKER_MIN_COMPETITIVENESS, 0.0)
        if age_s < min_age or competitiveness < min_competitiveness:
            return None

        if active.order_kind == "inventory_exit":
            age_relief = min(0.20, age_s / max(force_age, 1.0) * 0.20)
            exit_threshold = max(min_competitiveness, 0.55 - age_relief)
            aggressive_enough = competitiveness >= exit_threshold
        else:
            aggressive_enough = competitiveness >= max(min_competitiveness, 0.70)

        if not aggressive_enough and age_s < force_age:
            return None

        return active.price, active.size_usdc, "dry-run-passive-fill"

    @staticmethod
    def _filled_size_usdc(details: Optional[dict], active: ActiveQuote) -> float:
        if not details:
            return 0.0
        candidates = (
            details.get("filledSize"),
            details.get("size_matched"),
            details.get("fillAmount"),
        )
        for value in candidates:
            try:
                amount = float(value or 0)
            except (TypeError, ValueError):
                continue
            if amount > 0:
                return amount
        logger.warning(
            f"No fill amount found in order details for {active.order_id} — "
            "returning 0 instead of assumed full fill"
        )
        return 0.0

    @staticmethod
    def _fill_price(details: Optional[dict], active: ActiveQuote) -> float:
        if details:
            for key in ("avgPrice", "price"):
                try:
                    price = float(details.get(key, 0) or 0)
                except (TypeError, ValueError):
                    continue
                if price > 0:
                    return price
        return active.price
