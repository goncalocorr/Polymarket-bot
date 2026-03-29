"""Tests for the risk manager."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from risk.risk_manager import RiskManager
from strategies.base import Signal


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset singleton between tests."""
    RiskManager._instance = None
    yield
    RiskManager._instance = None


@pytest.fixture
def rm():
    return RiskManager(total_capital=500.0)


def make_signal(**kwargs) -> Signal:
    defaults = {
        "strategy": "test",
        "token_id": "token123",
        "market_question": "Test?",
        "side": "BUY",
        "price": 0.5,
        "size_usdc": 25.0,
    }
    defaults.update(kwargs)
    return Signal(**defaults)


class TestRiskManagerValidation:
    def test_allows_normal_order(self, rm):
        signal = make_signal()
        allowed, size, reason = rm.validate_order(signal, 0, False)
        assert allowed
        assert size > 0
        assert reason == "OK"

    def test_blocks_when_daily_stop_triggered(self, rm):
        # Trigger stop via actual losses (not just setting the flag,
        # since _reset_daily() clears it on first call each day)
        rm.record_trade_result(-26.0)  # 5% of 500 = 25, this exceeds it
        signal = make_signal()
        allowed, _, reason = rm.validate_order(signal, 0, False)
        assert not allowed
        assert "stop-loss" in reason.lower()

    def test_blocks_existing_position(self, rm):
        signal = make_signal()
        allowed, _, reason = rm.validate_order(signal, 0, True)
        assert not allowed
        assert "position" in reason.lower()

    def test_blocks_when_exposure_exceeded(self, rm):
        signal = make_signal()
        max_exposure = 500 * 0.60  # 300
        allowed, _, reason = rm.validate_order(signal, 300, False)
        assert not allowed
        assert "exposure" in reason.lower()

    def test_caps_position_size(self, rm):
        signal = make_signal(size_usdc=200)  # Way over 10% of 500
        allowed, size, _ = rm.validate_order(signal, 0, False)
        assert allowed
        assert size <= 500 * 0.10  # Max 50

    def test_circuit_breaker_blocks(self, rm):
        rm._circuit_breaker_until = datetime.now(timezone.utc) + timedelta(hours=1)
        signal = make_signal()
        allowed, _, reason = rm.validate_order(signal, 0, False)
        assert not allowed
        assert "circuit breaker" in reason.lower()

    def test_circuit_breaker_expires(self, rm):
        rm._circuit_breaker_until = datetime.now(timezone.utc) - timedelta(hours=1)
        signal = make_signal()
        allowed, _, _ = rm.validate_order(signal, 0, False)
        assert allowed


class TestRiskManagerTradeRecording:
    def test_consecutive_losses_trigger_circuit_breaker(self, rm):
        for _ in range(3):
            rm.record_trade_result(-5.0)
        assert rm._circuit_breaker_until is not None

    def test_win_resets_consecutive_losses(self, rm):
        rm.record_trade_result(-5.0)
        rm.record_trade_result(-5.0)
        rm.record_trade_result(10.0)
        assert rm._consecutive_losses == 0

    def test_daily_stop_loss_triggers(self, rm):
        # 5% of 500 = 25
        rm.record_trade_result(-26.0)
        assert rm._daily_stop_triggered

    def test_capital_updates(self, rm):
        rm.record_trade_result(10.0)
        assert rm.total_capital == 510.0
        rm.record_trade_result(-5.0)
        assert rm.total_capital == 505.0


class TestRiskManagerStatus:
    def test_running_status(self, rm):
        assert rm.status == "RUNNING"

    def test_stopped_status(self, rm):
        rm._daily_stop_triggered = True
        assert rm.status == "STOPPED"

    def test_paused_status(self, rm):
        rm._circuit_breaker_until = datetime.now(timezone.utc) + timedelta(hours=1)
        assert rm.status == "PAUSED"
