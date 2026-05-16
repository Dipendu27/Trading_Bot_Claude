#!/usr/bin/env python3
"""
Web monitor for the main Trade Claude bot.

This does not place orders. It reads the local journal database and bot log,
then renders a browser dashboard with TradingView Lightweight Charts.

Run:
    python web_dashboard.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python <3.9 fallback
    ZoneInfo = None


PROJECT_ROOT = Path(__file__).resolve().parent
DB_FILE = PROJECT_ROOT / "trades.db"
LOG_FILE = PROJECT_ROOT / "bot.log"
BOT_FILE = PROJECT_ROOT / "bot.py"
ACCESS_TOKEN_FILE = PROJECT_ROOT / ".access_token"

WATCHLIST = [
    {"label": "RELIANCE", "symbol": "RELIANCE"},
    {"label": "TCS", "symbol": "TCS"},
    {"label": "HDFCBANK", "symbol": "HDFCBANK"},
    {"label": "INFY", "symbol": "INFY"},
    {"label": "ICICIBANK", "symbol": "ICICIBANK"},
    {"label": "SBIN", "symbol": "SBIN"},
    {"label": "ITC", "symbol": "ITC"},
    {"label": "LT", "symbol": "LT"},
]

app = Flask(__name__)
_instrument_token_cache: dict[str, int] = {}

INTERVALS = {
    "1m": {"kite": "minute", "days": 1, "step_minutes": 1, "points": 180},
    "5m": {"kite": "5minute", "days": 5, "step_minutes": 5, "points": 180},
    "15m": {"kite": "15minute", "days": 10, "step_minutes": 15, "points": 160},
    "1h": {"kite": "60minute", "days": 30, "step_minutes": 60, "points": 120},
    "1D": {"kite": "day", "days": 180, "step_minutes": 1440, "points": 120},
}

BASE_PRICES = {
    "RELIANCE": 2900,
    "TCS": 3900,
    "HDFCBANK": 1700,
    "INFY": 1800,
    "ICICIBANK": 1200,
    "SBIN": 800,
    "ITC": 480,
    "LT": 3700,
}


def _connect() -> sqlite3.Connection | None:
    if not DB_FILE.exists():
        return None
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    return con


def _fetch_all(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    con = _connect()
    if con is None:
        return []
    try:
        rows = con.execute(query, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def _fetch_one(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    con = _connect()
    if con is None:
        return {}
    try:
        row = con.execute(query, params).fetchone()
        return dict(row) if row else {}
    finally:
        con.close()


def _money(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _bot_text() -> str:
    return BOT_FILE.read_text() if BOT_FILE.exists() else ""


def _bot_cfg_value(key: str) -> str | None:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*([^,\n#]+)', _bot_text())
    if not match:
        return None
    return match.group(1).strip().strip('"').strip("'")


def _saved_access_token() -> str | None:
    if not ACCESS_TOKEN_FILE.exists():
        return None
    try:
        saved_date, token = ACCESS_TOKEN_FILE.read_text().strip().split("|", 1)
    except ValueError:
        return None
    if saved_date != _today_ist().isoformat():
        return None
    return token


def _kite_client():
    api_key = _bot_cfg_value("api_key")
    access_token = _saved_access_token()
    if not api_key or api_key.startswith("YOUR_") or not access_token:
        return None
    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


def _kite_instrument_token(kite, symbol: str) -> int | None:
    if symbol in _instrument_token_cache:
        return _instrument_token_cache[symbol]
    instruments = kite.instruments("NSE")
    for inst in instruments:
        if inst.get("tradingsymbol") == symbol:
            token = int(inst["instrument_token"])
            _instrument_token_cache[symbol] = token
            return token
    return None


def _kite_candles(symbol: str, interval: str) -> dict[str, Any] | None:
    kite = _kite_client()
    if kite is None:
        return None
    meta = INTERVALS.get(interval, INTERVALS["5m"])
    token = _kite_instrument_token(kite, symbol)
    if token is None:
        return None

    to_dt = dt.datetime.now(ZoneInfo("Asia/Kolkata")) if ZoneInfo else dt.datetime.now()
    from_dt = to_dt - dt.timedelta(days=meta["days"])
    rows = kite.historical_data(token, from_dt, to_dt, meta["kite"])
    candles = []
    for row in rows[-meta["points"]:]:
        when = row["date"]
        if hasattr(when, "timestamp"):
            ts = int(when.timestamp())
        else:
            ts = int(dt.datetime.fromisoformat(str(when)).timestamp())
        candles.append({
            "time": ts,
            "open": _money(row["open"]),
            "high": _money(row["high"]),
            "low": _money(row["low"]),
            "close": _money(row["close"]),
        })
    return {
        "symbol": symbol,
        "interval": interval,
        "source": "kite",
        "message": "Zerodha Kite historical candles",
        "candles": candles,
    }


def _demo_candles(symbol: str, interval: str) -> dict[str, Any]:
    meta = INTERVALS.get(interval, INTERVALS["5m"])
    points = meta["points"]
    step = meta["step_minutes"]
    base = BASE_PRICES.get(symbol, 1000)
    now = dt.datetime.now(ZoneInfo("Asia/Kolkata")) if ZoneInfo else dt.datetime.now()
    if interval == "1D":
        start = (now - dt.timedelta(days=points)).replace(hour=15, minute=30, second=0, microsecond=0)
    else:
        start = now - dt.timedelta(minutes=points * step)

    candles = []
    previous = float(base)
    symbol_seed = sum(ord(ch) for ch in symbol)
    for idx in range(points):
        current_time = start + dt.timedelta(days=idx if interval == "1D" else 0,
                                           minutes=0 if interval == "1D" else idx * step)
        wave = math.sin((idx + symbol_seed) / 7.0) * 0.0025
        drift = math.sin((idx + symbol_seed) / 29.0) * 0.0012
        close = max(previous * (1 + wave + drift), 1)
        open_ = previous
        high = max(open_, close) * (1 + 0.0018 + abs(math.sin(idx)) * 0.0015)
        low = min(open_, close) * (1 - 0.0018 - abs(math.cos(idx)) * 0.0015)
        previous = close
        candles.append({
            "time": int(current_time.timestamp()),
            "open": round(open_, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(close, 2),
        })
    return {
        "symbol": symbol,
        "interval": interval,
        "source": "demo",
        "message": "Demo candles. Configure bot.py and today's .access_token for Kite intraday data.",
        "candles": candles,
    }


def _tail_log(lines: int = 80) -> list[str]:
    if not LOG_FILE.exists():
        return []
    with LOG_FILE.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        pos = size
        data = b""
        while pos > 0 and data.count(b"\n") <= lines:
            read_size = min(4096, pos)
            pos -= read_size
            f.seek(pos)
            data = f.read(read_size) + data
    return data.decode(errors="replace").splitlines()[-lines:]


def _today_ist() -> dt.date:
    if ZoneInfo is None:
        return dt.datetime.utcnow().date()
    return dt.datetime.now(ZoneInfo("Asia/Kolkata")).date()


def _market_status() -> dict[str, Any]:
    if ZoneInfo is None:
        now = dt.datetime.utcnow()
    else:
        now = dt.datetime.now(ZoneInfo("Asia/Kolkata"))
    open_at = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_at = now.replace(hour=15, minute=30, second=0, microsecond=0)
    is_open = now.weekday() < 5 and open_at <= now <= close_at
    return {
        "is_open": is_open,
        "label": "OPEN" if is_open else "CLOSED",
        "time": now.strftime("%Y-%m-%d %H:%M:%S IST"),
    }


def _read_bot_config() -> dict[str, Any]:
    text = _bot_text()
    if not text:
        return {}

    paper_match = re.search(r"PAPER_TRADE\s*:\s*bool\s*=\s*(True|False)", text)
    indices = re.findall(r'"(NIFTY 50|NIFTY BANK)"\s*:', text)
    return {
        "paper_trade": paper_match.group(1) == "True" if paper_match else None,
        "capital": _bot_cfg_value("capital"),
        "max_lots": _bot_cfg_value("max_lots"),
        "sl_pct": _bot_cfg_value("sl_pct"),
        "target_pct": _bot_cfg_value("target_pct"),
        "squareoff_time": _bot_cfg_value("squareoff_time"),
        "indices": indices,
    }


def build_overview() -> dict[str, Any]:
    today = _today_ist().isoformat()
    totals = _fetch_one(
        """
        SELECT
            COUNT(*) AS total_trades,
            SUM(CASE WHEN close_price IS NOT NULL THEN 1 ELSE 0 END) AS closed_trades,
            SUM(CASE WHEN close_price IS NULL THEN 1 ELSE 0 END) AS open_trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS winners,
            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losers,
            SUM(COALESCE(pnl, 0)) AS gross_pnl,
            AVG(CASE WHEN close_price IS NOT NULL THEN pnl END) AS avg_pnl,
            MAX(pnl) AS best_trade,
            MIN(pnl) AS worst_trade
        FROM trades
        """
    )
    total = int(totals.get("total_trades") or 0)
    closed = int(totals.get("closed_trades") or 0)
    winners = int(totals.get("winners") or 0)
    losers = int(totals.get("losers") or 0)
    win_rate = round((winners / closed * 100), 1) if closed else 0.0
    gross = _money(totals.get("gross_pnl"))

    daily_summary = _fetch_one(
        "SELECT SUM(charges) AS charges, SUM(net_pnl) AS net_pnl FROM daily_summary"
    )
    charges = _money(daily_summary.get("charges"))
    net_pnl = _money(daily_summary.get("net_pnl"))
    if not daily_summary or (charges == 0 and closed):
        charges = round(closed * 40, 2)
        net_pnl = round(gross - charges, 2)

    today_row = _fetch_one(
        """
        SELECT
            COUNT(*) AS trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS winners,
            SUM(COALESCE(pnl, 0)) AS pnl
        FROM trades WHERE date=?
        """,
        (today,),
    )

    return {
        "summary": {
            "total_trades": total,
            "closed_trades": closed,
            "open_trades": int(totals.get("open_trades") or 0),
            "winners": winners,
            "losers": losers,
            "win_rate": win_rate,
            "gross_pnl": gross,
            "charges": charges,
            "net_pnl": net_pnl,
            "avg_pnl": _money(totals.get("avg_pnl")),
            "best_trade": _money(totals.get("best_trade")),
            "worst_trade": _money(totals.get("worst_trade")),
            "today_trades": int(today_row.get("trades") or 0),
            "today_winners": int(today_row.get("winners") or 0),
            "today_pnl": _money(today_row.get("pnl")),
        },
        "symbols": get_symbol_rows(),
        "daily": get_daily_rows(),
        "recent_trades": get_recent_trades(),
        "sources": get_source_rows(),
        "logs": _tail_log(),
        "market": _market_status(),
        "bot": _read_bot_config(),
        "watchlist": WATCHLIST,
    }


def get_symbol_rows() -> list[dict[str, Any]]:
    rows = _fetch_all(
        """
        SELECT
            symbol,
            COUNT(*) AS trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losses,
            SUM(COALESCE(pnl, 0)) AS pnl,
            AVG(pnl) AS avg_pnl
        FROM trades
        GROUP BY symbol
        ORDER BY pnl DESC
        """
    )
    for row in rows:
        trades = row.get("trades") or 0
        row["pnl"] = _money(row.get("pnl"))
        row["avg_pnl"] = _money(row.get("avg_pnl"))
        row["win_rate"] = round((row.get("wins") or 0) / trades * 100, 1) if trades else 0
    return rows


def get_daily_rows() -> list[dict[str, Any]]:
    rows = _fetch_all(
        """
        SELECT
            date,
            COUNT(*) AS trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(COALESCE(pnl, 0)) AS pnl
        FROM trades
        GROUP BY date
        ORDER BY date
        """
    )
    cumulative = 0.0
    for row in rows:
        row["pnl"] = _money(row.get("pnl"))
        cumulative = round(cumulative + row["pnl"], 2)
        row["equity"] = cumulative
        trades = row.get("trades") or 0
        row["win_rate"] = round((row.get("wins") or 0) / trades * 100, 1) if trades else 0
    return rows


def get_recent_trades(limit: int = 40) -> list[dict[str, Any]]:
    rows = _fetch_all(
        """
        SELECT id, date, symbol, side, qty, price, close_price, reason,
               pnl, ai_decision, created_at, close_time
        FROM trades
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    for row in rows:
        row["price"] = _money(row.get("price"))
        row["close_price"] = _money(row.get("close_price")) if row.get("close_price") else None
        row["pnl"] = _money(row.get("pnl"))
    return rows


