"""Cross-market mispricing strategy using external fair-value references."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from dateutil.parser import isoparse
from loguru import logger

import config
from core.gamma_client import GammaClient
from core.websocket_client import MarketWebSocket
from data.external_sources import ExternalSources
from data.market_quality import MarketQualityScorer
from strategies.base import BaseStrategy, Signal


def _parse_outcome_price(raw: str | list | None, default: float | None = 0.5) -> float | None:
    """Safely parse Gamma outcomePrices payloads. Returns default on failure."""
    if raw is None:
        return default
    if isinstance(raw, list):
        try:
            return float(raw[0]) if raw else default
        except (ValueError, TypeError):
            return default
    if isinstance(raw, str):
        try:
            return float(raw.strip("[]").split(",")[0].strip().strip('"'))
        except (ValueError, IndexError):
            return default
    return default


def _normalise_tokens(tokens) -> list:
    """Normalise Gamma token payloads that may arrive as lists or JSON strings."""
    if not tokens:
        return []

    if isinstance(tokens, str):
        raw = tokens.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = [
                item.strip().strip("\"'")
                for item in raw.strip("[]").split(",")
                if item.strip()
            ]
        return parsed if isinstance(parsed, list) else [parsed]

    if isinstance(tokens, tuple):
        return list(tokens)

    if isinstance(tokens, list):
        return tokens

    return [tokens]


def _pick_token_id(tokens: list, is_yes: bool) -> str:
    """Pick the YES or NO token id, preferring explicit outcome tags when present."""
    tokens = _normalise_tokens(tokens)
    if not tokens:
        return ""

    for token in tokens:
        if isinstance(token, dict):
            outcome = (token.get("outcome", "") or "").upper()
            if is_yes and outcome == "YES":
                return token.get("token_id", "")
            if not is_yes and outcome == "NO":
                return token.get("token_id", "")

    selected = tokens[0] if is_yes else tokens[-1]
    return selected if isinstance(selected, str) else selected.get("token_id", "")


class MispricingStrategy(BaseStrategy):
    """Trade divergences between Polymarket and external reference prices."""

    def __init__(
        self,
        gamma: GammaClient,
        external: ExternalSources,
        ws: MarketWebSocket | None = None,
        quality_scorer: MarketQualityScorer | None = None,
        min_mispricing: float = config.MIN_MISPRICING_PCT,
        close_threshold: float = 0.02,
        max_position_pct: float = config.MAX_POSITION_SIZE_PCT,
        reentry_cooldown_min: int = config.MISPRICING_REENTRY_COOLDOWN_MIN,
    ):
        super().__init__("mispricing")
        self.gamma = gamma
        self.external = external
        self.ws = ws
        self.quality = quality_scorer
        self.min_mispricing = min_mispricing
        self.close_threshold = close_threshold
        self.max_position_pct = max_position_pct
        self.reentry_cooldown = timedelta(minutes=reentry_cooldown_min)
        self.strikeout_threshold = max(config.MISPRICING_STRIKEOUT_LOSSES, 1)
        self.strikeout_lookback = timedelta(
            hours=config.MISPRICING_STRIKEOUT_LOOKBACK_HOURS
        )
        self.strikeout_pause = timedelta(
            hours=config.MISPRICING_STRIKEOUT_PAUSE_HOURS
        )
        self._subscribed_tokens: set[str] = set()
        self._cooldown_until: dict[str, datetime] = {}
        self._loss_events: dict[str, list[datetime]] = {}
        self._blocked_until: dict[str, datetime] = {}

    async def initialize(self):
        logger.info("Mispricing: Strategy initialized")

    def _default_exit_levels(self, entry_price: float) -> tuple[float, float]:
        move = max(self.close_threshold, entry_price * 0.10)
        take_profit = min(entry_price + move, 0.99)
        stop_loss = max(entry_price - move, 0.01)
        return take_profit, stop_loss

    def _signal_exit_levels(
        self,
        entry_price: float,
        estimated_prob: float,
        live_edge: float,
    ) -> tuple[float, float]:
        """
        Build more responsive exits for large mispricing gaps.

        We target partial convergence toward fair value instead of waiting for
        the market to travel all the way to the external estimate. That keeps
        dry-run and live behaviour closer to realistic holding periods.
        """
        fair_gap = max(estimated_prob - entry_price, self.close_threshold)
        capture_move = min(
            max(self.close_threshold, fair_gap * 0.45),
            0.12,
        )
        take_profit = min(
            entry_price + capture_move,
            estimated_prob - (self.close_threshold * 0.5),
            0.99,
        )
        if take_profit <= entry_price:
            take_profit = min(entry_price + self.close_threshold, 0.99)

        stop_move = min(
            max(self.close_threshold, min(live_edge * 0.30, capture_move * 0.85)),
            0.10,
        )
        stop_loss = max(entry_price - stop_move, 0.01)
        if stop_loss >= entry_price:
            stop_loss = max(entry_price - self.close_threshold, 0.01)

        return take_profit, stop_loss

    def _restored_exit_levels(
        self,
        entry_price: float,
        current_take_profit: float | None,
        current_stop_loss: float | None,
    ) -> tuple[float, float]:
        """
        Tighten legacy restored exits that were created by older, overly-wide logic.

        When we only know the open position, infer a conservative fair-value guess
        from the existing TP and rebuild a more responsive convergence-based range.
        """
        inferred_prob = max(
            entry_price + self.close_threshold,
            (current_take_profit or entry_price) + self.close_threshold,
        )
        inferred_prob = min(inferred_prob, 0.99)
        inferred_edge = max(inferred_prob - entry_price, self.close_threshold)
        return self._signal_exit_levels(entry_price, inferred_prob, inferred_edge)

    def _in_reentry_cooldown(self, token_id: str) -> bool:
        cooldown_until = self._cooldown_until.get(token_id)
        if not cooldown_until:
            return False
        if datetime.now(timezone.utc) >= cooldown_until:
            del self._cooldown_until[token_id]
            return False
        return True

    def _in_strikeout_block(self, token_id: str) -> bool:
        blocked_until = self._blocked_until.get(token_id)
        if not blocked_until:
            return False
        if datetime.now(timezone.utc) >= blocked_until:
            del self._blocked_until[token_id]
            return False
        return True

    def _record_loss_event(
        self,
        token_id: str,
        occurred_at: datetime,
        reason: str = "",
    ):
        events = self._loss_events.setdefault(token_id, [])
        cutoff = datetime.now(timezone.utc) - self.strikeout_lookback
        events = [dt for dt in events if dt >= cutoff]
        events.append(occurred_at)
        events.sort()
        self._loss_events[token_id] = events

        if len(events) < self.strikeout_threshold:
            return

        blocked_until = max(events) + self.strikeout_pause
        if blocked_until <= datetime.now(timezone.utc):
            return
        previous = self._blocked_until.get(token_id)
        if not previous or blocked_until > previous:
            self._blocked_until[token_id] = blocked_until
            logger.warning(
                f"Mispricing: Strikeout block on {token_id[:16]}... until "
                f"{blocked_until.strftime('%H:%M UTC')} after {len(events)} losses "
                f"in {int(self.strikeout_lookback.total_seconds() // 3600)}h"
                f"{f' ({reason})' if reason else ''}"
            )

    def record_closed_trade(self, token_id: str, pnl_usdc: float, reason: str = ""):
        """Avoid immediate re-entry after a stop or losing exit."""
        normalized_reason = (reason or "").lower()
        is_stop_reason = "stop" in normalized_reason
        is_loss = pnl_usdc < 0
        if is_loss:
            self._record_loss_event(
                token_id,
                datetime.now(timezone.utc),
                reason=reason,
            )
        if pnl_usdc >= 0 and not is_stop_reason:
            return

        # Progressive cooldown: each recent loss on this token doubles the wait.
        # Base = reentry_cooldown (30 min), max = 8x (4 hours).
        recent_losses = self._loss_events.get(token_id, [])
        cutoff = datetime.now(timezone.utc) - self.strikeout_lookback
        recent_count = sum(1 for dt in recent_losses if dt >= cutoff)
        multiplier = min(2 ** max(recent_count - 1, 0), 8)
        cooldown = self.reentry_cooldown * multiplier

        until = datetime.now(timezone.utc) + cooldown
        self._cooldown_until[token_id] = until
        logger.info(
            f"Mispricing: Cooldown on {token_id[:16]}... until "
            f"{until.strftime('%H:%M UTC')} after {reason or 'loss'} "
            f"({multiplier}x, {recent_count} recent losses)"
        )

    async def restore_runtime_state(
        self,
        open_positions: list,
        recent_trades: list[dict],
        db=None,
    ):
        """Rehydrate subscriptions, cooldowns, and missing exits after a restart."""
        now = datetime.now(timezone.utc)

        for trade in recent_trades:
            if trade.get("strategy") != self.name:
                continue
            pnl = float(trade.get("pnl_usdc", 0) or 0)
            if pnl >= 0:
                continue
            closed_at = trade.get("closed_at")
            token_id = trade.get("token_id", "")
            if not token_id or not closed_at:
                continue
            try:
                closed_dt = isoparse(closed_at)
            except (ValueError, TypeError):
                continue
            until = closed_dt + self.reentry_cooldown
            if until > now:
                previous = self._cooldown_until.get(token_id)
                if not previous or until > previous:
                    self._cooldown_until[token_id] = until
            self._record_loss_event(
                token_id,
                closed_dt,
                reason=str(trade.get("close_reason", "") or ""),
            )

        for pos in open_positions:
            if getattr(pos, "strategy", "") != self.name:
                continue

            if self.ws and pos.token_id not in self._subscribed_tokens:
                await self.ws.subscribe(pos.token_id)
                self._subscribed_tokens.add(pos.token_id)

            updated_levels = False
            target_tp, target_sl = self._restored_exit_levels(
                pos.entry_price,
                pos.take_profit,
                pos.stop_loss,
            )
            if pos.take_profit is None or pos.take_profit > target_tp + 0.05:
                pos.take_profit = target_tp
                updated_levels = True
            if pos.stop_loss is None or pos.stop_loss < target_sl - 0.05:
                pos.stop_loss = target_sl
                updated_levels = True

            if updated_levels:
                logger.info(
                    f"Mispricing: Refreshed exits for restored position {pos.id}"
                )
                if db:
                    db.update_trade_risk_levels(
                        pos.id,
                        pos.take_profit,
                        pos.stop_loss,
                    )

    async def _ensure_market_data_ready(self, token_id: str) -> bool:
        """Ensure the chosen token is subscribed and has an order-book snapshot."""
        if not self.ws:
            return True

        if token_id not in self._subscribed_tokens:
            await self.ws.subscribe(token_id)
            self._subscribed_tokens.add(token_id)

        return self.ws.get_order_book(token_id) is not None

    def _current_market_price(self, token_id: str, fallback_price: float) -> float:
        """Prefer live mid-price when available to avoid stale entry snapshots."""
        if not self.ws:
            return fallback_price
        current_price = self.ws.get_mid_price(token_id)
        return current_price if current_price is not None else fallback_price

    async def evaluate(self) -> list[Signal]:
        """Scan markets for cross-platform divergences."""
        if not self.is_active:
            return []

        signals: list[Signal] = []
        poly_markets = await self.gamma.get_markets_for_mispricing()
        logger.info(f"Mispricing: Scanning {len(poly_markets)} eligible markets")

        for market in poly_markets:
            question = market.get("question", "")
            poly_price = _parse_outcome_price(market.get("outcomePrices"), default=None)
            if poly_price is None:
                continue
            # Skip extreme prices — huge spread, illiquid, and near-resolved
            if not (config.MISPRICING_MIN_PRICE <= poly_price <= config.MISPRICING_MAX_PRICE):
                continue
            external_prices = await self.external.find_matching_prices(question)
            if not external_prices:
                continue

            avg_external = sum(external_prices.values()) / len(external_prices)
            divergence = avg_external - poly_price
            if abs(divergence) < self.min_mispricing:
                continue

            if divergence > 0:
                side = "BUY"
                snapshot_price = poly_price
                traded_estimated_prob = avg_external
                reasoning = (
                    f"Polymarket YES at {poly_price:.2f} vs "
                    f"external avg {avg_external:.2f} "
                    f"(divergence: +{divergence:.2f}). "
                    f"Sources: {external_prices}"
                )
            else:
                side = "BUY"
                snapshot_price = 1.0 - poly_price
                traded_estimated_prob = 1.0 - avg_external
                reasoning = (
                    f"Polymarket NO at {snapshot_price:.2f}, "
                    f"external avg YES {avg_external:.2f} "
                    f"(divergence: {divergence:.2f}). "
                    f"Sources: {external_prices}"
                )

            tokens = _normalise_tokens(
                market.get("tokens", []) or market.get("clobTokenIds", [])
            )
            token_id = _pick_token_id(tokens, is_yes=(divergence > 0))
            if not token_id:
                continue

            if self._in_reentry_cooldown(token_id):
                logger.debug(
                    f"Mispricing: Cooldown active for '{question[:40]}...', skipping"
                )
                continue

            if self._in_strikeout_block(token_id):
                logger.info(
                    f"Mispricing: Strikeout block active for '{question[:40]}...', skipping"
                )
                continue

            if self.quality and not await self._ensure_market_data_ready(token_id):
                logger.debug(
                    f"Mispricing: Waiting for order book on '{question[:40]}...' "
                    "before quality check"
                )
                continue

            if self.quality:
                volume_24h = float(market.get("volume24hr", 0) or 0)
                tradeable, report = self.quality.is_tradeable(
                    token_id,
                    volume_24h=volume_24h,
                )
                if not tradeable:
                    logger.debug(
                        f"Mispricing: Skipping '{question[:40]}...' - low quality: {report}"
                    )
                    continue

            signal_price = self._current_market_price(token_id, snapshot_price)
            signed_edge = traded_estimated_prob - signal_price
            live_edge = abs(signed_edge)
            if live_edge < self.min_mispricing:
                logger.debug(
                    f"Mispricing: Skipping '{question[:40]}...' - live edge "
                    f"{live_edge:.3f} below threshold after price refresh"
                )
                continue

            if signed_edge <= 0:
                logger.debug(
                    f"Mispricing: Skipping '{question[:40]}...' - no positive edge "
                    f"after price refresh ({traded_estimated_prob:.3f} <= {signal_price:.3f})"
                )
                continue

            if live_edge < config.MIN_KELLY_EDGE:
                logger.debug(
                    f"Mispricing: Skipping '{question[:40]}...' - live edge "
                    f"{live_edge:.3f} below Kelly minimum {config.MIN_KELLY_EDGE:.3f}"
                )
                continue

            edge_strength = min(live_edge / (self.min_mispricing * 3), 1.0)
            signal_confidence = 0.35 + (edge_strength * 0.40)
            take_profit, stop_loss = self._signal_exit_levels(
                entry_price=signal_price,
                estimated_prob=traded_estimated_prob,
                live_edge=live_edge,
            )

            if signal_price >= take_profit:
                logger.debug(
                    f"Mispricing: Skipping '{question[:40]}...' - live price "
                    f"{signal_price:.3f} already at/through TP {take_profit:.3f}"
                )
                continue

            signals.append(
                Signal(
                    strategy=self.name,
                    token_id=token_id,
                    market_question=question,
                    side=side,
                    price=signal_price,
                    size_usdc=0,
                    take_profit=take_profit,
                    stop_loss=stop_loss,
                    estimated_prob=traded_estimated_prob,
                    confidence=signal_confidence,
                    reasoning=reasoning,
                )
            )

        if signals:
            logger.info(f"Mispricing: Found {len(signals)} opportunities")

        return signals

    async def shutdown(self):
        if self.ws:
            for token_id in list(self._subscribed_tokens):
                await self.ws.unsubscribe(token_id)
        self._subscribed_tokens.clear()
        logger.info("Mispricing: Shut down")
