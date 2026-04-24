"""SQLite log of every auto-trade decision — submitted, dry-run, or blocked.

Mirrors the design of tools/cost_log.py: a small schema, best-effort writes
that never raise into the caller, and a separate DB file so the trading
audit trail is independent of cost/token accounting.
"""
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "logs" / "trades.db"

log = logging.getLogger("trade_log")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    limit_price REAL,
    stop_loss_price REAL,
    status TEXT NOT NULL,
    reason TEXT,
    order_id TEXT,
    estimated_cost REAL,
    account_equity REAL,
    pnl_today_pct REAL
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_source ON trades(source);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def record(
    *,
    source: str,
    symbol: str,
    side: str,
    qty: int,
    order_type: str,
    status: str,
    limit_price: float | None = None,
    stop_loss_price: float | None = None,
    reason: str | None = None,
    order_id: str | None = None,
    estimated_cost: float | None = None,
    account_equity: float | None = None,
    pnl_today_pct: float | None = None,
) -> None:
    """Record one trade decision. Best-effort; never raises."""
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        conn = connect()
        try:
            conn.execute(
                """INSERT INTO trades (
                    ts, source, symbol, side, qty, order_type, limit_price,
                    stop_loss_price, status, reason, order_id,
                    estimated_cost, account_equity, pnl_today_pct
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts, source, symbol.upper(), side.lower(), int(qty),
                    order_type.lower(), limit_price, stop_loss_price,
                    status, reason, order_id,
                    estimated_cost, account_equity, pnl_today_pct,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        log.exception(f"trade_log.record failed for {symbol}/{source}")