def get_source_rows() -> list[dict[str, Any]]:
    rows = _fetch_all(
        """
        SELECT
            COALESCE(ai_decision, 'UNKNOWN') AS source,
            COUNT(*) AS trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(COALESCE(pnl, 0)) AS pnl
        FROM trades
        GROUP BY COALESCE(ai_decision, 'UNKNOWN')
        ORDER BY pnl DESC
        """
    )
    for row in rows:
        row["pnl"] = _money(row.get("pnl"))
        trades = row.get("trades") or 0
        row["win_rate"] = round((row.get("wins") or 0) / trades * 100, 1) if trades else 0
    return rows


@app.route("/")
def index():
    return render_template("monitor.html", watchlist=WATCHLIST)


@app.route("/api/overview")
def api_overview():
    return jsonify(build_overview())


@app.route("/api/candles")
def api_candles():
    symbol = request.args.get("symbol", WATCHLIST[0]["symbol"]).upper()
    interval = request.args.get("interval", "5m")
    allowed_symbols = {item["symbol"] for item in WATCHLIST}
    if symbol not in allowed_symbols:
        return jsonify({"error": "Unsupported symbol"}), 400
    if interval not in INTERVALS:
        return jsonify({"error": "Unsupported interval"}), 400

    try:
        data = _kite_candles(symbol, interval)
    except Exception as exc:
        data = None
        message = f"Kite data unavailable: {exc}"
    else:
        message = None

    if data is None:
        data = _demo_candles(symbol, interval)
        if message:
            data["message"] = f"{message}. Showing demo candles."

    return jsonify(data)


@app.route("/api/health")
def api_health():
    return jsonify(
        {
            "ok": True,
            "database": DB_FILE.exists(),
            "log": LOG_FILE.exists(),
            "kite_token": _saved_access_token() is not None,
            "market": _market_status(),
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Trade Claude web monitor")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5050)
    args = parser.parse_args()

    print(f"Trade Claude web monitor: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
