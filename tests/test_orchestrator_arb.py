"""Tests for atomic multi-leg arbitrage execution in Orchestrator."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from main import Orchestrator
from strategies.base import Signal


def _mk_signal(token_id: str, group_id: str) -> Signal:
    return Signal(
        strategy="multi_outcome_arb",
        token_id=token_id,
        market_question=f"Outcome {token_id}",
        side="BUY",
        price=0.30,
        size_usdc=3.0,
        confidence=0.8,
        arb_group_id=group_id,
        arb_event_id="evt-1",
    )


def _mk_position(pos_id: str, token_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=pos_id,
        token_id=token_id,
        market_question=f"Outcome {token_id}",
        strategy="multi_outcome_arb",
        side="BUY",
        entry_price=0.30,
        size_usdc=3.0,
        take_profit=None,
        stop_loss=None,
        opened_at=datetime.now(timezone.utc),
    )


def _mk_closed_position(pos_id: str, token_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=pos_id,
        token_id=token_id,
        market_question=f"Outcome {token_id}",
        strategy="multi_outcome_arb",
        side="BUY",
        size_usdc=3.0,
        pnl_usdc=0.25,
        exit_price=0.33,
        status="CLOSED",
    )


def _mk_orchestrator_stub() -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)

    orch.db = MagicMock()
    orch.telegram = SimpleNamespace(
        position_opened=AsyncMock(),
        position_closed=AsyncMock(),
    )
    orch.risk_manager = SimpleNamespace(
        total_capital=500.0,
        validate_order=AsyncMock(return_value=(True, 3.0, "OK")),
        record_trade_result=AsyncMock(),
    )
    orch.tracker = SimpleNamespace(
        get_size_multiplier=MagicMock(return_value=1.0),
        record_trade=MagicMock(),
    )
    orch.multi_outcome = SimpleNamespace(
        name="multi_outcome_arb",
        mark_event_executed=MagicMock(),
    )
    orch.order_manager = SimpleNamespace(
        get_total_exposure=MagicMock(return_value=0.0),
        has_position_in_market=MagicMock(return_value=False),
        open_position=AsyncMock(),
        close_position=AsyncMock(),
    )
    orch._trade_lock = asyncio.Lock()

    return orch


@pytest.mark.asyncio
async def test_process_arb_group_success_executes_all_legs():
    orch = _mk_orchestrator_stub()
    orch.order_manager.open_position.side_effect = [
        _mk_position("pos-1", "tok-a"),
        _mk_position("pos-2", "tok-b"),
    ]

    signals = [_mk_signal("tok-a", "arb-1"), _mk_signal("tok-b", "arb-1")]
    await Orchestrator.process_arb_group(orch, signals)

    assert orch.order_manager.open_position.await_count == 2
    assert orch.db.save_trade.call_count == 2
    assert orch.telegram.position_opened.await_count == 2
    assert orch.order_manager.close_position.await_count == 0
    assert orch.risk_manager.record_trade_result.await_count == 0
    orch.multi_outcome.mark_event_executed.assert_called_once_with("evt-1")


@pytest.mark.asyncio
async def test_process_arb_group_failure_rolls_back_opened_legs():
    orch = _mk_orchestrator_stub()
    first_leg = _mk_position("pos-1", "tok-a")
    orch.order_manager.open_position.side_effect = [first_leg, None]
    orch.order_manager.close_position.return_value = _mk_closed_position(
        "pos-1", "tok-a"
    )

    signals = [_mk_signal("tok-a", "arb-2"), _mk_signal("tok-b", "arb-2")]
    await Orchestrator.process_arb_group(orch, signals)

    assert orch.db.save_trade.call_count == 1
    orch.order_manager.close_position.assert_awaited_once()
    assert orch.risk_manager.record_trade_result.await_count == 1
    assert orch.db.close_trade.call_count == 1
    assert orch.tracker.record_trade.call_count == 1
    orch.multi_outcome.mark_event_executed.assert_not_called()

    # Position-open alerts are sent only after all legs succeed.
    assert orch.telegram.position_opened.await_count == 0
    assert orch.telegram.position_closed.await_count == 1


@pytest.mark.asyncio
async def test_process_arb_group_persist_failure_rolls_back_all_opened_legs():
    orch = _mk_orchestrator_stub()
    first_leg = _mk_position("pos-1", "tok-a")
    second_leg = _mk_position("pos-2", "tok-b")
    orch.order_manager.open_position.side_effect = [first_leg, second_leg]
    orch.db.save_trade.side_effect = [None, RuntimeError("db down")]
    orch.order_manager.close_position.side_effect = [
        _mk_closed_position("pos-2", "tok-b"),
        _mk_closed_position("pos-1", "tok-a"),
    ]

    signals = [_mk_signal("tok-a", "arb-3"), _mk_signal("tok-b", "arb-3")]
    await Orchestrator.process_arb_group(orch, signals)

    assert orch.db.save_trade.call_count == 2
    assert orch.order_manager.close_position.await_count == 2
    assert orch.risk_manager.record_trade_result.await_count == 2
    assert orch.telegram.position_opened.await_count == 0
    assert orch.telegram.position_closed.await_count == 2
    orch.multi_outcome.mark_event_executed.assert_not_called()


@pytest.mark.asyncio
async def test_process_signal_persist_failure_closes_untracked_position():
    orch = _mk_orchestrator_stub()
    orch.db.save_trade.side_effect = RuntimeError("db down")
    orch.order_manager.open_position.return_value = _mk_position("pos-1", "tok-a")
    orch.order_manager.close_position.return_value = _mk_closed_position(
        "pos-1", "tok-a"
    )

    signal = _mk_signal("tok-a", group_id="")
    signal.arb_group_id = None
    await Orchestrator.process_signal(orch, signal)

    orch.order_manager.open_position.assert_awaited_once()
    orch.order_manager.close_position.assert_awaited_once()
    assert orch.risk_manager.record_trade_result.await_count == 1
    assert orch.db.close_trade.call_count == 1
    assert orch.telegram.position_opened.await_count == 0
