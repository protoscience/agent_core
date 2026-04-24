"""Gated auto-execution layer for SuperSonic paper-trading.

Every order from every source (LLM scan, strategies, webhooks, manual
Discord commands) goes through `execute_order` and its risk rails.
Rails are enforced in code, not in the agent prompt.

See docs/decisions/auto-trading.md for the decision rationale.
"""
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
)

from tools import market_data, trade_log


# ── CONFIG ─────────────────────────────────────────────────────────────────

# Static watchlist — locked via docs/decisions/auto-trading.md
STATIC_WATCHLIST: set[str] = {
    "SPY", "QQQ", "TQQQ",
    "NVDA", "AMD", "AMZN", "AAPL", "MSFT", "META", "GOOGL", "ARM", "INTC",
}

# Mag7 — ride without auto-stop (Boss's call)
MAG7: set[str] = {"AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA"}

# Risk rails — tuned via docs/decisions/auto-trading.md
DOLLAR_PER_TRADE = 1000.0           # fixed position size
MAX_OPEN_POSITIONS = 5
DAILY_LOSS_PCT_KILL = -3.0          # kill-switch trip %
STOP_LOSS_PCT = 0.05                # 5% — applied to non-mag7 only
PER_SOURCE_DAILY_MAX_TRADES = 10

# Time windows (ET)
NO_TRADE_AT_OPEN_MIN = 5            # skip first 5 min after 9:30 open
NO_NEW_LONGS_BEFORE_CLOSE_MIN = 15  # skip last 15 min

ET = ZoneInfo("America/New_York")

KILL_SWITCH = Path(__file__).resolve().parent.parent / "logs" / "kill-switch"

# Dry-run flag — when true, every order is logged but NOT submitted to Alpaca.
# Default on. Set AUTO_TRADE_DRY_RUN=false in .env to go live paper.
def _dry_run() -> bool:
    return os.environ.get("AUTO_TRADE_DRY_RUN", "true").lower() != "false"


log = logging.getLogger("auto_trade")


# ── TYPES ──────────────────────────────────────────────────────────────────

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
Source = str  # "llm-scan" | "strategy-<name>" | "webhook-<name>" | "manual-discord"


@dataclass
class OrderIntent:
    symbol: str
    side: Side
    source: Source
    reason: str
    order_type: OrderType = "market"
    limit_price: float | None = None
    # Callers may omit qty; we size to DOLLAR_PER_TRADE automatically.
    qty: int | None = None
    # Dynamic-watchlist callers (Phase 2) set this to true to allow symbols
    # outside STATIC_WATCHLIST that they've validated via news/earnings sources.
    dynamic_allowed: bool = False


@dataclass
class OrderResult:
    status: str                      # submitted | dry_run | blocked:<rail> | error
    reason: str
    order_id: str | None = None
    qty: int | None = None
    stop_loss_price: float | None = None
    estimated_cost: float | None = None
    # For observability when callers want to know the rail context
    meta: dict = field(default_factory=dict)


# ── HELPERS ────────────────────────────────────────────────────────────────

def _paper_client() -> TradingClient:
    return TradingClient(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
        paper=True,  # hardcoded — this module is paper-only by policy
    )


def _now_et() -> datetime:
    return datetime.now(ET)


def _within_trading_window(now: datetime, side: Side) -> tuple[bool, str]:
    """Return (allowed, reason). Blocks first 5 min of RTH and last 15 min."""
    if now.weekday() >= 5:
        return False, "weekend"
    hhmm = now.hour * 100 + now.minute
    if hhmm < 930 or hhmm >= 1600:
        return False, "outside RTH"
    # Minutes since open
    minutes_since_open = (now.hour - 9) * 60 + (now.minute - 30)
    if minutes_since_open < NO_TRADE_AT_OPEN_MIN:
        return False, f"first {NO_TRADE_AT_OPEN_MIN} min of RTH"
    minutes_to_close = (16 * 60) - (now.hour * 60 + now.minute)
    if side == "buy" and minutes_to_close <= NO_NEW_LONGS_BEFORE_CLOSE_MIN:
        return False, f"last {NO_NEW_LONGS_BEFORE_CLOSE_MIN} min — no new longs"
    return True, "ok"


