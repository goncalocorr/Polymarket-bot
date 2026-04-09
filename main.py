"""
Polymarket Trading Bot — Main entry point and orchestrator.
Runs 24/7, coordinating strategies, execution, risk management, and monitoring.
"""

import asyncio
import os
import signal
import sys
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from dateutil.parser import isoparse

from apscheduler.schedulers.base import SchedulerNotRunningError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

import config
from ai.claude_analyst import ClaudeAnalyst
from ai.openai_analyst import OpenAIAnalyst
from core.bot import TradingBot
from core.gamma_client import GammaClient
from core.process_lock import SingleInstanceLock
from core.websocket_client import MarketWebSocket
from data.external_sources import ExternalSources
from data.market_quality import MarketQualityScorer
from data.market_scanner import MarketScanner
from execution.executor import ThreePhaseExecutor
from execution.order_manager import OrderManager
from monitor.dashboard import StatusWriter, TerminalDashboard
from notifications.telegram import TelegramNotifier
from risk.risk_manager import RiskManager
from risk.strategy_tracker import StrategyTracker
from storage.database import Database
from strategies.ai_agent import AIAgentStrategy
from strategies.base import Signal
from strategies.flash_crash import FlashCrashStrategy
from strategies.mispricing import MispricingStrategy
from strategies.multi_outcome import MultiOutcomeStrategy
from strategies.reward_market_maker import RewardMarketMakerStrategy

VERSION = "1.0.0"

# --- Logging ---
logger.remove()
logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")
if "PYTEST_CURRENT_TEST" not in os.environ and not any(
    "pytest" in arg.lower() for arg in sys.argv
):
    logger.add(
        str(config.LOGS_DIR / "bot_{time:YYYY-MM-DD}.log"),
        rotation="1 day",
        retention="30 days",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {module}:{line} | {message}",
    )


def build_analyst():
    """Build the configured AI analyst provider."""
    provider = config.AI_PROVIDER

    if provider == "openai":
        return OpenAIAnalyst()
    if provider == "anthropic":
        return ClaudeAnalyst()

    if config.OPENAI_API_KEY:
        return OpenAIAnalyst()
    return ClaudeAnalyst()


