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
ENV_FILE = PROJECT_ROOT / ".env"
ACCESS_TOKEN_FILE = PROJECT_ROOT / ".access_token"
ENV_KEYS = {
    "api_key": ("KITE_API_KEY", "ZERODHA_API_KEY"),
    "api_secret": ("KITE_API_SECRET", "ZERODHA_API_SECRET"),
}

WATCHLIST = [
    {"label": "NIFTY", "symbol": "NIFTY 50", "exchange": "NSE", "instrument": "NSE:NIFTY 50"},
    {"label": "BANKNIFTY", "symbol": "NIFTY BANK", "exchange": "NSE", "instrument": "NSE:NIFTY BANK"},
    {"label": "RELIANCE", "symbol": "RELIANCE", "exchange": "NSE", "instrument": "NSE:RELIANCE"},
    {"label": "TCS", "symbol": "TCS", "exchange": "NSE", "instrument": "NSE:TCS"},
    {"label": "HDFCBANK", "symbol": "HDFCBANK", "exchange": "NSE", "instrument": "NSE:HDFCBANK"},
    {"label": "INFY", "symbol": "INFY", "exchange": "NSE", "instrument": "NSE:INFY"},
    {"label": "ICICIBANK", "symbol": "ICICIBANK", "exchange": "NSE", "instrument": "NSE:ICICIBANK"},
    {"label": "SBIN", "symbol": "SBIN", "exchange": "NSE", "instrument": "NSE:SBIN"},
    {"label": "ITC", "symbol": "ITC", "exchange": "NSE", "instrument": "NSE:ITC"},
    {"label": "LT", "symbol": "LT", "exchange": "NSE", "instrument": "NSE:LT"},
]

app = Flask(__name__)
_instrument_token_cache: dict[str, int] = {}

INTERVALS = {
    "1m": {"kite": "minute", "days": 1, "points": 240},
    "3m": {"kite": "3minute", "days": 3, "points": 220},
    "5m": {"kite": "5minute", "days": 5, "points": 220},
    "10m": {"kite": "10minute", "days": 10, "points": 180},
    "15m": {"kite": "15minute", "days": 15, "points": 180},
    "30m": {"kite": "30minute", "days": 30, "points": 160},
    "1h": {"kite": "60minute", "days": 45, "points": 150},
    "1D": {"kite": "day", "days": 220, "points": 160},
}


class MarketDataError(RuntimeError):
    """Raised when live broker market data cannot be loaded."""


def _load_env_file() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file()


def _write_env_values(values: dict[str, str]) -> None:
    existing: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" not in line or line.strip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            existing[key.strip()] = value.strip().strip('"').strip("'")
    for key, value in values.items():
        if value:
            existing[key] = value.strip()
            os.environ[key] = value.strip()
    ordered_keys = [
        "KITE_API_KEY",
        "KITE_API_SECRET",
        "ANTHROPIC_API_KEY",
        "KITE_ACCESS_TOKEN",
    ]
    keys = ordered_keys + sorted(key for key in existing if key not in ordered_keys)
    lines = [f"{key}={existing[key]}" for key in keys if existing.get(key)]
    ENV_FILE.write_text("\n".join(lines) + ("\n" if lines else ""))


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
    for env_key in ENV_KEYS.get(key, ()):
        value = os.getenv(env_key)
        if value:
            return value.strip()
    match = re.search(rf'"{re.escape(key)}"\s*:\s*([^,\n#]+)', _bot_text())
    if not match:
        return None
    return match.group(1).strip().strip('"').strip("'")


def _saved_access_token() -> str | None:
    env_token = os.getenv("KITE_ACCESS_TOKEN") or os.getenv("ZERODHA_ACCESS_TOKEN")
    if env_token:
        return env_token.strip()
    if not ACCESS_TOKEN_FILE.exists():
        return None
    try:
        saved_date, token = ACCESS_TOKEN_FILE.read_text().strip().split("|", 1)
    except ValueError:
        return None
    if saved_date != _today_ist().isoformat():
        return None
    return token


def _kite_status() -> dict[str, Any]:
    api_key = _bot_cfg_value("api_key")
    api_secret = _bot_cfg_value("api_secret")
    has_key = bool(api_key and not api_key.startswith("YOUR_"))
    has_secret = bool(api_secret and not api_secret.startswith("YOUR_"))
    token = _saved_access_token()
    if not has_key:
        reason = "Zerodha Kite API key is not configured"
    elif not token:
        reason = f"No Zerodha access token for {_today_ist().isoformat()}"
    elif not has_secret:
        reason = "Kite token available. API secret is only needed to renew login."
    else:
        reason = "Kite token available"
    return {
        "configured": has_key,
        "secret": has_secret,
        "token": token is not None,
        "connected": has_key and token is not None,
        "reason": reason,
        "env_file": ENV_FILE.exists(),
        "token_file": ACCESS_TOKEN_FILE.exists(),
    }


