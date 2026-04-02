"""Tests for the Mispricing strategy."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock

from strategies.mispricing import MispricingStrategy, _normalise_tokens
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
    def test_normalise_tokens_parses_json_string_payload(self):
        tokens = _normalise_tokens('["tok_yes","tok_no"]')
        assert tokens == ["tok_yes", "tok_no"]

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
        assert signals[0].estimated_prob == pytest.approx(0.535)
        assert signals[0].take_profit is not None
        assert signals[0].stop_loss is not None

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

    @pytest.mark.asyncio
    async def test_waits_for_order_book_before_quality_gate(self, gamma, external):
        ws = MagicMock()
        ws.subscribe = AsyncMock()
        ws.get_order_book = MagicMock(return_value=None)
        quality = MagicMock()

        strategy = MispricingStrategy(
            gamma, external, ws=ws, quality_scorer=quality
        )
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
        }

        signals = await strategy.evaluate()

        assert signals == []
        ws.subscribe.assert_awaited_once_with("t_yes")
        quality.is_tradeable.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_quality_gate_once_order_book_exists(self, gamma, external):
        ws = MagicMock()
        ws.subscribe = AsyncMock()
        ws.get_order_book = MagicMock(side_effect=[None, {"bids": [], "asks": []}])
        ws.get_mid_price = MagicMock(return_value=0.44)
        quality = MagicMock()
        quality.is_tradeable.return_value = (
            True,
            MagicMock(total=0.8, is_tradeable=True),
        )

        strategy = MispricingStrategy(
            gamma, external, ws=ws, quality_scorer=quality
        )
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
        }

        first_signals = await strategy.evaluate()
        second_signals = await strategy.evaluate()

        assert first_signals == []
        assert len(second_signals) == 1
        ws.subscribe.assert_awaited_once_with("t_yes")
        quality.is_tradeable.assert_called_once_with("t_yes", volume_24h=10000.0)
        assert second_signals[0].price == pytest.approx(0.44)

    @pytest.mark.asyncio
    async def test_parses_string_clob_token_ids_before_subscribe(self, gamma, external):
        ws = MagicMock()
        ws.subscribe = AsyncMock()
        ws.get_order_book = MagicMock(return_value=None)
        quality = MagicMock()

        strategy = MispricingStrategy(
            gamma, external, ws=ws, quality_scorer=quality
        )
        gamma.get_markets_for_mispricing.return_value = [
            {
                "question": "Will event X happen?",
                "outcomePrices": "[0.40]",
                "volume24hr": 10000,
                "clobTokenIds": '["tok_yes","tok_no"]',
            }
        ]
        external.find_matching_prices.return_value = {
            "kalshi": 0.55,
            "metaculus": 0.52,
        }

        await strategy.evaluate()

        ws.subscribe.assert_awaited_once_with("tok_yes")

    @pytest.mark.asyncio
    async def test_skips_market_during_reentry_cooldown(self, gamma, external):
        ws = MagicMock()
        ws.subscribe = AsyncMock()
        ws.get_order_book = MagicMock(return_value={"bids": [], "asks": []})
        quality = MagicMock()
        quality.is_tradeable.return_value = (
            True,
            MagicMock(total=0.8, is_tradeable=True),
        )

        strategy = MispricingStrategy(
            gamma, external, ws=ws, quality_scorer=quality
        )
        gamma.get_markets_for_mispricing.return_value = [
            {
                "question": "Will event X happen?",
                "outcomePrices": "[0.40]",
                "volume24hr": 10000,
                "tokens": ["tok_yes", "tok_no"],
            }
        ]
        external.find_matching_prices.return_value = {
            "kalshi": 0.55,
            "metaculus": 0.52,
        }
        strategy.record_closed_trade("tok_yes", -5.0, "position-stop-20%")

        signals = await strategy.evaluate()

        assert signals == []

    def test_take_profit_does_not_trigger_reentry_cooldown(self, gamma, external):
        strategy = MispricingStrategy(gamma, external)

        strategy.record_closed_trade("tok_yes", 12.0, "take-profit")

        assert not strategy._in_reentry_cooldown("tok_yes")

    @pytest.mark.asyncio
    async def test_restore_runtime_state_subscribes_open_positions_and_backfills_exits(
        self, gamma, external
    ):
        ws = MagicMock()
        ws.subscribe = AsyncMock()
        strategy = MispricingStrategy(gamma, external, ws=ws)
        db = MagicMock()
        position = SimpleNamespace(
            id="pos-1",
            token_id="tok_yes",
            strategy="mispricing",
            entry_price=0.50,
            take_profit=None,
            stop_loss=None,
        )

        await strategy.restore_runtime_state([position], [], db=db)

        ws.subscribe.assert_awaited_once_with("tok_yes")
        assert position.take_profit is not None
        assert position.stop_loss is not None
        db.update_trade_risk_levels.assert_called_once_with(
            "pos-1",
            position.take_profit,
            position.stop_loss,
        )

    @pytest.mark.asyncio
    async def test_restore_runtime_state_recovers_recent_loss_cooldown(
        self, gamma, external
    ):
        strategy = MispricingStrategy(gamma, external)
        recent_trade = {
            "strategy": "mispricing",
            "token_id": "tok_yes",
            "pnl_usdc": -5.0,
            "closed_at": (
                datetime.now(timezone.utc) - timedelta(minutes=5)
            ).isoformat(),
        }

        await strategy.restore_runtime_state([], [recent_trade])

        assert strategy._in_reentry_cooldown("tok_yes")

    @pytest.mark.asyncio
    async def test_skips_signal_if_live_price_already_through_take_profit(
        self, gamma, external
    ):
        ws = MagicMock()
        ws.subscribe = AsyncMock()
        ws.get_order_book = MagicMock(return_value={"bids": [], "asks": []})
        ws.get_mid_price = MagicMock(return_value=0.9865)
        quality = MagicMock()
        quality.is_tradeable.return_value = (
            True,
            MagicMock(total=0.8, is_tradeable=True),
        )
        strategy = MispricingStrategy(
            gamma, external, ws=ws, quality_scorer=quality
        )
        gamma.get_markets_for_mispricing.return_value = [
            {
                "question": "Will event X happen?",
                "outcomePrices": "[0.50]",
                "volume24hr": 10000,
                "tokens": ["t_yes", "t_no"],
            }
        ]
        external.find_matching_prices.return_value = {"kalshi": 1.0}

        signals = await strategy.evaluate()

        assert signals == []
