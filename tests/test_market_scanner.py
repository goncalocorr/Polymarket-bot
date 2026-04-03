"""Tests for scanner parallelism and per-strategy timeouts."""

import asyncio
import time

import pytest

from data.market_scanner import MarketScanner
from strategies.base import BaseStrategy


class _Strategy(BaseStrategy):
    def __init__(self, name: str, delay: float, result=None):
        super().__init__(name)
        self.delay = delay
        self.result = result or []

    async def initialize(self):
        return None

    async def evaluate(self):
        await asyncio.sleep(self.delay)
        return self.result

    async def shutdown(self):
        return None


@pytest.mark.asyncio
async def test_market_scanner_runs_strategies_in_parallel():
    fast_a = _Strategy("a", delay=0.05, result=["sig-a"])
    fast_b = _Strategy("b", delay=0.05, result=["sig-b"])
    scanner = MarketScanner(
        [fast_a, fast_b],
        scan_interval_s=30,
        strategy_timeout_s=1.0,
    )

    started = time.perf_counter()
    signals = await scanner.scan_once()
    elapsed = time.perf_counter() - started

    assert signals == ["sig-a", "sig-b"]
    assert elapsed < 0.09


@pytest.mark.asyncio
async def test_market_scanner_times_out_slow_strategy_without_blocking_fast_one():
    slow = _Strategy("slow", delay=0.2, result=["slow-sig"])
    fast = _Strategy("fast", delay=0.01, result=["fast-sig"])
    scanner = MarketScanner(
        [slow, fast],
        scan_interval_s=30,
        strategy_timeout_s=0.05,
    )

    signals = await scanner.scan_once()

    assert signals == ["fast-sig"]
