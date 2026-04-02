"""
Tests for the 4 profitability improvements:
1. Kelly Criterion sizing
2. Market Quality Score
3. Multi-Outcome Arbitrage
4. Strategy Performance Tracker
"""

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from strategies.base import Signal


# ============================================================
# 1. Kelly Criterion Tests
# ============================================================


class TestKellyCriterion:
    """Test Kelly Criterion position sizing in RiskManager."""

    def _make_rm(self, capital=500.0):
        from risk.risk_manager import RiskManager
        return RiskManager(total_capital=capital)

    def test_kelly_positive_edge(self):
        """Kelly should return positive fraction when estimated_prob > market_price."""
        rm = self._make_rm()
        # estimated_prob=0.70, market_price=0.50 → strong edge
        frac = rm.kelly_size(0.70, 0.50)
        assert frac > 0
        assert frac <= config.MAX_POSITION_SIZE_PCT

    def test_kelly_no_edge(self):
        """Kelly should return 0 when estimated_prob == market_price (no edge)."""
        rm = self._make_rm()
        frac = rm.kelly_size(0.50, 0.50)
        assert frac == 0.0

    def test_kelly_negative_edge(self):
        """Kelly should return 0 when estimated_prob < market_price (negative edge)."""
        rm = self._make_rm()
        frac = rm.kelly_size(0.30, 0.50)
        assert frac == 0.0

    def test_kelly_extreme_price_low(self):
        """Kelly should return 0 for extreme market_price near 0."""
        rm = self._make_rm()
        frac = rm.kelly_size(0.80, 0.005)
        assert frac == 0.0

    def test_kelly_extreme_price_high(self):
        """Kelly should return 0 for extreme market_price near 1."""
        rm = self._make_rm()
        frac = rm.kelly_size(0.80, 0.995)
        assert frac == 0.0

    def test_kelly_capped_at_max_position(self):
        """Kelly should never exceed MAX_POSITION_SIZE_PCT."""
        rm = self._make_rm()
        # Huge edge should still be capped
        frac = rm.kelly_size(0.99, 0.10, confidence=1.0)
        assert frac <= config.MAX_POSITION_SIZE_PCT

    def test_kelly_scales_with_confidence(self):
        """Lower confidence should reduce Kelly size proportionally."""
        rm = self._make_rm()
        full = rm.kelly_size(0.70, 0.50, confidence=1.0)
        half = rm.kelly_size(0.70, 0.50, confidence=0.5)
        assert half < full
        assert half > 0

    @pytest.mark.asyncio
    async def test_validate_order_uses_kelly_when_confident(self):
        """validate_order should use Kelly when Signal has high confidence."""
        rm = self._make_rm(500.0)
        signal = Signal(
            strategy="test",
            token_id="tok_1",
            market_question="Will X?",
            side="BUY",
            price=0.40,
            size_usdc=0,  # Let Kelly decide
            confidence=0.65,  # > KELLY_MIN_CONFIDENCE
        )
        allowed, size, reason = await rm.validate_order(
            signal,
            current_exposure=0,
            has_existing_position=False,
        )
        assert "kelly" in reason.lower()
        if allowed:
            assert size > 0

    @pytest.mark.asyncio
    async def test_validate_order_uses_fixed_when_low_confidence(self):
        """validate_order should fall back to fixed sizing when confidence is low."""
        rm = self._make_rm(500.0)
        signal = Signal(
            strategy="test",
            token_id="tok_1",
            market_question="Will X?",
            side="BUY",
            price=0.50,
            size_usdc=25.0,
            confidence=0.3,  # < KELLY_MIN_CONFIDENCE
        )
        allowed, size, reason = await rm.validate_order(
            signal,
            current_exposure=0,
            has_existing_position=False,
        )
        assert allowed
        assert "fixed" in reason.lower()

    @pytest.mark.asyncio
    async def test_validate_order_uses_estimated_probability_with_confidence_scaling(self):
        """estimated_prob should drive Kelly while confidence scales the size."""
        rm = self._make_rm(500.0)
        signal = Signal(
            strategy="test",
            token_id="tok_1",
            market_question="Will X?",
            side="BUY",
            price=0.50,
            size_usdc=0,
            estimated_prob=0.58,
            confidence=0.50,
        )
        allowed, size, reason = await rm.validate_order(
            signal,
            current_exposure=0,
            has_existing_position=False,
        )
        assert allowed
        assert 15.0 <= size <= 25.0
        assert "kelly-estimated" in reason.lower()

    @pytest.mark.asyncio
    async def test_validate_order_blocks_small_edge(self):
        """validate_order should block trades with edge below MIN_KELLY_EDGE."""
        rm = self._make_rm(500.0)
        signal = Signal(
            strategy="test",
            token_id="tok_1",
            market_question="Will X?",
            side="BUY",
            price=0.50,
            size_usdc=0,
            confidence=0.51,  # edge = 0.01, below MIN_KELLY_EDGE (0.03)
        )
        allowed, size, reason = await rm.validate_order(
            signal,
            current_exposure=0,
            has_existing_position=False,
        )
        assert not allowed
        assert "edge" in reason.lower()


