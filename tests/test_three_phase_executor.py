"""Tests for the Three-Phase Order Executor."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from execution.executor import ThreePhaseExecutor
from core.bot import TradingBot


@pytest.fixture
def bot():
    b = MagicMock(spec=TradingBot)
    b.place_order = AsyncMock()
    b.cancel_order = AsyncMock(return_value=True)
    b.get_order = AsyncMock()
    return b


@pytest.fixture
def executor(bot):
    return ThreePhaseExecutor(bot)


class TestThreePhaseExecution:
    @pytest.mark.asyncio
    @patch("config.DRY_RUN", True)
    async def test_dry_run_returns_immediately(self, executor, bot):
        bot.place_order.return_value = {"id": "dry-run-123", "status": "DRY_RUN"}

        result = await executor.execute("token1", 0.50, 10.0, "BUY")
        assert result.filled
        assert result.phases_used == 1
        bot.place_order.assert_called_once()

    @pytest.mark.asyncio
    @patch("config.DRY_RUN", False)
    async def test_phase1_full_fill(self, executor, bot):
        bot.place_order.return_value = {"id": "order1"}
        bot.get_order.return_value = {"status": "FILLED", "filledSize": 10.0}

        result = await executor.execute("token1", 0.50, 10.0, "BUY")
        assert result.filled
        assert result.phases_used == 1

    @pytest.mark.asyncio
    @patch("config.DRY_RUN", False)
    async def test_order_placement_failure(self, executor, bot):
        bot.place_order.return_value = None

        result = await executor.execute("token1", 0.50, 10.0, "BUY")
        assert not result.filled
        assert "failed" in result.error.lower()

    @pytest.mark.asyncio
    @patch("config.DRY_RUN", False)
    @patch("config.MIN_ORDER_SIZE_USDC", 2.0)
    async def test_skips_phase_when_size_too_small(self, executor, bot):
        bot.place_order.return_value = None

        result = await executor.execute("token1", 0.50, 1.0, "BUY")
        assert not result.filled
