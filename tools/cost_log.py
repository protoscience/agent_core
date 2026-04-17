import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "logs" / "cost.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    ts TEXT NOT NULL,
    channel TEXT NOT NULL,
    peer TEXT NOT NULL,
    turns INTEGER NOT NULL,
    cost_usd REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts);

CREATE TABLE IF NOT EXISTS daily (
    date TEXT NOT NULL,
    channel TEXT NOT NULL,
    peer TEXT NOT NULL,
    turns INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    PRIMARY KEY (date, channel, peer)
);

CREATE TABLE IF NOT EXISTS weekly (
    week_start TEXT NOT NULL,
    channel TEXT NOT NULL,
    peer TEXT NOT NULL,
    turns INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    PRIMARY KEY (week_start, channel, peer)
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def log_turn(channel: str, peer: str, turns: int, cost_usd: float) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO turns (ts, channel, peer, turns, cost_usd) VALUES (?, ?, ?, ?, ?)",
            (ts, channel, str(peer), int(turns or 0), float(cost_usd or 0.0)),
        )
        conn.commit()
    finally:
        conn.close()