# ============================================================
# 2. Market Quality Score Tests
# ============================================================


class TestMarketQuality:
    """Test MarketQualityScorer."""

    def _make_scorer(self, depth=None, spread=None, volatility=None):
        from data.market_quality import MarketQualityScorer

        ws = MagicMock()
        ws.get_depth.return_value = depth
        ws.get_spread.return_value = spread
        ws.get_volatility.return_value = volatility
        return MarketQualityScorer(ws)

    def test_high_quality_market(self):
        """A market with good liquidity, tight spread, and low volatility scores high."""
        scorer = self._make_scorer(
            depth={"bid_depth": 300, "ask_depth": 250},
            spread=0.01,  # 1 cent
            volatility=0.02,
        )
        report = scorer.score("tok_1", volume_24h=15000)
        assert report.total > 0.6
        assert report.is_tradeable

    def test_low_quality_no_liquidity(self):
        """A market with no order book data should score 0 on liquidity."""
        scorer = self._make_scorer(depth=None, spread=None, volatility=None)
        report = scorer.score("tok_1", volume_24h=0)
        assert report.liquidity_score == 0.0
        assert report.spread_score == 0.0
        assert report.volume_score == 0.0

    def test_wide_spread_penalized(self):
        """A market with >5¢ spread should get 0 spread score."""
        scorer = self._make_scorer(
            depth={"bid_depth": 200, "ask_depth": 200},
            spread=0.06,  # 6 cents
            volatility=0.01,
        )
        report = scorer.score("tok_1", volume_24h=10000)
        assert report.spread_score == 0.0

    def test_is_tradeable_respects_threshold(self):
        """is_tradeable returns False when total < MIN_MARKET_QUALITY_SCORE."""
        scorer = self._make_scorer(depth=None, spread=None, volatility=None)
        tradeable, report = scorer.is_tradeable("tok_1", volume_24h=0)
        assert not tradeable

    def test_perfect_market(self):
        """A market with perfect scores in all dimensions."""
        scorer = self._make_scorer(
            depth={"bid_depth": 600, "ask_depth": 600},
            spread=0.005,
            volatility=0.0,
        )
        report = scorer.score("tok_1", volume_24h=25000)
        assert report.total >= 0.9


# ============================================================
# 3. Multi-Outcome Arbitrage Tests
# ============================================================


