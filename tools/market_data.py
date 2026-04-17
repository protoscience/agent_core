import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

ET = ZoneInfo("America/New_York")


_client: StockHistoricalDataClient | None = None


def _get_client() -> StockHistoricalDataClient:
    global _client
    if _client is None:
        _client = StockHistoricalDataClient(
            api_key=os.environ["ALPACA_API_KEY"],
            secret_key=os.environ["ALPACA_SECRET_KEY"],
        )
    return _client


def get_latest_quote(symbol: str) -> dict:
    req = StockLatestQuoteRequest(symbol_or_symbols=symbol.upper())
    quotes = _get_client().get_stock_latest_quote(req)
    q = quotes[symbol.upper()]
    return {
        "symbol": symbol.upper(),
        "bid_price": float(q.bid_price),
        "ask_price": float(q.ask_price),
        "bid_size": int(q.bid_size),
        "ask_size": int(q.ask_size),
        "timestamp": q.timestamp.isoformat(),
    }


def get_recent_bars(symbol: str, days: int = 30, timeframe: str = "1Day") -> list[dict]:
    tf_map = {
        "1Min": TimeFrame.Minute,
        "5Min": TimeFrame(5, TimeFrame.Minute.unit),
        "15Min": TimeFrame(15, TimeFrame.Minute.unit),
        "1Hour": TimeFrame.Hour,
        "1Day": TimeFrame.Day,
    }
    tf = tf_map.get(timeframe, TimeFrame.Day)

    req = StockBarsRequest(
        symbol_or_symbols=symbol.upper(),
        timeframe=tf,
        start=datetime.now(timezone.utc) - timedelta(days=days),
    )
    bars = _get_client().get_stock_bars(req)
    out = []
    for b in bars[symbol.upper()]:
        out.append({
            "t": b.timestamp.strftime("%m/%d"),
            "o": round(float(b.open), 2),
            "h": round(float(b.high), 2),
            "l": round(float(b.low), 2),
            "c": round(float(b.close), 2),
            "v": int(b.volume),
        })
    return out


def _classify_session(now_et: datetime) -> str:
    if now_et.weekday() >= 5:
        return "weekend"
    hhmm = now_et.hour * 100 + now_et.minute
    if 400 <= hhmm < 930:
        return "pre-market"
    if 930 <= hhmm < 1600:
        return "regular"
    if 1600 <= hhmm < 2000:
        return "after-hours"
    return "closed"


def get_premarket_snapshot(symbol: str) -> dict:
    """
    Pre-market snapshot for a US equity.
    Returns session, previous RTH close, today's pre-market stats,
    latest quote, and gap % vs previous close.
    """
    sym = symbol.upper()
    client = _get_client()
    now_et = datetime.now(ET)
    session = _classify_session(now_et)

    # --- previous RTH close (from daily bars, pick the most recent bar strictly before today)
    daily = client.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=sym,
        timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=10),
    ))
    day_bars = daily[sym] if sym in daily.data else []
    today_et_date = now_et.date()
    prev_close = None
    prev_close_date = None
    for b in reversed(day_bars):
        bar_date = b.timestamp.astimezone(ET).date()
        if bar_date < today_et_date:
            prev_close = round(float(b.close), 2)
            prev_close_date = bar_date.isoformat()
            break
    if prev_close is None and day_bars:
        prev_close = round(float(day_bars[-1].close), 2)
        prev_close_date = day_bars[-1].timestamp.astimezone(ET).date().isoformat()

    # --- today's pre-market bars (04:00–09:30 ET)
    pm_start_et = now_et.replace(hour=4, minute=0, second=0, microsecond=0)
    pm_end_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    pm_resp = client.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=sym,
        timeframe=TimeFrame.Minute,
        start=pm_start_et.astimezone(timezone.utc),
        end=pm_end_et.astimezone(timezone.utc),
    ))
    pm_bars = pm_resp[sym] if sym in pm_resp.data else []

    if pm_bars:
        pm = {
            "bars": len(pm_bars),
            "first": round(float(pm_bars[0].open), 2),
            "last": round(float(pm_bars[-1].close), 2),
            "high": round(max(float(b.high) for b in pm_bars), 2),
            "low": round(min(float(b.low) for b in pm_bars), 2),
            "volume": int(sum(b.volume for b in pm_bars)),
            "first_bar_et": pm_bars[0].timestamp.astimezone(ET).strftime("%H:%M"),
            "last_bar_et": pm_bars[-1].timestamp.astimezone(ET).strftime("%H:%M"),
        }
    else:
        pm = {"bars": 0}

    # --- latest quote (works outside RTH too)
    try:
        q = client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=sym))[sym]
        quote = {
            "bid": round(float(q.bid_price), 2),
            "ask": round(float(q.ask_price), 2),
            "ts_et": q.timestamp.astimezone(ET).strftime("%m/%d %H:%M"),
        }
    except Exception:
        quote = None

    # --- gap vs previous close
    ref = pm.get("last") if pm.get("last") is not None else (quote and quote["bid"])
    gap_pct = round((ref - prev_close) / prev_close * 100, 2) if (ref and prev_close) else None

    return {
        "symbol": sym,
        "session": session,
        "now_et": now_et.strftime("%Y-%m-%d %H:%M"),
        "prev_close": prev_close,
        "prev_close_date": prev_close_date,
        "premarket": pm,
        "quote": quote,
        "gap_pct": gap_pct,
    }
