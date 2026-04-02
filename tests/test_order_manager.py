"""Tests for order-manager close reasons and exit ordering."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from execution.order_manager import OrderManager, Position


@pytest.mark.asyncio
async def test_close_position_persists_close_reason():
    executor = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                filled=True,
                fill_size=10.0,
                fill_price=0.44,
                phases_used=1,
                error=None,
            )
        )
    )
    ws = SimpleNamespace(get_mid_price=MagicMock(return_value=0.44))
    manager = OrderManager(bot=SimpleNamespace(), ws=ws, executor=executor)
    pos = Position(
        id="pos-1",
        token_id="tok-1",
        market_question="Test market",
        strategy="mispricing",
        side="BUY",
        entry_price=0.50,
        size_usdc=10.0,
        opened_at=datetime.now(timezone.utc),
    )
    manager.positions[pos.id] = pos

    closed = await manager.close_position(pos.id, reason="stop-loss")

    assert closed is not None
    assert closed.close_reason == "stop-loss"
    assert closed.status == "CLOSED"


@pytest.mark.asyncio
async def test_check_exits_uses_strategy_stop_before_position_stop():
    manager = OrderManager(
        bot=SimpleNamespace(),
        ws=SimpleNamespace(get_mid_price=MagicMock(return_value=0.39)),
        executor=SimpleNamespace(),
    )
    pos = Position(
        id="pos-1",
        token_id="tok-1",
        market_question="Test market",
        strategy="mispricing",
        side="BUY",
        entry_price=0.50,
        size_usdc=100.0,
        take_profit=0.80,
        stop_loss=0.40,
        opened_at=datetime.now(timezone.utc),
    )
    manager.positions[pos.id] = pos
    manager.close_position = AsyncMock(return_value=pos)

    await manager.check_exits()

    manager.close_position.assert_awaited_once_with("pos-1", "stop-loss")


@pytest.mark.asyncio
async def test_check_exits_keeps_position_stop_as_fallback_without_strategy_stop():
    manager = OrderManager(
        bot=SimpleNamespace(),
        ws=SimpleNamespace(get_mid_price=MagicMock(return_value=0.39)),
        executor=SimpleNamespace(),
    )
    pos = Position(
        id="pos-1",
        token_id="tok-1",
        market_question="Test market",
        strategy="flash_crash",
        side="BUY",
        entry_price=0.50,
        size_usdc=10.0,
        take_profit=None,
        stop_loss=None,
        opened_at=datetime.now(timezone.utc),
    )
    manager.positions[pos.id] = pos
    manager.close_position = AsyncMock(return_value=pos)

    await manager.check_exits()

    manager.close_position.assert_awaited_once_with("pos-1", "position-stop-20%")
