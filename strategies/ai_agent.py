"""
AI Agent Strategy — uses Claude to analyse markets in the grey zone
where cross-market divergence is ambiguous (5-8%).
"""

import json
from datetime import datetime, timezone

from loguru import logger

import config
from ai.claude_analyst import ClaudeAnalyst
from core.gamma_client import GammaClient
from data.external_sources import ExternalSources
from strategies.base import BaseStrategy, Signal
from strategies.mispricing import _parse_outcome_price, _pick_token_id


class AIAgentStrategy(BaseStrategy):
    """
    Uses Claude API as a qualitative confirmation layer.
    Only invoked when mispricing divergence is in the grey zone (5-8%).
    Limited to MAX_CLAUDE_CALLS_PER_DAY to control costs.
    """

    def __init__(
        self,
        gamma: GammaClient,
        external: ExternalSources,
        analyst: ClaudeAnalyst,
        db=None,
        grey_zone_min: float = 0.05,
        grey_zone_max: float = 0.08,
    ):
        super().__init__("ai_agent")
        self.gamma = gamma
        self.external = external
        self.analyst = analyst
        self.db = db
        self.grey_zone_min = grey_zone_min
        self.grey_zone_max = grey_zone_max
        self._calls_today = 0
        self._last_reset_date: str = ""
        self._state_key = "ai_agent:daily_calls"

    def _persist_daily_counter(self):
        if not self.db or not self._last_reset_date:
            return
        self.db.save_state(
            self._state_key,
            json.dumps({
                "date": self._last_reset_date,
                "calls": self._calls_today,
            }),
        )

    def _load_daily_counter(self):
        if not self.db:
            return

        raw = self.db.get_state(self._state_key)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if not raw:
            self._last_reset_date = today
            self._calls_today = 0
            self._persist_daily_counter()
            return

        try:
            state = json.loads(raw)
            state_date = str(state.get("date", ""))
            state_calls = int(state.get("calls", 0) or 0)
        except (ValueError, TypeError, json.JSONDecodeError):
            logger.warning("AIAgent: Invalid persisted Claude call counter, resetting")
            self._last_reset_date = today
            self._calls_today = 0
            self._persist_daily_counter()
            return

        if state_date == today:
            self._last_reset_date = state_date
            self._calls_today = max(state_calls, 0)
            return

        self._last_reset_date = today
        self._calls_today = 0
        self._persist_daily_counter()

    def _reset_daily_counter(self):
        if self.db and not self._last_reset_date:
            self._load_daily_counter()

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._last_reset_date:
            self._calls_today = 0
            self._last_reset_date = today
            self._persist_daily_counter()

    async def initialize(self):
        self._reset_daily_counter()
        if not getattr(self.analyst, "api_key", ""):
            self.disable()
            logger.warning(
                "AIAgent: Disabled because ANTHROPIC_API_KEY is not configured"
            )
            return
        logger.info("AIAgent: Strategy initialized")

    async def evaluate(self) -> list[Signal]:
        """Find grey-zone markets and ask Claude for confirmation."""
        if not self.is_active:
            return []

        self._reset_daily_counter()
        if self._calls_today >= config.MAX_CLAUDE_CALLS_PER_DAY:
            logger.debug("AIAgent: Daily call limit reached")
            return []

        signals = []
        poly_markets = await self.gamma.get_markets_for_mispricing()
        markets_with_external = 0
        grey_zone_candidates = 0
        claude_calls_this_cycle = 0

        for market in poly_markets:
            if self._calls_today >= config.MAX_CLAUDE_CALLS_PER_DAY:
                break

            question = market.get("question", "")
            poly_price = _parse_outcome_price(market.get("outcomePrices"))
            volume_24h = float(market.get("volume24hr", 0) or 0)
            end_date = market.get("endDate", "")

            # Find external prices
            external_prices = await self.external.find_matching_prices(question)
            if not external_prices:
                continue
            markets_with_external += 1

            avg_external = sum(external_prices.values()) / len(external_prices)
            divergence = abs(avg_external - poly_price)

            # Only process grey zone divergences
            if not (self.grey_zone_min <= divergence <= self.grey_zone_max):
                continue
            grey_zone_candidates += 1

            # Fetch recent news for context
            news = await self.external.get_recent_news(question)

            # Ask Claude
            market_data = {
                "question": question,
                "price": poly_price,
                "volume": volume_24h,
                "end_date": end_date,
                "external_prices": external_prices,
            }

            try:
                analysis = await self.analyst.analyse_market(market_data, news)
                self._calls_today += 1
                claude_calls_this_cycle += 1
                self._persist_daily_counter()
            except Exception as e:
                logger.error(f"AIAgent: Claude analysis failed for '{question}': {e}")
                continue

            claude_prob = analysis.get("probability", 0.5)
            claude_confidence = analysis.get("confidence", 0.0)
            reasoning = analysis.get("reasoning", "")

            # Claude must agree with external sources AND be confident enough
            claude_divergence = claude_prob - poly_price
            if abs(claude_divergence) < self.grey_zone_min:
                logger.debug(
                    f"AIAgent: Claude says fair price for '{question[:50]}...'"
                )
                continue

            if claude_confidence < 0.6:
                logger.debug(
                    f"AIAgent: Low confidence ({claude_confidence:.2f}) for "
                    f"'{question[:50]}...'"
                )
                continue

            # Generate signal
            if claude_divergence > 0:
                side = "BUY"
                price = poly_price
            else:
                side = "BUY"
                price = 1.0 - poly_price

            tokens = market.get("tokens", []) or market.get("clobTokenIds", [])
            token_id = _pick_token_id(tokens, is_yes=(claude_divergence > 0))

            if not token_id:
                continue

            signal = Signal(
                strategy=self.name,
                token_id=token_id,
                market_question=question,
                side=side,
                price=price,
                size_usdc=0,  # Calculated by risk manager
                confidence=claude_confidence,
                reasoning=(
                    f"Claude estimate: {claude_prob:.2f} (confidence: "
                    f"{claude_confidence:.2f}). {reasoning}"
                ),
            )
            signals.append(signal)

            q_display = question[:60] + ("..." if len(question) > 60 else "")
            logger.info(
                f"AIAgent: Signal for '{q_display}' — "
                f"Claude prob={claude_prob:.2f}, conf={claude_confidence:.2f}"
            )

        logger.debug(
            "AIAgent: scan summary — "
            f"markets={len(poly_markets)} "
            f"external={markets_with_external} "
            f"grey_zone={grey_zone_candidates} "
            f"claude_calls={claude_calls_this_cycle} "
            f"signals={len(signals)} "
            f"remaining_budget={max(config.MAX_CLAUDE_CALLS_PER_DAY - self._calls_today, 0)}"
        )

        return signals

    async def shutdown(self):
        self._persist_daily_counter()
        logger.info(
            f"AIAgent: Shut down (used {self._calls_today} Claude calls today)"
        )
