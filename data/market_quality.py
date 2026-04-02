"""
Market Quality Score — evaluates whether a market is safe to trade.

Scoring dimensions (0.0–1.0 each):
  - Liquidity (40%): order book depth on best 3 levels
  - Spread   (25%): bid-ask spread in cents
  - Volume   (20%): 24h volume normalised
  - Stability(15%): price volatility over 5 minutes

Markets scoring below MIN_MARKET_QUALITY_SCORE are untradeable.
"""

from dataclasses import dataclass
from typing import Optional

from loguru import logger

import config
from core.websocket_client import MarketWebSocket


@dataclass
class QualityReport:
    """Breakdown of a market's quality score."""

    token_id: str
    liquidity_score: float = 0.0
    spread_score: float = 0.0
    volume_score: float = 0.0
    stability_score: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.liquidity_score * 0.40
            + self.spread_score * 0.25
            + self.volume_score * 0.20
            + self.stability_score * 0.15
        )

    @property
    def is_tradeable(self) -> bool:
        return self.total >= config.MIN_MARKET_QUALITY_SCORE

    def __str__(self) -> str:
        return (
            f"Quality={self.total:.2f} "
            f"[liq={self.liquidity_score:.2f} spread={self.spread_score:.2f} "
            f"vol={self.volume_score:.2f} stab={self.stability_score:.2f}]"
        )


class MarketQualityScorer:
    """Scores market quality using real-time order book data."""

    def __init__(self, ws: MarketWebSocket):
        self.ws = ws

    def score(
        self,
        token_id: str,
        volume_24h: float = 0.0,
    ) -> QualityReport:
        """
        Compute a quality score for a market.

        Args:
            token_id: The token to evaluate.
            volume_24h: 24-hour volume in USDC (from Gamma API metadata).

        Returns:
            QualityReport with per-dimension scores and a total.
        """
        report = QualityReport(token_id=token_id)

        # --- Liquidity: order book depth ---
        depth = self.ws.get_depth(token_id, levels=3)
        if depth:
            bid_depth = depth["bid_depth"]
            ask_depth = depth["ask_depth"]
            min_side = min(bid_depth, ask_depth)
            # Score: 0 below $50, linear to 1.0 at $500+
            report.liquidity_score = min(max(min_side - 50, 0) / 450, 1.0)
        else:
            report.liquidity_score = 0.0

        # --- Spread ---
        spread = self.ws.get_spread(token_id)
        if spread is not None:
            spread_cents = spread * 100
            # Perfect: 0–1¢ → 1.0; Bad: 5¢+ → 0.0
            report.spread_score = max(1.0 - (spread_cents - 1.0) / 4.0, 0.0)
        else:
            report.spread_score = 0.0

        # --- Volume ---
        # $0 → 0.0; $5k → 0.5; $20k+ → 1.0
        report.volume_score = min(volume_24h / 20_000, 1.0)

        # --- Stability (inverse volatility) ---
        vol = self.ws.get_volatility(token_id, lookback_seconds=300)
        if vol is not None:
            # 0% volatility → 1.0; 15%+ → 0.0
            report.stability_score = max(1.0 - vol / 0.15, 0.0)
        else:
            # No data = assume moderate stability
            report.stability_score = 0.5

        return report

    def is_tradeable(
        self,
        token_id: str,
        volume_24h: float = 0.0,
    ) -> tuple[bool, QualityReport]:
        """Quick check: is this market good enough to trade?"""
        report = self.score(token_id, volume_24h)
        if not report.is_tradeable:
            logger.debug(
                f"Market {token_id[:12]}… blocked: {report}"
            )
        return report.is_tradeable, report
