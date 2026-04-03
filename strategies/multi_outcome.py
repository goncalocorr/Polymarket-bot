"""
Multi-Outcome Arbitrage Strategy — detects pure arbitrage in multi-outcome events.

When the sum of YES prices across all mutually exclusive outcomes in an event
is < 1.00, buying one share of each outcome guarantees a profit of (1 - sum)
per unit, regardless of which outcome resolves.

This is the only "risk-free" strategy in prediction markets.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict

from loguru import logger

import config
from core.gamma_client import GammaClient
from core.websocket_client import MarketWebSocket
from data.market_quality import MarketQualityScorer
from strategies.base import BaseStrategy, Signal
from strategies.mispricing import _normalise_tokens, _parse_outcome_price


class MultiOutcomeStrategy(BaseStrategy):
    """
    Scans multi-outcome events for arbitrage where the sum of all
    outcome prices is below the arbitrage threshold (default: 0.97).

    When found, generates BUY signals for ALL outcomes in the event,
    tagged with a shared arb_group_id so the orchestrator can execute
    them atomically (all-or-nothing).
    """

    def __init__(
        self,
        gamma: GammaClient,
        ws: MarketWebSocket | None = None,
        quality_scorer: MarketQualityScorer | None = None,
        max_sum: float = config.ARBITRAGE_MAX_SUM,
        min_outcomes: int = config.ARBITRAGE_MIN_OUTCOMES,
        size_usdc: float = config.ARBITRAGE_SIZE_USDC,
    ):
        super().__init__("multi_outcome_arb")
        self.gamma = gamma
        self.ws = ws
        self.quality = quality_scorer
        self.max_sum = max_sum
        self.min_outcomes = min_outcomes
        self.size_usdc = size_usdc
        # Track events we've already arbed to avoid duplicates.
        # OrderedDict preserves insertion order so pruning keeps the most recent.
        self._arbed_events: OrderedDict[str, bool] = OrderedDict()
        self._subscribed_tokens: set[str] = set()

    async def initialize(self):
        logger.info(
            f"MultiOutcome: Initialized (max_sum={self.max_sum}, "
            f"min_outcomes={self.min_outcomes})"
        )

    async def _ensure_market_data_ready(self, token_id: str) -> bool:
        if not self.ws:
            return True

        if token_id not in self._subscribed_tokens:
            await self.ws.subscribe(token_id)
            self._subscribed_tokens.add(token_id)

        return self.ws.get_order_book(token_id) is not None

    async def _event_is_tradeable(
        self,
        event_title: str,
        outcome_data: list[dict],
    ) -> bool:
        if not self.quality:
            return True

        for outcome in outcome_data:
            token_id = outcome["token_id"]
            if not await self._ensure_market_data_ready(token_id):
                logger.debug(
                    f"MultiOutcome: Waiting for order book on '{event_title[:40]}...' "
                    f"({outcome['question'][:30]}...) before quality check"
                )
                return False

            tradeable, report = self.quality.is_tradeable(
                token_id,
                volume_24h=outcome.get("volume_24h", 0.0),
            )
            if not tradeable:
                logger.debug(
                    f"MultiOutcome: Skipping '{event_title[:40]}...' - "
                    f"low quality on '{outcome['question'][:30]}...': {report}"
                )
                return False

        return True

    async def evaluate(self) -> list[Signal]:
        """Scan for multi-outcome arbitrage opportunities."""
        if not self.is_active:
            return []

        signals = []

        # Find events with multiple outcomes
        events = await self.gamma.get_arbitrage_events(self.min_outcomes)
        logger.info(f"MultiOutcome: Scanning {len(events)} multi-outcome events")

        for event in events:
            event_id = event.get("id", "") or event.get("event_id", "")
            event_title = event.get("title", "")[:60]

            if not event_id or event_id in self._arbed_events:
                continue

            # Get all markets in this event
            markets = event.get("markets", [])
            if not markets:
                markets = await self.gamma.get_event_markets(event_id)
                # Rate limit between events
                await asyncio.sleep(0.3)

            if len(markets) < self.min_outcomes:
                continue

            # Calculate sum of YES prices for all outcomes
            outcome_data = self._extract_outcomes(markets)
            if not outcome_data or len(outcome_data) < self.min_outcomes:
                continue

            # Safety: if any market was filtered out (near-resolved), skip the
            # entire event — buying a subset is NOT a guaranteed arbitrage.
            if len(outcome_data) != len(markets):
                logger.debug(
                    f"MultiOutcome: Skipping '{event_title}' — "
                    f"{len(markets) - len(outcome_data)} outcomes filtered "
                    "(near-resolved); partial arb is unsafe"
                )
                continue

            price_sum = sum(o["price"] for o in outcome_data)

            if price_sum <= 0 or price_sum >= self.max_sum:
                continue

            if not await self._event_is_tradeable(event_title, outcome_data):
                continue

            # We have an arbitrage opportunity!
            spread = 1.0 - price_sum
            actual_profit = self.size_usdc * spread / price_sum
            logger.warning(
                f"MultiOutcome ARB FOUND: '{event_title}' — "
                f"{len(outcome_data)} outcomes, sum={price_sum:.4f}, "
                f"spread={spread:.4f} (${actual_profit:.2f} profit)"
            )

            # Tag all legs with a shared group ID so the orchestrator
            # can execute them atomically (all-or-nothing).
            arb_group_id = f"arb-{uuid.uuid4().hex[:8]}"

            # Generate BUY signals for each outcome
            event_signals = []
            for outcome in outcome_data:
                token_id = outcome["token_id"]
                if not token_id:
                    continue

                # Size each leg proportionally to its price
                leg_size = self.size_usdc * outcome["price"] / price_sum

                signal = Signal(
                    strategy=self.name,
                    token_id=token_id,
                    market_question=outcome["question"],
                    side="BUY",
                    price=outcome["price"],
                    size_usdc=leg_size,
                    arb_group_id=arb_group_id,
                    arb_event_id=event_id,
                    confidence=min(spread / 0.05, 1.0),  # 5% spread = max confidence
                    reasoning=(
                        f"Multi-outcome arb (group={arb_group_id}): "
                        f"{len(outcome_data)} outcomes, "
                        f"sum={price_sum:.4f}, guaranteed spread={spread:.4f}. "
                        f"Event: {event_title}"
                    ),
                )
                event_signals.append(signal)

            # Only emit signals if we have legs for ALL outcomes
            if len(event_signals) == len(outcome_data):
                # Mark as pending to prevent re-signalling on next cycle
                self._arbed_events[event_id] = False  # False = pending, not yet executed
                signals.extend(event_signals)
            else:
                logger.warning(
                    f"MultiOutcome: Skipping '{event_title}' — "
                    f"could only build {len(event_signals)}/{len(outcome_data)} legs"
                )

        # Prune arbed events to prevent unbounded memory growth.
        # OrderedDict preserves insertion order, so we can safely
        # remove the oldest entries.
        if len(self._arbed_events) > 500:
            while len(self._arbed_events) > 250:
                self._arbed_events.popitem(last=False)  # Remove oldest

        if signals:
            logger.info(
                f"MultiOutcome: Generated {len(signals)} signals "
                f"across {len(signals) // max(self.min_outcomes, 1)} events"
            )

        return signals

    def mark_event_executed(self, event_id: str):
        """Mark an event as successfully arbitraged to suppress duplicates."""
        if not event_id:
            return
        self._arbed_events[event_id] = True
        if len(self._arbed_events) > 500:
            while len(self._arbed_events) > 250:
                self._arbed_events.popitem(last=False)

    def _extract_outcomes(self, markets: list[dict]) -> list[dict]:
        """
        Extract token_id and YES price from each market in an event.
        Returns list of {"token_id": str, "price": float, "question": str}.
        """
        outcomes = []

        for m in markets:
            question = m.get("question", "")
            price = _parse_outcome_price(m.get("outcomePrices"), default=None)
            if price is None:
                continue

            # Skip markets with extreme prices (practically resolved)
            if price <= 0.02 or price >= 0.98:
                continue

            # Get YES token ID
            tokens = _normalise_tokens(
                m.get("tokens", []) or m.get("clobTokenIds", [])
            )
            token_id = ""
            for t in tokens:
                if isinstance(t, dict):
                    outcome = (t.get("outcome", "") or "").upper()
                    if outcome == "YES":
                        token_id = t.get("token_id", "")
                        break
                elif isinstance(t, str):
                    token_id = t
                    break

            if token_id:
                outcomes.append({
                    "token_id": token_id,
                    "price": price,
                    "question": question,
                    "volume_24h": float(m.get("volume24hr", 0) or 0),
                })

        return outcomes

    async def shutdown(self):
        if self.ws:
            for token_id in list(self._subscribed_tokens):
                await self.ws.unsubscribe(token_id)
        self._subscribed_tokens.clear()
        n = len(self._arbed_events)
        self._arbed_events.clear()
        logger.info(f"MultiOutcome: Shut down ({n} events arbed)")
