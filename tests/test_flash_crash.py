"""Tests for the Flash Crash strategy."""

import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from strategies.flash_crash import FlashCrashStrategy
from core.websocket_client import MarketWebSocket
from core.gamma_client import GammaClient


@pytest.fixture
def ws():
    ws = MagicMock(spec=MarketWebSocket)
    ws.subscribe = AsyncMock()
    ws.unsubscribe = AsyncMock()
    ws.get_imbalance.return_value = 0.50  # Normal imbalance (not manipulated)
    return ws


@pytest.fixture
def gamma():
    gamma = MagicMock(spec=GammaClient)
    gamma.get_15min_crypto_markets = AsyncMock(return_value=[])
    return gamma


@pytest.fixture
def strategy(ws, gamma):
    return FlashCrashStrategy(ws, gamma)


class TestFlashCrashDetection:
    @pytest.mark.asyncio
    async def test_no_signal_when_no_drop(self, strategy, ws):
        strategy._tracked_markets = {"token1": {"question": "BTC Up?", "end_date": ""}}
        ws.get_price_drop.return_value = 0.05  # Small drop
        ws.get_mid_price.return_value = 0.50

        signals = await strategy.evaluate()
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_signal_on_flash_crash(self, strategy, ws):
        strategy._tracked_markets = {"token1": {"question": "BTC Up?", "end_date": ""}}
        ws.get_price_drop.return_value = 0.35  # Big drop > 0.28 threshold
        ws.get_mid_price.return_value = 0.30

        signals = await strategy.evaluate()
        assert len(signals) == 1
        assert signals[0].side == "BUY"
        assert signals[0].strategy == "flash_crash"
        assert signals[0].price == 0.30

    @pytest.mark.asyncio
    async def test_no_duplicate_signal(self, strategy, ws):
        strategy._tracked_markets = {"token1": {"question": "BTC Up?", "end_date": ""}}
        ws.get_price_drop.return_value = 0.35
        ws.get_mid_price.return_value = 0.30

        signals1 = await strategy.evaluate()
        assert len(signals1) == 1

        # Second evaluation should not produce duplicate
        signals2 = await strategy.evaluate()
        assert len(signals2) == 0

    @pytest.mark.asyncio
    async def test_no_signal_when_paused(self, strategy, ws):
        strategy._tracked_markets = {"token1": {"question": "BTC Up?", "end_date": ""}}
        ws.get_price_drop.return_value = 0.35
        ws.get_mid_price.return_value = 0.30
        strategy.pause()

        signals = await strategy.evaluate()
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_take_profit_and_stop_loss_in_signal(self, strategy, ws):
        strategy._tracked_markets = {"token1": {"question": "BTC Up?", "end_date": ""}}
        ws.get_price_drop.return_value = 0.35
        ws.get_mid_price.return_value = 0.30

        signals = await strategy.evaluate()
        assert signals[0].take_profit == pytest.approx(0.30 + 0.08)
        assert signals[0].stop_loss == pytest.approx(0.30 - 0.04)

    @pytest.mark.asyncio
    async def test_no_signal_when_no_price_data(self, strategy, ws):
        strategy._tracked_markets = {"token1": {"question": "BTC Up?", "end_date": ""}}
        ws.get_price_drop.return_value = None

        signals = await strategy.evaluate()
        assert len(signals) == 0


class TestFlashCrashInitialize:
    @pytest.mark.asyncio
    async def test_subscribes_to_markets(self, strategy, ws, gamma):
        gamma.get_15min_crypto_markets.return_value = [
            {
                "question": "Will BTC be above $100k?",
                "tokens": [{"token_id": "abc123"}],
                "conditionId": "cond1",
                "endDate": "2025-01-01T00:15:00Z",
            }
        ]
        await strategy.initialize()
        assert "abc123" in strategy._tracked_markets
        ws.subscribe.assert_called_once_with("abc123")
