"""Tests for persistent Claude daily call limiting in the AI strategy."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from storage.database import Database
from strategies.ai_agent import AIAgentStrategy


@pytest.mark.asyncio
async def test_ai_agent_persists_daily_call_budget_across_restarts(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = Database(db_path=Path(tmp.name))

    try:
        gamma = MagicMock()
        gamma.get_markets_for_mispricing = AsyncMock(return_value=[
            {
                "question": "Will X happen?",
                "outcomePrices": "0.50",
                "volume24hr": 10000,
                "endDate": "2026-12-31",
                "tokens": [
                    {"token_id": "tok_yes", "outcome": "YES"},
                    {"token_id": "tok_no", "outcome": "NO"},
                ],
            }
        ])

        external = MagicMock()
        external.find_matching_prices = AsyncMock(return_value={"kalshi": 0.56})
        external.get_recent_news = AsyncMock(return_value=["Headline"])

        analyst = MagicMock()
        analyst.analyse_market = AsyncMock(return_value={
            "probability": 0.65,
            "confidence": 0.75,
            "reasoning": "Edge confirmed",
        })

        monkeypatch.setattr("config.MAX_AI_CALLS_PER_DAY", 1)

        strategy = AIAgentStrategy(gamma, external, analyst, db=db)
        await strategy.initialize()
        signals = await strategy.evaluate()

        assert len(signals) == 1
        assert analyst.analyse_market.await_count == 1

        restarted = AIAgentStrategy(gamma, external, analyst, db=db)
        await restarted.initialize()
        second_signals = await restarted.evaluate()

        assert second_signals == []
        assert analyst.analyse_market.await_count == 1
    finally:
        Path(tmp.name).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_ai_agent_disables_itself_without_api_key():
    gamma = MagicMock()
    external = MagicMock()
    analyst = MagicMock()
    analyst.api_key = ""

    strategy = AIAgentStrategy(gamma, external, analyst, db=None)
    await strategy.initialize()

    assert not strategy.is_active
    signals = await strategy.evaluate()
    assert signals == []


@pytest.mark.asyncio
async def test_ai_agent_signal_uses_claude_probability_for_estimated_prob():
    gamma = MagicMock()
    gamma.get_markets_for_mispricing = AsyncMock(return_value=[
        {
            "question": "Will X happen?",
            "outcomePrices": "0.50",
            "volume24hr": 10000,
            "endDate": "2026-12-31",
            "tokens": [
                {"token_id": "tok_yes", "outcome": "YES"},
                {"token_id": "tok_no", "outcome": "NO"},
            ],
        }
    ])
    external = MagicMock()
    external.find_matching_prices = AsyncMock(return_value={"kalshi": 0.56})
    external.get_recent_news = AsyncMock(return_value=["Headline"])
    analyst = MagicMock()
    analyst.api_key = "configured"
    analyst.analyse_market = AsyncMock(return_value={
        "probability": 0.68,
        "confidence": 0.75,
        "reasoning": "Edge confirmed",
    })

    strategy = AIAgentStrategy(gamma, external, analyst, db=None)
    await strategy.initialize()
    signals = await strategy.evaluate()

    assert len(signals) == 1
    assert signals[0].estimated_prob == pytest.approx(0.68)
    assert signals[0].confidence == pytest.approx(0.75)
