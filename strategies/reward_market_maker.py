"""Reward-aware micro market maker for Polymarket liquidity rewards."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from math import log1p

from loguru import logger

import config
from core.bot import TradingBot
from core.gamma_client import GammaClient
from core.websocket_client import MarketWebSocket
from data.market_quality import MarketQualityScorer
from data.reward_markets import RewardMarketCandidate, RewardMarketSource
from execution.quote_manager import DesiredQuote, QuoteManager
from risk.risk_manager import RiskManager
from strategies.base import BaseStrategy


def _round_down(value: float, tick: float) -> float:
    tick = max(tick, 0.001)
    return max(round((value // tick) * tick, 6), tick)


def _round_up(value: float, tick: float) -> float:
    tick = max(tick, 0.001)
    scaled = int(value / tick)
    if scaled * tick < value:
        scaled += 1
    return min(round(scaled * tick, 6), 0.99)


@dataclass
class SelectedRewardMarket:
    candidate: RewardMarketCandidate
    yes_mid: float
    no_mid: float
    quality_score: float
    ranking_score: float
    yes_quote_price: float
    no_quote_price: float
    yes_min_quote_usdc: float
    no_min_quote_usdc: float
    min_total_quote_usdc: float
    reward_efficiency: float
    estimated_daily_reward_usdc: float
    efficiency_relaxed: bool


@dataclass
class RewardBidPlan:
    token_id: str
    outcome: str
    target_price: float
    min_quote_usdc: float
    reward_daily_rate: float
    score_estimate: float


class RewardMarketMakerStrategy(BaseStrategy):
    """Passive two-sided quoting using YES/NO complementary bids and inventory exits."""

    def __init__(
        self,
        gamma: GammaClient,
        ws: MarketWebSocket,
        bot: TradingBot,
        db,
        risk_manager: RiskManager,
        quality_scorer: MarketQualityScorer | None = None,
        order_manager=None,
    ):
        super().__init__("reward_market_maker")
        self.gamma = gamma
        self.ws = ws
        self.bot = bot
        self.db = db
        self.risk_manager = risk_manager
        self.quality = quality_scorer
        self.order_manager = order_manager
        self.reward_source = RewardMarketSource(gamma)
        self.quote_manager = QuoteManager(bot, db, ws=ws)
        self._running = False
        self._selected_markets: list[SelectedRewardMarket] = []
        self._subscribed_tokens: set[str] = set()
        self._last_scoring_refresh = 0.0
        self._estimated_rewards_accrued_usdc = 0.0
        self._last_reward_accrual_at: datetime | None = None
        self._last_cycle_diagnostics: dict[str, dict[str, int] | str] = {
            "candidate_skips": {},
            "quote_skips": {},
        }

    async def initialize(self):
        if not config.MAKER_ENABLED:
            self.disable()
            logger.info("RewardMarketMaker: Disabled via MAKER_ENABLED=false")
            return
        if not config.DRY_RUN and not config.MAKER_ALLOW_LIVE:
            self.disable()
            logger.warning(
                "RewardMarketMaker: Disabled in LIVE until MAKER_ALLOW_LIVE=true"
            )
            return

        self.quote_manager.restore_state()
        self._restore_reward_estimate_state()
        await self._restore_subscriptions_from_state()
        self._running = True
        logger.info(
            f"RewardMarketMaker: Initialized "
            f"(capital=${config.MAKER_CAPITAL_USDC:.2f}, max_markets={config.MAKER_MAX_MARKETS})"
        )

    async def evaluate(self):
        return []

    async def run(self):
        consecutive_errors = 0
        while self._running:
            try:
                if self.is_active:
                    await self.cycle()
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                backoff = min(config.MAKER_LOOP_INTERVAL_S * (2 ** consecutive_errors), 300)
                logger.error(
                    f"RewardMarketMaker loop error (#{consecutive_errors}): {e} "
                    f"— backing off {backoff:.0f}s"
                )
                await asyncio.sleep(backoff)
                continue
            await asyncio.sleep(config.MAKER_LOOP_INTERVAL_S)

    async def cycle(self):
        self._last_cycle_diagnostics = {
            "candidate_skips": {},
            "quote_skips": {},
            "last_cycle_at": datetime.now(timezone.utc).isoformat(),
        }

        if self.risk_manager.status != "RUNNING":
            await self.quote_manager.cancel_matching_quotes(
                lambda quote: quote.order_kind != "inventory_exit",
                reason=f"risk-{self.risk_manager.status.lower()}",
            )
            inventory_quotes = self._build_inventory_only_quotes()
            if inventory_quotes:
                await self.quote_manager.sync_quotes(inventory_quotes)
            self._selected_markets = []
            return

        await self.quote_manager.reconcile_live_orders()

        candidates = await self.reward_source.get_reward_markets(
            limit=max(config.MAKER_MAX_MARKETS * 4, 8)
        )
        selected = await self._select_markets(candidates)
        self._selected_markets = selected

        desired_quotes: list[DesiredQuote] = []
        max_total_exposure = self.risk_manager.total_capital * config.MAX_TOTAL_EXPOSURE_PCT
        directional_exposure = (
            self.order_manager.get_total_exposure()
            if self.order_manager is not None
            else 0.0
        )
        inventory_notional = self.quote_manager.inventory_notional(
            lambda token_id: self.ws.get_mid_price(token_id) or 0.0
        )
        maker_budget_remaining = max(
            0.0,
            config.MAKER_CAPITAL_USDC - inventory_notional,
        )
        global_remaining = max(
            0.0,
            max_total_exposure - directional_exposure - inventory_notional,
        )
        self._last_cycle_diagnostics["budget"] = {
            "maker_capital_usdc": round(config.MAKER_CAPITAL_USDC, 2),
            "inventory_notional_usdc": round(inventory_notional, 2),
            "maker_budget_remaining_usdc": round(maker_budget_remaining, 2),
            "global_remaining_usdc": round(global_remaining, 2),
        }
        remaining_capital = max(
            0.0,
            min(maker_budget_remaining, global_remaining),
        )
        covered_inventory_tokens: set[str] = set()

        for market in selected:
            market_quotes = self._build_market_quotes(market, remaining_capital)
            if not market_quotes:
                continue
            remaining_capital -= sum(q.size_usdc for q in market_quotes if q.side == "BUY")
            covered_inventory_tokens.update(
                q.token_id for q in market_quotes if q.order_kind == "inventory_exit"
            )
            desired_quotes.extend(market_quotes)

        desired_quotes.extend(
            self._build_inventory_only_quotes(
                exclude_token_ids=covered_inventory_tokens,
            )
        )

        if selected and not desired_quotes:
            logger.info(
                "RewardMarketMaker: selected markets but placed 0 quotes "
                f"(quote_skips={self._last_cycle_diagnostics.get('quote_skips', {})})"
            )

        await self.quote_manager.sync_quotes(desired_quotes)
        await self._refresh_scoring_if_due()
        self._accrue_estimated_rewards()
        self._persist_reward_snapshots()

    async def _select_markets(
        self,
        candidates: list[RewardMarketCandidate],
    ) -> list[SelectedRewardMarket]:
        strict_threshold = max(config.MAKER_MIN_REWARD_EFFICIENCY, 0.0)
        self._last_cycle_diagnostics["selection_mode"] = "strict"
        self._last_cycle_diagnostics["efficiency_threshold_used"] = strict_threshold

        selected = await self._select_markets_with_threshold(
            candidates,
            efficiency_threshold=strict_threshold,
            strict_threshold=strict_threshold,
        )
        if selected:
            return selected

        relaxed_allowed = (
            config.DRY_RUN
            and config.MAKER_RELAX_EFFICIENCY_IN_DRY_RUN
            and strict_threshold > 0
        )
        if not relaxed_allowed:
            return []

        relaxed_threshold = strict_threshold * max(
            config.MAKER_RELAXED_EFFICIENCY_FACTOR,
            0.0,
        )
        if relaxed_threshold >= strict_threshold:
            return []

        # In DRY_RUN we can be more permissive so we still observe maker behaviour
        # without weakening the stricter LIVE threshold.
        self._last_cycle_diagnostics["candidate_skips"] = {}
        self._last_cycle_diagnostics["quote_skips"] = {}
        selected = await self._select_markets_with_threshold(
            candidates,
            efficiency_threshold=relaxed_threshold,
            strict_threshold=strict_threshold,
        )
        if selected:
            self._last_cycle_diagnostics["selection_mode"] = "relaxed-efficiency"
            self._last_cycle_diagnostics["efficiency_threshold_used"] = relaxed_threshold
            self._last_cycle_diagnostics["strict_efficiency_threshold"] = strict_threshold
            self._last_cycle_diagnostics["relaxed_efficiency_factor"] = (
                config.MAKER_RELAXED_EFFICIENCY_FACTOR
            )
        return selected

    async def _select_markets_with_threshold(
        self,
        candidates: list[RewardMarketCandidate],
        *,
        efficiency_threshold: float,
        strict_threshold: float,
    ) -> list[SelectedRewardMarket]:
        selected: list[SelectedRewardMarket] = []

        for candidate in candidates:
            if len(selected) >= config.MAKER_MAX_MARKETS:
                break

            if not await self._ensure_books(candidate):
                self._record_diag("candidate_skips", "order-book-not-ready")
                continue
            if self._market_books_stale(candidate):
                self._record_diag("candidate_skips", "book-stale")
                continue

            yes_mid = self.ws.get_mid_price(candidate.yes_token_id)
            no_mid = self.ws.get_mid_price(candidate.no_token_id)
            if yes_mid is None or no_mid is None:
                self._record_diag("candidate_skips", "mid-missing")
                continue

            if yes_mid <= 0.02 or no_mid <= 0.02 or yes_mid >= 0.98 or no_mid >= 0.98:
                self._record_diag("candidate_skips", "mid-out-of-range")
                continue

            if self._combined_spread(candidate) > min(
                config.MAKER_MARKET_MAX_SPREAD_CENTS / 100.0,
                candidate.rewards_max_spread * 1.5,
            ):
                self._record_diag("candidate_skips", "spread-too-wide")
                continue

            quality_score = await self._market_quality_score(candidate)
            if quality_score <= 0:
                self._record_diag("candidate_skips", "quality-gate")
                continue

            yes_plan, yes_reason = self._prepare_reward_bid_plan(
                candidate=candidate,
                token_id=candidate.yes_token_id,
                outcome="YES",
                market_mid=yes_mid,
            )
            if not yes_plan:
                self._record_diag("quote_skips", f"yes:{yes_reason or 'not-quoteable'}")
                continue

            no_plan, no_reason = self._prepare_reward_bid_plan(
                candidate=candidate,
                token_id=candidate.no_token_id,
                outcome="NO",
                market_mid=no_mid,
            )
            if not no_plan:
                self._record_diag("quote_skips", f"no:{no_reason or 'not-quoteable'}")
                continue

            min_total_quote_usdc = yes_plan.min_quote_usdc + no_plan.min_quote_usdc
            if min_total_quote_usdc > config.MAKER_MAX_PER_MARKET_USDC:
                self._record_diag("candidate_skips", "budget-infeasible")
                continue

            reward_efficiency = candidate.reward_daily_rate / max(
                min_total_quote_usdc,
                1e-9,
            )
            if reward_efficiency < efficiency_threshold:
                self._record_diag("candidate_skips", "reward-efficiency-too-low")
                continue

            ranking_score = self._ranking_score(
                candidate,
                yes_mid,
                no_mid,
                quality_score,
                reward_efficiency,
            )
            selected.append(
                SelectedRewardMarket(
                    candidate=candidate,
                    yes_mid=yes_mid,
                    no_mid=no_mid,
                    quality_score=quality_score,
                    ranking_score=ranking_score,
                    yes_quote_price=yes_plan.target_price,
                    no_quote_price=no_plan.target_price,
                    yes_min_quote_usdc=yes_plan.min_quote_usdc,
                    no_min_quote_usdc=no_plan.min_quote_usdc,
                    min_total_quote_usdc=min_total_quote_usdc,
                    reward_efficiency=reward_efficiency,
                    estimated_daily_reward_usdc=candidate.reward_daily_rate,
                    efficiency_relaxed=reward_efficiency < strict_threshold,
                )
            )

        selected.sort(key=lambda market: market.ranking_score, reverse=True)
        return selected[: config.MAKER_MAX_MARKETS]

    async def _restore_subscriptions_from_state(self):
        token_ids = {
            quote.token_id for quote in self.quote_manager.get_active_quotes()
        }
        token_ids.update(
            lot.token_id for lot in self.quote_manager.get_inventory()
        )
        for token_id in token_ids:
            if token_id not in self._subscribed_tokens:
                await self.ws.subscribe(token_id)
                self._subscribed_tokens.add(token_id)

    def _restore_reward_estimate_state(self):
        try:
            accrued = float(self.db.get_state("maker_estimated_rewards_accrued_usdc") or 0)
        except (TypeError, ValueError):
            accrued = 0.0
        self._estimated_rewards_accrued_usdc = max(accrued, 0.0)

        raw_ts = self.db.get_state("maker_estimated_rewards_last_accrual_at")
        if isinstance(raw_ts, str) and raw_ts:
            try:
                self._last_reward_accrual_at = datetime.fromisoformat(
                    raw_ts.replace("Z", "+00:00")
                )
            except ValueError:
                self._last_reward_accrual_at = None
        else:
            self._last_reward_accrual_at = None

    def _accrue_estimated_rewards(self):
        now = datetime.now(timezone.utc)
        if self._last_reward_accrual_at is None:
            self._last_reward_accrual_at = now
            self.db.save_state(
                "maker_estimated_rewards_last_accrual_at",
                now.isoformat(),
            )
            return

        elapsed_s = max((now - self._last_reward_accrual_at).total_seconds(), 0.0)
        self._last_reward_accrual_at = now
        self.db.save_state(
            "maker_estimated_rewards_last_accrual_at",
            now.isoformat(),
        )
        if elapsed_s <= 0:
            return

        reward_rates_by_market: dict[str, float] = {}
        for quote in self.quote_manager.get_active_quotes():
            if not quote.scoring or quote.reward_daily_rate <= 0:
                continue
            reward_rates_by_market[quote.market_id] = max(
                reward_rates_by_market.get(quote.market_id, 0.0),
                quote.reward_daily_rate,
            )
        if not reward_rates_by_market:
            return

        accrued = sum(reward_rates_by_market.values()) * (elapsed_s / 86400.0)
        self._estimated_rewards_accrued_usdc += accrued
        self.db.save_state(
            "maker_estimated_rewards_accrued_usdc",
            f"{self._estimated_rewards_accrued_usdc:.10f}",
        )

    async def _ensure_books(self, candidate: RewardMarketCandidate) -> bool:
        for token_id in (candidate.yes_token_id, candidate.no_token_id):
            if token_id not in self._subscribed_tokens:
                await self.ws.subscribe(token_id)
                self._subscribed_tokens.add(token_id)
        return (
            self.ws.get_order_book(candidate.yes_token_id) is not None
            and self.ws.get_order_book(candidate.no_token_id) is not None
        )

    def _market_books_stale(self, candidate: RewardMarketCandidate) -> bool:
        yes_age = self.ws.get_book_age_seconds(candidate.yes_token_id)
        no_age = self.ws.get_book_age_seconds(candidate.no_token_id)
        if yes_age is None or no_age is None:
            return True
        return max(yes_age, no_age) > config.MAKER_BOOK_STALE_S

    async def _market_quality_score(self, candidate: RewardMarketCandidate) -> float:
        if not self.quality:
            return 1.0

        yes_ok, yes_report = self.quality.is_tradeable(
            candidate.yes_token_id,
            volume_24h=candidate.volume_24h,
        )
        no_ok, no_report = self.quality.is_tradeable(
            candidate.no_token_id,
            volume_24h=candidate.volume_24h,
        )
        if not (yes_ok and no_ok):
            logger.debug(
                f"RewardMarketMaker: Skipping '{candidate.question[:40]}...' - "
                f"quality yes={yes_report.total:.2f} no={no_report.total:.2f}"
            )
            return 0.0
        return (yes_report.total + no_report.total) / 2

    def _combined_spread(self, candidate: RewardMarketCandidate) -> float:
        yes_spread = self.ws.get_spread(candidate.yes_token_id) or 0.0
        no_spread = self.ws.get_spread(candidate.no_token_id) or 0.0
        return max(yes_spread, no_spread)

    def _ranking_score(
        self,
        candidate: RewardMarketCandidate,
        yes_mid: float,
        no_mid: float,
        quality_score: float,
        reward_efficiency: float,
    ) -> float:
        reward_component = log1p(candidate.reward_signal_strength + 1.0)
        efficiency_component = log1p((reward_efficiency * 10_000) + 1.0)
        spread_component = max(
            0.1,
            1.0 - (self._combined_spread(candidate) / max(candidate.rewards_max_spread, 0.01)),
        )
        balance_component = 1.0 - abs((yes_mid + no_mid) - 1.0)
        return (
            reward_component
            * efficiency_component
            * max(quality_score, 0.01)
            * spread_component
            * max(balance_component, 0.1)
        )

    def _build_market_quotes(
        self,
        market: SelectedRewardMarket,
        remaining_capital: float,
    ) -> list[DesiredQuote]:
        candidate = market.candidate
        per_market_budget = min(config.MAKER_MAX_PER_MARKET_USDC, remaining_capital)
        if per_market_budget < config.MAKER_MIN_QUOTE_SIZE_USDC:
            self._record_diag("quote_skips", "market:budget-too-small")
            return []

        yes_bid = self._reward_bid_quote(
            candidate=candidate,
            token_id=candidate.yes_token_id,
            outcome="YES",
            target_price=market.yes_quote_price,
            size_usdc=market.yes_min_quote_usdc,
        )
        no_bid = self._reward_bid_quote(
            candidate=candidate,
            token_id=candidate.no_token_id,
            outcome="NO",
            target_price=market.no_quote_price,
            size_usdc=market.no_min_quote_usdc,
        )
        min_total_quote_usdc = market.yes_min_quote_usdc + market.no_min_quote_usdc
        if min_total_quote_usdc > per_market_budget:
            self._record_diag("quote_skips", "market:capital-limited")
            yes_bid = None
            no_bid = None

        inventory_quotes = self._inventory_exit_quotes(candidate)
        quotes = [quote for quote in (yes_bid, no_bid) if quote]
        if len(quotes) < 2 and not inventory_quotes:
            self._record_diag("quote_skips", "market:two-sided-not-feasible")
            return []
        quotes.extend(inventory_quotes)
        return quotes

    def _prepare_reward_bid_plan(
        self,
        *,
        candidate: RewardMarketCandidate,
        token_id: str,
        outcome: str,
        market_mid: float,
    ) -> tuple[RewardBidPlan | None, str | None]:
        best_bid, best_ask = self.ws.get_best_bid_ask(token_id)
        if best_ask is None:
            return None, "best-ask-missing"

        inventory = self.quote_manager.inventory.get(token_id)
        inventory_notional = (
            inventory.current_notional(market_mid) if inventory else 0.0
        )
        if inventory_notional >= config.MAKER_MAX_INVENTORY_USDC:
            return None, "inventory-cap"

        reward_band = min(candidate.rewards_max_spread, best_ask - 0.001)
        quote_distance = max(
            candidate.price_tick_size,
            min(reward_band * 0.35, max((best_ask - (best_bid or market_mid)) * 0.5, candidate.price_tick_size)),
        )
        target_price = _round_down(
            min(market_mid - quote_distance, best_ask - candidate.price_tick_size),
            candidate.price_tick_size,
        )
        if target_price <= 0.01:
            return None, "target-too-low"
        distance_from_mid = max(0.0, market_mid - target_price)
        if distance_from_mid > candidate.rewards_max_spread:
            return None, "outside-reward-band"

        min_reward_notional = candidate.rewards_min_size * target_price
        min_quote_usdc = max(config.MAKER_MIN_QUOTE_SIZE_USDC, min_reward_notional)
        if inventory_notional > 0:
            reduction = min(min_quote_usdc * 0.5, inventory_notional * 0.5)
            min_quote_usdc = max(
                config.MIN_ORDER_SIZE_USDC,
                min_reward_notional,
                min_quote_usdc - reduction,
            )
        if min_quote_usdc < min_reward_notional or min_quote_usdc < config.MIN_ORDER_SIZE_USDC:
            return None, "reward-min-size"

        score_estimate = self._estimate_quote_score(
            market_mid=market_mid,
            quote_price=target_price,
            reward_max_spread=candidate.rewards_max_spread,
            size_usdc=min_quote_usdc,
        )
        return RewardBidPlan(
            token_id=token_id,
            outcome=outcome,
            target_price=target_price,
            min_quote_usdc=min_quote_usdc,
            reward_daily_rate=candidate.reward_daily_rate,
            score_estimate=score_estimate,
        ), None

    def _reward_bid_quote(
        self,
        *,
        candidate: RewardMarketCandidate,
        token_id: str,
        outcome: str,
        target_price: float,
        size_usdc: float,
    ) -> DesiredQuote | None:
        return DesiredQuote(
            quote_key=f"{candidate.market_id}:{outcome}:reward_bid",
            market_id=candidate.market_id,
            token_id=token_id,
            market_question=candidate.question,
            outcome=outcome,
            side="BUY",
            price=target_price,
            size_usdc=size_usdc,
            order_kind="reward_bid",
            reward_max_spread=candidate.rewards_max_spread,
            reward_min_size=candidate.rewards_min_size,
            reward_daily_rate=candidate.reward_daily_rate,
            score_estimate=self._estimate_quote_score(
                market_mid=self.ws.get_mid_price(token_id) or target_price,
                quote_price=target_price,
                reward_max_spread=candidate.rewards_max_spread,
                size_usdc=size_usdc,
            ),
        )

    def _inventory_exit_quotes(self, candidate: RewardMarketCandidate) -> list[DesiredQuote]:
        quotes: list[DesiredQuote] = []
        for token_id, outcome in (
            (candidate.yes_token_id, "YES"),
            (candidate.no_token_id, "NO"),
        ):
            inventory = self.quote_manager.inventory.get(token_id)
            quote = self._build_inventory_exit_quote(
                inventory,
                tick=candidate.price_tick_size,
                reward_max_spread=candidate.rewards_max_spread,
                reward_min_size=candidate.rewards_min_size,
                reward_daily_rate=candidate.reward_daily_rate,
            )
            if quote:
                quotes.append(quote)
        return quotes

    def _inventory_exit_profit_bps(self, lot: "InventoryLot") -> float:
        lot_inventory_pressure = min(
            1.0,
            lot.current_notional(self.ws.get_mid_price(lot.token_id) or lot.avg_entry_price)
            / max(config.MAKER_MAX_INVENTORY_USDC, 0.01),
        )
        total_inventory_notional = self.quote_manager.inventory_notional(
            lambda token_id: self.ws.get_mid_price(token_id) or 0.0
        )
        global_inventory_pressure = min(
            1.0,
            total_inventory_notional / max(config.MAKER_CAPITAL_USDC, 0.01),
        )
        age_minutes = max(
            0.0,
            (datetime.now(timezone.utc) - lot.updated_at).total_seconds() / 60.0,
        )
        age_pressure = min(1.0, age_minutes / 120.0)
        saturated = total_inventory_notional >= config.MAKER_CAPITAL_USDC
        multiplier = max(
            0.08 if saturated else 0.15,
            1.0
            - (0.30 * lot_inventory_pressure)
            - (0.45 * global_inventory_pressure)
            - (0.35 * age_pressure),
        )
        floor_bps = 3.0 if saturated else 8.0
        return max(floor_bps, config.MAKER_EXIT_PROFIT_BPS * multiplier)

    def _build_inventory_exit_quote(
        self,
        lot,
        *,
        tick: float,
        reward_max_spread: float = 0.0,
        reward_min_size: float = 0.0,
        reward_daily_rate: float = 0.0,
    ) -> DesiredQuote | None:
        if not lot or lot.is_flat:
            return None

        market_mid = self.ws.get_mid_price(lot.token_id)
        best_bid, best_ask = self.ws.get_best_bid_ask(lot.token_id)
        if market_mid is None or best_bid is None or best_ask is None:
            return None

        tick = max(tick, 0.01)
        target_profit_bps = self._inventory_exit_profit_bps(lot)
        inventory_notional = lot.current_notional(market_mid)
        pressure_ratio = min(
            1.0,
            inventory_notional / max(config.MAKER_MAX_INVENTORY_USDC, 0.01),
        )
        age_minutes = max(
            0.0,
            (datetime.now(timezone.utc) - lot.updated_at).total_seconds() / 60.0,
        )
        age_pressure = min(1.0, age_minutes / 120.0)
        global_inventory_pressure = min(
            1.0,
            self.quote_manager.inventory_notional(
                lambda token_id: self.ws.get_mid_price(token_id) or 0.0
            ) / max(config.MAKER_CAPITAL_USDC, 0.01),
        )
        exit_pressure = max(pressure_ratio, age_pressure, global_inventory_pressure)
        spread = max(best_ask - best_bid, tick)
        book_offset = max(
            tick,
            spread * max(0.10, 0.35 - (0.20 * exit_pressure)),
        )
        target = max(
            lot.avg_entry_price * (1 + target_profit_bps / 10_000),
            best_bid + book_offset,
        )
        if exit_pressure < 0.35:
            target = max(
                target,
                market_mid + max(tick, spread * 0.15),
            )
        target = min(_round_up(target, tick), 0.97)  # Cap below 0.99 so exits can realistically fill
        size_usdc = lot.shares * target
        if size_usdc < config.MIN_ORDER_SIZE_USDC:
            return None

        return DesiredQuote(
            quote_key=f"{lot.market_id}:{lot.outcome}:inventory_exit",
            market_id=lot.market_id,
            token_id=lot.token_id,
            market_question=lot.market_question,
            outcome=lot.outcome,
            side="SELL",
            price=target,
            size_usdc=size_usdc,
            order_kind="inventory_exit",
            reward_max_spread=reward_max_spread,
            reward_min_size=reward_min_size,
            reward_daily_rate=reward_daily_rate,
            score_estimate=0.0,
        )

    def _build_inventory_only_quotes(self, exclude_token_ids: set[str] | None = None) -> list[DesiredQuote]:
        quotes: list[DesiredQuote] = []
        excluded = exclude_token_ids or set()
        for lot in self.quote_manager.get_inventory():
            if lot.token_id in excluded:
                continue
            quote = self._build_inventory_exit_quote(lot, tick=0.01)
            if quote:
                quotes.append(quote)
        return quotes

    @staticmethod
    def _estimate_quote_score(
        *,
        market_mid: float,
        quote_price: float,
        reward_max_spread: float,
        size_usdc: float,
    ) -> float:
        distance = abs(market_mid - quote_price)
        if reward_max_spread <= 0 or distance > reward_max_spread:
            return 0.0
        shares = size_usdc / max(quote_price, 0.01)
        utility = ((reward_max_spread - distance) / reward_max_spread) ** 2
        return utility * shares

    async def _refresh_scoring_if_due(self):
        now = asyncio.get_running_loop().time()
        if now - self._last_scoring_refresh < config.MAKER_SCORING_REFRESH_S:
            return
        self._last_scoring_refresh = now

        active_quotes = self.quote_manager.get_active_quotes()
        if not active_quotes:
            return
        scoring_flags = await self.bot.get_orders_scoring(
            [quote.order_id for quote in active_quotes]
        )
        await self.quote_manager.update_scoring(scoring_flags)

    def _persist_reward_snapshots(self):
        for quote in self.quote_manager.get_active_quotes():
            self.db.save_maker_reward_snapshot({
                "market_id": quote.market_id,
                "token_id": quote.token_id,
                "quote_key": quote.quote_key,
                "estimated_score": quote.score_estimate,
                "reward_daily_rate": quote.reward_daily_rate,
                "scoring": quote.scoring,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            })

    def status_payload(self) -> dict:
        summary = self.db.get_maker_summary()
        active_quotes = self.quote_manager.get_active_quotes()
        recent_fills = self.db.get_recent_maker_fills(5)

        def price_lookup(token_id: str) -> float:
            return self.ws.get_mid_price(token_id) or 0.0

        estimated_inventory_notional = round(
            self.quote_manager.inventory_notional(price_lookup),
            2,
        )
        reward_rates_by_market: dict[str, float] = {}
        for quote in active_quotes:
            reward_rates_by_market[quote.market_id] = max(
                reward_rates_by_market.get(quote.market_id, 0.0),
                quote.reward_daily_rate,
            )
        estimated_daily_rewards = round(
            sum(reward_rates_by_market.values()),
            4,
        )
        gross_open_quote_notional = round(self.quote_manager.committed_capital(), 2)
        committed_capital = round(self.quote_manager.pending_buy_capital(), 2)
        inventory_exit_notional = round(self.quote_manager.inventory_exit_notional(), 2)
        reward_bid_quotes = sum(
            1 for quote in active_quotes if quote.order_kind == "reward_bid"
        )
        inventory_exit_quotes = sum(
            1 for quote in active_quotes if quote.order_kind == "inventory_exit"
        )
        estimated_daily_reward_yield_pct = round(
            (
                (estimated_daily_rewards / committed_capital) * 100
                if committed_capital > 0
                else 0.0
            ),
            4,
        )

        return {
            "enabled": self.is_active,
            "selected_markets": [
                {
                    "market": market.candidate.question[:80],
                    "ranking_score": round(market.ranking_score, 3),
                    "quality_score": round(market.quality_score, 3),
                    "reward_daily_rate": round(market.candidate.reward_daily_rate, 4),
                    "reward_amount": round(market.candidate.reward_amount, 4),
                    "has_measurable_daily_reward": (
                        market.candidate.has_measurable_daily_reward
                    ),
                    "min_quote_usdc": round(market.min_total_quote_usdc, 2),
                    "estimated_daily_reward_usdc": round(
                        market.estimated_daily_reward_usdc,
                        4,
                    ),
                    "reward_efficiency_daily_pct": round(
                        market.reward_efficiency * 100,
                        4,
                    ),
                    "efficiency_relaxed": market.efficiency_relaxed,
                }
                for market in self._selected_markets
            ],
            "active_quotes": len(active_quotes),
            "active_reward_bid_quotes": reward_bid_quotes,
            "active_inventory_exit_quotes": inventory_exit_quotes,
            "committed_capital_usdc": committed_capital,
            "pending_buy_capital_usdc": committed_capital,
            "inventory_exit_notional_usdc": inventory_exit_notional,
            "gross_open_quote_notional_usdc": gross_open_quote_notional,
            "inventory_notional_usdc": estimated_inventory_notional,
            "estimated_daily_rewards_usdc": estimated_daily_rewards,
            "estimated_daily_reward_yield_pct": estimated_daily_reward_yield_pct,
            "estimated_rewards_accrued_usdc": round(
                self._estimated_rewards_accrued_usdc,
                4,
            ),
            "estimated_total_pnl_usdc": round(
                (summary.get("realized_pnl_usdc", 0) or 0)
                + self._estimated_rewards_accrued_usdc,
                4,
            ),
            "scoring_quotes": int(summary.get("scoring_open_orders", 0) or 0),
            "realized_pnl_usdc": round(summary.get("realized_pnl_usdc", 0) or 0, 2),
            "avg_estimated_score": round(summary.get("avg_estimated_score", 0) or 0, 2),
            "recent_fills": [
                {
                    "market": fill.get("market_question", "")[:80],
                    "outcome": fill.get("outcome", ""),
                    "side": fill.get("side", ""),
                    "fill_price": fill.get("fill_price", 0),
                    "fill_size_usdc": round(fill.get("fill_size_usdc", 0), 2),
                    "realized_pnl_usdc": round(fill.get("realized_pnl_usdc", 0), 2),
                }
                for fill in recent_fills
            ],
            "diagnostics": self._last_cycle_diagnostics,
        }

    def _record_diag(self, bucket: str, reason: str):
        diagnostics = self._last_cycle_diagnostics.setdefault(bucket, {})
        diagnostics[reason] = diagnostics.get(reason, 0) + 1

    async def shutdown(self):
        self._running = False
        await self.quote_manager.cancel_all(reason="shutdown")
        for token_id in list(self._subscribed_tokens):
            await self.ws.unsubscribe(token_id)
        self._subscribed_tokens.clear()
        logger.info("RewardMarketMaker: Shut down")
