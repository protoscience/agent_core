"""CLI cost summary. Reads raw `turns` + rolled-up `daily` tables."""
from datetime import datetime, timedelta, timezone

from tools.cost_log import connect


def _spend_between(conn, start_iso: str, end_iso: str) -> dict:
    raw = conn.execute(
        "SELECT channel, SUM(cost_usd) FROM turns WHERE ts >= ? AND ts < ? GROUP BY channel",
        (start_iso, end_iso),
    ).fetchall()
    daily = conn.execute(
        "SELECT channel, SUM(cost_usd) FROM daily WHERE date >= ? AND date < ? GROUP BY channel",
        (start_iso[:10], end_iso[:10]),
    ).fetchall()
    out = {}
    for ch, cost in raw + daily:
        out[ch] = out.get(ch, 0.0) + (cost or 0.0)
    return out


def main() -> None:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    windows = [
        ("Today", today_start, now + timedelta(seconds=1)),
        ("7 days", now - timedelta(days=7), now + timedelta(seconds=1)),
        ("30 days", now - timedelta(days=30), now + timedelta(seconds=1)),
    ]

    conn = connect()
    try:
        for label, start, end in windows:
            by_channel = _spend_between(conn, start.isoformat(), end.isoformat())
            total = sum(by_channel.values())
            parts = ", ".join(f"{ch} ${v:.4f}" for ch, v in sorted(by_channel.items())) or "—"
            print(f"{label:<8} ${total:>8.4f}   ({parts})")

        # Top peers over 30 days (combined raw + daily)
        start_date = (now - timedelta(days=30)).date().isoformat()
        rows = conn.execute(
            """
            SELECT channel, peer, SUM(c) AS cost FROM (
                SELECT channel, peer, SUM(cost_usd) AS c FROM turns
                  WHERE date(ts) >= ? GROUP BY channel, peer
                UNION ALL
                SELECT channel, peer, SUM(cost_usd) AS c FROM daily
                  WHERE date >= ? GROUP BY channel, peer
            ) GROUP BY channel, peer ORDER BY cost DESC LIMIT 10
            """,
            (start_date, start_date),
        ).fetchall()
        if rows:
            print("\nTop peers (30d):")
            for ch, peer, cost in rows:
                print(f"  {ch:<8} {peer:<24} ${cost:.4f}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
