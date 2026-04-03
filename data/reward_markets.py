"""Discovery and ranking helpers for Polymarket liquidity reward markets."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from dateutil.parser import isoparse
from loguru import logger

import config
from core.gamma_client import GammaClient
from strategies.mispricing import _normalise_tokens, _pick_token_id


def _normalise_reward_spread(raw) -> float:
    """Reward max spread may arrive as cents or price units."""
    try:
        value = float(raw or 0)
    except (TypeError, ValueError):
        return 0.0
    if value > 1.0:
        return value / 100.0
    return value


def _reward_daily_rate(market: dict) -> float:
    rewards = market.get("clobRewards") or []
    total = 0.0
    for reward in rewards:
        if not isinstance(reward, dict):
            continue
        try:
            total += float(reward.get("rewardsDailyRate", 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _reward_amount(market: dict) -> float:
    rewards = market.get("clobRewards") or []
    total = 0.0
    for reward in rewards:
        if not isinstance(reward, dict):
            continue
        try:
            total += float(reward.get("rewardsAmount", 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


@dataclass
class RewardMarketCandidate:
    market_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    end_date: str
    rewards_min_size: float
    rewards_max_spread: float
    reward_daily_rate: float
    reward_amount: float
    volume_24h: float
    liquidity_clob: float
    price_tick_size: float

    @property
    def reward_signal_strength(self) -> float:
        return max(self.reward_daily_rate, 0.0) + max(self.reward_amount, 0.0) * 0.05

    @property
    def has_measurable_daily_reward(self) -> bool:
        return self.reward_daily_rate > 0


class RewardMarketSource:
    """Loads and filters active Polymarket markets with liquidity rewards."""

    def __init__(self, gamma: GammaClient):
        self.gamma = gamma
        self._cache: list[RewardMarketCandidate] = []
        self._cache_until = 0.0
        self._lock = asyncio.Lock()

    async def get_reward_markets(
        self,
        limit: int = 10,
    ) -> list[RewardMarketCandidate]:
        now = asyncio.get_running_loop().time()
        if self._cache and now < self._cache_until:
            return self._cache[:limit]

        async with self._lock:
            now = asyncio.get_running_loop().time()
            if self._cache and now < self._cache_until:
                return self._cache[:limit]

            candidates = await self._discover_reward_markets()
            self._cache = candidates
            self._cache_until = now + config.MAKER_MARKETS_CACHE_TTL_S
            return candidates[:limit]

    async def _discover_reward_markets(self) -> list[RewardMarketCandidate]:
        all_markets: list[dict] = []
        offset = 0

        while len(all_markets) < 400:
            try:
                batch = await self.gamma.get_markets(active=True, limit=100, offset=offset)
            except Exception as e:
                logger.warning(f"RewardMarketSource: gamma.get_markets failed at offset {offset}: {e}")
                break
            if not batch:
                break
            all_markets.extend(batch)
            offset += len(batch)
            if len(batch) < 100:
                break
            await asyncio.sleep(0.2)

        candidates: list[RewardMarketCandidate] = []
        now = datetime.now(timezone.utc)

        for market in all_markets:
            rewards_min_size = float(market.get("rewardsMinSize", 0) or 0)
            rewards_max_spread = _normalise_reward_spread(
                market.get("rewardsMaxSpread", 0)
            )
            reward_daily_rate = _reward_daily_rate(market)
            reward_amount = _reward_amount(market)

            if rewards_min_size <= 0 or rewards_max_spread <= 0:
                continue
            if (
                config.MAKER_REQUIRE_MEASURABLE_DAILY_REWARD
                and reward_daily_rate <= 0
            ):
                continue
            if reward_daily_rate < config.MAKER_MIN_REWARD_DAILY_RATE:
                continue
            if market.get("closed") is True or market.get("active") is False:
                continue
            if market.get("acceptingOrders") is False:
                continue

            end_date_str = market.get("endDate") or market.get("end_date_iso", "")
            if not end_date_str:
                continue
            try:
                end_date = isoparse(end_date_str.replace("Z", "+00:00"))
                if end_date.tzinfo is None:
                    end_date = end_date.replace(tzinfo=timezone.utc)
                days_to_resolution = (end_date - now).total_seconds() / 86400
            except (TypeError, ValueError):
                continue
            if not (
                config.MAKER_MIN_DAYS_TO_RESOLUTION
                <= days_to_resolution
                <= config.MAKER_MAX_DAYS_TO_RESOLUTION
            ):
                continue

            tokens = _normalise_tokens(
                market.get("tokens", []) or market.get("clobTokenIds", [])
            )
            yes_token_id = _pick_token_id(tokens, is_yes=True)
            no_token_id = _pick_token_id(tokens, is_yes=False)
            if not yes_token_id or not no_token_id:
                continue

            candidate = RewardMarketCandidate(
                market_id=str(market.get("id", "")),
                question=market.get("question", ""),
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
                end_date=end_date.isoformat(),
                rewards_min_size=rewards_min_size,
                rewards_max_spread=rewards_max_spread,
                reward_daily_rate=reward_daily_rate,
                reward_amount=reward_amount,
                volume_24h=float(market.get("volume24hr", 0) or 0),
                liquidity_clob=float(market.get("liquidityClob", 0) or 0),
                price_tick_size=float(market.get("orderPriceMinTickSize", 0.01) or 0.01),
            )
            candidates.append(candidate)

        candidates.sort(
            key=lambda market: (
                market.reward_signal_strength,
                market.volume_24h,
                market.liquidity_clob,
            ),
            reverse=True,
        )

        logger.info(
            f"RewardMarketSource: Found {len(candidates)} reward-eligible markets "
            f"from {len(all_markets)} active markets"
        )
        return candidates
