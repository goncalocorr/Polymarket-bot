"""
Polymarket Trading Bot — Main entry point and orchestrator.
Runs 24/7, coordinating strategies, execution, risk management, and monitoring.
"""

import asyncio
import signal
import sys
import traceback
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

import config
from ai.claude_analyst import ClaudeAnalyst
from core.bot import TradingBot
from core.gamma_client import GammaClient
from core.websocket_client import MarketWebSocket
from data.external_sources import ExternalSources
from data.market_scanner import MarketScanner
from execution.executor import ThreePhaseExecutor
from execution.order_manager import OrderManager
from monitor.dashboard import StatusWriter, TerminalDashboard
from notifications.telegram import TelegramNotifier
from risk.risk_manager import RiskManager
from storage.database import Database
from strategies.ai_agent import AIAgentStrategy
from strategies.base import Signal
from strategies.flash_crash import FlashCrashStrategy
from strategies.mispricing import MispricingStrategy

VERSION = "1.0.0"

# --- Logging ---
logger.remove()
logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")
logger.add(
    str(config.LOGS_DIR / "bot_{time:YYYY-MM-DD}.log"),
    rotation="1 day",
    retention="30 days",
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {module}:{line} | {message}",
)


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

        # Risk
        self.risk_manager = RiskManager()

        # Execution
        self.executor = ThreePhaseExecutor(self.bot)
        self.order_manager = OrderManager(self.bot, self.ws, self.executor)

        # AI
        self.analyst = ClaudeAnalyst()

        # Strategies
        self.flash_crash = FlashCrashStrategy(self.ws, self.gamma)
        self.mispricing = MispricingStrategy(self.gamma, self.external)
        self.ai_agent = AIAgentStrategy(
            self.gamma, self.external, self.analyst
        )
        self.strategies = [self.flash_crash, self.mispricing, self.ai_agent]

        # Scanner
        self.scanner = MarketScanner(self.strategies, scan_interval_s=30)

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

    async def process_signal(self, signal: Signal):
        """Process a trading signal through risk validation and execution."""
        # Risk check
        allowed, size, reason = self.risk_manager.validate_order(
            signal,
            current_exposure=self.order_manager.get_total_exposure(),
            has_existing_position=self.order_manager.has_position_in_market(
                signal.token_id
            ),
        )

        if not allowed:
            logger.info(f"Signal blocked by risk manager: {reason}")
            return

        # Execute
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
            # Persist to DB
            self.db.save_trade({
                "id": position.id,
                "token_id": position.token_id,
                "market_question": position.market_question,
                "strategy": position.strategy,
                "side": position.side,
                "entry_price": position.entry_price,
                "size_usdc": position.size_usdc,
                "status": "OPEN",
                "opened_at": position.opened_at.isoformat(),
            })

            # Telegram notification
            await self.telegram.position_opened(
                position.market_question,
                position.side,
                position.entry_price,
                position.size_usdc,
                position.strategy,
            )

    async def check_exits_loop(self):
        """Periodically check for exit conditions on open positions."""
        while self._running:
            try:
                closed = await self.order_manager.check_exits()
                for pos in closed:
                    self.risk_manager.record_trade_result(pos.pnl_usdc)
                    self.db.close_trade(pos.id, pos.exit_price or 0, pos.pnl_usdc)

                    pnl_pct = (pos.pnl_usdc / max(pos.size_usdc, 0.01)) * 100
                    await self.telegram.position_closed(
                        pos.market_question, pos.pnl_usdc, pnl_pct, pos.status
                    )
            except Exception as e:
                logger.error(f"Exit check error: {e}")

            await asyncio.sleep(5)

    async def scan_and_trade_loop(self):
        """Continuously scan for signals and execute trades."""
        while self._running:
            try:
                signals = await self.scanner.scan_once()
                for signal in signals:
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

    async def start(self):
        """Start the bot."""
        logger.info(f"Starting Polymarket Bot v{VERSION}")
        logger.info(f"Mode: {'DRY RUN' if config.DRY_RUN else 'LIVE'}")
        self._running = True

        # Fail fast if live mode lacks credentials
        if not config.DRY_RUN and not config.POLYGON_WALLET_PRIVATE_KEY:
            raise ValueError(
                "POLYGON_WALLET_PRIVATE_KEY is required when DRY_RUN=false. "
                "Set it in .env or as an environment variable."
            )

        # Initialize balance
        if not config.DRY_RUN:
            balance = await self.bot.get_balance()
            self.risk_manager.update_capital(balance)
            logger.info(f"Balance: ${balance:.2f} USDC")

        # Initialize strategies
        await self.flash_crash.initialize()
        await self.mispricing.initialize()
        await self.ai_agent.initialize()

        # Send startup notification
        await self.telegram.bot_started(VERSION)

        # Schedule periodic tasks
        self.scheduler.add_job(
            self.refresh_flash_crash_markets, "interval", minutes=5
        )
        self.scheduler.add_job(
            self.daily_report, "cron", hour=config.DAILY_REPORT_HOUR_UTC
        )
        self.scheduler.start()

        # Run all loops concurrently
        self._tasks = [
            asyncio.create_task(self.ws.run_with_reconnect()),
            asyncio.create_task(self.scan_and_trade_loop()),
            asyncio.create_task(self.check_exits_loop()),
            asyncio.create_task(self.status_writer.run()),
        ]

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
        self.scheduler.shutdown(wait=False)

        # Cancel all running tasks so gather() exits
        for task in getattr(self, "_tasks", []):
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

        logger.info("Bot stopped")


async def main():
    orchestrator = Orchestrator()

    # Handle graceful shutdown
    loop = asyncio.get_event_loop()

    def shutdown_handler():
        asyncio.create_task(orchestrator.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    try:
        await orchestrator.start()
    except KeyboardInterrupt:
        await orchestrator.stop()
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
        logger.critical(traceback.format_exc())
        await orchestrator.telegram.critical_error(
            type(e).__name__, traceback.format_exc()[-500:]
        )
        await orchestrator.stop()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