def _kite_client():
    api_key = _bot_cfg_value("api_key")
    access_token = _saved_access_token()
    if not api_key or api_key.startswith("YOUR_"):
        raise MarketDataError("Zerodha Kite API key is not configured")
    if not access_token:
        raise MarketDataError(
            f"No Zerodha access token for {_today_ist().isoformat()}. Run python bot.py and complete Kite login."
        )
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


def _watchlist_item(symbol: str) -> dict[str, Any] | None:
    for item in WATCHLIST:
        if item["symbol"] == symbol:
            return item
    return None


def _serialise_time(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return _serialise_time(value)


def _quote_rows(kite, symbols: list[str] | None = None) -> list[dict[str, Any]]:
    selected = [item for item in WATCHLIST if symbols is None or item["symbol"] in symbols]
    if not selected:
        return []

    quotes = kite.quote([item["instrument"] for item in selected])
    rows = []
    for item in selected:
        raw = quotes.get(item["instrument"], {})
        ohlc = raw.get("ohlc") or {}
        last_price = _money(raw.get("last_price"))
        previous_close = _money(ohlc.get("close"))
        change = round(last_price - previous_close, 2) if previous_close else _money(raw.get("net_change"))
        change_pct = round(change / previous_close * 100, 2) if previous_close else 0.0
        rows.append({
            "label": item["label"],
            "symbol": item["symbol"],
            "exchange": item["exchange"],
            "instrument": item["instrument"],
            "last_price": last_price,
            "last_quantity": int(raw.get("last_quantity") or 0),
            "average_price": _money(raw.get("average_price")),
            "volume": int(raw.get("volume") or 0),
            "buy_quantity": int(raw.get("buy_quantity") or 0),
            "sell_quantity": int(raw.get("sell_quantity") or 0),
            "open": _money(ohlc.get("open")),
            "high": _money(ohlc.get("high")),
            "low": _money(ohlc.get("low")),
            "close": previous_close,
            "change": change,
            "change_pct": change_pct,
            "oi": int(raw.get("oi") or 0),
            "timestamp": _serialise_time(raw.get("timestamp")),
            "last_trade_time": _serialise_time(raw.get("last_trade_time")),
            "depth": _json_safe(raw.get("depth") or {"buy": [], "sell": []}),
        })
    return rows


def _kite_candles(symbol: str, interval: str) -> dict[str, Any]:
    kite = _kite_client()
    meta = INTERVALS.get(interval, INTERVALS["5m"])
    token = _kite_instrument_token(kite, symbol)
    if token is None:
        raise MarketDataError(f"Instrument token not found for NSE:{symbol}")

    to_dt = dt.datetime.now(ZoneInfo("Asia/Kolkata")) if ZoneInfo else dt.datetime.now()
    from_dt = to_dt - dt.timedelta(days=meta["days"])
    rows = kite.historical_data(token, from_dt, to_dt, meta["kite"])
    if not rows:
        raise MarketDataError(f"Kite returned no historical candles for NSE:{symbol}")

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
            "volume": int(row.get("volume") or 0),
        })
    return {
        "symbol": symbol,
        "interval": interval,
        "source": "kite",
        "asof": to_dt.isoformat(),
        "message": "Zerodha Kite historical candles",
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
        "kite": _kite_status(),
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
    except MarketDataError as exc:
        return jsonify({
            "error": str(exc),
            "source": "kite",
            "kite": _kite_status(),
            "candles": [],
        }), 503
    except Exception as exc:
        return jsonify({
            "error": f"Kite data unavailable: {exc}",
            "source": "kite",
            "kite": _kite_status(),
            "candles": [],
        }), 502

    return jsonify(data)


@app.route("/api/quotes")
def api_quotes():
    requested = [item.strip().upper() for item in request.args.get("symbols", "").split(",") if item.strip()]
    try:
        rows = _quote_rows(_kite_client(), requested or None)
    except MarketDataError as exc:
        return jsonify({
            "error": str(exc),
            "source": "kite",
            "kite": _kite_status(),
            "quotes": [],
        }), 503
    except Exception as exc:
        return jsonify({
            "error": f"Kite quotes unavailable: {exc}",
            "source": "kite",
            "kite": _kite_status(),
            "quotes": [],
        }), 502

    return jsonify({
        "source": "kite",
        "kite": _kite_status(),
        "quotes": rows,
        "asof": (dt.datetime.now(ZoneInfo("Asia/Kolkata")) if ZoneInfo else dt.datetime.now()).isoformat(),
    })


@app.route("/api/depth")
def api_depth():
    symbol = request.args.get("symbol", WATCHLIST[0]["symbol"]).upper()
    if _watchlist_item(symbol) is None:
        return jsonify({"error": "Unsupported symbol"}), 400
    try:
        rows = _quote_rows(_kite_client(), [symbol])
    except MarketDataError as exc:
        return jsonify({
            "error": str(exc),
            "source": "kite",
            "kite": _kite_status(),
            "depth": {"buy": [], "sell": []},
        }), 503
    except Exception as exc:
        return jsonify({
            "error": f"Kite depth unavailable: {exc}",
            "source": "kite",
            "kite": _kite_status(),
            "depth": {"buy": [], "sell": []},
        }), 502

    quote = rows[0] if rows else {}
    return jsonify({
        "source": "kite",
        "kite": _kite_status(),
        "symbol": symbol,
        "quote": quote,
        "depth": quote.get("depth") or {"buy": [], "sell": []},
    })


@app.route("/api/broker")
def api_broker():
    try:
        kite = _kite_client()
        positions = kite.positions()
        orders = kite.orders()
        margins = kite.margins("equity")
    except MarketDataError as exc:
        return jsonify({
            "error": str(exc),
            "source": "kite",
            "kite": _kite_status(),
            "positions": {"day": [], "net": []},
            "orders": [],
            "margins": {},
        }), 503
    except Exception as exc:
        return jsonify({
            "error": f"Kite broker data unavailable: {exc}",
            "source": "kite",
            "kite": _kite_status(),
            "positions": {"day": [], "net": []},
            "orders": [],
            "margins": {},
        }), 502

    return jsonify({
        "source": "kite",
        "kite": _kite_status(),
        "positions": _json_safe(positions),
        "orders": _json_safe(orders[-50:]),
        "margins": _json_safe(margins),
    })


@app.route("/api/health")
def api_health():
    return jsonify(
        {
            "ok": True,
            "database": DB_FILE.exists(),
            "log": LOG_FILE.exists(),
            "kite_token": _saved_access_token() is not None,
            "kite": _kite_status(),
            "market": _market_status(),
        }
    )


@app.route("/api/kite/status")
def api_kite_status():
    return jsonify(_kite_status())


@app.route("/api/kite/config", methods=["POST"])
def api_kite_config():
    payload = request.get_json(silent=True) or {}
    api_key = str(payload.get("api_key") or "").strip()
    api_secret = str(payload.get("api_secret") or "").strip()
    if not api_key or not api_secret:
        return jsonify({"error": "Both Kite API key and API secret are required"}), 400
    _write_env_values({
        "KITE_API_KEY": api_key,
        "KITE_API_SECRET": api_secret,
    })
    return jsonify({
        "ok": True,
        "message": "Kite credentials saved to local .env",
        "kite": _kite_status(),
    })


@app.route("/api/kite/login-url")
def api_kite_login_url():
    api_key = _bot_cfg_value("api_key")
    if not api_key or api_key.startswith("YOUR_"):
        return jsonify({"error": "Set Kite API key first"}), 400
    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=api_key)
    return jsonify({"login_url": kite.login_url(), "kite": _kite_status()})