class TestMultiOutcomeArbitrage:
    """Test MultiOutcomeStrategy."""

    def _make_strategy(
        self,
        events=None,
        event_markets=None,
        ws=None,
        quality_scorer=None,
    ):
        from strategies.multi_outcome import MultiOutcomeStrategy

        gamma = AsyncMock()
        gamma.get_arbitrage_events = AsyncMock(return_value=events or [])
        gamma.get_event_markets = AsyncMock(return_value=event_markets or [])
        return MultiOutcomeStrategy(
            gamma,
            ws=ws,
            quality_scorer=quality_scorer,
        )

    @pytest.mark.asyncio
    async def test_detects_pure_arbitrage(self):
        """Should generate signals when price sum < 0.97."""
        events = [{
            "id": "evt_1",
            "title": "Who wins?",
            "markets": [
                {
                    "question": "Alice wins?",
                    "outcomePrices": "0.30",
                    "tokens": [{"token_id": "tok_a", "outcome": "Yes"}],
                },
                {
                    "question": "Bob wins?",
                    "outcomePrices": "0.25",
                    "tokens": [{"token_id": "tok_b", "outcome": "Yes"}],
                },
                {
                    "question": "Carol wins?",
                    "outcomePrices": "0.20",
                    "tokens": [{"token_id": "tok_c", "outcome": "Yes"}],
                },
            ],
        }]
        strategy = self._make_strategy(events=events)
        await strategy.initialize()
        signals = await strategy.evaluate()
        # Sum = 0.75 < 0.97, should find arb
        assert len(signals) == 3
        assert all(s.side == "BUY" for s in signals)
        assert all(s.strategy == "multi_outcome_arb" for s in signals)

    @pytest.mark.asyncio
    async def test_no_arbitrage_when_sum_high(self):
        """Should not generate signals when price sum >= 0.97."""
        events = [{
            "id": "evt_2",
            "title": "Who wins?",
            "markets": [
                {
                    "question": "Alice wins?",
                    "outcomePrices": "0.50",
                    "tokens": [{"token_id": "tok_a", "outcome": "Yes"}],
                },
                {
                    "question": "Bob wins?",
                    "outcomePrices": "0.30",
                    "tokens": [{"token_id": "tok_b", "outcome": "Yes"}],
                },
                {
                    "question": "Carol wins?",
                    "outcomePrices": "0.20",
                    "tokens": [{"token_id": "tok_c", "outcome": "Yes"}],
                },
            ],
        }]
        strategy = self._make_strategy(events=events)
        signals = await strategy.evaluate()
        # Sum = 1.00 >= 0.97, no arb
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_skips_already_arbed_events(self):
        """Should not re-arb the same event twice."""
        events = [{
            "id": "evt_1",
            "title": "Who wins?",
            "markets": [
                {
                    "question": "A wins?",
                    "outcomePrices": "0.20",
                    "tokens": [{"token_id": "tok_a", "outcome": "Yes"}],
                },
                {
                    "question": "B wins?",
                    "outcomePrices": "0.20",
                    "tokens": [{"token_id": "tok_b", "outcome": "Yes"}],
                },
                {
                    "question": "C wins?",
                    "outcomePrices": "0.20",
                    "tokens": [{"token_id": "tok_c", "outcome": "Yes"}],
                },
            ],
        }]
        strategy = self._make_strategy(events=events)
        signals1 = await strategy.evaluate()
        strategy.mark_event_executed("evt_1")
        signals2 = await strategy.evaluate()
        assert len(signals1) == 3
        assert len(signals2) == 0  # Already arbed

    @pytest.mark.asyncio
    async def test_event_not_marked_until_execution_succeeds(self):
        """A failed arb attempt should remain eligible on the next scan."""
        events = [{
            "id": "evt_retry",
            "title": "Who wins?",
            "markets": [
                {
                    "question": "A wins?",
                    "outcomePrices": "0.20",
                    "tokens": [{"token_id": "tok_a", "outcome": "Yes"}],
                },
                {
                    "question": "B wins?",
                    "outcomePrices": "0.20",
                    "tokens": [{"token_id": "tok_b", "outcome": "Yes"}],
                },
                {
                    "question": "C wins?",
                    "outcomePrices": "0.20",
                    "tokens": [{"token_id": "tok_c", "outcome": "Yes"}],
                },
            ],
        }]
        strategy = self._make_strategy(events=events)

        signals1 = await strategy.evaluate()
        signals2 = await strategy.evaluate()

        assert len(signals1) == 3
        assert len(signals2) == 3

    @pytest.mark.asyncio
    async def test_skips_resolved_markets(self):
        """Markets with extreme prices (near 0 or 1) should be excluded."""
        events = [{
            "id": "evt_3",
            "title": "Who wins?",
            "markets": [
                {
                    "question": "A wins?",
                    "outcomePrices": "0.99",  # Practically resolved
                    "tokens": [{"token_id": "tok_a", "outcome": "Yes"}],
                },
                {
                    "question": "B wins?",
                    "outcomePrices": "0.01",  # Practically resolved
                    "tokens": [{"token_id": "tok_b", "outcome": "Yes"}],
                },
                {
                    "question": "C wins?",
                    "outcomePrices": "0.005",
                    "tokens": [{"token_id": "tok_c", "outcome": "Yes"}],
                },
            ],
        }]
        strategy = self._make_strategy(events=events)
        signals = await strategy.evaluate()
        # All extreme → filtered out → no signals
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_inactive_strategy_returns_empty(self):
        """Paused strategy should return no signals."""
        strategy = self._make_strategy(events=[])
        strategy.pause()
        signals = await strategy.evaluate()
        assert signals == []

    @pytest.mark.asyncio
    async def test_skips_arb_when_any_leg_has_low_market_quality(self):
        events = [{
            "id": "evt_low_quality",
            "title": "Who wins?",
            "markets": [
                {
                    "question": "Alice wins?",
                    "outcomePrices": "0.30",
                    "volume24hr": 10000,
                    "tokens": [{"token_id": "tok_a", "outcome": "Yes"}],
                },
                {
                    "question": "Bob wins?",
                    "outcomePrices": "0.25",
                    "volume24hr": 10000,
                    "tokens": [{"token_id": "tok_b", "outcome": "Yes"}],
                },
                {
                    "question": "Carol wins?",
                    "outcomePrices": "0.20",
                    "volume24hr": 10000,
                    "tokens": [{"token_id": "tok_c", "outcome": "Yes"}],
                },
            ],
        }]
        ws = MagicMock()
        ws.subscribe = AsyncMock()
        ws.unsubscribe = AsyncMock()
        ws.get_order_book.return_value = {"bids": [{"price": "0.3", "size": "100"}]}

        quality = MagicMock()
        quality.is_tradeable.side_effect = [
            (True, "ok"),
            (False, "too wide"),
        ]

        strategy = self._make_strategy(
            events=events,
            ws=ws,
            quality_scorer=quality,
        )

        signals = await strategy.evaluate()

        assert signals == []
        assert ws.subscribe.await_count >= 1
        quality.is_tradeable.assert_any_call("tok_a", volume_24h=10000.0)


