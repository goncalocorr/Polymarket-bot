"""AI Agent Strategy for grey-zone market analysis."""

import json
from datetime import datetime, timezone

from loguru import logger

import config
from ai.base_analyst import MarketAnalyst
from core.gamma_client import GammaClient
from data.external_sources import ExternalSources
from strategies.base import BaseStrategy, Signal
from strategies.mispricing import _parse_outcome_price, _pick_token_id


class AIAgentStrategy(BaseStrategy):
    """
    Uses an AI analyst as a qualitative confirmation layer.
    Only invoked when mispricing divergence is in the grey zone (5-8%).
    Limited to MAX_AI_CALLS_PER_DAY to control costs.
    """

    def __init__(
        self,
        gamma: GammaClient,
        external: ExternalSources,
        analyst: MarketAnalyst,
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
            # Even without a DB, set the date so _reset_daily_counter
            # doesn't reset the counter on every single cycle.
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self._last_reset_date = today
            self._calls_today = 0
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
            logger.warning("AIAgent: Invalid persisted AI call counter, resetting")
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
            missing_env = getattr(self.analyst, "required_api_key_env", "AI API key")
            logger.warning(
                f"AIAgent: Disabled because {missing_env} is not configured"
            )
            return

        provider = getattr(self.analyst, "provider_name", "unknown")
        model = getattr(self.analyst, "model", "unknown")
        logger.info(f"AIAgent: Strategy initialized ({provider}:{model})")

    async def evaluate(self) -> list[Signal]:
        """Find grey-zone markets and ask the configured AI provider for confirmation."""
        if not self.is_active:
            return []

        self._reset_daily_counter()
        if self._calls_today >= config.MAX_AI_CALLS_PER_DAY:
            logger.debug("AIAgent: Daily call limit reached")
            return []

        signals = []
        poly_markets = await self.gamma.get_markets_for_mispricing()
        markets_with_external = 0
        grey_zone_candidates = 0
        ai_calls_this_cycle = 0

        for market in poly_markets:
            if self._calls_today >= config.MAX_AI_CALLS_PER_DAY:
                break

            question = market.get("question", "")
            poly_price = _parse_outcome_price(market.get("outcomePrices"), default=None)
            if poly_price is None:
                continue
            volume_24h = float(market.get("volume24hr", 0) or 0)
            end_date = market.get("endDate", "")

            external_prices = await self.external.find_matching_prices(question)
            if not external_prices:
                continue
            markets_with_external += 1

            avg_external = sum(external_prices.values()) / len(external_prices)
            divergence = abs(avg_external - poly_price)

            if not (self.grey_zone_min <= divergence <= self.grey_zone_max):
                continue
            grey_zone_candidates += 1

            news = await self.external.get_recent_news(question)
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
                ai_calls_this_cycle += 1
                self._persist_daily_counter()
            except Exception as e:
                provider = getattr(self.analyst, "provider_name", "AI")
                logger.error(
                    f"AIAgent: {provider} analysis failed for '{question}': {e}"
                )
                continue

            ai_prob = analysis.get("probability", 0.5)
            ai_confidence = analysis.get("confidence", 0.0)
            reasoning = analysis.get("reasoning", "")

            ai_divergence = ai_prob - poly_price
            if abs(ai_divergence) < self.grey_zone_min:
                logger.debug(
                    f"AIAgent: AI says fair price for '{question[:50]}...'"
                )
                continue

            if ai_confidence < 0.6:
                logger.debug(
                    f"AIAgent: Low confidence ({ai_confidence:.2f}) for "
                    f"'{question[:50]}...'"
                )
                continue

            if ai_divergence > 0:
                side = "BUY"
                price = poly_price
                estimated_prob = ai_prob
            else:
                side = "BUY"
                price = 1.0 - poly_price
                estimated_prob = 1.0 - ai_prob

            tokens = market.get("tokens", []) or market.get("clobTokenIds", [])
            token_id = _pick_token_id(tokens, is_yes=(ai_divergence > 0))

            if not token_id:
                continue

            signal = Signal(
                strategy=self.name,
                token_id=token_id,
                market_question=question,
                side=side,
                price=price,
                size_usdc=0,
                estimated_prob=estimated_prob,
                confidence=ai_confidence,
                reasoning=(
                    f"AI estimate: {ai_prob:.2f} (confidence: "
                    f"{ai_confidence:.2f}). {reasoning}"
                ),
            )
            signals.append(signal)

            q_display = question[:60] + ("..." if len(question) > 60 else "")
            logger.info(
                f"AIAgent: Signal for '{q_display}' - "
                f"AI prob={ai_prob:.2f}, conf={ai_confidence:.2f}"
            )

        logger.debug(
            "AIAgent: scan summary - "
            f"markets={len(poly_markets)} "
            f"external={markets_with_external} "
            f"grey_zone={grey_zone_candidates} "
            f"ai_calls={ai_calls_this_cycle} "
            f"signals={len(signals)} "
            f"remaining_budget={max(config.MAX_AI_CALLS_PER_DAY - self._calls_today, 0)}"
        )

        return signals

    async def shutdown(self):
        self._persist_daily_counter()
        close = getattr(self.analyst, "close", None)
        if callable(close):
            await close()
        logger.info(
            f"AIAgent: Shut down (used {self._calls_today} AI calls today)"
        )

