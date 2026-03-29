"""
SQLite storage for positions, trades, and daily P&L.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

import config

DB_PATH = config.DATA_DIR / "bot.db"


class Database:
    """SQLite database for persisting bot state."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    id TEXT PRIMARY KEY,
                    token_id TEXT NOT NULL,
                    market_question TEXT,
                    strategy TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    size_usdc REAL NOT NULL,
                    pnl_usdc REAL DEFAULT 0,
                    status TEXT DEFAULT 'OPEN',
                    opened_at TEXT NOT NULL,
                    closed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS daily_pnl (
                    date TEXT PRIMARY KEY,
                    starting_capital REAL,
                    ending_capital REAL,
                    pnl_usdc REAL DEFAULT 0,
                    trades_count INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
                CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
                CREATE INDEX IF NOT EXISTS idx_trades_opened ON trades(opened_at);
            """)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def save_trade(self, trade: dict):
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO trades
                   (id, token_id, market_question, strategy, side,
                    entry_price, exit_price, size_usdc, pnl_usdc,
                    status, opened_at, closed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade["id"],
                    trade["token_id"],
                    trade.get("market_question", ""),
                    trade["strategy"],
                    trade["side"],
                    trade["entry_price"],
                    trade.get("exit_price"),
                    trade["size_usdc"],
                    trade.get("pnl_usdc", 0),
                    trade.get("status", "OPEN"),
                    trade["opened_at"],
                    trade.get("closed_at"),
                ),
            )

    def close_trade(self, trade_id: str, exit_price: float, pnl: float):
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """UPDATE trades SET exit_price=?, pnl_usdc=?, status='CLOSED',
                   closed_at=? WHERE id=?""",
                (exit_price, pnl, now, trade_id),
            )

    def get_open_trades(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status='OPEN' ORDER BY opened_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_recent_trades(self, limit: int = 10) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM trades WHERE status='CLOSED'
                   ORDER BY closed_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_today_pnl(self) -> float:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl_usdc), 0) as total FROM trades "
                "WHERE closed_at LIKE ? AND status='CLOSED'",
                (f"{today}%",),
            ).fetchone()
            return row["total"] if row else 0.0

    def get_total_pnl(self) -> float:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl_usdc), 0) as total FROM trades "
                "WHERE status='CLOSED'"
            ).fetchone()
            return row["total"] if row else 0.0

    def save_daily_summary(
        self,
        date: str,
        starting_capital: float,
        ending_capital: float,
        pnl: float,
        trades_count: int,
        wins: int,
        losses: int,
    ):
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO daily_pnl
                   (date, starting_capital, ending_capital, pnl_usdc,
                    trades_count, wins, losses)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (date, starting_capital, ending_capital, pnl, trades_count, wins, losses),
            )

    def save_state(self, key: str, value: str):
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bot_state (key, value, updated_at) "
                "VALUES (?, ?, ?)",
                (key, value, now),
            )

    def get_state(self, key: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM bot_state WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def get_trade_stats(self, date_filter: str = "") -> dict:
        """Get trading statistics, optionally filtered by date (YYYY-MM-DD)."""
        where = "WHERE status='CLOSED'"
        params = ()
        if date_filter:
            where += " AND closed_at LIKE ?"
            params = (f"{date_filter}%",)
        with self._conn() as conn:
            row = conn.execute(
                f"""SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN pnl_usdc < 0 THEN 1 ELSE 0 END) as losses,
                    COALESCE(SUM(pnl_usdc), 0) as total_pnl,
                    COALESCE(AVG(pnl_usdc), 0) as avg_pnl,
                    COALESCE(MAX(pnl_usdc), 0) as best_trade,
                    COALESCE(MIN(pnl_usdc), 0) as worst_trade
                FROM trades {where}""",
                params,
            ).fetchone()
            return dict(row) if row else {}
