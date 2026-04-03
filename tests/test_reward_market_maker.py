"""Tests for the reward-aware market maker roadmap (phases 1-3)."""

from __future__ import annotations

import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
from core.websocket_client import MarketWebSocket
from data.market_quality import MarketQualityScorer
from execution.quote_manager import InventoryLot
from storage.database import Database
from strategies.reward_market_maker import RewardMarketMakerStrategy


class _FakeGamma:
    def __init__(self, markets):
        self.markets = markets

    async def get_markets(self, active=True, closed=False, limit=100, offset=0):
        return self.markets[offset: offset + limit]


class _FakeBot:
    def __init__(self):
        self.orders = {}
        self.cancelled = []
        self.open_order_ids = set()
        self.order_details = {}
        self.scoring_flags = {}
        self.counter = 0

    async def place_order(self, token_id, price, size, side, order_type="GTC", expiration=0):
        self.counter += 1
        order_id = f"maker-{self.counter}"
        self.orders[order_id] = {
            "token_id": token_id,
            "price": price,
            "size": size,
            "side": side,
            "orderType": order_type,
        }
        self.open_order_ids.add(order_id)
        return {"id": order_id}

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        self.open_order_ids.discard(order_id)
        return True

    async def get_open_orders(self, market=""):
        return [{"id": order_id} for order_id in self.open_order_ids]

    async def get_order(self, order_id):
        return self.order_details.get(order_id)

    async def get_orders_scoring(self, order_ids):
        return {order_id: self.scoring_flags.get(order_id, True) for order_id in order_ids}


class _Risk:
    def __init__(self, status="RUNNING", capital=500.0):
        self.status = status
        self.total_capital = capital


class _OrderManager:
    def __init__(self, exposure: float = 0.0):
        self._exposure = exposure

    def get_total_exposure(self):
        return self._exposure


def _make_db():
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    return Database(db_path=Path(handle.name)), Path(handle.name)


def _reward_market(
    market_id: str,
    question: str,
    yes_token: str,
    no_token: str,
    *,
    reward_daily_rate: float,
    reward_amount: float = 20.0,
):
    return {
        "id": market_id,
        "question": question,
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "rewardsMinSize": 20,
        "rewardsMaxSpread": 0.03,
        "volume24hr": 45000,
        "liquidityClob": 50000,
        "orderPriceMinTickSize": 0.01,
        "endDate": "2026-04-20T12:00:00Z",
        "clobRewards": [
            {
                "rewardsDailyRate": reward_daily_rate,
                "rewardsAmount": reward_amount,
            }
        ],
        "tokens": [
            {"outcome": "YES", "token_id": yes_token},
            {"outcome": "NO", "token_id": no_token},
        ],
    }


def _seed_book(ws: MarketWebSocket, token_id: str, bid: float, ask: float):
    now = time.time()
    ws._order_books[token_id] = {
        "bids": [
            {"price": f"{bid:.3f}", "size": "180"},
            {"price": f"{bid - 0.01:.3f}", "size": "150"},
            {"price": f"{bid - 0.02:.3f}", "size": "140"},
        ],
        "asks": [
            {"price": f"{ask:.3f}", "size": "190"},
            {"price": f"{ask + 0.01:.3f}", "size": "150"},
            {"price": f"{ask + 0.02:.3f}", "size": "130"},
        ],
        "timestamp": now,
    }
    ws._price_history[token_id].extend(
        [(now - 50, bid), (now - 20, (bid + ask) / 2), (now - 5, ask)]
    )


@pytest.fixture
def maker_config(monkeypatch):
    monkeypatch.setattr(config, "MAKER_ENABLED", True)
    monkeypatch.setattr(config, "MAKER_ALLOW_LIVE", True)
    monkeypatch.setattr(config, "MAKER_CAPITAL_USDC", 150.0)
    monkeypatch.setattr(config, "MAKER_MAX_MARKETS", 2)
    monkeypatch.setattr(config, "MAKER_MAX_PER_MARKET_USDC", 60.0)
    monkeypatch.setattr(config, "MAKER_MAX_INVENTORY_USDC", 80.0)
    monkeypatch.setattr(config, "MAKER_MIN_QUOTE_SIZE_USDC", 10.0)
    monkeypatch.setattr(config, "MAKER_REQUOTE_BPS", 25.0)
    monkeypatch.setattr(config, "MAKER_MIN_REWARD_EFFICIENCY", 0.0001)
    monkeypatch.setattr(config, "MAKER_RELAX_EFFICIENCY_IN_DRY_RUN", True)
    monkeypatch.setattr(config, "MAKER_RELAXED_EFFICIENCY_FACTOR", 0.30)
    monkeypatch.setattr(config, "MAKER_BOOK_STALE_S", 20.0)
    monkeypatch.setattr(config, "MAKER_SCORING_REFRESH_S", 0)
    monkeypatch.setattr(config, "MAKER_MARKETS_CACHE_TTL_S", 0)
    monkeypatch.setattr(config, "MIN_MARKET_QUALITY_SCORE", 0.4)