@app.route("/api/kite/session", methods=["POST"])
def api_kite_session():
    payload = request.get_json(silent=True) or {}
    request_token = str(payload.get("request_token") or "").strip()
    api_key = _bot_cfg_value("api_key")
    api_secret = _bot_cfg_value("api_secret")
    if not request_token:
        return jsonify({"error": "request_token is required"}), 400
    if not api_key or api_key.startswith("YOUR_") or not api_secret or api_secret.startswith("YOUR_"):
        return jsonify({"error": "Kite API key and secret are required"}), 400

    from kiteconnect import KiteConnect

    try:
        kite = KiteConnect(api_key=api_key)
        sess = kite.generate_session(request_token, api_secret=api_secret)
    except Exception as exc:
        return jsonify({"error": f"Kite login failed: {exc}"}), 502

    access_token = sess["access_token"]
    today = _today_ist().isoformat()
    ACCESS_TOKEN_FILE.write_text(f"{today}|{access_token}")
    os.environ["KITE_ACCESS_TOKEN"] = access_token
    return jsonify({
        "ok": True,
        "message": f"Kite access token saved for {today}",
        "kite": _kite_status(),
    })


def main() -> None:
    parser = argparse.ArgumentParser(description="Trade Claude web monitor")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5050)
    args = parser.parse_args()

    print(f"Trade Claude web monitor: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
