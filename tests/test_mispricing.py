"""Tests for the Mispricing strategy."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from strategies.mispricing import MispricingStrategy
from core.gamma_client import GammaClient
from data.external_sources import ExternalSources


@pytest.fixture
def gamma():
    g = MagicMock(spec=GammaClient)
    g.get_markets_for_mispricing = AsyncMock(return_value=[])
    return g


@pytest.fixture
def external():
    e = MagicMock(spec=ExternalSources)
    e.find_matching_prices = AsyncMock(return_value={})
    return e


@pytest.fixture
def strategy(gamma, external):
    return MispricingStrategy(gamma, external)


class TestMispricingDetection:
    @pytest.mark.asyncio
    async def test_no_signal_when_no_markets(self, strategy):
        signals = await strategy.evaluate()
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_no_signal_when_no_external_prices(self, strategy, gamma, external):
        gamma.get_markets_for_mispricing.return_value = [
            {
                "question": "Will event X happen?",
                "outcomePrices": "[0.50]",
                "volume24hr": 10000,
                "tokens": [{"token_id": "t1"}, {"token_id": "t2"}],
            }
        ]
        external.find_matching_prices.return_value = {}

        signals = await strategy.evaluate()
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_no_signal_when_divergence_small(self, strategy, gamma, external):
        gamma.get_markets_for_mispricing.return_value = [
            {
                "question": "Will event X happen?",
                "outcomePrices": "[0.50]",
                "volume24hr": 10000,
                "tokens": [{"token_id": "t1"}, {"token_id": "t2"}],
            }
        ]
        external.find_matching_prices.return_value = {"kalshi": 0.53}  # Only 3% diff

        signals = await strategy.evaluate()
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_signal_on_large_divergence(self, strategy, gamma, external):
        gamma.get_markets_for_mispricing.return_value = [
            {
                "question": "Will event X happen?",
                "outcomePrices": "[0.40]",
                "volume24hr": 10000,
                "tokens": ["t_yes", "t_no"],
            }
        ]
        external.find_matching_prices.return_value = {
            "kalshi": 0.55,
            "metaculus": 0.52,
        }  # ~13.5% divergence

        signals = await strategy.evaluate()
        assert len(signals) == 1
        assert signals[0].side == "BUY"
        assert signals[0].strategy == "mispricing"

    @pytest.mark.asyncio
    async def test_no_signal_when_paused(self, strategy, gamma, external):
        gamma.get_markets_for_mispricing.return_value = [
            {
                "question": "Will event X happen?",
                "outcomePrices": "[0.40]",
                "volume24hr": 10000,
                "tokens": ["t_yes", "t_no"],
            }
        ]
        external.find_matching_prices.return_value = {"kalshi": 0.55}
        strategy.pause()

        signals = await strategy.evaluate()
        assert len(signals) == 0
