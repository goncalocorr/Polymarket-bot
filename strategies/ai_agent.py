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
    Uses an AI classifier to find directional opportunities in niche markets.

    Pipeline: volume filter -> price range filter -> news/external lookup
    -> AI classification (direction + materiality) -> signal generation.

    External price sources are optional context — not a hard requirement.
    This allows the strategy to function even when external sources are down.
    """

    def __init__(
        self,
        gamma: GammaClient,
        external: ExternalSources,
        analyst: MarketAnalyst,
        db=None,
    ):
        super().__init__("ai_agent")
        self.gamma = gamma
        self.external = external
        self.analyst = analyst
        self.db = db
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
        """Find niche markets, gather context, and classify with AI.

        Pipeline: volume filter -> price range filter -> news/external lookup
        -> AI classification (direction + materiality) -> signal generation.

        External prices are optional enrichment, not a hard gate.
        """
        if not self.is_active:
            return []

        self._reset_daily_counter()
        if self._calls_today >= config.MAX_AI_CALLS_PER_DAY:
            logger.debug("AIAgent: Daily call limit reached")
            return []

        signals = []
        poly_markets = await self.gamma.get_markets_for_mispricing()
        volume_filtered = 0
        price_filtered = 0
        news_filtered = 0
        ai_calls_this_cycle = 0
        max_per_cycle = config.AI_AGENT_MAX_CLASSIFICATIONS_PER_CYCLE

        for market in poly_markets:
            if self._calls_today >= config.MAX_AI_CALLS_PER_DAY:
                break
            if ai_calls_this_cycle >= max_per_cycle:
                break

            question = market.get("question", "")
            poly_price = _parse_outcome_price(market.get("outcomePrices"), default=None)
            if poly_price is None:
                continue
            volume_24h = float(market.get("volume24hr", 0) or 0)
            end_date = market.get("endDate", "")

            # Niche market filter: skip high-volume (sophisticated bots) and
            # low-volume (illiquid) markets.
            if not (config.AI_AGENT_MIN_VOLUME <= volume_24h <= config.AI_AGENT_MAX_VOLUME):
                volume_filtered += 1
                continue

            # Price range filter: skip near-resolved markets where the outcome
            # is already largely priced in.
            if not (config.AI_AGENT_MIN_PRICE <= poly_price <= config.AI_AGENT_MAX_PRICE):
                price_filtered += 1
                continue

            # Gather context: news and external prices (both optional).
            news = await self.external.get_recent_news(question)
            external_prices = await self.external.find_matching_prices(question)

            # If configured to require news and none found, skip.
            if config.AI_AGENT_REQUIRE_NEWS and not news:
                news_filtered += 1
                continue

            market_data = {
                "question": question,
                "price": poly_price,
                "volume": volume_24h,
                "end_date": end_date,
                "external_prices": external_prices or {},
            }

            try:
                classification = await self.analyst.classify_market(market_data, news)
                self._calls_today += 1
                ai_calls_this_cycle += 1
                self._persist_daily_counter()
            except Exception as e:
                provider = getattr(self.analyst, "provider_name", "AI")
                err_str = str(e).lower()
                # On rate-limit, retryable, or timeout errors, stop trying more
                # markets this cycle to avoid burning the evaluation timeout.
                if any(kw in err_str for kw in ("429", "rate", "timed out", "retryable", "credit", "balance", "quota")):
                    logger.warning(
                        f"AIAgent: {provider} rate-limited — stopping cycle "
                        f"after {ai_calls_this_cycle} calls ({e})"
                    )
                    break
                logger.error(
                    f"AIAgent: {provider} classification failed for '{question}': {e}"
                )
                continue

            direction = classification.get("direction", "NEUTRAL")
            materiality = classification.get("materiality", 0.0)
            ai_confidence = classification.get("confidence", 0.0)
            reasoning = classification.get("reasoning", "")

            # Skip neutral signals or low-materiality noise
            if direction == "NEUTRAL":
                logger.debug(f"AIAgent: Neutral for '{question[:50]}...'")
                continue

            if materiality < config.AI_AGENT_MIN_MATERIALITY:
                logger.debug(
                    f"AIAgent: Low materiality ({materiality:.2f}) for "
                    f"'{question[:50]}...'"
                )
                continue

            if ai_confidence < 0.6:
                logger.debug(
                    f"AIAgent: Low confidence ({ai_confidence:.2f}) for "
                    f"'{question[:50]}...'"
                )
                continue

            # Edge formula: materiality-weighted distance from current price.
            # Bullish: edge = materiality * (1 - price)  → YES token underpriced
            # Bearish: edge = materiality * price         → NO token underpriced
            min_edge = config.MIN_KELLY_EDGE  # Reuse the 3% min-edge threshold
            if direction == "BULLISH":
                edge = materiality * (1 - poly_price)
                is_yes = True
                price = poly_price
                estimated_prob = poly_price + edge
            else:  # BEARISH
                edge = materiality * poly_price
                is_yes = False
                price = 1.0 - poly_price
                estimated_prob = (1.0 - poly_price) + edge

            estimated_prob = min(estimated_prob, 0.99)

            if edge < min_edge:
                logger.debug(
                    f"AIAgent: Edge too small ({edge:.3f}) for '{question[:50]}...'"
                )
                continue

            tokens = market.get("tokens", []) or market.get("clobTokenIds", [])
            token_id = _pick_token_id(tokens, is_yes=is_yes)

            if not token_id:
                continue

            # Exit levels derived from materiality-weighted edge
            tp_price = min(price + edge * 0.8, 0.95)
            sl_price = max(price - edge * 1.5, 0.01)

            signal = Signal(
                strategy=self.name,
                token_id=token_id,
                market_question=question,
                side="BUY",
                price=price,
                size_usdc=0,
                take_profit=tp_price,
                stop_loss=sl_price,
                estimated_prob=estimated_prob,
                confidence=ai_confidence,
                reasoning=(
                    f"AI classification: {direction} (materiality: "
                    f"{materiality:.2f}, confidence: {ai_confidence:.2f}). "
                    f"{reasoning}"
                ),
            )
            signals.append(signal)

            q_display = question[:60] + ("..." if len(question) > 60 else "")
            logger.info(
                f"AIAgent: Signal for '{q_display}' - "
                f"{direction} mat={materiality:.2f}, conf={ai_confidence:.2f}, "
                f"edge={edge:.3f}"
            )

        logger.debug(
            "AIAgent: scan summary - "
            f"markets={len(poly_markets)} "
            f"vol_skip={volume_filtered} "
            f"price_skip={price_filtered} "
            f"news_skip={news_filtered} "
            f"ai_calls={ai_calls_this_cycle} "
            f"signals={len(signals)} "
            f"budget_left={max(config.MAX_AI_CALLS_PER_DAY - self._calls_today, 0)}"
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