# ============================================================
# 4. Strategy Performance Tracker Tests
# ============================================================


class TestStrategyTracker:
    """Test StrategyTracker auto-pause and auto-boost."""

    def _make_tracker(self, wins=0, losses=0):
        from risk.strategy_tracker import StrategyTracker
        from strategies.base import BaseStrategy

        db = MagicMock()

        class DummyStrategy(BaseStrategy):
            async def initialize(self): pass
            async def evaluate(self): return []
            async def shutdown(self): pass

        strat = DummyStrategy("test_strat")
        tracker = StrategyTracker(db, [strat])

        # Simulate rolling stats
        total = wins + losses
        db.get_strategy_rolling_stats.return_value = {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / max(total, 1),
            "total_pnl": (wins * 5.0) - (losses * 8.0),
            "avg_pnl": ((wins * 5.0) - (losses * 8.0)) / max(total, 1),
        }
        return tracker, strat, db

    def test_boost_high_win_rate(self):
        """Strategy with >65% win rate should get boosted multiplier."""
        tracker, strat, db = self._make_tracker(wins=15, losses=5)
        mult = tracker.get_size_multiplier("test_strat")
        assert mult == config.TRACKER_BOOST_FACTOR

    def test_normal_win_rate(self):
        """Strategy with 45-65% win rate should get 1.0 multiplier."""
        tracker, strat, db = self._make_tracker(wins=10, losses=10)
        mult = tracker.get_size_multiplier("test_strat")
        assert mult == 1.0

    def test_auto_pause_low_win_rate(self):
        """Strategy with <35% win rate should be auto-paused."""
        tracker, strat, db = self._make_tracker(wins=4, losses=16)
        # Record a losing trade to trigger evaluation
        tracker.record_trade("test_strat", -5.0)
        mult = tracker.get_size_multiplier("test_strat")
        assert mult == 0.0
        assert not strat.is_active

    def test_record_trade_calls_db(self):
        """record_trade should save to database."""
        tracker, strat, db = self._make_tracker(wins=10, losses=5)
        tracker.record_trade("test_strat", 5.0)
        db.record_strategy_trade.assert_called_once_with("test_strat", 5.0, 1)

    def test_record_loss_calls_db(self):
        """Negative P&L should be recorded as a loss."""
        tracker, strat, db = self._make_tracker(wins=10, losses=5)
        tracker.record_trade("test_strat", -3.0)
        db.record_strategy_trade.assert_called_once_with("test_strat", -3.0, 0)

    def test_get_all_stats(self):
        """get_all_stats should return stats for all strategies."""
        tracker, strat, db = self._make_tracker(wins=10, losses=5)
        all_stats = tracker.get_all_stats()
        assert "test_strat" in all_stats
        assert "win_rate" in all_stats["test_strat"]

    def test_reconcile_states_auto_resumes_expired_pause(self):
        """Paused strategies should resume on a reconciliation cycle."""
        tracker, strat, db = self._make_tracker(wins=10, losses=10)
        strat.pause()
        tracker._paused_until["test_strat"] = datetime.now(timezone.utc) - timedelta(
            minutes=1
        )

        tracker.reconcile_states()

        assert strat.is_active
        assert "test_strat" not in tracker._paused_until


