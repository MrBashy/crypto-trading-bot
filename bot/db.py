"""SQLite storage for positions, trades, orders, equity history and the activity log."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    mode              TEXT NOT NULL,
    exchange          TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    amount            REAL NOT NULL,
    entry_price       REAL NOT NULL,
    cost              REAL NOT NULL,   -- quote currency spent, including fees
    entry_fee         REAL NOT NULL,
    entry_time        REAL NOT NULL,
    stop_price        REAL NOT NULL,
    take_profit_price REAL,            -- NULL when take-profit is off
    highest_price     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mode        TEXT NOT NULL,
    exchange    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    amount      REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price  REAL NOT NULL,
    entry_time  REAL NOT NULL,
    exit_time   REAL NOT NULL,
    cost        REAL NOT NULL,
    proceeds    REAL NOT NULL,
    fees        REAL NOT NULL,
    pnl         REAL NOT NULL,
    pnl_pct     REAL NOT NULL,
    exit_reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    mode              TEXT NOT NULL,
    exchange          TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    amount            REAL NOT NULL,
    price             REAL NOT NULL,
    cost              REAL NOT NULL,
    fee               REAL NOT NULL,
    time              REAL NOT NULL,
    reason            TEXT NOT NULL,
    exchange_order_id TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    time   REAL NOT NULL,
    mode   TEXT NOT NULL,
    equity REAL NOT NULL,
    cash   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equity_mode_time ON equity(mode, time);
CREATE INDEX IF NOT EXISTS idx_orders_mode_symbol ON orders(mode, symbol, time);
CREATE TABLE IF NOT EXISTS logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    time    REAL NOT NULL,
    level   TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @contextmanager
    def tx(self):
        """Run several statements atomically (all or nothing)."""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- key/value ----------
    def get(self, key: str, default=None):
        rows = self.query("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set(self, key: str, value, conn: sqlite3.Connection | None = None) -> None:
        sql = "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        if conn is not None:
            conn.execute(sql, (key, json.dumps(value)))
        else:
            with self.tx() as c:
                c.execute(sql, (key, json.dumps(value)))

    def delete(self, key: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM kv WHERE key = ?", (key,))

    # ---------- logs ----------
    def log(self, level: str, message: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO logs(time, level, message) VALUES(?, ?, ?)", (time.time(), level, message))
            # Keep the log table small.
            c.execute("DELETE FROM logs WHERE id <= (SELECT MAX(id) FROM logs) - 5000")

    def recent_logs(self, limit: int = 100) -> list[dict]:
        return self.query("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,))

    # ---------- positions ----------
    def open_positions(self, mode: str) -> list[dict]:
        return self.query("SELECT * FROM positions WHERE mode = ? ORDER BY entry_time", (mode,))

    def get_position(self, position_id: int) -> dict | None:
        rows = self.query("SELECT * FROM positions WHERE id = ?", (position_id,))
        return rows[0] if rows else None

    def update_highest_price(self, position_id: int, price: float) -> None:
        with self.tx() as c:
            c.execute("UPDATE positions SET highest_price = ? WHERE id = ? AND highest_price < ?",
                      (price, position_id, price))

    # ---------- trades / orders / equity ----------
    def trades(self, mode: str, limit: int = 100) -> list[dict]:
        return self.query("SELECT * FROM trades WHERE mode = ? ORDER BY exit_time DESC LIMIT ?", (mode, limit))

    def trade_stats(self, mode: str) -> dict:
        row = self.query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(pnl), 0) AS pnl, "
            "COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins FROM trades WHERE mode = ?",
            (mode,),
        )[0]
        n = row["n"]
        return {"count": n, "total_pnl": row["pnl"], "wins": row["wins"],
                "win_rate": (row["wins"] / n * 100) if n else None}

    def orders_for_symbol(self, mode: str, symbol: str, since: float) -> list[dict]:
        return self.query(
            "SELECT * FROM orders WHERE mode = ? AND symbol = ? AND time >= ? ORDER BY time",
            (mode, symbol, since),
        )

    def add_equity_point(self, mode: str, equity: float, cash: float) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO equity(time, mode, equity, cash) VALUES(?, ?, ?, ?)",
                      (time.time(), mode, equity, cash))

    def equity_history(self, mode: str, since: float, max_points: int = 1500) -> list[dict]:
        rows = self.query("SELECT time, equity FROM equity WHERE mode = ? AND time >= ? ORDER BY time",
                          (mode, since))
        if len(rows) > max_points:
            step = len(rows) / max_points
            rows = [rows[int(i * step)] for i in range(max_points)] + [rows[-1]]
        return rows

    def clear_mode_history(self, mode: str) -> None:
        """Wipe trades, orders and equity history for a mode (used by paper reset)."""
        with self.tx() as c:
            for table in ("trades", "orders", "equity", "positions"):
                c.execute(f"DELETE FROM {table} WHERE mode = ?", (mode,))