class Orchestrator:
    """Main bot orchestrator — wires everything together."""

    def __init__(self):
        # Core
        self.bot = TradingBot()
        self.ws = MarketWebSocket()
        self.gamma = GammaClient()
        self.db = Database()
        self.telegram = TelegramNotifier()
        self.external = ExternalSources()

        # Market quality scorer
        self.quality_scorer = MarketQualityScorer(self.ws)

        # Risk
        self.risk_manager = RiskManager()

        # Execution
        self.executor = ThreePhaseExecutor(self.bot)
        self.order_manager = OrderManager(self.bot, self.ws, self.executor)

        # Wire partial exit persistence so DB stays in sync after partial fills
        db_ref = self.db
        risk_ref = self.risk_manager
        async def _persist_partial_exit(pos, fill_size, partial_pnl=0.0):
            try:
                db_ref.update_trade_size(
                    pos.id, pos.size_usdc, pos.realized_partial_pnl
                )
            except Exception as e:
                logger.error(f"Failed to persist partial exit for {pos.id}: {e}")
            if partial_pnl != 0.0:
                try:
                    await risk_ref.record_trade_result(partial_pnl, close_reason="partial-exit")
                except Exception as e:
                    logger.error(f"Failed to record partial exit P&L for {pos.id}: {e}")
        self.order_manager._on_partial_exit = _persist_partial_exit

        # AI
        self.analyst = build_analyst()

        # Strategies
        self.flash_crash = FlashCrashStrategy(
            self.ws, self.gamma, quality_scorer=self.quality_scorer
        )
        self.mispricing = MispricingStrategy(
            self.gamma, self.external,
            ws=self.ws, quality_scorer=self.quality_scorer,
        )
        self.ai_agent = AIAgentStrategy(
            self.gamma, self.external, self.analyst, db=self.db
        )
        self.multi_outcome = MultiOutcomeStrategy(
            self.gamma,
            ws=self.ws,
            quality_scorer=self.quality_scorer,
        )
        self.reward_market_maker = RewardMarketMakerStrategy(
            self.gamma,
            self.ws,
            self.bot,
            self.db,
            self.risk_manager,
            quality_scorer=self.quality_scorer,
            order_manager=self.order_manager,
        )
        self.signal_strategies = [
            self.flash_crash, self.mispricing,
            self.ai_agent, self.multi_outcome,
        ]
        self.strategies = self.signal_strategies + [self.reward_market_maker]

        # Strategy performance tracker
        self.tracker = StrategyTracker(self.db, self.signal_strategies)

        # Scanner
        self.scanner = MarketScanner(self.signal_strategies, scan_interval_s=30)

        # Monitoring
        self.status_writer = StatusWriter(
            self.order_manager, self.risk_manager, self.db, self.strategies
        )
        self.dashboard = TerminalDashboard(
            self.order_manager, self.risk_manager, self.db, self.strategies
        )

        # Scheduler for periodic tasks
        self.scheduler = AsyncIOScheduler()

        # Wire up risk callbacks
        self.risk_manager.on_stop_loss(self._on_daily_stop)
        self.risk_manager.on_circuit_breaker(self._on_circuit_breaker)

        self._running = False
        self._tasks = []
        self._trade_lock = asyncio.Lock()  # Serialise exposure check + position open
        self._initial_capital = self.risk_manager.total_capital
        self._instance_lock = SingleInstanceLock(config.DATA_DIR / "bot.lock")

    def _on_daily_stop(self, total_loss: float, capital: float):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.telegram.daily_stop_loss(total_loss, capital))
        except RuntimeError:
            logger.error("No running event loop — cannot send daily stop notification")

    def _on_circuit_breaker(self, resume_time):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self.telegram.circuit_breaker(
                    f"{config.CIRCUIT_BREAKER_CONSECUTIVE_LOSSES} consecutive losses",
                    resume_time.strftime("%H:%M UTC"),
                )
            )
        except RuntimeError:
            logger.error("No running event loop — cannot send circuit breaker notification")

    async def _save_open_position(self, position):
        """Persist an opened position before it becomes part of tracked state."""
        self.db.save_trade({
            "id": position.id,
            "token_id": position.token_id,
            "market_question": position.market_question,
            "strategy": position.strategy,
            "side": position.side,
            "entry_price": position.entry_price,
            "size_usdc": position.size_usdc,
            "take_profit": position.take_profit,
            "stop_loss": position.stop_loss,
            "status": "OPEN",
            "opened_at": position.opened_at.isoformat(),
        })

    async def _notify_position_opened(self, position):
        """Send the position-opened notification without affecting persistence."""
        await self.telegram.position_opened(
            position.market_question,
            position.side,
            position.entry_price,
            position.size_usdc,
            position.strategy,
        )

    async def _finalize_closed_position(self, pos, reason: str = ""):
        """Apply accounting side-effects for a closed position."""
        close_reason = getattr(pos, "close_reason", "") or reason or pos.status
        # Partial exit P&L was already recorded to risk manager incrementally;
        # only send the remainder P&L to avoid double-counting.
        partial_already_recorded = getattr(pos, "realized_partial_pnl", 0.0)
        remainder_pnl = pos.pnl_usdc - partial_already_recorded
        await self.risk_manager.record_trade_result(
            remainder_pnl,
            close_reason=close_reason,
        )
        self.db.close_trade(
            pos.id,
            pos.exit_price or 0,
            pos.pnl_usdc,
            close_reason=close_reason,
        )
        # Record the full P&L (including partials) for strategy tracking
        self.tracker.record_trade(pos.strategy, pos.pnl_usdc)
        mispricing = getattr(self, "mispricing", None)
        if mispricing and pos.strategy == mispricing.name:
            self.mispricing.record_closed_trade(
                pos.token_id,
                pos.pnl_usdc,
                close_reason,
            )

        pnl_pct = (pos.pnl_usdc / max(pos.size_usdc, 0.01)) * 100
        await self.telegram.position_closed(
            pos.market_question,
            pos.pnl_usdc,
            pnl_pct,
            close_reason,
        )

    async def _close_untracked_position(self, position, reason: str):
        """Best-effort close when a live position could not be persisted locally."""
        logger.warning(
            f"Closing untracked position {position.id} after local failure: {reason}"
        )
        closed = await self.order_manager.close_position(position.id, reason=reason)
        if not closed:
            logger.critical(
                f"Failed to close untracked position {position.id}; "
                "manual intervention required."
            )
            return

        try:
            await self._finalize_closed_position(closed, reason=reason)
        except Exception as e:
            logger.critical(
                f"Closed untracked position {position.id}, but could not finalize "
                f"accounting: {e}"
            )

    async def _rollback_arb_group(self, opened_positions: list, group_id: str):
        """Best-effort rollback when an arbitrage group fails mid-execution."""
        if not opened_positions:
            return

        logger.warning(
            f"Rolling back arb group {group_id} "
            f"({len(opened_positions)} already-opened legs)"
        )
        failed_rollbacks: list[str] = []

        for pos in reversed(opened_positions):
            closed = await self.order_manager.close_position(
                pos.id, reason=f"arb-rollback:{group_id}"
            )
            if not closed:
                failed_rollbacks.append(pos.id)
                continue
            await self._finalize_closed_position(closed, reason="arb-rollback")

        if failed_rollbacks:
            logger.critical(
                f"ARB ROLLBACK INCOMPLETE for group {group_id}: "
                f"{failed_rollbacks}. Manual intervention required."
            )

    async def process_arb_group(self, signals: list[Signal]):
        """
        Execute a multi-leg arbitrage group atomically (all-or-nothing).
        If any leg fails, close all previously opened legs (best effort).
        """
        if not signals:
            return

        group_id = signals[0].arb_group_id or "arb-unknown"
        logger.info(f"Processing arb group {group_id} with {len(signals)} legs")

        async with self._trade_lock:
            planned: list[tuple[Signal, float]] = []
            projected_exposure = self.order_manager.get_total_exposure()
            reserved_tokens: set[str] = set()

            for signal in signals:
                size_multiplier = self.tracker.get_size_multiplier(signal.strategy)
                if size_multiplier <= 0:
                    logger.info(
                        f"Arb group {group_id} blocked: strategy {signal.strategy} paused"
                    )
                    return

                has_existing_position = (
                    self.order_manager.has_position_in_market(signal.token_id)
                    or signal.token_id in reserved_tokens
                )

                allowed, size, reason = await self.risk_manager.validate_order(
                    signal,
                    current_exposure=projected_exposure,
                    has_existing_position=has_existing_position,
                )
                if not allowed:
                    logger.info(f"Arb group {group_id} blocked by risk manager: {reason}")
                    return

                size *= size_multiplier
                max_exposure = self.risk_manager.total_capital * config.MAX_TOTAL_EXPOSURE_PCT
                max_position = self.risk_manager.total_capital * config.MAX_POSITION_SIZE_PCT
                remaining_room = max_exposure - projected_exposure
                size = min(size, max_position, remaining_room)
                if size < config.MIN_ORDER_SIZE_USDC:
                    logger.info(
                        f"Arb group {group_id} blocked: leg size ${size:.2f} "
                        "below minimum after cap"
                    )
                    return

                planned.append((signal, size))
                projected_exposure += size
                reserved_tokens.add(signal.token_id)

            opened_positions = []
            for signal, size in planned:
                position = await self.order_manager.open_position(
                    token_id=signal.token_id,
                    market_question=signal.market_question,
                    strategy=signal.strategy,
                    side=signal.side,
                    price=signal.price,
                    size_usdc=size,
                    take_profit=signal.take_profit,
                    stop_loss=signal.stop_loss,
                )

                if not position:
                    logger.error(
                        f"Arb group {group_id} failed while opening "
                        f"token {signal.token_id[:16]}..."
                    )
                    await self._rollback_arb_group(opened_positions, group_id)
                    return

                opened_positions.append(position)
                try:
                    await self._save_open_position(position)
                except Exception as e:
                    logger.error(
                        f"Arb group {group_id} failed while persisting "
                        f"position {position.id}: {e}"
                    )
                    await self._rollback_arb_group(opened_positions, group_id)
                    return

        # Mark event as executed immediately so it's not re-signalled,
        # even if notifications below fail.
        event_id = signals[0].arb_event_id
        if (
            event_id
            and signals[0].strategy == self.multi_outcome.name
        ):
            self.multi_outcome.mark_event_executed(event_id)

        for pos in opened_positions:
            try:
                await self._notify_position_opened(pos)
            except Exception as e:
                logger.error(
                    f"Failed to send open notification for {pos.id}: {e}"
                )

        logger.info(
            f"Arb group {group_id} executed successfully "
            f"({len(opened_positions)} legs)"
        )

    async def process_signal(self, signal: Signal):
        """Process a trading signal through risk validation and execution."""
        # Discard stale signals whose price snapshot is too old
        signal_age = (datetime.now(timezone.utc) - signal.timestamp).total_seconds()
        if signal_age > config.STRATEGY_EVALUATION_TIMEOUT_S * 2:
            logger.debug(
                f"Signal discarded: {signal.strategy} signal is {signal_age:.0f}s old"
            )
            return

        # Strategy tracker gate — check if strategy is auto-paused or boosted
        size_multiplier = self.tracker.get_size_multiplier(signal.strategy)
        if size_multiplier <= 0:
            logger.info(
                f"Signal blocked by strategy tracker: {signal.strategy} is paused"
            )
            return

        async with self._trade_lock:
            # Risk check (under lock to prevent TOCTOU with concurrent signals)
            allowed, size, reason = await self.risk_manager.validate_order(
                signal,
                current_exposure=self.order_manager.get_total_exposure(),
                has_existing_position=self.order_manager.has_position_in_market(
                    signal.token_id
                ),
            )

            if not allowed:
                logger.info(f"Signal blocked by risk manager: {reason}")
                return

            # Apply strategy boost/reduction, but re-cap to risk limits
            size *= size_multiplier
            max_exposure = self.risk_manager.total_capital * config.MAX_TOTAL_EXPOSURE_PCT
            max_position = self.risk_manager.total_capital * config.MAX_POSITION_SIZE_PCT
            remaining_room = max_exposure - self.order_manager.get_total_exposure()
            size = min(size, max_position, remaining_room)
            if size < config.MIN_ORDER_SIZE_USDC:
                logger.info(f"Boosted size ${size:.2f} below minimum after cap")
                return

            # Execute (under lock so exposure is updated atomically)
            position = await self.order_manager.open_position(
                token_id=signal.token_id,
                market_question=signal.market_question,
                strategy=signal.strategy,
                side=signal.side,
                price=signal.price,
                size_usdc=size,
                take_profit=signal.take_profit,
                stop_loss=signal.stop_loss,
            )

        if position:
            try:
                await self._save_open_position(position)
            except Exception as e:
                logger.error(
                    f"Failed to persist opened position {position.id}: {e}"
                )
                await self._close_untracked_position(
                    position, reason="open-persist-failed"
                )
                return

            try:
                await self._notify_position_opened(position)
            except Exception as e:
                logger.error(
                    f"Failed to send open notification for {position.id}: {e}"
                )

    async def check_exits_loop(self):
        """Periodically check for exit conditions on open positions."""
        while self._running:
            try:
                closed = await self.order_manager.check_exits()
                for pos in closed:
                    await self._finalize_closed_position(pos)
            except Exception as e:
                logger.error(f"Exit check error: {e}")

            await asyncio.sleep(5)

    async def scan_and_trade_loop(self):
        """Continuously scan for signals and execute trades."""
        while self._running:
            try:
                self.tracker.reconcile_states()
                signals = await self.scanner.scan_once()
                arb_groups: dict[str, list[Signal]] = defaultdict(list)
                regular_signals: list[Signal] = []

                for signal in signals:
                    if signal.arb_group_id:
                        arb_groups[signal.arb_group_id].append(signal)
                    else:
                        regular_signals.append(signal)

                for group_signals in arb_groups.values():
                    await self.process_arb_group(group_signals)

                for signal in regular_signals:
                    await self.process_signal(signal)
            except Exception as e:
                logger.error(f"Scan loop error: {e}")

            await asyncio.sleep(30)

    async def refresh_flash_crash_markets(self):
        """Refresh 15-min crypto markets every 5 minutes."""
        try:
            await self.flash_crash.refresh_markets()
        except Exception as e:
            logger.error(f"Flash crash market refresh error: {e}")

    async def refresh_balance(self):
        """Refresh USDC balance from Polymarket (live mode only)."""
        try:
            balance = await self.bot.get_balance()
            if balance > 0:
                await self.risk_manager.update_capital(balance)
                logger.debug(f"Balance refreshed: ${balance:.2f} USDC")
        except Exception as e:
            logger.error(f"Balance refresh error: {e}")

    async def daily_report(self):
        """Send daily report at 08:00 UTC."""
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            stats = self.db.get_trade_stats(date_filter=today)
            total = stats.get("total_trades", 0)
            wins = stats.get("wins", 0)
            win_rate = wins / max(total, 1)

            await self.telegram.daily_report(
                capital=self.risk_manager.total_capital,
                day_pnl=self.db.get_today_pnl(),
                open_positions=len(self.order_manager.get_open_positions()),
                trades_count=total,
                win_rate=win_rate,
            )
        except Exception as e:
            logger.error(f"Daily report error: {e}")

        # Housekeeping: prune old reward snapshots to prevent unbounded growth
        try:
            pruned = self.db.prune_maker_reward_snapshots(keep_recent=5000)
            if pruned:
                logger.info(f"Pruned {pruned} old maker reward snapshots")
        except Exception as e:
            logger.error(f"Reward snapshot cleanup error: {e}")

    async def _restore_risk_state_from_db(self):
        """Rebuild risk state from persisted closed trades after a restart."""
        today_pnl = self.db.get_today_pnl()

        if config.DRY_RUN:
            total_capital = self._initial_capital + self.db.get_total_pnl()
        else:
            total_capital = self.risk_manager.total_capital

        circuit_breaker_until = None
        recent_trades = self.db.get_recent_trades(
            config.CIRCUIT_BREAKER_CONSECUTIVE_LOSSES + 2
        )
        recent_losses = []
        for trade in recent_trades:
            pnl = float(trade.get("pnl_usdc", 0) or 0)
            close_reason = str(trade.get("close_reason", "") or "")
            # Skip time-stop exits in dry-run (not real losses)
            if config.DRY_RUN and close_reason == "time-stop":
                continue
            # Breakeven shouldn't interrupt a losing streak, but also isn't a loss
            if pnl > 0:
                break
            if pnl == 0:
                continue
            closed_at = trade.get("closed_at")
            if not closed_at:
                break
            try:
                closed_dt = isoparse(closed_at)
            except (ValueError, TypeError):
                break
            recent_losses.append(closed_dt)

        if len(recent_losses) >= config.CIRCUIT_BREAKER_CONSECUTIVE_LOSSES:
            latest_loss = recent_losses[0]
            candidate_until = latest_loss + timedelta(
                hours=config.CIRCUIT_BREAKER_PAUSE_HOURS
            )
            if candidate_until > datetime.now(timezone.utc):
                circuit_breaker_until = candidate_until

        await self.risk_manager.restore_state(
            total_capital=total_capital,
            daily_pnl=today_pnl,
            circuit_breaker_until=circuit_breaker_until,
        )

    async def start(self):
        """Start the bot."""
        instance_lock = getattr(self, "_instance_lock", None)
        if instance_lock is None:
            instance_lock = SingleInstanceLock(config.DATA_DIR / "bot.lock")
            self._instance_lock = instance_lock

        if not instance_lock.acquire():
            raise RuntimeError(
                "Another Polymarket bot instance is already running. "
                "Stop the existing process before starting a new one."
            )

        logger.info(f"Starting Polymarket Bot v{VERSION}")
        logger.info(f"Mode: {'DRY RUN' if config.DRY_RUN else 'LIVE'}")
        self._running = True

        # Fail fast if live mode lacks credentials
        if not config.DRY_RUN and not config.POLYGON_WALLET_PRIVATE_KEY:
            raise ValueError(
                "POLYGON_WALLET_PRIVATE_KEY is required when DRY_RUN=false. "
                "Set it in .env or as an environment variable."
            )

        # Ensure authenticated CLOB credentials exist in live mode
        if not config.DRY_RUN and (
            not self.bot.api_creds or not self.bot.api_creds.is_valid()
        ):
            logger.info("No L2 API credentials configured - deriving from wallet")
            try:
                await self.bot.derive_api_credentials()
            except Exception as e:
                raise RuntimeError(
                    "Failed to derive Polymarket L2 API credentials. "
                    "Set POLY_API_KEY/POLY_API_SECRET/POLY_API_PASSPHRASE "
                    "or verify wallet credentials."
                ) from e

        # Initialize balance
        if not config.DRY_RUN:
            balance = await self.bot.get_balance()
            if balance <= 0:
                raise RuntimeError(
                    "Failed to initialize live balance from Polymarket. "
                    "Received a non-positive balance, so startup was aborted "
                    "to avoid invalid risk limits."
                )
            await self.risk_manager.update_capital(balance)
            logger.info(f"Balance: ${balance:.2f} USDC")

        await self._restore_risk_state_from_db()

        # Restore open positions from database (survive restarts)
        open_trades = self.db.get_open_trades()
        if open_trades:
            self.order_manager.load_open_positions(open_trades)
            logger.info(
                f"Restored {len(open_trades)} open positions from database"
            )

        # Connect WebSocket before strategies that need subscriptions
        await self.ws.connect()

        # Initialize strategies
        await self.flash_crash.initialize()
        await self.mispricing.initialize()
        await self.ai_agent.initialize()
        await self.multi_outcome.initialize()
        reward_market_maker = getattr(self, "reward_market_maker", None)
        if reward_market_maker:
            await reward_market_maker.initialize()
        await self.mispricing.restore_runtime_state(
            self.order_manager.get_open_positions(),
            self.db.get_recent_trades(50),
            db=self.db,
        )

        # Send startup notification
        await self.telegram.bot_started(VERSION)

        # Schedule periodic tasks
        self.scheduler.add_job(
            self.refresh_flash_crash_markets, "interval", minutes=5
        )
        self.scheduler.add_job(
            self.daily_report, "cron", hour=config.DAILY_REPORT_HOUR_UTC
        )
        # Periodic balance refresh (live mode only) to keep risk limits accurate
        if not config.DRY_RUN:
            self.scheduler.add_job(
                self.refresh_balance, "interval", minutes=10
            )
        self.scheduler.start()

        # Run all loops concurrently
        self._tasks = [
            asyncio.create_task(self.ws.run_with_reconnect()),
            asyncio.create_task(self.scan_and_trade_loop()),
            asyncio.create_task(self.check_exits_loop()),
            asyncio.create_task(self.status_writer.run()),
        ]
        if reward_market_maker:
            self._tasks.append(asyncio.create_task(reward_market_maker.run()))

        # Only run TUI if we have a terminal
        if sys.stdout.isatty():
            self._tasks.append(asyncio.create_task(self.dashboard.run()))

        logger.info("All systems running")

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            logger.info("Bot shutting down...")

    async def stop(self):
        """Gracefully shut down the bot."""
        logger.info("Stopping bot...")
        self._running = False
        self.status_writer.stop()
        self.dashboard.stop()
        try:
            self.scheduler.shutdown(wait=False)
        except SchedulerNotRunningError:
            logger.debug("Scheduler was not running during shutdown")

        # Cancel all running tasks so gather() exits
        for task in self._tasks:
            task.cancel()

        # Close all strategies
        for strategy in self.strategies:
            await strategy.shutdown()

        # Close connections
        await self.ws.close()
        await self.gamma.close()
        await self.external.close()
        await self.bot.close()
        await self.telegram.close()
        analyst = getattr(self, "analyst", None)
        if analyst and hasattr(analyst, "close"):
            await analyst.close()
        instance_lock = getattr(self, "_instance_lock", None)
        if instance_lock:
            instance_lock.release()

        logger.info("Bot stopped")


