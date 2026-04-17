"""Nightly rollup: raw turns -> daily, daily -> weekly, then prune.

Raw turns older than today (UTC) are summed into `daily` and deleted.
`weekly` is rebuilt from `daily` each run (cheap at our scale).
`daily` rows older than DAILY_RETENTION_DAYS are dropped.
"""
from datetime import datetime, timedelta, timezone

from tools.cost_log import connect

DAILY_RETENTION_DAYS = 365


def _rollup_raw_to_daily(conn) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    cur = conn.execute(
        """
        INSERT INTO daily (date, channel, peer, turns, cost_usd)
        SELECT date(ts), channel, peer, SUM(turns), SUM(cost_usd)
        FROM turns
        WHERE date(ts) < ?
        GROUP BY date(ts), channel, peer
        ON CONFLICT(date, channel, peer) DO UPDATE SET
            turns = daily.turns + excluded.turns,
            cost_usd = daily.cost_usd + excluded.cost_usd
        """,
        (today,),
    )
    rolled = cur.rowcount
    conn.execute("DELETE FROM turns WHERE date(ts) < ?", (today,))
    return rolled


def _rebuild_weekly(conn) -> None:
    conn.execute("DELETE FROM weekly")
    # strftime('%w'): Sunday=0..Saturday=6. Monday-based week_start:
    #   offset_days = (w + 6) % 7  (Mon→0, Tue→1, ..., Sun→6)
    conn.execute(
        """
        INSERT INTO weekly (week_start, channel, peer, turns, cost_usd)
        SELECT
            date(date, '-' || ((CAST(strftime('%w', date) AS INTEGER) + 6) % 7) || ' days') AS week_start,
            channel, peer, SUM(turns), SUM(cost_usd)
        FROM daily
        GROUP BY week_start, channel, peer
        """
    )


def _prune_daily(conn) -> int:
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=DAILY_RETENTION_DAYS)).isoformat()
    cur = conn.execute("DELETE FROM daily WHERE date < ?", (cutoff,))
    return cur.rowcount


def main() -> None:
    conn = connect()
    try:
        rolled = _rollup_raw_to_daily(conn)
        _rebuild_weekly(conn)
        pruned = _prune_daily(conn)
        conn.commit()
        print(f"cost-rollup: raw→daily groups={rolled}, weekly rebuilt, daily pruned={pruned}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
