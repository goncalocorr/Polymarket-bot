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
                    take_profit REAL,
                    stop_loss REAL,
                    pnl_usdc REAL DEFAULT 0,
                    status TEXT DEFAULT 'OPEN',
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    close_reason TEXT
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

                CREATE TABLE IF NOT EXISTS predictions (
                    id TEXT PRIMARY KEY,
                    token_id TEXT NOT NULL,
                    market_question TEXT,
                    strategy TEXT NOT NULL,
                    predicted_prob REAL NOT NULL,
                    market_price REAL NOT NULL,
                    confidence REAL DEFAULT 0,
                    outcome INTEGER,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS strategy_performance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy TEXT NOT NULL,
                    pnl_usdc REAL NOT NULL,
                    won INTEGER NOT NULL,
                    recorded_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_predictions_strategy
                    ON predictions(strategy);

                CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
                CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
                CREATE INDEX IF NOT EXISTS idx_trades_opened ON trades(opened_at);
                CREATE INDEX IF NOT EXISTS idx_strat_perf_strategy
                    ON strategy_performance(strategy);
            """)
            self._ensure_trade_columns(conn)

    @staticmethod
    def _ensure_trade_columns(conn: sqlite3.Connection):
        """Online migration for older DBs missing newer trade columns."""
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        if "take_profit" not in cols:
            conn.execute("ALTER TABLE trades ADD COLUMN take_profit REAL")
        if "stop_loss" not in cols:
            conn.execute("ALTER TABLE trades ADD COLUMN stop_loss REAL")
        if "close_reason" not in cols:
            conn.execute("ALTER TABLE trades ADD COLUMN close_reason TEXT")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        # WAL mode allows concurrent readers while writing, preventing
        # "database is locked" errors from multiple coroutines.
        conn.execute("PRAGMA journal_mode=WAL")
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
                    entry_price, exit_price, size_usdc, take_profit, stop_loss, pnl_usdc,
                    status, opened_at, closed_at, close_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade["id"],
                    trade["token_id"],
                    trade.get("market_question", ""),
                    trade["strategy"],
                    trade["side"],
                    trade["entry_price"],
                    trade.get("exit_price"),
                    trade["size_usdc"],
                    trade.get("take_profit"),
                    trade.get("stop_loss"),
                    trade.get("pnl_usdc", 0),
                    trade.get("status", "OPEN"),
                    trade["opened_at"],
                    trade.get("closed_at"),
                    trade.get("close_reason", ""),
                ),
            )

    def close_trade(
        self,
        trade_id: str,
        exit_price: float,
        pnl: float,
        close_reason: str = "",
    ):
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """UPDATE trades SET exit_price=?, pnl_usdc=?, status='CLOSED',
                   closed_at=?, close_reason=? WHERE id=?""",
                (exit_price, pnl, now, close_reason, trade_id),
            )

    def update_trade_risk_levels(
        self,
        trade_id: str,
        take_profit: Optional[float],
        stop_loss: Optional[float],
    ):
        with self._conn() as conn:
            conn.execute(
                "UPDATE trades SET take_profit=?, stop_loss=? WHERE id=?",
                (take_profit, stop_loss, trade_id),
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

    def save_prediction(self, prediction: dict):
        """Save a probability prediction for later calibration analysis."""
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO predictions
                   (id, token_id, market_question, strategy,
                    predicted_prob, market_price, confidence, outcome, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    prediction["id"],
                    prediction["token_id"],
                    prediction.get("market_question", ""),
                    prediction["strategy"],
                    prediction["predicted_prob"],
                    prediction["market_price"],
                    prediction.get("confidence", 0),
                    prediction.get("outcome"),
                    prediction["created_at"],
                ),
            )

    def resolve_prediction(self, prediction_id: str, outcome: int):
        """Mark a prediction as resolved (1 = YES, 0 = NO)."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE predictions SET outcome=? WHERE id=?",
                (outcome, prediction_id),
            )

    def get_brier_score(self, strategy: str = "") -> Optional[float]:
        """
        Compute Brier score for resolved predictions.
        Brier = mean((predicted_prob - outcome)^2).
        Lower is better; 0.25 = random, 0.0 = perfect.
        """
        where = "WHERE outcome IS NOT NULL"
        params: tuple = ()
        if strategy:
            where += " AND strategy=?"
            params = (strategy,)
        with self._conn() as conn:
            row = conn.execute(
                f"""SELECT AVG((predicted_prob - outcome) * (predicted_prob - outcome))
                    as brier FROM predictions {where}""",
                params,
            ).fetchone()
            return row["brier"] if row and row["brier"] is not None else None

    def get_calibration_buckets(self, bucket_size: float = 0.1) -> list[dict]:
        """
        Get win rate by predicted probability bucket for calibration analysis.
        Returns list of {bucket, count, wins, actual_rate, predicted_avg}.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT
                    CAST(CAST(predicted_prob / ? AS INTEGER) * ? AS REAL) as bucket,
                    COUNT(*) as count,
                    SUM(outcome) as wins,
                    AVG(CAST(outcome AS REAL)) as actual_rate,
                    AVG(predicted_prob) as predicted_avg
                FROM predictions
                WHERE outcome IS NOT NULL
                GROUP BY bucket
                ORDER BY bucket""",
                (bucket_size, bucket_size),
            ).fetchall()
            return [dict(r) for r in rows]

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

    # --- Strategy Performance ---

    def record_strategy_trade(self, strategy: str, pnl: float, won: int):
        """Record a trade result for strategy performance tracking."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO strategy_performance
                   (strategy, pnl_usdc, won, recorded_at)
                   VALUES (?, ?, ?, ?)""",
                (strategy, pnl, won, now),
            )

    def get_strategy_rolling_stats(
        self, strategy: str, window: int = 20
    ) -> dict:
        """
        Get rolling performance stats for a strategy over the last N trades.
        Returns {total_trades, wins, losses, win_rate, total_pnl, avg_pnl}.
        """
        with self._conn() as conn:
            row = conn.execute(
                """SELECT
                    COUNT(*) as total_trades,
                    COALESCE(SUM(won), 0) as wins,
                    COUNT(*) - COALESCE(SUM(won), 0) as losses,
                    CASE WHEN COUNT(*) > 0
                        THEN CAST(SUM(won) AS REAL) / COUNT(*)
                        ELSE 0.5
                    END as win_rate,
                    COALESCE(SUM(pnl_usdc), 0) as total_pnl,
                    COALESCE(AVG(pnl_usdc), 0) as avg_pnl
                FROM (
                    SELECT pnl_usdc, won
                    FROM strategy_performance
                    WHERE strategy = ?
                    ORDER BY id DESC
                    LIMIT ?
                )""",
                (strategy, window),
            ).fetchone()
            return dict(row) if row else {
                "total_trades": 0, "wins": 0, "losses": 0,
                "win_rate": 0.5, "total_pnl": 0, "avg_pnl": 0,
            }