def _get_reference_price(symbol: str, limit_price: float | None) -> float | None:
    """Best-effort reference price for sizing. Uses limit if given, else
    the latest quote mid. Returns None if unavailable."""
    if limit_price:
        return float(limit_price)
    try:
        q = market_data.get_latest_quote(symbol)
        bid = q.get("bid")
        ask = q.get("ask")
        if bid and ask:
            return round((float(bid) + float(ask)) / 2.0, 2)
        if ask:
            return float(ask)
        if bid:
            return float(bid)
    except Exception:
        log.exception(f"reference price lookup failed for {symbol}")
    return None


def _size_to_dollar(price: float) -> int:
    """Compute share count to approximate DOLLAR_PER_TRADE. Integer shares,
    rounded down. Returns 0 if a single share exceeds the dollar budget
    (e.g. a $2,000 stock with $1k budget)."""
    if price <= 0:
        return 0
    return int(math.floor(DOLLAR_PER_TRADE / price))


def _whitelisted(symbol: str, dynamic_allowed: bool) -> bool:
    return symbol.upper() in STATIC_WATCHLIST or dynamic_allowed


def _needs_stop(symbol: str) -> bool:
    return symbol.upper() not in MAG7


def _source_daily_count(conn, source: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE source = ? AND date(ts) = date('now') "
        "AND status IN ('submitted', 'dry_run')",
        (source,),
    ).fetchone()
    return row[0] if row else 0


# ── MAIN ENTRYPOINT ────────────────────────────────────────────────────────

