"""
Terminal TUI dashboard (using rich) and status.json writer for the web dashboard.
"""

import json
import asyncio
from datetime import datetime, timezone

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from loguru import logger

import config
from execution.order_manager import OrderManager
from risk.risk_manager import RiskManager
from storage.database import Database
from strategies.base import BaseStrategy


class StatusWriter:
    """Writes status.json every 60s for the web dashboard."""

    def __init__(
        self,
        order_manager: OrderManager,
        risk_manager: RiskManager,
        db: Database,
        strategies: list[BaseStrategy],
    ):
        self.order_manager = order_manager
        self.risk_manager = risk_manager
        self.db = db
        self.strategies = strategies
        self._running = False

    def _build_status(self) -> dict:
        now = datetime.now(timezone.utc)
        open_positions = self.order_manager.get_open_positions()
        recent_trades = self.db.get_recent_trades(10)
        pnl_today = self.db.get_today_pnl()
        updated_at = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")

        return {
            "updated_at": updated_at,
            "mode": "DRY_RUN" if config.DRY_RUN else "LIVE",
            "is_simulated": config.DRY_RUN,
            "pnl_mode": "simulated_mark_to_mid" if config.DRY_RUN else "realized",
            "status": self.risk_manager.status,
            "capital_usdc": round(self.risk_manager.total_capital, 2),
            "pnl_today_usdc": round(pnl_today, 2),
            "pnl_today_pct": round(
                (pnl_today / max(self.risk_manager.total_capital, 1)) * 100,
                2,
            ),
            "pnl_total_usdc": round(self.db.get_total_pnl(), 2),
            "open_positions": [
                {
                    "market": p.market_question[:100],
                    "side": p.side,
                    "entry_price": p.entry_price,
                    "current_price": round(
                        self.order_manager.ws.get_mid_price(p.token_id) or p.entry_price,
                        4,
                    ),
                    "size_usdc": round(p.size_usdc, 2),
                    "unrealised_pnl_usdc": round(
                        p.current_pnl(
                            self.order_manager.ws.get_mid_price(p.token_id) or p.entry_price
                        ),
                        2,
                    ),
                }
                for p in open_positions
            ],
            "recent_trades": [
                {
                    "market": t.get("market_question", "")[:100],
                    "side": t.get("side", ""),
                    "entry_price": t.get("entry_price", 0),
                    "exit_price": t.get("exit_price", 0),
                    "pnl_usdc": round(t.get("pnl_usdc", 0), 2),
                    "close_reason": t.get("close_reason", ""),
                    "closed_at": t.get("closed_at", ""),
                }
                for t in recent_trades
            ],
            "strategies": {
                s.name: s.state.value for s in self.strategies
            },
        }

    async def write_status(self):
        """Write status.json to disk."""
        try:
            status = self._build_status()
            path = config.STATUS_JSON_PATH
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                json.dump(status, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to write status.json: {e}")

    async def run(self, interval_s: float = config.STATUS_UPDATE_INTERVAL_S):
        """Periodically write status.json."""
        self._running = True
        while self._running:
            await self.write_status()
            await asyncio.sleep(interval_s)

    def stop(self):
        self._running = False


class TerminalDashboard:
    """Rich-based terminal dashboard."""

    def __init__(
        self,
        order_manager: OrderManager,
        risk_manager: RiskManager,
        db: Database,
        strategies: list[BaseStrategy],
    ):
        self.order_manager = order_manager
        self.risk_manager = risk_manager
        self.db = db
        self.strategies = strategies
        self.console = Console()

    def _make_header(self) -> Panel:
        mode = "DRY RUN" if config.DRY_RUN else "LIVE"
        status = self.risk_manager.status
        colors = {"RUNNING": "green", "STOPPED": "red", "PAUSED": "yellow"}
        color = colors.get(status, "white")

        text = Text()
        text.append("Polymarket Bot", style="bold white")
        text.append(f"  [{mode}]", style="cyan")
        text.append(f"  {status}", style=f"bold {color}")
        text.append(
            f"  {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}",
            style="dim",
        )
        return Panel(text, style="bold blue")

    def _make_capital(self) -> Panel:
        capital = self.risk_manager.total_capital
        day_pnl = self.db.get_today_pnl()
        total_pnl = self.db.get_total_pnl()
        day_pct = (day_pnl / max(capital, 1)) * 100  # single query, reused below
        today_label = "Simulated P&L Today" if config.DRY_RUN else "Today P&L"
        total_label = "Simulated Total P&L" if config.DRY_RUN else "Total P&L"

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Label", style="dim")
        table.add_column("Value", justify="right")

        table.add_row("Capital", f"${capital:.2f}")

        pnl_style = "green" if day_pnl >= 0 else "red"
        table.add_row(
            today_label,
            Text(f"${day_pnl:+.2f} ({day_pct:+.1f}%)", style=pnl_style),
        )

        total_style = "green" if total_pnl >= 0 else "red"
        table.add_row(
            total_label,
            Text(f"${total_pnl:+.2f}", style=total_style),
        )

        exposure = self.order_manager.get_total_exposure()
        table.add_row("Exposure", f"${exposure:.2f}")

        return Panel(table, title="Capital", border_style="green")

    def _make_positions(self) -> Panel:
        positions = self.order_manager.get_open_positions()

        table = Table(show_header=True, header_style="bold")
        table.add_column("Market", max_width=40)
        table.add_column("Side", width=4)
        table.add_column("Entry", justify="right", width=7)
        table.add_column("Current", justify="right", width=7)
        table.add_column(
            "Unrealised P&L*" if config.DRY_RUN else "P&L",
            justify="right",
            width=14,
        )

        if not positions:
            table.add_row("No open positions", "", "", "", "")
        else:
            for p in positions:
                current = self.order_manager.ws.get_mid_price(p.token_id) or p.entry_price
                pnl = p.current_pnl(current)
                style = "green" if pnl >= 0 else "red"
                table.add_row(
                    p.market_question[:40],
                    p.side,
                    f"{p.entry_price:.3f}",
                    f"{current:.3f}",
                    Text(f"${pnl:+.2f}", style=style),
                )

        return Panel(table, title="Open Positions", border_style="cyan")

    def _make_trades(self) -> Panel:
        trades = self.db.get_recent_trades(10)

        table = Table(show_header=True, header_style="bold")
        table.add_column("Market", max_width=35)
        table.add_column("P&L", justify="right", width=10)
        table.add_column("Strategy", width=12)

        if not trades:
            table.add_row("No closed trades", "", "")
        else:
            for t in trades:
                pnl = t.get("pnl_usdc", 0)
                style = "green" if pnl >= 0 else "red"
                table.add_row(
                    t.get("market_question", "")[:35],
                    Text(f"${pnl:+.2f}", style=style),
                    t.get("strategy", ""),
                )

        return Panel(table, title="Recent Trades", border_style="yellow")

    def _make_strategies(self) -> Panel:
        table = Table(show_header=False, box=None)
        table.add_column("Strategy", width=15)
        table.add_column("Status", width=10)

        colors = {"ACTIVE": "green", "PAUSED": "yellow", "DISABLED": "red"}
        for s in self.strategies:
            color = colors.get(s.state.value, "white")
            table.add_row(s.name, Text(s.state.value, style=color))

        return Panel(table, title="Strategies", border_style="magenta")

    def render(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(self._make_header(), size=3),
            Layout(name="body"),
        )
        layout["body"].split_row(
            Layout(name="left"),
            Layout(name="right"),
        )
        layout["left"].split_column(
            Layout(self._make_capital(), size=8),
            Layout(self._make_positions()),
        )
        layout["right"].split_column(
            Layout(self._make_strategies(), size=8),
            Layout(self._make_trades()),
        )
        return layout

    async def run(self, refresh_rate: float = 2.0):
        """Run the live terminal dashboard."""
        self._running = True
        with Live(console=self.console, refresh_per_second=1 / refresh_rate) as live:
            while self._running:
                live.update(self.render())
                await asyncio.sleep(refresh_rate)

    def stop(self):
        self._running = False
