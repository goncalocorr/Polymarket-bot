"""Runtime safety tests for startup, shutdown, and status reporting."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from apscheduler.schedulers.base import SchedulerNotRunningError

import config
from main import Orchestrator
from monitor.dashboard import StatusWriter


@pytest.mark.asyncio
async def test_stop_ignores_scheduler_not_running():
    orch = Orchestrator.__new__(Orchestrator)
    orch._running = True
    orch._tasks = []
    orch.scheduler = MagicMock()
    orch.scheduler.shutdown.side_effect = SchedulerNotRunningError()
    orch.status_writer = SimpleNamespace(stop=MagicMock())
    orch.dashboard = SimpleNamespace(stop=MagicMock())
    orch.strategies = []
    orch.ws = SimpleNamespace(close=AsyncMock())
    orch.gamma = SimpleNamespace(close=AsyncMock())
    orch.external = SimpleNamespace(close=AsyncMock())
    orch.bot = SimpleNamespace(close=AsyncMock())
    orch.telegram = SimpleNamespace(close=AsyncMock())

    await Orchestrator.stop(orch)

    orch.scheduler.shutdown.assert_called_once_with(wait=False)
    orch.status_writer.stop.assert_called_once()
    orch.dashboard.stop.assert_called_once()
    orch.ws.close.assert_awaited_once()
    orch.telegram.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_rejects_non_positive_live_balance():
    orch = Orchestrator.__new__(Orchestrator)
    orch._running = False
    orch._tasks = []
    orch._instance_lock = SimpleNamespace(acquire=MagicMock(return_value=True))
    orch.bot = SimpleNamespace(
        api_creds=SimpleNamespace(is_valid=lambda: True),
        get_balance=AsyncMock(return_value=0.0),
    )
    orch.risk_manager = SimpleNamespace(update_capital=AsyncMock())
    orch.db = SimpleNamespace(get_open_trades=MagicMock(return_value=[]))
    orch.order_manager = SimpleNamespace(load_open_positions=MagicMock())
    orch.ws = SimpleNamespace(connect=AsyncMock())
    orch.flash_crash = SimpleNamespace(initialize=AsyncMock())
    orch.mispricing = SimpleNamespace(
        initialize=AsyncMock(),
        restore_runtime_state=AsyncMock(),
    )
    orch.ai_agent = SimpleNamespace(initialize=AsyncMock())
    orch.multi_outcome = SimpleNamespace(initialize=AsyncMock())
    orch.telegram = SimpleNamespace(bot_started=AsyncMock())
    orch.scheduler = MagicMock()
    orch.status_writer = SimpleNamespace(run=AsyncMock())
    orch.dashboard = SimpleNamespace(run=AsyncMock())
    orch.strategies = []

    with (
        patch.object(config, "DRY_RUN", False),
        patch.object(config, "POLYGON_WALLET_PRIVATE_KEY", "test-key"),
        pytest.raises(RuntimeError, match="non-positive balance"),
    ):
        await Orchestrator.start(orch)

    orch.risk_manager.update_capital.assert_not_awaited()
    orch.ws.connect.assert_not_awaited()


def test_status_writer_emits_valid_utc_timestamp():
    order_manager = SimpleNamespace(
        get_open_positions=MagicMock(return_value=[]),
        ws=SimpleNamespace(get_mid_price=MagicMock(return_value=None)),
    )
    risk_manager = SimpleNamespace(status="RUNNING", total_capital=500.0, _daily_start_capital=500.0)
    db = SimpleNamespace(
        get_recent_trades=MagicMock(return_value=[]),
        get_today_pnl=MagicMock(return_value=12.5),
        get_total_pnl=MagicMock(return_value=18.0),
    )
    writer = StatusWriter(order_manager, risk_manager, db, strategies=[])

    status = writer._build_status()

    parsed = status["updated_at"].replace("Z", "+00:00")
    assert parsed.endswith("+00:00")
    fromiso = datetime.fromisoformat(parsed)
    assert fromiso.tzinfo is not None
    assert not status["updated_at"].endswith("+00:00Z")
    assert isinstance(status["process_id"], int)
    assert status["is_simulated"] is True
    assert status["pnl_mode"] == "simulated_mark_to_mid"
    assert status["stop_reason"] == ""
    assert status["resume_at"] == ""


def test_status_writer_emits_pause_metadata():
    order_manager = SimpleNamespace(
        get_open_positions=MagicMock(return_value=[]),
        ws=SimpleNamespace(get_mid_price=MagicMock(return_value=None)),
    )
    risk_manager = SimpleNamespace(
        status="PAUSED",
        stop_reason="Circuit breaker active",
        resume_at="2026-04-02T09:00:00Z",
        total_capital=500.0,
        _daily_start_capital=500.0,
    )
    db = SimpleNamespace(
        get_recent_trades=MagicMock(return_value=[]),
        get_today_pnl=MagicMock(return_value=-12.5),
        get_total_pnl=MagicMock(return_value=-12.5),
    )
    writer = StatusWriter(order_manager, risk_manager, db, strategies=[])

    status = writer._build_status()

    assert status["status"] == "PAUSED"
    assert status["stop_reason"] == "Circuit breaker active"
    assert status["resume_at"] == "2026-04-02T09:00:00Z"


def test_status_writer_includes_close_reason_for_recent_trades():
    order_manager = SimpleNamespace(
        get_open_positions=MagicMock(return_value=[]),
        ws=SimpleNamespace(get_mid_price=MagicMock(return_value=None)),
    )
    risk_manager = SimpleNamespace(status="RUNNING", total_capital=500.0, _daily_start_capital=500.0)
    db = SimpleNamespace(
        get_recent_trades=MagicMock(
            return_value=[
                {
                    "market_question": "Test market",
                    "side": "BUY",
                    "entry_price": 0.5,
                    "exit_price": 0.6,
                    "pnl_usdc": 10.0,
                    "close_reason": "take-profit",
                    "closed_at": "2026-04-02T00:00:00+00:00",
                }
            ]
        ),
        get_today_pnl=MagicMock(return_value=10.0),
        get_total_pnl=MagicMock(return_value=10.0),
    )
    writer = StatusWriter(order_manager, risk_manager, db, strategies=[])

    status = writer._build_status()

    assert status["recent_trades"][0]["close_reason"] == "take-profit"


@pytest.mark.asyncio
async def test_restore_risk_state_from_db_in_dry_run_recovers_capital_and_stop():
    orch = Orchestrator.__new__(Orchestrator)
    orch._initial_capital = 500.0
    orch.risk_manager = SimpleNamespace(
        total_capital=500.0,
        restore_state=AsyncMock(),
    )
    latest_loss = datetime.now(timezone.utc) - timedelta(minutes=10)
    orch.db = SimpleNamespace(
        get_today_pnl=MagicMock(return_value=-26.64),
        get_total_pnl=MagicMock(return_value=-26.64),
        get_recent_trades=MagicMock(
            return_value=[
                {"pnl_usdc": -13.14, "closed_at": latest_loss.isoformat()},
                {"pnl_usdc": -13.50, "closed_at": (latest_loss - timedelta(minutes=1)).isoformat()},
                {"pnl_usdc": -5.00, "closed_at": (latest_loss - timedelta(minutes=2)).isoformat()},
            ]
        ),
    )

    with patch.object(config, "DRY_RUN", True):
        await Orchestrator._restore_risk_state_from_db(orch)

    orch.risk_manager.restore_state.assert_called_once()
    kwargs = orch.risk_manager.restore_state.call_args.kwargs
    assert kwargs["total_capital"] == pytest.approx(473.36)
    assert kwargs["daily_pnl"] == pytest.approx(-26.64)
    assert kwargs["circuit_breaker_until"] is not None


@pytest.mark.asyncio
async def test_restore_risk_state_ignores_time_stop_losses_for_circuit_breaker_in_dry_run():
    orch = Orchestrator.__new__(Orchestrator)
    orch._initial_capital = 500.0
    orch.risk_manager = SimpleNamespace(
        total_capital=500.0,
        restore_state=AsyncMock(),
    )
    latest_loss = datetime.now(timezone.utc) - timedelta(minutes=10)
    orch.db = SimpleNamespace(
        get_today_pnl=MagicMock(return_value=-3.58),
        get_total_pnl=MagicMock(return_value=-3.58),
        get_recent_trades=MagicMock(
            return_value=[
                {
                    "pnl_usdc": -0.31,
                    "close_reason": "time-stop",
                    "closed_at": latest_loss.isoformat(),
                },
                {
                    "pnl_usdc": -3.11,
                    "close_reason": "time-stop",
                    "closed_at": (latest_loss - timedelta(hours=2)).isoformat(),
                },
                {
                    "pnl_usdc": -0.16,
                    "close_reason": "time-stop",
                    "closed_at": (latest_loss - timedelta(hours=10)).isoformat(),
                },
            ]
        ),
    )

    with patch.object(config, "DRY_RUN", True):
        await Orchestrator._restore_risk_state_from_db(orch)

    kwargs = orch.risk_manager.restore_state.call_args.kwargs
    assert kwargs["circuit_breaker_until"] is None


@pytest.mark.asyncio
async def test_start_restores_mispricing_runtime_state():
    orch = Orchestrator.__new__(Orchestrator)
    orch._running = False
    orch._tasks = []
    orch._initial_capital = 500.0
    orch._instance_lock = SimpleNamespace(acquire=MagicMock(return_value=True))
    orch.bot = SimpleNamespace(api_creds=None)
    orch.risk_manager = SimpleNamespace(
        total_capital=500.0,
        restore_state=AsyncMock(),
    )
    open_positions = [SimpleNamespace(id="pos-1")]
    open_trades = [{"id": "pos-1"}]
    recent_trades = [{"id": "trade-1"}]
    orch.db = SimpleNamespace(
        get_today_pnl=MagicMock(return_value=0.0),
        get_total_pnl=MagicMock(return_value=0.0),
        get_recent_trades=MagicMock(return_value=recent_trades),
        get_open_trades=MagicMock(return_value=open_trades),
    )
    orch.order_manager = SimpleNamespace(
        load_open_positions=MagicMock(),
        get_open_positions=MagicMock(return_value=open_positions),
    )
    orch.ws = SimpleNamespace(connect=AsyncMock(), run_with_reconnect=AsyncMock())
    orch.flash_crash = SimpleNamespace(initialize=AsyncMock())
    orch.mispricing = SimpleNamespace(
        initialize=AsyncMock(),
        restore_runtime_state=AsyncMock(),
        name="mispricing",
    )
    orch.ai_agent = SimpleNamespace(initialize=AsyncMock())
    orch.multi_outcome = SimpleNamespace(initialize=AsyncMock())
    orch.telegram = SimpleNamespace(bot_started=AsyncMock())
    orch.scheduler = MagicMock()
    orch.status_writer = SimpleNamespace(run=AsyncMock())
    orch.dashboard = SimpleNamespace(run=AsyncMock())
    orch.strategies = []

    def _fake_create_task(coro):
        coro.close()
        return MagicMock()

    with (
        patch.object(config, "DRY_RUN", True),
        patch.object(config, "DAILY_REPORT_HOUR_UTC", 8),
        patch("main.asyncio.create_task", side_effect=_fake_create_task),
        patch("main.asyncio.gather", AsyncMock(side_effect=asyncio.CancelledError())),
    ):
        await Orchestrator.start(orch)

    orch.mispricing.restore_runtime_state.assert_awaited_once_with(
        open_positions,
        recent_trades,
        db=orch.db,
    )


@pytest.mark.asyncio
async def test_start_rejects_second_instance():
    orch = Orchestrator.__new__(Orchestrator)
    orch._instance_lock = SimpleNamespace(acquire=MagicMock(return_value=False))

    with pytest.raises(RuntimeError, match="already running"):
        await Orchestrator.start(orch)


@pytest.mark.asyncio
async def test_finalize_closed_position_uses_persisted_close_reason():
    orch = Orchestrator.__new__(Orchestrator)
    orch.risk_manager = SimpleNamespace(record_trade_result=AsyncMock())
    orch.db = SimpleNamespace(close_trade=MagicMock())
    orch.tracker = SimpleNamespace(record_trade=MagicMock())
    orch.mispricing = SimpleNamespace(name="mispricing", record_closed_trade=MagicMock())
    orch.telegram = SimpleNamespace(position_closed=AsyncMock())
    pos = SimpleNamespace(
        id="pos-1",
        exit_price=0.42,
        pnl_usdc=-12.0,
        strategy="mispricing",
        token_id="tok-1",
        market_question="Test market",
        size_usdc=50.0,
        close_reason="stop-loss",
        status="CLOSED",
    )

    await Orchestrator._finalize_closed_position(orch, pos)

    orch.db.close_trade.assert_called_once_with(
        "pos-1",
        0.42,
        -12.0,
        close_reason="stop-loss",
    )
    orch.mispricing.record_closed_trade.assert_called_once_with(
        "tok-1",
        -12.0,
        "stop-loss",
    )
    orch.telegram.position_closed.assert_awaited_once()
    orch.risk_manager.record_trade_result.assert_awaited_once_with(
        -12.0,
        close_reason="stop-loss",
    )
    assert orch.telegram.position_closed.await_args.args[3] == "stop-loss"