def _build_strategy(markets, maker_config):
    db, path = _make_db()
    ws = MarketWebSocket(url="wss://test")
    ws._ws = MagicMock()
    ws._ws.send = AsyncMock()
    bot = _FakeBot()
    gamma = _FakeGamma(markets)
    risk = _Risk()
    order_manager = _OrderManager()
    strategy = RewardMarketMakerStrategy(
        gamma,
        ws,
        bot,
        db,
        risk,
        quality_scorer=MarketQualityScorer(ws),
        order_manager=order_manager,
    )
    return strategy, bot, ws, db, path, order_manager, risk


@pytest.mark.asyncio
async def test_phase1_places_complementary_reward_bids(maker_config):
    market = _reward_market("m1", "Will Team A win?", "yes-a", "no-a", reward_daily_rate=45)
    strategy, bot, ws, db, path, _, _ = _build_strategy([market], maker_config)
    _seed_book(ws, "yes-a", 0.44, 0.46)
    _seed_book(ws, "no-a", 0.54, 0.56)

    try:
        await strategy.initialize()
        await strategy.cycle()

        quotes = strategy.quote_manager.get_active_quotes()
        assert len(quotes) == 2
        assert {quote.outcome for quote in quotes} == {"YES", "NO"}
        assert all(quote.side == "BUY" for quote in quotes)
        for quote in quotes:
            mid = ws.get_mid_price(quote.token_id)
            assert mid is not None and quote.price < mid
            assert (mid - quote.price) <= market["rewardsMaxSpread"] + 1e-9

        status = strategy.status_payload()
        assert status["active_quotes"] == 2
        assert status["estimated_daily_rewards_usdc"] > 0
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_phase1_requotes_when_price_moves(maker_config):
    market = _reward_market("m1", "Will Team A win?", "yes-a", "no-a", reward_daily_rate=45)
    strategy, bot, ws, db, path, _, _ = _build_strategy([market], maker_config)
    _seed_book(ws, "yes-a", 0.44, 0.46)
    _seed_book(ws, "no-a", 0.54, 0.56)

    try:
        await strategy.initialize()
        await strategy.cycle()
        first_order_ids = {quote.order_id for quote in strategy.quote_manager.get_active_quotes()}

        _seed_book(ws, "yes-a", 0.39, 0.41)
        _seed_book(ws, "no-a", 0.59, 0.61)
        await strategy.cycle()

        second_order_ids = {quote.order_id for quote in strategy.quote_manager.get_active_quotes()}
        assert first_order_ids != second_order_ids
        assert bot.cancelled
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_phase2_reconcile_filled_quote_updates_inventory(maker_config, monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", False)
    market = _reward_market("m1", "Will Team A win?", "yes-a", "no-a", reward_daily_rate=45)
    strategy, bot, ws, db, path, _, _ = _build_strategy([market], maker_config)
    _seed_book(ws, "yes-a", 0.44, 0.46)
    _seed_book(ws, "no-a", 0.54, 0.56)

    try:
        await strategy.initialize()
        await strategy.cycle()
        yes_quote = next(
            quote for quote in strategy.quote_manager.get_active_quotes()
            if quote.outcome == "YES"
        )

        bot.open_order_ids.discard(yes_quote.order_id)
        bot.order_details[yes_quote.order_id] = {
            "status": "FILLED",
            "price": yes_quote.price,
            "filledSize": yes_quote.size_usdc,
        }
        await strategy.quote_manager.reconcile_live_orders()

        inventory = strategy.quote_manager.inventory["yes-a"]
        assert inventory.shares > 0
        assert db.get_recent_maker_fills(5)[0]["token_id"] == "yes-a"
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_phase2_builds_inventory_exit_quote(maker_config):
    market = _reward_market("m1", "Will Team A win?", "yes-a", "no-a", reward_daily_rate=45)
    strategy, bot, ws, db, path, _, _ = _build_strategy([market], maker_config)
    _seed_book(ws, "yes-a", 0.44, 0.46)
    _seed_book(ws, "no-a", 0.54, 0.56)

    try:
        await strategy.initialize()
        candidate = (await strategy.reward_source.get_reward_markets(limit=1))[0]
        selected = (await strategy._select_markets([candidate]))[0]
        strategy.quote_manager.inventory["yes-a"] = InventoryLot(
            token_id="yes-a",
            market_id="m1",
            market_question=market["question"],
            outcome="YES",
            shares=50,
            avg_entry_price=0.42,
        )

        quotes = strategy._build_market_quotes(selected, remaining_capital=60)
        assert any(quote.side == "SELL" and quote.order_kind == "inventory_exit" for quote in quotes)
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_phase2_stale_books_are_skipped(maker_config):
    market = _reward_market("m1", "Will Team A win?", "yes-a", "no-a", reward_daily_rate=45)
    strategy, bot, ws, db, path, _, _ = _build_strategy([market], maker_config)
    _seed_book(ws, "yes-a", 0.44, 0.46)
    _seed_book(ws, "no-a", 0.54, 0.56)
    ws._order_books["yes-a"]["timestamp"] = time.time() - 100
    ws._order_books["no-a"]["timestamp"] = time.time() - 100

    try:
        await strategy.initialize()
        await strategy.cycle()
        assert strategy.quote_manager.get_active_quotes() == []
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_phase3_ranking_prefers_higher_reward_market(maker_config):
    better = _reward_market("m1", "Will Team A win?", "yes-a", "no-a", reward_daily_rate=70)
    weaker = _reward_market("m2", "Will Team B win?", "yes-b", "no-b", reward_daily_rate=15)
    strategy, bot, ws, db, path, _, _ = _build_strategy([weaker, better], maker_config)
    _seed_book(ws, "yes-a", 0.44, 0.46)
    _seed_book(ws, "no-a", 0.54, 0.56)
    _seed_book(ws, "yes-b", 0.44, 0.46)
    _seed_book(ws, "no-b", 0.54, 0.56)

    try:
        await strategy.initialize()
        candidates = await strategy.reward_source.get_reward_markets(limit=5)
        selected = await strategy._select_markets(candidates)
        assert selected[0].candidate.market_id == "m1"
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_phase3_scoring_and_status_analytics(maker_config):
    market = _reward_market("m1", "Will Team A win?", "yes-a", "no-a", reward_daily_rate=45)
    strategy, bot, ws, db, path, _, _ = _build_strategy([market], maker_config)
    _seed_book(ws, "yes-a", 0.44, 0.46)
    _seed_book(ws, "no-a", 0.54, 0.56)

    try:
        await strategy.initialize()
        await strategy.cycle()
        for quote in strategy.quote_manager.get_active_quotes():
            bot.scoring_flags[quote.order_id] = quote.outcome == "YES"
        await strategy._refresh_scoring_if_due()
        strategy._persist_reward_snapshots()

        snapshots = db.get_recent_maker_reward_snapshots(10)
        assert snapshots
        status = strategy.status_payload()
        assert "avg_estimated_score" in status
        assert status["active_quotes"] == 2
        assert status["estimated_daily_rewards_usdc"] == pytest.approx(45.0)
        assert status["estimated_daily_reward_yield_pct"] > 0
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_maker_uses_asymmetric_budget_when_market_is_affordable(maker_config):
    market = _reward_market("m1", "Will Team A win?", "yes-a", "no-a", reward_daily_rate=45)
    market["rewardsMinSize"] = 50
    strategy, bot, ws, db, path, _, _ = _build_strategy([market], maker_config)
    _seed_book(ws, "yes-a", 0.59, 0.61)
    _seed_book(ws, "no-a", 0.39, 0.41)

    try:
        await strategy.initialize()
        await strategy.cycle()

        quotes = strategy.quote_manager.get_active_quotes()
        assert len(quotes) == 2
        by_outcome = {quote.outcome: quote for quote in quotes}
        assert by_outcome["YES"].size_usdc > by_outcome["NO"].size_usdc
        assert sum(quote.size_usdc for quote in quotes) <= config.MAKER_MAX_PER_MARKET_USDC
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_maker_status_reports_budget_diagnostics(maker_config, monkeypatch):
    monkeypatch.setattr(config, "MAKER_MAX_PER_MARKET_USDC", 40.0)
    market = _reward_market("m1", "Will Team A win?", "yes-a", "no-a", reward_daily_rate=45)
    market["rewardsMinSize"] = 80
    strategy, bot, ws, db, path, _, _ = _build_strategy([market], maker_config)
    _seed_book(ws, "yes-a", 0.59, 0.61)
    _seed_book(ws, "no-a", 0.39, 0.41)

    try:
        await strategy.initialize()
        await strategy.cycle()

        status = strategy.status_payload()
        assert status["active_quotes"] == 0
        assert status["diagnostics"]["candidate_skips"]["budget-infeasible"] >= 1
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_maker_skips_markets_without_measurable_daily_reward(
    maker_config,
    monkeypatch,
):
    monkeypatch.setattr(config, "MAKER_REQUIRE_MEASURABLE_DAILY_REWARD", True)
    market = _reward_market("m1", "Will Team A win?", "yes-a", "no-a", reward_daily_rate=0)
    market["clobRewards"][0]["rewardsAmount"] = 50
    strategy, bot, ws, db, path, _, _ = _build_strategy([market], maker_config)
    _seed_book(ws, "yes-a", 0.44, 0.46)
    _seed_book(ws, "no-a", 0.54, 0.56)

    try:
        await strategy.initialize()
        await strategy.cycle()

        status = strategy.status_payload()
        assert status["selected_markets"] == []
        assert status["active_quotes"] == 0
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_maker_status_preserves_small_reward_precision(
    maker_config,
    monkeypatch,
):
    monkeypatch.setattr(config, "MAKER_REQUIRE_MEASURABLE_DAILY_REWARD", True)
    market = _reward_market("m1", "Will Team A win?", "yes-a", "no-a", reward_daily_rate=0.0042)
    strategy, bot, ws, db, path, _, _ = _build_strategy([market], maker_config)
    _seed_book(ws, "yes-a", 0.44, 0.46)
    _seed_book(ws, "no-a", 0.54, 0.56)

    try:
        await strategy.initialize()
        await strategy.cycle()

        status = strategy.status_payload()
        assert status["selected_markets"][0]["has_measurable_daily_reward"] is True
        assert status["selected_markets"][0]["reward_daily_rate"] == pytest.approx(0.0042)
        assert status["selected_markets"][0]["reward_efficiency_daily_pct"] > 0
        assert status["estimated_daily_rewards_usdc"] == pytest.approx(0.0042)
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_maker_skips_markets_with_poor_reward_efficiency(
    maker_config,
    monkeypatch,
):
    monkeypatch.setattr(config, "MAKER_RELAX_EFFICIENCY_IN_DRY_RUN", False)
    monkeypatch.setattr(config, "MAKER_MIN_REWARD_EFFICIENCY", 0.0001)
    market = _reward_market(
        "m1",
        "Will Team A win?",
        "yes-a",
        "no-a",
        reward_daily_rate=0.001,
    )
    strategy, bot, ws, db, path, _, _ = _build_strategy([market], maker_config)
    _seed_book(ws, "yes-a", 0.44, 0.46)
    _seed_book(ws, "no-a", 0.54, 0.56)

    try:
        await strategy.initialize()
        await strategy.cycle()

        status = strategy.status_payload()
        assert status["selected_markets"] == []
        assert status["active_quotes"] == 0
        assert status["diagnostics"]["candidate_skips"]["reward-efficiency-too-low"] >= 1
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_maker_relaxes_efficiency_threshold_in_dry_run(maker_config):
    market = _reward_market(
        "m1",
        "Will Team A win?",
        "yes-a",
        "no-a",
        reward_daily_rate=0.001,
    )
    strategy, bot, ws, db, path, _, _ = _build_strategy([market], maker_config)
    _seed_book(ws, "yes-a", 0.44, 0.46)
    _seed_book(ws, "no-a", 0.54, 0.56)

    try:
        await strategy.initialize()
        await strategy.cycle()

        status = strategy.status_payload()
        assert status["selected_markets"]
        assert status["diagnostics"]["selection_mode"] == "relaxed-efficiency"
        assert status["diagnostics"]["efficiency_threshold_used"] == pytest.approx(
            0.00003
        )
        assert status["selected_markets"][0]["efficiency_relaxed"] is True
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_dry_run_maker_simulates_resting_fill(maker_config, monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", True)
    monkeypatch.setattr(config, "DRY_RUN_MAKER_MIN_FILL_AGE_S", 0.0)
    monkeypatch.setattr(config, "DRY_RUN_MAKER_FORCE_FILL_AGE_S", 0.0)
    monkeypatch.setattr(config, "DRY_RUN_MAKER_MIN_COMPETITIVENESS", 0.0)
    market = _reward_market("m1", "Will Team A win?", "yes-a", "no-a", reward_daily_rate=45)
    strategy, bot, ws, db, path, _, _ = _build_strategy([market], maker_config)
    _seed_book(ws, "yes-a", 0.44, 0.46)
    _seed_book(ws, "no-a", 0.54, 0.56)

    try:
        await strategy.initialize()
        await strategy.cycle()
        for quote in strategy.quote_manager.get_active_quotes():
            quote.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)

        await strategy.quote_manager.reconcile_live_orders()

        fills = db.get_recent_maker_fills(10)
        assert fills
        assert strategy.quote_manager.get_inventory()
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_maker_status_reports_accrued_rewards(maker_config):
    market = _reward_market("m1", "Will Team A win?", "yes-a", "no-a", reward_daily_rate=45)
    strategy, bot, ws, db, path, _, _ = _build_strategy([market], maker_config)
    _seed_book(ws, "yes-a", 0.44, 0.46)
    _seed_book(ws, "no-a", 0.54, 0.56)

    try:
        await strategy.initialize()
        await strategy.cycle()
        strategy._last_reward_accrual_at = datetime.now(timezone.utc) - timedelta(hours=1)
        strategy._accrue_estimated_rewards()

        status = strategy.status_payload()
        assert status["estimated_rewards_accrued_usdc"] > 0
        assert status["estimated_total_pnl_usdc"] >= status["estimated_rewards_accrued_usdc"]
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_maker_does_not_invent_fill_when_order_state_unknown(maker_config, monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", False)
    market = _reward_market("m1", "Will Team A win?", "yes-a", "no-a", reward_daily_rate=45)
    strategy, bot, ws, db, path, _, _ = _build_strategy([market], maker_config)
    _seed_book(ws, "yes-a", 0.44, 0.46)
    _seed_book(ws, "no-a", 0.54, 0.56)

    try:
        await strategy.initialize()
        await strategy.cycle()
        quote = strategy.quote_manager.get_active_quotes()[0]
        bot.open_order_ids.discard(quote.order_id)
        bot.order_details[quote.order_id] = None

        await strategy.quote_manager.reconcile_live_orders()

        assert quote.quote_key in strategy.quote_manager.active_quotes
        assert strategy.quote_manager.active_quotes[quote.quote_key].reconcile_pending is True
        assert db.get_recent_maker_fills(5) == []
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_maker_respects_global_exposure_budget(maker_config):
    market = _reward_market("m1", "Will Team A win?", "yes-a", "no-a", reward_daily_rate=45)
    strategy, bot, ws, db, path, order_manager, risk = _build_strategy([market], maker_config)
    _seed_book(ws, "yes-a", 0.44, 0.46)
    _seed_book(ws, "no-a", 0.54, 0.56)
    order_manager._exposure = 240.0
    risk.total_capital = 500.0

    try:
        await strategy.initialize()
        await strategy.cycle()
        quotes = strategy.quote_manager.get_active_quotes()
        assert quotes
        assert sum(q.size_usdc for q in quotes if q.side == "BUY") <= 60.0
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_maker_risk_stop_cancels_reward_quotes_but_keeps_inventory_exit(maker_config):
    market = _reward_market("m1", "Will Team A win?", "yes-a", "no-a", reward_daily_rate=45)
    strategy, bot, ws, db, path, _, risk = _build_strategy([market], maker_config)
    _seed_book(ws, "yes-a", 0.44, 0.46)
    _seed_book(ws, "no-a", 0.54, 0.56)

    try:
        await strategy.initialize()
        await strategy.cycle()
        strategy.quote_manager.inventory["yes-a"] = InventoryLot(
            token_id="yes-a",
            market_id="m1",
            market_question=market["question"],
            outcome="YES",
            shares=50,
            avg_entry_price=0.42,
        )
        risk.status = "STOPPED"

        await strategy.cycle()

        active = strategy.quote_manager.get_active_quotes()
        assert active
        assert all(quote.order_kind == "inventory_exit" for quote in active)
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_maker_stops_new_reward_bids_when_inventory_uses_budget(maker_config):
    market = _reward_market("m1", "Will Team A win?", "yes-a", "no-a", reward_daily_rate=45)
    strategy, bot, ws, db, path, _, _ = _build_strategy([market], maker_config)
    _seed_book(ws, "yes-a", 0.49, 0.51)
    _seed_book(ws, "no-a", 0.49, 0.51)

    try:
        await strategy.initialize()
        strategy.quote_manager.inventory["yes-a"] = InventoryLot(
            token_id="yes-a",
            market_id="m1",
            market_question=market["question"],
            outcome="YES",
            shares=300,
            avg_entry_price=0.50,
        )

        await strategy.cycle()

        active = strategy.quote_manager.get_active_quotes()
        assert active
        assert all(quote.order_kind == "inventory_exit" for quote in active)
        status = strategy.status_payload()
        budget = status["diagnostics"]["budget"]
        assert budget["maker_budget_remaining_usdc"] == 0.0
        assert status["committed_capital_usdc"] == 0.0
        assert status["pending_buy_capital_usdc"] == 0.0
        assert status["inventory_exit_notional_usdc"] > 0
        assert status["active_reward_bid_quotes"] == 0
        assert status["active_inventory_exit_quotes"] == len(active)
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_maker_builds_exit_for_inventory_from_unselected_market(maker_config, monkeypatch):
    monkeypatch.setattr(config, "MAKER_MAX_MARKETS", 1)
    strong = _reward_market("m1", "Will Team A win?", "yes-a", "no-a", reward_daily_rate=70)
    weak = _reward_market("m2", "Will Team B win?", "yes-b", "no-b", reward_daily_rate=30)
    strategy, bot, ws, db, path, _, _ = _build_strategy([strong, weak], maker_config)
    _seed_book(ws, "yes-a", 0.44, 0.46)
    _seed_book(ws, "no-a", 0.54, 0.56)
    _seed_book(ws, "yes-b", 0.44, 0.46)
    _seed_book(ws, "no-b", 0.54, 0.56)

    try:
        await strategy.initialize()
        strategy.quote_manager.inventory["yes-b"] = InventoryLot(
            token_id="yes-b",
            market_id="m2",
            market_question=weak["question"],
            outcome="YES",
            shares=40,
            avg_entry_price=0.42,
        )

        await strategy.cycle()

        active = strategy.quote_manager.get_active_quotes()
        assert any(
            quote.order_kind == "inventory_exit" and quote.token_id == "yes-b"
            for quote in active
        )
        assert any(
            quote.order_kind == "reward_bid" and quote.market_id == "m1"
            for quote in active
        )
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_inventory_exit_gets_more_aggressive_when_inventory_is_old_and_saturated(
    maker_config,
):
    market = _reward_market("m1", "Will Team A win?", "yes-a", "no-a", reward_daily_rate=45)
    strategy, bot, ws, db, path, _, _ = _build_strategy([market], maker_config)
    _seed_book(ws, "yes-a", 0.44, 0.46)
    _seed_book(ws, "no-a", 0.54, 0.56)

    try:
        await strategy.initialize()

        fresh = InventoryLot(
            token_id="yes-a",
            market_id="m1",
            market_question=market["question"],
            outcome="YES",
            shares=200,
            avg_entry_price=0.42,
            updated_at=datetime.now(timezone.utc),
        )
        stale = InventoryLot(
            token_id="yes-a",
            market_id="m1",
            market_question=market["question"],
            outcome="YES",
            shares=200,
            avg_entry_price=0.42,
            updated_at=datetime.now(timezone.utc) - timedelta(hours=4),
        )
        strategy.quote_manager.inventory["other"] = InventoryLot(
            token_id="no-a",
            market_id="m1",
            market_question=market["question"],
            outcome="NO",
            shares=400,
            avg_entry_price=0.54,
        )

        fresh_quote = strategy._build_inventory_exit_quote(fresh, tick=0.01)
        stale_quote = strategy._build_inventory_exit_quote(stale, tick=0.01)

        assert fresh_quote is not None
        assert stale_quote is not None
        assert stale_quote.price <= fresh_quote.price
    finally:
        path.unlink(missing_ok=True)