# ============================================================
# 5. Database Strategy Performance Tests
# ============================================================


class TestDatabaseStrategyPerformance:
    """Test database methods for strategy performance tracking."""

    def _make_db(self):
        from storage.database import Database
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        return Database(db_path=Path(tmp.name)), tmp.name

    def test_record_and_retrieve(self):
        """Should record and retrieve strategy performance data."""
        db, db_path = self._make_db()
        try:
            db.record_strategy_trade("flash_crash", 5.0, 1)
            db.record_strategy_trade("flash_crash", -3.0, 0)
            db.record_strategy_trade("flash_crash", 2.0, 1)

            stats = db.get_strategy_rolling_stats("flash_crash", window=10)
            assert stats["total_trades"] == 3
            assert stats["wins"] == 2
            assert stats["losses"] == 1
            assert stats["win_rate"] == pytest.approx(2 / 3, abs=0.01)
        finally:
            os.unlink(db_path)

    def test_rolling_window_limits(self):
        """Stats should only consider the last N trades."""
        db, db_path = self._make_db()
        try:
            # Record 5 losses, then 5 wins
            for _ in range(5):
                db.record_strategy_trade("test", -1.0, 0)
            for _ in range(5):
                db.record_strategy_trade("test", 1.0, 1)

            # Window of 5 should only see the wins
            stats = db.get_strategy_rolling_stats("test", window=5)
            assert stats["wins"] == 5
            assert stats["win_rate"] == pytest.approx(1.0, abs=0.01)
        finally:
            os.unlink(db_path)

    def test_empty_strategy(self):
        """Should return defaults for a strategy with no trades."""
        db, db_path = self._make_db()
        try:
            stats = db.get_strategy_rolling_stats("nonexistent", window=20)
            assert stats["total_trades"] == 0
            assert stats["win_rate"] == pytest.approx(0.5, abs=0.01)
        finally:
            os.unlink(db_path)
