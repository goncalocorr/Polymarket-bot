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

                CREATE TABLE IF NOT EXISTS maker_orders (
                    order_id TEXT PRIMARY KEY,
                    quote_key TEXT NOT NULL,
                    market_id TEXT,
                    token_id TEXT NOT NULL,
                    market_question TEXT,
                    outcome TEXT,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    size_usdc REAL NOT NULL,
                    order_kind TEXT,
                    reward_max_spread REAL,
                    reward_min_size REAL,
                    reward_daily_rate REAL DEFAULT 0,
                    score_estimate REAL DEFAULT 0,
                    scoring INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'OPEN',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT,
                    close_reason TEXT,
                    fill_size_usdc REAL DEFAULT 0,
                    fill_price REAL
                );

                CREATE TABLE IF NOT EXISTS maker_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    quote_key TEXT,
                    market_id TEXT,
                    token_id TEXT NOT NULL,
                    market_question TEXT,
                    outcome TEXT,
                    side TEXT NOT NULL,
                    fill_price REAL NOT NULL,
                    fill_size_usdc REAL NOT NULL,
                    shares REAL NOT NULL,
                    realized_pnl_usdc REAL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS maker_inventory (
                    token_id TEXT PRIMARY KEY,
                    market_id TEXT,
                    market_question TEXT,
                    outcome TEXT,
                    shares REAL NOT NULL,
                    avg_entry_price REAL NOT NULL,
                    realized_pnl_usdc REAL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS maker_reward_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT,
                    token_id TEXT NOT NULL,
                    quote_key TEXT,
                    estimated_score REAL DEFAULT 0,
                    reward_daily_rate REAL DEFAULT 0,
                    scoring INTEGER DEFAULT 0,
                    recorded_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_predictions_strategy
                    ON predictions(strategy);

                CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
                CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
                CREATE INDEX IF NOT EXISTS idx_trades_opened ON trades(opened_at);
                CREATE INDEX IF NOT EXISTS idx_strat_perf_strategy
                    ON strategy_performance(strategy);
                CREATE INDEX IF NOT EXISTS idx_maker_orders_status
                    ON maker_orders(status);
                CREATE INDEX IF NOT EXISTS idx_maker_orders_quote_key
                    ON maker_orders(quote_key);
                CREATE INDEX IF NOT EXISTS idx_maker_fills_token
                    ON maker_fills(token_id);
                CREATE INDEX IF NOT EXISTS idx_maker_reward_snapshots_market
                    ON maker_reward_snapshots(market_id);
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

    def update_trade_size(self, trade_id: str, size_usdc: float):
        """Update the position size after a partial exit."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE trades SET size_usdc=? WHERE id=?",
                (size_usdc, trade_id),
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
        params: list = []
        query = ("SELECT AVG((predicted_prob - outcome) * (predicted_prob - outcome))"
                 " as brier FROM predictions WHERE outcome IS NOT NULL")
        if strategy:
            query += " AND strategy=?"
            params.append(strategy)
        with self._conn() as conn:
            row = conn.execute(query, params).fetchone()
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
        params: list = []
        query = """SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN pnl_usdc < 0 THEN 1 ELSE 0 END) as losses,
                    COALESCE(SUM(pnl_usdc), 0) as total_pnl,
                    COALESCE(AVG(pnl_usdc), 0) as avg_pnl,
                    COALESCE(MAX(pnl_usdc), 0) as best_trade,
                    COALESCE(MIN(pnl_usdc), 0) as worst_trade
                FROM trades WHERE status='CLOSED'"""
        if date_filter:
            query += " AND closed_at LIKE ?"
            params.append(f"{date_filter}%")
        with self._conn() as conn:
            row = conn.execute(query, params).fetchone()
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

    # --- Reward Market Maker ---

    def save_maker_order(self, order: dict):
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO maker_orders
                   (order_id, quote_key, market_id, token_id, market_question,
                    outcome, side, price, size_usdc, order_kind, reward_max_spread,
                    reward_min_size, reward_daily_rate, score_estimate, scoring,
                    status, created_at, updated_at, closed_at, close_reason,
                    fill_size_usdc, fill_price)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order["order_id"],
                    order["quote_key"],
                    order.get("market_id", ""),
                    order["token_id"],
                    order.get("market_question", ""),
                    order.get("outcome", ""),
                    order["side"],
                    order["price"],
                    order["size_usdc"],
                    order.get("order_kind", ""),
                    order.get("reward_max_spread"),
                    order.get("reward_min_size"),
                    order.get("reward_daily_rate", 0.0),
                    order.get("score_estimate", 0.0),
                    int(bool(order.get("scoring", False))),
                    order.get("status", "OPEN"),
                    order.get("created_at", now),
                    order.get("updated_at", now),
                    order.get("closed_at"),
                    order.get("close_reason", ""),
                    order.get("fill_size_usdc", 0.0),
                    order.get("fill_price"),
                ),
            )

    def update_maker_order(
        self,
        order_id: str,
        *,
        status: Optional[str] = None,
        scoring: Optional[bool] = None,
        score_estimate: Optional[float] = None,
        fill_size_usdc: Optional[float] = None,
        fill_price: Optional[float] = None,
        close_reason: Optional[str] = None,
    ):
        updates = []
        params: list = []

        if status is not None:
            updates.append("status=?")
            params.append(status)
        if scoring is not None:
            updates.append("scoring=?")
            params.append(int(bool(scoring)))
        if score_estimate is not None:
            updates.append("score_estimate=?")
            params.append(score_estimate)
        if fill_size_usdc is not None:
            updates.append("fill_size_usdc=?")
            params.append(fill_size_usdc)
        if fill_price is not None:
            updates.append("fill_price=?")
            params.append(fill_price)
        if close_reason is not None:
            updates.append("close_reason=?")
            params.append(close_reason)
        if status and status != "OPEN":
            updates.append("closed_at=?")
            params.append(datetime.now(timezone.utc).isoformat())

        updates.append("updated_at=?")
        params.append(datetime.now(timezone.utc).isoformat())
        params.append(order_id)

        _ALLOWED_COLUMNS = {
            "status", "scoring", "score_estimate", "fill_size_usdc",
            "fill_price", "close_reason", "closed_at", "updated_at",
        }
        for clause in updates:
            col = clause.split("=")[0]
            if col not in _ALLOWED_COLUMNS:
                raise ValueError(f"Unexpected column in UPDATE: {col}")

        with self._conn() as conn:
            conn.execute(
                "UPDATE maker_orders SET " + ", ".join(updates) + " WHERE order_id=?",
                tuple(params),
            )

    def get_open_maker_orders(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM maker_orders WHERE status='OPEN' ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def save_maker_fill(self, fill: dict):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO maker_fills
                   (order_id, quote_key, market_id, token_id, market_question,
                    outcome, side, fill_price, fill_size_usdc, shares,
                    realized_pnl_usdc, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fill["order_id"],
                    fill.get("quote_key", ""),
                    fill.get("market_id", ""),
                    fill["token_id"],
                    fill.get("market_question", ""),
                    fill.get("outcome", ""),
                    fill["side"],
                    fill["fill_price"],
                    fill["fill_size_usdc"],
                    fill["shares"],
                    fill.get("realized_pnl_usdc", 0.0),
                    fill.get("created_at", datetime.now(timezone.utc).isoformat()),
                ),
            )

    def upsert_maker_inventory(self, inventory: dict):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO maker_inventory
                   (token_id, market_id, market_question, outcome, shares,
                    avg_entry_price, realized_pnl_usdc, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(token_id) DO UPDATE SET
                       market_id=excluded.market_id,
                       market_question=excluded.market_question,
                       outcome=excluded.outcome,
                       shares=excluded.shares,
                       avg_entry_price=excluded.avg_entry_price,
                       realized_pnl_usdc=excluded.realized_pnl_usdc,
                       updated_at=excluded.updated_at""",
                (
                    inventory["token_id"],
                    inventory.get("market_id", ""),
                    inventory.get("market_question", ""),
                    inventory.get("outcome", ""),
                    inventory["shares"],
                    inventory["avg_entry_price"],
                    inventory.get("realized_pnl_usdc", 0.0),
                    inventory.get("updated_at", datetime.now(timezone.utc).isoformat()),
                ),
            )

    def delete_maker_inventory(self, token_id: str):
        with self._conn() as conn:
            conn.execute("DELETE FROM maker_inventory WHERE token_id=?", (token_id,))

    def get_maker_inventory(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM maker_inventory ORDER BY updated_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def save_maker_reward_snapshot(self, snapshot: dict):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO maker_reward_snapshots
                   (market_id, token_id, quote_key, estimated_score,
                    reward_daily_rate, scoring, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot.get("market_id", ""),
                    snapshot["token_id"],
                    snapshot.get("quote_key", ""),
                    snapshot.get("estimated_score", 0.0),
                    snapshot.get("reward_daily_rate", 0.0),
                    int(bool(snapshot.get("scoring", False))),
                    snapshot.get("recorded_at", datetime.now(timezone.utc).isoformat()),
                ),
            )

    def get_recent_maker_fills(self, limit: int = 25) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM maker_fills
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_recent_maker_reward_snapshots(self, limit: int = 100) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM maker_reward_snapshots
                   ORDER BY recorded_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_maker_summary(self) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT
                    (SELECT COUNT(*) FROM maker_orders WHERE status='OPEN') as open_orders,
                    (SELECT COUNT(*) FROM maker_orders WHERE status='FILLED') as filled_orders,
                    (SELECT COUNT(*) FROM maker_inventory) as inventory_positions,
                    COALESCE((SELECT SUM(realized_pnl_usdc) FROM maker_fills), 0) as realized_pnl_usdc,
                    COALESCE((SELECT AVG(estimated_score) FROM maker_reward_snapshots), 0) as avg_estimated_score,
                    COALESCE((SELECT SUM(scoring) FROM maker_orders WHERE status='OPEN'), 0) as scoring_open_orders
                """
            ).fetchone()
            return dict(row) if row else {}