async def main():
    orchestrator = Orchestrator()

    # Handle graceful shutdown
    loop = asyncio.get_running_loop()

    def shutdown_handler():
        asyncio.create_task(orchestrator.stop())

    def _make_win_handler(loop_ref, handler_ref):
        def _win_handler(signum, frame):
            loop_ref.call_soon_threadsafe(handler_ref)
        return _win_handler

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler — use signal.signal
            signal.signal(sig, _make_win_handler(loop, shutdown_handler))

    try:
        await orchestrator.start()
    except KeyboardInterrupt:
        await orchestrator.stop()
    except Exception as e:
        # Redact secrets BEFORE logging to prevent leaking keys to disk
        raw_trace = traceback.format_exc()
        safe_trace = raw_trace
        for secret in (
            config.POLYGON_WALLET_PRIVATE_KEY,
            config.POLY_API_SECRET,
            config.POLY_API_KEY,
            config.POLY_API_PASSPHRASE,
            config.POLY_BUILDER_API_SECRET,
            config.ANTHROPIC_API_KEY,
            config.OPENAI_API_KEY,
            config.KALSHI_API_SECRET,
            config.TELEGRAM_BOT_TOKEN,
            config.NEWSAPI_KEY,
        ):
            if secret:
                safe_trace = safe_trace.replace(secret, "[REDACTED]")
        logger.critical(f"Fatal error: {e}")
        logger.critical(safe_trace)
        await orchestrator.telegram.critical_error(
            type(e).__name__, safe_trace[-500:]
        )
        await orchestrator.stop()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
