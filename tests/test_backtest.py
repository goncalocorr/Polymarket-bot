"""Tests for the backtesting engine."""

import pytest
import numpy as np
import pandas as pd

from scripts.backtest import backtest_flash_crash, backtest_mispricing, BacktestResult


@pytest.fixture
def synthetic_trades():
    """Create synthetic trade data with injected flash crashes."""
    np.random.seed(42)
    n = 5000
    prices = np.random.uniform(0.3, 0.7, n)

    # Inject flash crashes at known positions
    for start in [500, 1500, 2500, 3500]:
        prices[start : start + 6] = prices[start] * 0.5

    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=n, freq="1s"),
        "price": prices,
        "market_id": "test_market",
    })


@pytest.fixture
def synthetic_markets():
    return pd.DataFrame({
        "id": [f"market_{i}" for i in range(50)],
        "question": [f"Will event {i} happen?" for i in range(50)],
        "volume24hr": np.random.uniform(1000, 50000, 50),
    })


@pytest.fixture
def synthetic_kalshi():
    return pd.DataFrame({
        "id": [f"kalshi_{i}" for i in range(30)],
        "title": [f"Will event {i} happen?" for i in range(30)],
        "price": np.random.uniform(0.2, 0.8, 30),
    })


class TestBacktestFlashCrash:
    def test_detects_injected_crashes(self, synthetic_trades):
        result = backtest_flash_crash(synthetic_trades)
        assert result.total_trades > 0
        assert result.wins + result.losses == result.total_trades

    def test_metrics_computed(self, synthetic_trades):
        result = backtest_flash_crash(synthetic_trades)
        result.compute_metrics()
        assert 0 <= result.win_rate <= 1
        assert result.max_drawdown >= 0

    def test_empty_data_returns_zero_trades(self):
        result = backtest_flash_crash(pd.DataFrame())
        assert result.total_trades == 0

    def test_missing_columns_handled(self):
        df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
        result = backtest_flash_crash(df)
        assert result.total_trades == 0


class TestBacktestMispricing:
    def test_runs_with_data(self, synthetic_trades, synthetic_markets, synthetic_kalshi):
        result = backtest_mispricing(synthetic_trades, synthetic_markets, synthetic_kalshi)
        assert result.total_trades > 0

    def test_empty_data_returns_zero(self):
        result = backtest_mispricing(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
        assert result.total_trades == 0


class TestBacktestResult:
    def test_sharpe_ratio_calculation(self):
        result = BacktestResult(strategy="test")
        result.pnl_history = [1.0, -0.5, 2.0, -1.0, 0.5, 1.5, -0.3]
        result.total_trades = len(result.pnl_history)
        result.wins = sum(1 for p in result.pnl_history if p > 0)
        result.losses = sum(1 for p in result.pnl_history if p < 0)
        result.total_pnl = sum(result.pnl_history)
        result.compute_metrics()

        assert result.sharpe_ratio != 0
        assert result.max_drawdown >= 0
        assert result.win_rate == pytest.approx(4 / 7)

    def test_str_output(self):
        result = BacktestResult(strategy="test", total_trades=10, wins=6, losses=4)
        result.total_pnl = 15.0
        result.compute_metrics()
        text = str(result)
        assert "test" in text
        assert "15.00" in text
