"""
Cross-Market Mispricing Strategy — finds probability divergences between
Polymarket and external prediction markets (Kalshi, Metaculus, Manifold).
"""

import asyncio
from datetime import datetime, timezone
from typing import Union

from loguru import logger

import config
from core.gamma_client import GammaClient
from data.external_sources import ExternalSources
from strategies.base import BaseStrategy, Signal


def _parse_outcome_price(raw: Union[str, list, None], default: float = 0.5) -> float:
    """Safely parse outcomePrices from Gamma API (can be string or list)."""
    if raw is None:
        return default
    if isinstance(raw, list):
        return float(raw[0]) if raw else default
    if isinstance(raw, str):
        try:
            return float(raw.strip("[]").split(",")[0])
        except (ValueError, IndexError):
            return default
    return default


def _pick_token_id(tokens: list, is_yes: bool) -> str:
    """Pick YES or NO token ID, checking 'outcome' field if available."""
    if not tokens:
        return ""
    for t in tokens:
        if isinstance(t, dict):
            outcome = (t.get("outcome", "") or "").upper()
            if is_yes and outcome == "YES":
                return t.get("token_id", "")
            if not is_yes and outcome == "NO":
                return t.get("token_id", "")
    # Fallback: first for YES, last for NO
    t = tokens[0] if is_yes else tokens[-1]
    return t if isinstance(t, str) else t.get("token_id", "")


class MispricingStrategy(BaseStrategy):
    """
    Compares Polymarket odds with Kalshi/Metaculus/Manifold.
    If divergence exceeds threshold, trades the underpriced side.
    """

    def __init__(
        self,
        gamma: GammaClient,
        external: ExternalSources,
        min_mispricing: float = config.MIN_MISPRICING_PCT,
        close_threshold: float = 0.02,
        max_position_pct: float = config.MAX_POSITION_SIZE_PCT,
    ):
        super().__init__("mispricing")
        self.gamma = gamma
        self.external = external
        self.min_mispricing = min_mispricing
        self.close_threshold = close_threshold
        self.max_position_pct = max_position_pct

    async def initialize(self):
        logger.info("Mispricing: Strategy initialized")

    async def evaluate(self) -> list[Signal]:
        """Scan markets for cross-platform divergences."""
        if not self.is_active:
            return []

        signals = []

        # Get Polymarket markets suitable for mispricing
        poly_markets = await self.gamma.get_markets_for_mispricing()
        logger.info(f"Mispricing: Scanning {len(poly_markets)} eligible markets")

        for market in poly_markets:
            question = market.get("question", "")
            poly_price = _parse_outcome_price(market.get("outcomePrices"))

            # Find the same event on external platforms
            external_prices = await self.external.find_matching_prices(question)

            if not external_prices:
                continue

            # Average external probability
            avg_external = sum(external_prices.values()) / len(external_prices)
            divergence = avg_external - poly_price

            if abs(divergence) < self.min_mispricing:
                continue

            # Determine trade direction
            if divergence > 0:
                # External says higher probability → Polymarket is underpriced → BUY YES
                side = "BUY"
                signal_price = poly_price
                reasoning = (
                    f"Polymarket YES at {poly_price:.2f} vs "
                    f"external avg {avg_external:.2f} "
                    f"(divergence: +{divergence:.2f}). "
                    f"Sources: {external_prices}"
                )
            else:
                # External says lower probability → Polymarket YES is overpriced → BUY NO
                side = "BUY"
                signal_price = 1.0 - poly_price  # NO side price
                reasoning = (
                    f"Polymarket NO at {signal_price:.2f}, "
                    f"external avg YES {avg_external:.2f} "
                    f"(divergence: {divergence:.2f}). "
                    f"Sources: {external_prices}"
                )

            tokens = market.get("tokens", []) or market.get("clobTokenIds", [])
            token_id = _pick_token_id(tokens, is_yes=(divergence > 0))

            if not token_id:
                continue

            signal = Signal(
                strategy=self.name,
                token_id=token_id,
                market_question=question,
                side=side,
                price=signal_price,
                size_usdc=0,  # Will be calculated by risk manager
                take_profit=None,  # Close when divergence < close_threshold
                stop_loss=None,  # Managed by risk manager (20% position stop)
                confidence=min(abs(divergence) / self.min_mispricing, 1.0),
                reasoning=reasoning,
            )
            signals.append(signal)

        if signals:
            logger.info(f"Mispricing: Found {len(signals)} opportunities")

        return signals

    async def shutdown(self):
        logger.info("Mispricing: Shut down")