def execute_order(intent: OrderIntent) -> OrderResult:
    """Run all rails, then submit (or dry-run log) an order.

    Never raises. Returns OrderResult describing the outcome.
    """
    symbol = intent.symbol.upper()
    side = intent.side
    source = intent.source

    def _log_and_return(status: str, reason: str,
                        qty: int | None = None,
                        order_id: str | None = None,
                        stop_loss_price: float | None = None,
                        est_cost: float | None = None,
                        equity: float | None = None,
                        pnl_pct: float | None = None) -> OrderResult:
        trade_log.record(
            source=source, symbol=symbol, side=side,
            qty=qty or 0, order_type=intent.order_type,
            limit_price=intent.limit_price,
            stop_loss_price=stop_loss_price,
            status=status, reason=reason, order_id=order_id,
            estimated_cost=est_cost, account_equity=equity, pnl_today_pct=pnl_pct,
        )
        return OrderResult(status=status, reason=reason, order_id=order_id,
                           qty=qty, stop_loss_price=stop_loss_price,
                           estimated_cost=est_cost)

    # ── Rail 1: kill-switch file ───────────────────────────────────────────
    if KILL_SWITCH.exists():
        return _log_and_return("blocked:kill-switch", f"kill-switch file present at {KILL_SWITCH}")

    # ── Rail 2: paper-only ─────────────────────────────────────────────────
    if os.environ.get("ALPACA_PAPER", "").lower() != "true":
        return _log_and_return("blocked:not-paper", "ALPACA_PAPER != true; refusing to trade")

    # ── Rail 3: side sanity ────────────────────────────────────────────────
    if side not in ("buy", "sell"):
        return _log_and_return("blocked:invalid-side", f"invalid side: {side!r}")

    # ── Rail 4: symbol whitelist ───────────────────────────────────────────
    if not _whitelisted(symbol, intent.dynamic_allowed):
        return _log_and_return(
            "blocked:not-whitelisted",
            f"{symbol} not in static watchlist and dynamic_allowed=False",
        )

    # ── Rail 5: time window ────────────────────────────────────────────────
    ok, why = _within_trading_window(_now_et(), side)
    if not ok:
        return _log_and_return(f"blocked:time-window", why)

    # ── Rail 6: account + daily loss kill-switch ───────────────────────────
    try:
        client = _paper_client()
        account = client.get_account()
        equity = float(account.equity)
        last_equity = float(account.last_equity)
        pnl_pct = (equity - last_equity) / last_equity * 100.0 if last_equity else 0.0
        buying_power = float(account.buying_power)
    except Exception as e:
        return _log_and_return("error", f"account fetch failed: {e}")

    if pnl_pct <= DAILY_LOSS_PCT_KILL:
        return _log_and_return(
            "blocked:daily-loss",
            f"daily P&L {pnl_pct:.2f}% <= {DAILY_LOSS_PCT_KILL}% threshold",
            equity=equity, pnl_pct=pnl_pct,
        )

    # ── Rail 7: max open positions (only on buy) ───────────────────────────
    if side == "buy":
        try:
            positions = client.get_all_positions()
            if len(positions) >= MAX_OPEN_POSITIONS:
                return _log_and_return(
                    "blocked:max-positions",
                    f"{len(positions)} open positions; cap is {MAX_OPEN_POSITIONS}",
                    equity=equity, pnl_pct=pnl_pct,
                )
        except Exception as e:
            return _log_and_return("error", f"positions fetch failed: {e}",
                                   equity=equity, pnl_pct=pnl_pct)

    # ── Rail 8: per-source daily trade cap ─────────────────────────────────
    try:
        conn = trade_log.connect()
        try:
            daily_count = _source_daily_count(conn, source)
        finally:
            conn.close()
    except Exception:
        daily_count = 0
    if daily_count >= PER_SOURCE_DAILY_MAX_TRADES:
        return _log_and_return(
            "blocked:source-daily-cap",
            f"source {source} already executed {daily_count} trades today "
            f"(cap {PER_SOURCE_DAILY_MAX_TRADES})",
            equity=equity, pnl_pct=pnl_pct,
        )

    # ── Sizing: compute qty to target $1k ──────────────────────────────────
    ref_price = _get_reference_price(symbol, intent.limit_price)
    if ref_price is None:
        return _log_and_return("blocked:no-reference-price",
                               f"could not resolve reference price for {symbol}",
                               equity=equity, pnl_pct=pnl_pct)

    qty = intent.qty if intent.qty is not None else _size_to_dollar(ref_price)
    if qty <= 0:
        return _log_and_return(
            "blocked:sizing-zero",
            f"${DOLLAR_PER_TRADE:.0f} budget vs ${ref_price:.2f} ref price → 0 shares",
            equity=equity, pnl_pct=pnl_pct,
        )
    est_cost = round(qty * ref_price, 2)
    if side == "buy" and est_cost > buying_power:
        return _log_and_return(
            "blocked:insufficient-buying-power",
            f"est cost ${est_cost:.2f} exceeds buying power ${buying_power:.2f}",
            qty=qty, est_cost=est_cost, equity=equity, pnl_pct=pnl_pct,
        )

    # ── Stop-loss policy: 5% on non-mag7 buys ──────────────────────────────
    stop_price = None
    if side == "buy" and _needs_stop(symbol):
        stop_price = round(ref_price * (1 - STOP_LOSS_PCT), 2)

    # ── Dry-run gate ───────────────────────────────────────────────────────
    if _dry_run():
        return _log_and_return(
            "dry_run",
            f"DRY_RUN — would {side} {qty} {symbol} @ {intent.order_type} "
            f"{intent.limit_price or 'market'} (stop {stop_price or 'none'}); "
            f"source={source}; {intent.reason}",
            qty=qty, stop_loss_price=stop_price, est_cost=est_cost,
            equity=equity, pnl_pct=pnl_pct,
        )

    # ── Live paper submit ──────────────────────────────────────────────────
    try:
        side_enum = OrderSide.BUY if side == "buy" else OrderSide.SELL
        order_kwargs = dict(
            symbol=symbol,
            qty=qty,
            side=side_enum,
            time_in_force=TimeInForce.DAY,
        )
        # Bracket stop only on buys with stop_price
        if stop_price and side == "buy":
            order_kwargs["order_class"] = OrderClass.BRACKET
            order_kwargs["stop_loss"] = StopLossRequest(stop_price=stop_price)

        if intent.order_type == "limit":
            if intent.limit_price is None:
                return _log_and_return(
                    "blocked:limit-missing-price",
                    "limit order without limit_price",
                    qty=qty, equity=equity, pnl_pct=pnl_pct,
                )
            req = LimitOrderRequest(limit_price=intent.limit_price, **order_kwargs)
        else:
            req = MarketOrderRequest(**order_kwargs)

        order = client.submit_order(req)
        order_id = str(order.id)
        return _log_and_return(
            "submitted",
            f"submitted {side} {qty} {symbol} @ {intent.order_type} "
            f"{intent.limit_price or 'market'} (stop {stop_price or 'none'}); "
            f"source={source}; {intent.reason}",
            qty=qty, order_id=order_id, stop_loss_price=stop_price,
            est_cost=est_cost, equity=equity, pnl_pct=pnl_pct,
        )
    except Exception as e:
        log.exception(f"order submit failed for {symbol}")
        return _log_and_return(
            "error", f"submit raised: {type(e).__name__}: {e}",
            qty=qty, stop_loss_price=stop_price, est_cost=est_cost,
            equity=equity, pnl_pct=pnl_pct,
        )
