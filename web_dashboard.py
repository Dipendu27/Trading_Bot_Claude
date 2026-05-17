#!/usr/bin/env python3
"""
Trade_Claude — Professional Web Dashboard (TradingView-style)
=============================================================
Run:  python web_dashboard.py
Open: http://127.0.0.1:5050

Backend logic is identical to the original. Only the HTML template
has been replaced with a full professional dark-theme UI.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template_string, request

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

PROJECT_ROOT = Path(__file__).resolve().parent
DB_FILE = PROJECT_ROOT / "trades.db"
LOG_FILE = PROJECT_ROOT / "bot.log"
BOT_FILE = PROJECT_ROOT / "bot.py"
ENV_FILE = PROJECT_ROOT / ".env"
ACCESS_TOKEN_FILE = PROJECT_ROOT / ".access_token"
ENV_KEYS = {
    "api_key":    ("KITE_API_KEY", "ZERODHA_API_KEY"),
    "api_secret": ("KITE_API_SECRET", "ZERODHA_API_SECRET"),
}

WATCHLIST = [
    {"label": "NIFTY",     "symbol": "NIFTY 50",   "exchange": "NSE", "instrument": "NSE:NIFTY 50"},
    {"label": "BANKNIFTY", "symbol": "NIFTY BANK",  "exchange": "NSE", "instrument": "NSE:NIFTY BANK"},
    {"label": "RELIANCE",  "symbol": "RELIANCE",    "exchange": "NSE", "instrument": "NSE:RELIANCE"},
    {"label": "TCS",       "symbol": "TCS",         "exchange": "NSE", "instrument": "NSE:TCS"},
    {"label": "HDFCBANK",  "symbol": "HDFCBANK",    "exchange": "NSE", "instrument": "NSE:HDFCBANK"},
    {"label": "INFY",      "symbol": "INFY",        "exchange": "NSE", "instrument": "NSE:INFY"},
    {"label": "ICICIBANK", "symbol": "ICICIBANK",   "exchange": "NSE", "instrument": "NSE:ICICIBANK"},
    {"label": "SBIN",      "symbol": "SBIN",        "exchange": "NSE", "instrument": "NSE:SBIN"},
    {"label": "ITC",       "symbol": "ITC",         "exchange": "NSE", "instrument": "NSE:ITC"},
    {"label": "LT",        "symbol": "LT",          "exchange": "NSE", "instrument": "NSE:LT"},
]

app = Flask(__name__)
_instrument_token_cache: dict[str, int] = {}

INTERVALS = {
    "1m":  {"kite": "minute",    "days": 1,   "points": 240},
    "3m":  {"kite": "3minute",   "days": 3,   "points": 220},
    "5m":  {"kite": "5minute",   "days": 5,   "points": 220},
    "10m": {"kite": "10minute",  "days": 10,  "points": 180},
    "15m": {"kite": "15minute",  "days": 15,  "points": 180},
    "30m": {"kite": "30minute",  "days": 30,  "points": 160},
    "1h":  {"kite": "60minute",  "days": 45,  "points": 150},
    "1D":  {"kite": "day",       "days": 220, "points": 160},
}


class MarketDataError(RuntimeError):
    pass


# ── env / credential helpers ──────────────────────────────────────────────────

def _load_env_file() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip(); value = value.strip().strip('"').strip("'")
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
            existing[key] = value.strip(); os.environ[key] = value.strip()
    ordered = ["KITE_API_KEY", "KITE_API_SECRET", "ANTHROPIC_API_KEY", "KITE_ACCESS_TOKEN"]
    keys = ordered + sorted(k for k in existing if k not in ordered)
    lines = [f"{k}={existing[k]}" for k in keys if existing.get(k)]
    ENV_FILE.write_text("\n".join(lines) + ("\n" if lines else ""))


def _connect() -> sqlite3.Connection | None:
    if not DB_FILE.exists():
        return None
    con = sqlite3.connect(DB_FILE); con.row_factory = sqlite3.Row; return con


def _fetch_all(q: str, p: tuple = ()) -> list[dict]:
    con = _connect()
    if con is None: return []
    try:   return [dict(r) for r in con.execute(q, p).fetchall()]
    finally: con.close()


def _fetch_one(q: str, p: tuple = ()) -> dict:
    con = _connect()
    if con is None: return {}
    try:
        row = con.execute(q, p).fetchone()
        return dict(row) if row else {}
    finally: con.close()


def _money(v: Any) -> float:
    try:    return round(float(v or 0), 2)
    except: return 0.0


def _bot_text() -> str:
    return BOT_FILE.read_text() if BOT_FILE.exists() else ""


def _bot_cfg_value(key: str) -> str | None:
    for env_key in ENV_KEYS.get(key, ()):
        v = os.getenv(env_key)
        if v: return v.strip()
    m = re.search(rf'"{re.escape(key)}"\s*:\s*([^,\n#]+)', _bot_text())
    return m.group(1).strip().strip('"').strip("'") if m else None


def _saved_access_token() -> str | None:
    t = os.getenv("KITE_ACCESS_TOKEN") or os.getenv("ZERODHA_ACCESS_TOKEN")
    if t: return t.strip()
    if not ACCESS_TOKEN_FILE.exists(): return None
    try:
        saved_date, token = ACCESS_TOKEN_FILE.read_text().strip().split("|", 1)
    except ValueError:
        return None
    return token if saved_date == _today_ist().isoformat() else None


def _kite_status() -> dict:
    api_key = _bot_cfg_value("api_key")
    api_secret = _bot_cfg_value("api_secret")
    has_key    = bool(api_key and not api_key.startswith("YOUR_"))
    has_secret = bool(api_secret and not api_secret.startswith("YOUR_"))
    token      = _saved_access_token()
    if not has_key:        reason = "Kite API key not configured"
    elif not token:        reason = f"No access token for {_today_ist().isoformat()}"
    elif not has_secret:   reason = "Token OK. Secret needed only to renew login."
    else:                  reason = "Kite connected"
    return {"configured": has_key, "secret": has_secret, "token": token is not None,
            "connected": has_key and token is not None, "reason": reason,
            "env_file": ENV_FILE.exists(), "token_file": ACCESS_TOKEN_FILE.exists()}


def _kite_client():
    api_key = _bot_cfg_value("api_key")
    token   = _saved_access_token()
    if not api_key or api_key.startswith("YOUR_"):
        raise MarketDataError("Kite API key not configured")
    if not token:
        raise MarketDataError(f"No access token for {_today_ist().isoformat()}. Run bot.py first.")
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=api_key); kite.set_access_token(token); return kite


def _kite_instrument_token(kite, symbol: str) -> int | None:
    if symbol in _instrument_token_cache: return _instrument_token_cache[symbol]
    for inst in kite.instruments("NSE"):
        if inst.get("tradingsymbol") == symbol:
            tok = int(inst["instrument_token"])
            _instrument_token_cache[symbol] = tok; return tok
    return None


def _serialise_time(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (dt.datetime, dt.date)) else v


def _json_safe(v: Any) -> Any:
    if isinstance(v, dict):         return {str(k): _json_safe(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)): return [_json_safe(i) for i in v]
    return _serialise_time(v)


def _quote_rows(kite, symbols: list[str] | None = None) -> list[dict]:
    selected = [i for i in WATCHLIST if symbols is None or i["symbol"] in symbols]
    if not selected: return []
    quotes = kite.quote([i["instrument"] for i in selected])
    rows = []
    for item in selected:
        raw = quotes.get(item["instrument"], {}); ohlc = raw.get("ohlc") or {}
        last = _money(raw.get("last_price")); prev = _money(ohlc.get("close"))
        change = round(last - prev, 2) if prev else _money(raw.get("net_change"))
        chg_pct = round(change / prev * 100, 2) if prev else 0.0
        rows.append({"label": item["label"], "symbol": item["symbol"],
                     "exchange": item["exchange"], "instrument": item["instrument"],
                     "last_price": last, "last_quantity": int(raw.get("last_quantity") or 0),
                     "average_price": _money(raw.get("average_price")),
                     "volume": int(raw.get("volume") or 0),
                     "buy_quantity": int(raw.get("buy_quantity") or 0),
                     "sell_quantity": int(raw.get("sell_quantity") or 0),
                     "open": _money(ohlc.get("open")), "high": _money(ohlc.get("high")),
                     "low": _money(ohlc.get("low")), "close": prev,
                     "change": change, "change_pct": chg_pct,
                     "oi": int(raw.get("oi") or 0),
                     "timestamp": _serialise_time(raw.get("timestamp")),
                     "last_trade_time": _serialise_time(raw.get("last_trade_time")),
                     "depth": _json_safe(raw.get("depth") or {"buy": [], "sell": []})})
    return rows


def _kite_candles(symbol: str, interval: str) -> dict:
    kite = _kite_client(); meta = INTERVALS.get(interval, INTERVALS["5m"])
    token = _kite_instrument_token(kite, symbol)
    if token is None: raise MarketDataError(f"Instrument token not found for NSE:{symbol}")
    to_dt   = dt.datetime.now(ZoneInfo("Asia/Kolkata")) if ZoneInfo else dt.datetime.now()
    from_dt = to_dt - dt.timedelta(days=meta["days"])
    rows = kite.historical_data(token, from_dt, to_dt, meta["kite"])
    if not rows: raise MarketDataError(f"No candles for NSE:{symbol}")
    candles = []
    for row in rows[-meta["points"]:]:
        when = row["date"]
        ts = int(when.timestamp()) if hasattr(when, "timestamp") else int(dt.datetime.fromisoformat(str(when)).timestamp())
        candles.append({"time": ts, "open": _money(row["open"]), "high": _money(row["high"]),
                        "low": _money(row["low"]), "close": _money(row["close"]),
                        "volume": int(row.get("volume") or 0)})
    return {"symbol": symbol, "interval": interval, "source": "kite",
            "asof": to_dt.isoformat(), "candles": candles}


def _tail_log(lines: int = 80) -> list[str]:
    if not LOG_FILE.exists(): return []
    with LOG_FILE.open("rb") as f:
        f.seek(0, os.SEEK_END); size = f.tell(); pos = size; data = b""
        while pos > 0 and data.count(b"\n") <= lines:
            rs = min(4096, pos); pos -= rs; f.seek(pos)
            data = f.read(rs) + data
    return data.decode(errors="replace").splitlines()[-lines:]


def _today_ist() -> dt.date:
    return dt.datetime.now(ZoneInfo("Asia/Kolkata")).date() if ZoneInfo else dt.datetime.utcnow().date()


def _market_status() -> dict:
    now = dt.datetime.now(ZoneInfo("Asia/Kolkata")) if ZoneInfo else dt.datetime.utcnow()
    open_at  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_at = now.replace(hour=15, minute=30, second=0, microsecond=0)
    is_open  = now.weekday() < 5 and open_at <= now <= close_at
    return {"is_open": is_open, "label": "OPEN" if is_open else "CLOSED",
            "time": now.strftime("%Y-%m-%d %H:%M:%S IST")}


def _read_bot_config() -> dict:
    text = _bot_text()
    if not text: return {}
    pm = re.search(r"PAPER_TRADE\s*:\s*bool\s*=\s*(True|False)", text)
    return {"paper_trade": pm.group(1) == "True" if pm else None,
            "capital": _bot_cfg_value("capital"), "max_lots": _bot_cfg_value("max_lots"),
            "sl_pct": _bot_cfg_value("sl_pct"), "target_pct": _bot_cfg_value("target_pct"),
            "squareoff_time": _bot_cfg_value("squareoff_time"),
            "indices": re.findall(r'"(NIFTY 50|NIFTY BANK)"\s*:', text)}


def build_overview() -> dict:
    today = _today_ist().isoformat()
    totals = _fetch_one("""
        SELECT COUNT(*) AS total_trades,
               SUM(CASE WHEN close_price IS NOT NULL THEN 1 ELSE 0 END) AS closed_trades,
               SUM(CASE WHEN close_price IS NULL THEN 1 ELSE 0 END) AS open_trades,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS winners,
               SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losers,
               SUM(COALESCE(pnl,0)) AS gross_pnl,
               AVG(CASE WHEN close_price IS NOT NULL THEN pnl END) AS avg_pnl,
               MAX(pnl) AS best_trade, MIN(pnl) AS worst_trade FROM trades""")
    total = int(totals.get("total_trades") or 0)
    closed = int(totals.get("closed_trades") or 0)
    winners = int(totals.get("winners") or 0)
    losers  = int(totals.get("losers") or 0)
    win_rate = round(winners / closed * 100, 1) if closed else 0.0
    gross = _money(totals.get("gross_pnl"))
    ds = _fetch_one("SELECT SUM(charges) AS charges, SUM(net_pnl) AS net_pnl FROM daily_summary")
    charges = _money(ds.get("charges")); net_pnl = _money(ds.get("net_pnl"))
    if not ds or (charges == 0 and closed):
        charges = round(closed * 40, 2); net_pnl = round(gross - charges, 2)
    today_row = _fetch_one("""SELECT COUNT(*) AS trades,
        SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS winners,
        SUM(COALESCE(pnl,0)) AS pnl FROM trades WHERE date=?""", (today,))
    pf_wins   = _fetch_one("SELECT SUM(pnl) AS v FROM trades WHERE pnl > 0")
    pf_losses = _fetch_one("SELECT ABS(SUM(pnl)) AS v FROM trades WHERE pnl < 0")
    pf_w = _money(pf_wins.get("v")); pf_l = _money(pf_losses.get("v"))
    profit_factor = round(pf_w / pf_l, 2) if pf_l else 0.0
    return {
        "summary": {"total_trades": total, "closed_trades": closed,
                    "open_trades": int(totals.get("open_trades") or 0),
                    "winners": winners, "losers": losers, "win_rate": win_rate,
                    "gross_pnl": gross, "charges": charges, "net_pnl": net_pnl,
                    "avg_pnl": _money(totals.get("avg_pnl")),
                    "best_trade": _money(totals.get("best_trade")),
                    "worst_trade": _money(totals.get("worst_trade")),
                    "today_trades": int(today_row.get("trades") or 0),
                    "today_winners": int(today_row.get("winners") or 0),
                    "today_pnl": _money(today_row.get("pnl")),
                    "profit_factor": profit_factor},
        "symbols": get_symbol_rows(), "daily": get_daily_rows(),
        "recent_trades": get_recent_trades(), "sources": get_source_rows(),
        "logs": _tail_log(), "market": _market_status(),
        "bot": _read_bot_config(), "kite": _kite_status(), "watchlist": WATCHLIST,
    }


def get_symbol_rows() -> list[dict]:
    rows = _fetch_all("""SELECT symbol, COUNT(*) AS trades,
        SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
        SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losses,
        SUM(COALESCE(pnl,0)) AS pnl, AVG(pnl) AS avg_pnl
        FROM trades GROUP BY symbol ORDER BY pnl DESC""")
    for r in rows:
        t = r.get("trades") or 0; r["pnl"] = _money(r.get("pnl"))
        r["avg_pnl"] = _money(r.get("avg_pnl"))
        r["win_rate"] = round((r.get("wins") or 0) / t * 100, 1) if t else 0
    return rows


def get_daily_rows() -> list[dict]:
    rows = _fetch_all("""SELECT date, COUNT(*) AS trades,
        SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
        SUM(COALESCE(pnl,0)) AS pnl FROM trades GROUP BY date ORDER BY date""")
    cum = 0.0
    for r in rows:
        r["pnl"] = _money(r.get("pnl")); cum = round(cum + r["pnl"], 2)
        r["equity"] = cum; t = r.get("trades") or 0
        r["win_rate"] = round((r.get("wins") or 0) / t * 100, 1) if t else 0
    return rows


def get_recent_trades(limit: int = 40) -> list[dict]:
    rows = _fetch_all("""SELECT id, date, symbol, side, qty, price, close_price,
        reason, pnl, ai_decision, created_at, close_time FROM trades
        ORDER BY id DESC LIMIT ?""", (limit,))
    for r in rows:
        r["price"] = _money(r.get("price"))
        r["close_price"] = _money(r.get("close_price")) if r.get("close_price") else None
        r["pnl"] = _money(r.get("pnl"))
    return rows


def get_source_rows() -> list[dict]:
    rows = _fetch_all("""SELECT COALESCE(ai_decision,'UNKNOWN') AS source,
        COUNT(*) AS trades, SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
        SUM(COALESCE(pnl,0)) AS pnl FROM trades
        GROUP BY COALESCE(ai_decision,'UNKNOWN') ORDER BY pnl DESC""")
    for r in rows:
        r["pnl"] = _money(r.get("pnl")); t = r.get("trades") or 0
        r["win_rate"] = round((r.get("wins") or 0) / t * 100, 1) if t else 0
    return rows


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML, watchlist=WATCHLIST)

@app.route("/api/overview")
def api_overview(): return jsonify(build_overview())

@app.route("/api/candles")
def api_candles():
    symbol   = request.args.get("symbol", WATCHLIST[0]["symbol"]).upper()
    interval = request.args.get("interval", "5m")
    if symbol not in {i["symbol"] for i in WATCHLIST}:
        return jsonify({"error": "Unsupported symbol"}), 400
    if interval not in INTERVALS:
        return jsonify({"error": "Unsupported interval"}), 400
    try:
        return jsonify(_kite_candles(symbol, interval))
    except MarketDataError as e:
        return jsonify({"error": str(e), "kite": _kite_status(), "candles": []}), 503
    except Exception as e:
        return jsonify({"error": f"Kite error: {e}", "kite": _kite_status(), "candles": []}), 502

@app.route("/api/quotes")
def api_quotes():
    req = [s.strip().upper() for s in request.args.get("symbols","").split(",") if s.strip()]
    try:
        rows = _quote_rows(_kite_client(), req or None)
        return jsonify({"source":"kite","kite":_kite_status(),"quotes":rows,
                        "asof":(dt.datetime.now(ZoneInfo("Asia/Kolkata")) if ZoneInfo else dt.datetime.now()).isoformat()})
    except MarketDataError as e:
        return jsonify({"error":str(e),"kite":_kite_status(),"quotes":[]}), 503
    except Exception as e:
        return jsonify({"error":f"Kite error:{e}","kite":_kite_status(),"quotes":[]}), 502

@app.route("/api/depth")
def api_depth():
    symbol = request.args.get("symbol", WATCHLIST[0]["symbol"]).upper()
    try:
        rows = _quote_rows(_kite_client(), [symbol])
        q = rows[0] if rows else {}
        return jsonify({"source":"kite","kite":_kite_status(),"symbol":symbol,
                        "quote":q,"depth":q.get("depth") or {"buy":[],"sell":[]}})
    except MarketDataError as e:
        return jsonify({"error":str(e),"kite":_kite_status(),"depth":{"buy":[],"sell":[]}}), 503
    except Exception as e:
        return jsonify({"error":f"Kite error:{e}","kite":_kite_status(),"depth":{"buy":[],"sell":[]}}), 502

@app.route("/api/broker")
def api_broker():
    try:
        kite = _kite_client()
        return jsonify({"source":"kite","kite":_kite_status(),
                        "positions":_json_safe(kite.positions()),
                        "orders":_json_safe(kite.orders()[-50:]),
                        "margins":_json_safe(kite.margins("equity"))})
    except MarketDataError as e:
        return jsonify({"error":str(e),"kite":_kite_status(),"positions":{"day":[],"net":[]},"orders":[],"margins":{}}), 503
    except Exception as e:
        return jsonify({"error":f"Kite error:{e}","kite":_kite_status(),"positions":{"day":[],"net":[]},"orders":[],"margins":{}}), 502

@app.route("/api/health")
def api_health():
    return jsonify({"ok":True,"database":DB_FILE.exists(),"log":LOG_FILE.exists(),
                    "kite_token":_saved_access_token() is not None,
                    "kite":_kite_status(),"market":_market_status()})

@app.route("/api/kite/status")
def api_kite_status(): return jsonify(_kite_status())

@app.route("/api/kite/config", methods=["POST"])
def api_kite_config():
    p = request.get_json(silent=True) or {}
    k = str(p.get("api_key") or "").strip(); s = str(p.get("api_secret") or "").strip()
    if not k or not s: return jsonify({"error":"Both key and secret required"}), 400
    _write_env_values({"KITE_API_KEY": k, "KITE_API_SECRET": s})
    return jsonify({"ok":True,"message":"Saved to .env","kite":_kite_status()})

@app.route("/api/kite/login-url")
def api_kite_login_url():
    k = _bot_cfg_value("api_key")
    if not k or k.startswith("YOUR_"): return jsonify({"error":"Set API key first"}), 400
    from kiteconnect import KiteConnect
    return jsonify({"login_url": KiteConnect(api_key=k).login_url(), "kite": _kite_status()})

@app.route("/api/kite/session", methods=["POST"])
def api_kite_session():
    p = request.get_json(silent=True) or {}
    rt = str(p.get("request_token") or "").strip()
    k  = _bot_cfg_value("api_key"); s = _bot_cfg_value("api_secret")
    if not rt: return jsonify({"error":"request_token required"}), 400
    if not k or k.startswith("YOUR_") or not s or s.startswith("YOUR_"):
        return jsonify({"error":"API key and secret required"}), 400
    from kiteconnect import KiteConnect
    try:
        kite = KiteConnect(api_key=k); sess = kite.generate_session(rt, api_secret=s)
    except Exception as e:
        return jsonify({"error":f"Kite login failed: {e}"}), 502
    at = sess["access_token"]; today = _today_ist().isoformat()
    ACCESS_TOKEN_FILE.write_text(f"{today}|{at}"); os.environ["KITE_ACCESS_TOKEN"] = at
    return jsonify({"ok":True,"message":f"Token saved for {today}","kite":_kite_status()})


# ══════════════════════════════════════════════════════════════════════════════
#  DASHBOARD HTML  — TradingView-inspired professional dark terminal
# ══════════════════════════════════════════════════════════════════════════════
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trade_Claude — Terminal</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/lightweight-charts/4.1.3/lightweight-charts.standalone.production.js"></script>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
/* ── Reset & Tokens ─────────────────────────── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg0:#0b0e11;--bg1:#131722;--bg2:#1c2030;--bg3:#232838;--bg4:#2a3044;
  --border:#2e3650;--border2:#3d4a6b;
  --txt0:#d1d4dc;--txt1:#9199af;--txt2:#5d6480;--txt3:#3d4260;
  --bull:#26a69a;--bull2:#1a7a72;--bull-bg:rgba(38,166,154,.12);
  --bear:#ef5350;--bear2:#b53b38;--bear-bg:rgba(239,83,80,.12);
  --accent:#2962ff;--accent2:#1a4fd6;--accent-bg:rgba(41,98,255,.12);
  --gold:#f59e0b;--gold-bg:rgba(245,158,11,.1);
  --purple:#8b5cf6;--purple-bg:rgba(139,92,246,.1);
  --cyan:#06b6d4;
  --radius:6px;--radius2:10px;
  --mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;
}
html,body{height:100%;background:var(--bg0);color:var(--txt0);font-family:var(--sans);font-size:13px;overflow:hidden}

/* ── Layout ─────────────────────────────────── */
.app{display:grid;grid-template-rows:48px 1fr;height:100vh}
.main{display:grid;grid-template-columns:200px 1fr 280px;gap:0;height:100%;overflow:hidden}
.sidebar-l{background:var(--bg1);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
.center{display:flex;flex-direction:column;overflow:hidden;background:var(--bg0)}
.sidebar-r{background:var(--bg1);border-left:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}

/* ── Topbar ─────────────────────────────────── */
.topbar{background:var(--bg1);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 16px;gap:16px;z-index:100}
.logo{display:flex;align-items:center;gap:8px;font-family:var(--mono);font-weight:700;font-size:14px;letter-spacing:.5px;color:#fff;flex-shrink:0}
.logo-icon{width:26px;height:26px;background:var(--accent);border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800}
.topbar-divider{width:1px;height:24px;background:var(--border);flex-shrink:0}
.market-chip{display:flex;align-items:center;gap:6px;padding:4px 10px;border-radius:4px;background:var(--bg2);border:1px solid var(--border);font-family:var(--mono);font-size:11px;cursor:pointer;transition:border-color .15s}
.market-chip:hover{border-color:var(--border2)}
.market-chip .dot{width:6px;height:6px;border-radius:50%;background:var(--bear)}
.market-chip .dot.open{background:var(--bull)}
.stat-pill{display:flex;align-items:center;gap:8px;padding:4px 12px;border-radius:4px;background:var(--bg2);border:1px solid var(--border);font-family:var(--mono);font-size:11px}
.stat-pill .label{color:var(--txt2);font-size:10px;text-transform:uppercase;letter-spacing:.5px}
.stat-pill .value{font-weight:600}
.bull-text{color:var(--bull)} .bear-text{color:var(--bear)} .accent-text{color:var(--accent)}
.topbar-right{margin-left:auto;display:flex;align-items:center;gap:8px}
.btn{padding:5px 12px;border-radius:4px;border:1px solid var(--border);background:var(--bg3);color:var(--txt0);font-family:var(--mono);font-size:11px;cursor:pointer;transition:all .15s;white-space:nowrap}
.btn:hover{background:var(--bg4);border-color:var(--border2)}
.btn-accent{background:var(--accent);border-color:var(--accent);color:#fff}
.btn-accent:hover{background:var(--accent2)}
.badge-paper{background:var(--gold-bg);border:1px solid var(--gold);color:var(--gold);padding:3px 8px;border-radius:4px;font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:.5px;animation:pulse-gold 2s infinite}
@keyframes pulse-gold{0%,100%{opacity:1}50%{opacity:.6}}

/* ── Left Sidebar — Watchlist ───────────────── */
.section-header{padding:10px 12px 6px;font-size:9px;text-transform:uppercase;letter-spacing:1px;color:var(--txt2);font-weight:600;border-bottom:1px solid var(--border);flex-shrink:0}
.watchlist{flex:1;overflow-y:auto}
.wl-item{padding:8px 12px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .1s;display:flex;flex-direction:column;gap:2px}
.wl-item:hover{background:var(--bg2)}
.wl-item.active{background:var(--bg3);border-left:2px solid var(--accent)}
.wl-item .name{font-family:var(--mono);font-size:12px;font-weight:600;color:var(--txt0)}
.wl-item .price{font-family:var(--mono);font-size:13px;font-weight:700}
.wl-item .row2{display:flex;justify-content:space-between;align-items:center}
.wl-item .chg{font-family:var(--mono);font-size:10px;font-weight:600}
.wl-item .vol{font-family:var(--mono);font-size:9px;color:var(--txt2)}
.sidebar-bot{border-top:1px solid var(--border);flex-shrink:0}

/* ── Center — Chart ─────────────────────────── */
.chart-header{background:var(--bg1);border-bottom:1px solid var(--border);padding:8px 14px;display:flex;align-items:center;gap:10px;flex-shrink:0}
.chart-symbol{font-family:var(--mono);font-weight:700;font-size:15px}
.chart-ohlc{font-family:var(--mono);font-size:11px;color:var(--txt1);display:flex;gap:10px}
.chart-ohlc span{color:var(--txt2);font-size:10px}
.interval-bar{display:flex;gap:2px;margin-left:auto}
.iv-btn{padding:3px 8px;border-radius:3px;border:none;background:transparent;color:var(--txt1);font-family:var(--mono);font-size:11px;cursor:pointer;transition:all .1s}
.iv-btn:hover{background:var(--bg3);color:var(--txt0)}
.iv-btn.active{background:var(--accent-bg);color:var(--accent)}
#chart-container{flex:1;position:relative}
.chart-error{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;color:var(--txt2);font-family:var(--mono);font-size:12px}
.chart-error .err-icon{font-size:32px}

/* ── Bottom tabs ────────────────────────────── */
.bottom-panel{height:220px;flex-shrink:0;background:var(--bg1);border-top:1px solid var(--border);display:flex;flex-direction:column}
.tab-bar{display:flex;border-bottom:1px solid var(--border);flex-shrink:0}
.tab{padding:8px 16px;font-family:var(--mono);font-size:11px;color:var(--txt2);cursor:pointer;border-bottom:2px solid transparent;transition:all .15s}
.tab:hover{color:var(--txt0)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab-content{flex:1;overflow:auto}
.tab-pane{display:none;height:100%}
.tab-pane.active{display:block;height:100%}

/* ── Tables ─────────────────────────────────── */
.data-table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:11px}
.data-table th{padding:6px 10px;text-align:left;color:var(--txt2);font-size:10px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg2);font-weight:500}
.data-table th.r,.data-table td.r{text-align:right}
.data-table td{padding:5px 10px;border-bottom:1px solid var(--border);color:var(--txt0)}
.data-table tr:hover td{background:var(--bg2)}
.tag{padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600}
.tag-ce{background:var(--bull-bg);color:var(--bull)}
.tag-pe{background:var(--bear-bg);color:var(--bear)}
.tag-algo{background:var(--bg3);color:var(--txt1)}
.tag-claude{background:var(--accent-bg);color:var(--accent)}
.tag-local{background:var(--purple-bg);color:var(--purple)}
.reason-tag{padding:1px 5px;border-radius:3px;font-size:9px;background:var(--bg3);color:var(--txt2)}

/* ── Log ────────────────────────────────────── */
.log-pane{height:100%;overflow:auto;padding:6px;background:var(--bg0)}
.log-line{font-family:var(--mono);font-size:10.5px;padding:1px 6px;border-radius:3px;white-space:nowrap;line-height:1.7}
.log-buy{color:var(--bull)} .log-sell{color:var(--bear)} .log-warn{color:var(--gold)}
.log-err{color:var(--bear);font-weight:700} .log-info{color:var(--txt1)} .log-paper{color:var(--purple)}
.log-claude{color:var(--cyan)}

/* ── Right Sidebar ──────────────────────────── */
.r-section{border-bottom:1px solid var(--border);flex-shrink:0}
.r-section-head{padding:8px 12px;font-size:9px;text-transform:uppercase;letter-spacing:1px;color:var(--txt2);font-weight:600;display:flex;justify-content:space-between;align-items:center}
.r-section-body{padding:8px 12px 10px}
.kpi-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.kpi{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:8px 10px}
.kpi .k{font-size:9px;text-transform:uppercase;letter-spacing:.5px;color:var(--txt2);margin-bottom:3px}
.kpi .v{font-family:var(--mono);font-size:15px;font-weight:700}
.kpi .sub{font-family:var(--mono);font-size:9px;color:var(--txt2);margin-top:1px}
.kpi-wide{grid-column:span 2}
.progress-bar{height:4px;background:var(--bg3);border-radius:2px;overflow:hidden;margin-top:6px}
.progress-fill{height:100%;border-radius:2px;transition:width .4s}
/* AI source breakdown */
.source-row{display:flex;align-items:center;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border)}
.source-row:last-child{border-bottom:none}
.source-name{font-family:var(--mono);font-size:11px;font-weight:600}
.source-stats{font-family:var(--mono);font-size:10px;color:var(--txt2)}
/* Connection panel */
.conn-row{display:flex;align-items:center;gap:8px;padding:5px 0;font-family:var(--mono);font-size:11px}
.conn-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.conn-ok{background:var(--bull)} .conn-warn{background:var(--gold)} .conn-bad{background:var(--bear)}
.conn-label{color:var(--txt1);flex:1}
.conn-status{color:var(--txt2);font-size:10px}
.input-row{display:flex;gap:6px;margin-top:6px}
.inp{flex:1;background:var(--bg0);border:1px solid var(--border);border-radius:var(--radius);padding:5px 8px;color:var(--txt0);font-family:var(--mono);font-size:11px;outline:none}
.inp:focus{border-color:var(--accent)}
/* Regime badge */
.regime-badge{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:3px;font-family:var(--mono);font-size:10px;font-weight:700}
.regime-bull{background:var(--bull-bg);color:var(--bull)}
.regime-bear{background:var(--bear-bg);color:var(--bear)}
.regime-side{background:var(--bg3);color:var(--txt1)}
/* Scrollbars */
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--bg4);border-radius:2px}
/* Equity curve chart */
#equity-mini{height:80px;background:var(--bg0);border-radius:var(--radius);margin-top:6px}
/* Depth */
.depth-row{display:flex;align-items:center;gap:0;font-family:var(--mono);font-size:10px;padding:2px 0;position:relative}
.depth-row .d-qty{width:70px;text-align:right;z-index:1}
.depth-row .d-price{width:80px;text-align:center;font-weight:600;z-index:1}
.depth-row .d-orders{width:50px;text-align:left;color:var(--txt2);z-index:1}
.depth-row .d-bar{position:absolute;top:0;bottom:0;opacity:.15;border-radius:2px}
.depth-buy .d-bar{background:var(--bull);right:50%}
.depth-sell .d-bar{background:var(--bear);left:50%}
.depth-buy .d-qty{color:var(--bull)}
.depth-sell .d-qty{color:var(--bear)}
/* Animations */
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
.fade-in{animation:fadeIn .2s ease forwards}
@keyframes flash-green{0%{background:rgba(38,166,154,.3)}100%{background:transparent}}
@keyframes flash-red{0%{background:rgba(239,83,80,.3)}100%{background:transparent}}
.flash-g{animation:flash-green .5s}
.flash-r{animation:flash-red .5s}
/* Modal */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:999;display:none;align-items:center;justify-content:center}
.modal-overlay.show{display:flex}
.modal{background:var(--bg2);border:1px solid var(--border2);border-radius:var(--radius2);padding:24px;min-width:380px;max-width:90vw}
.modal-title{font-family:var(--mono);font-weight:700;font-size:14px;margin-bottom:16px}
.modal-close{float:right;cursor:pointer;color:var(--txt2);background:none;border:none;font-size:16px}
</style>
</head>
<body>
<div class="app">

<!-- ══ TOPBAR ═══════════════════════════════════════════════ -->
<header class="topbar">
  <div class="logo">
    <div class="logo-icon">TC</div>
    Trade_Claude
  </div>
  <div class="topbar-divider"></div>
  <div class="market-chip" id="market-chip">
    <div class="dot" id="market-dot"></div>
    <span id="market-label">—</span>
    <span id="market-time" style="color:var(--txt2);font-size:10px"></span>
  </div>
  <div class="stat-pill">
    <span class="label">Net P&L</span>
    <span class="value" id="tp-net">—</span>
  </div>
  <div class="stat-pill">
    <span class="label">Win Rate</span>
    <span class="value" id="tp-wr">—</span>
  </div>
  <div class="stat-pill">
    <span class="label">Today</span>
    <span class="value" id="tp-today">—</span>
  </div>
  <div class="stat-pill">
    <span class="label">Profit Factor</span>
    <span class="value" id="tp-pf">—</span>
  </div>
  <div class="topbar-right">
    <span class="badge-paper" id="paper-badge" style="display:none">PAPER</span>
    <button class="btn" onclick="refreshAll()">⟳ Refresh</button>
    <button class="btn btn-accent" onclick="showKiteModal()">⚡ Kite Login</button>
  </div>
</header>

<!-- ══ MAIN ═════════════════════════════════════════════════ -->
<div class="main">

  <!-- ── LEFT SIDEBAR ───────────────────────────────────── -->
  <aside class="sidebar-l">
    <div class="section-header">Watchlist</div>
    <div class="watchlist" id="watchlist-body">
      {% for item in watchlist %}
      <div class="wl-item{% if loop.first %} active{% endif %}" id="wl-{{ item.label }}" onclick="selectSymbol('{{ item.symbol }}','{{ item.label }}')">
        <div class="name">{{ item.label }}</div>
        <div class="price" id="wl-price-{{ item.label }}">—</div>
        <div class="row2">
          <span class="chg" id="wl-chg-{{ item.label }}">—</span>
          <span class="vol" id="wl-vol-{{ item.label }}">—</span>
        </div>
      </div>
      {% endfor %}
    </div>
    <div class="sidebar-bot">
      <div class="section-header">Bot Config</div>
      <div style="padding:8px 12px;font-family:var(--mono);font-size:10px;color:var(--txt2);line-height:2">
        <div>Capital: <span style="color:var(--txt0)" id="cfg-cap">—</span></div>
        <div>Max Lots: <span style="color:var(--txt0)" id="cfg-lots">—</span></div>
        <div>SL: <span style="color:var(--bear)" id="cfg-sl">—</span> | T: <span style="color:var(--bull)" id="cfg-tgt">—</span></div>
        <div>Squareoff: <span style="color:var(--gold)" id="cfg-sq">—</span></div>
      </div>
    </div>
  </aside>

  <!-- ── CENTER ─────────────────────────────────────────── -->
  <section class="center">
    <div class="chart-header">
      <span class="chart-symbol" id="chart-sym">NIFTY 50</span>
      <div class="chart-ohlc" id="chart-ohlc">
        <span><span>O</span> <span id="co-o">—</span></span>
        <span><span>H</span> <span id="co-h" class="bull-text">—</span></span>
        <span><span>L</span> <span id="co-l" class="bear-text">—</span></span>
        <span><span>C</span> <span id="co-c">—</span></span>
        <span><span>V</span> <span id="co-v" style="color:var(--txt2)">—</span></span>
      </div>
      <div class="interval-bar" id="iv-bar">
        <button class="iv-btn" onclick="setInterval('1m')">1m</button>
        <button class="iv-btn active" onclick="setInterval('5m')">5m</button>
        <button class="iv-btn" onclick="setInterval('15m')">15m</button>
        <button class="iv-btn" onclick="setInterval('30m')">30m</button>
        <button class="iv-btn" onclick="setInterval('1h')">1h</button>
        <button class="iv-btn" onclick="setInterval('1D')">1D</button>
      </div>
    </div>
    <div id="chart-container">
      <div class="chart-error" id="chart-error" style="display:none">
        <div class="err-icon">📡</div>
        <div id="chart-error-msg">Connect Kite to see live charts</div>
        <button class="btn btn-accent" style="margin-top:8px" onclick="showKiteModal()">Connect Kite</button>
      </div>
    </div>

    <!-- Bottom Tab Panel -->
    <div class="bottom-panel">
      <div class="tab-bar">
        <div class="tab active" onclick="showTab('trades')">Recent Trades</div>
        <div class="tab" onclick="showTab('daily')">Daily P&L</div>
        <div class="tab" onclick="showTab('symbols')">By Symbol</div>
        <div class="tab" onclick="showTab('depth')">Market Depth</div>
        <div class="tab" onclick="showTab('log')">Bot Log</div>
      </div>
      <div class="tab-content">

        <!-- Trades -->
        <div class="tab-pane active" id="pane-trades">
          <table class="data-table" id="trades-table">
            <thead><tr>
              <th>#</th><th>Date</th><th>Symbol</th><th>Dir</th>
              <th class="r">Entry ₹</th><th class="r">Exit ₹</th>
              <th class="r">Qty</th><th class="r">P&L ₹</th>
              <th>Reason</th><th>AI</th>
            </tr></thead>
            <tbody id="trades-body"></tbody>
          </table>
        </div>

        <!-- Daily -->
        <div class="tab-pane" id="pane-daily">
          <table class="data-table" id="daily-table">
            <thead><tr>
              <th>Date</th><th class="r">Trades</th><th class="r">Wins</th>
              <th class="r">Win%</th><th class="r">P&L ₹</th><th class="r">Equity ₹</th>
            </tr></thead>
            <tbody id="daily-body"></tbody>
          </table>
        </div>

        <!-- Symbols -->
        <div class="tab-pane" id="pane-symbols">
          <table class="data-table">
            <thead><tr>
              <th>Symbol</th><th class="r">Trades</th><th class="r">W</th>
              <th class="r">L</th><th class="r">Win%</th>
              <th class="r">Total P&L ₹</th><th class="r">Avg P&L ₹</th>
            </tr></thead>
            <tbody id="symbols-body"></tbody>
          </table>
        </div>

        <!-- Depth -->
        <div class="tab-pane" id="pane-depth">
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:0;height:100%">
            <div style="padding:8px 12px">
              <div style="font-size:9px;text-transform:uppercase;letter-spacing:.5px;color:var(--txt2);margin-bottom:6px">BID</div>
              <div id="depth-buy"></div>
            </div>
            <div style="padding:8px 12px;border-left:1px solid var(--border)">
              <div style="font-size:9px;text-transform:uppercase;letter-spacing:.5px;color:var(--txt2);margin-bottom:6px">ASK</div>
              <div id="depth-sell"></div>
            </div>
          </div>
        </div>

        <!-- Log -->
        <div class="tab-pane" id="pane-log">
          <div class="log-pane" id="log-body"></div>
        </div>

      </div>
    </div>
  </section>

  <!-- ── RIGHT SIDEBAR ──────────────────────────────────── -->
  <aside class="sidebar-r">

    <!-- KPI Grid -->
    <div class="r-section">
      <div class="r-section-head">Performance</div>
      <div class="r-section-body">
        <div class="kpi-grid">
          <div class="kpi kpi-wide">
            <div class="k">Net P&L (All-time)</div>
            <div class="v" id="kpi-net">—</div>
            <div class="sub" id="kpi-gross">Gross: —</div>
          </div>
          <div class="kpi">
            <div class="k">Win Rate</div>
            <div class="v" id="kpi-wr">—</div>
            <div class="progress-bar"><div class="progress-fill bull-bg" id="wr-bar" style="width:0%;background:var(--bull)"></div></div>
          </div>
          <div class="kpi">
            <div class="k">Profit Factor</div>
            <div class="v" id="kpi-pf">—</div>
            <div class="sub">Target >1.5</div>
          </div>
          <div class="kpi">
            <div class="k">Total Trades</div>
            <div class="v" id="kpi-trades">—</div>
            <div class="sub" id="kpi-open">Open: —</div>
          </div>
          <div class="kpi">
            <div class="k">Best Trade</div>
            <div class="v bull-text" id="kpi-best">—</div>
          </div>
          <div class="kpi">
            <div class="k">Worst Trade</div>
            <div class="v bear-text" id="kpi-worst">—</div>
          </div>
          <div class="kpi">
            <div class="k">Today P&L</div>
            <div class="v" id="kpi-today">—</div>
            <div class="sub" id="kpi-today-wr">— trades</div>
          </div>
          <div class="kpi">
            <div class="k">Avg Trade</div>
            <div class="v" id="kpi-avg">—</div>
          </div>
        </div>
        <!-- Mini equity curve -->
        <div id="equity-mini"></div>
      </div>
    </div>

    <!-- AI Sources -->
    <div class="r-section">
      <div class="r-section-head">AI Decision Source</div>
      <div class="r-section-body" id="sources-body" style="padding-top:4px"></div>
    </div>

    <!-- Kite Connection -->
    <div class="r-section" style="flex:1;display:flex;flex-direction:column">
      <div class="r-section-head">
        Kite Connection
        <span id="kite-status-dot" class="conn-dot conn-bad"></span>
      </div>
      <div class="r-section-body" style="flex:1">
        <div class="conn-row">
          <div class="conn-dot" id="kd-key"></div>
          <div class="conn-label">API Key</div>
          <div class="conn-status" id="kd-key-s">—</div>
        </div>
        <div class="conn-row">
          <div class="conn-dot" id="kd-token"></div>
          <div class="conn-label">Access Token</div>
          <div class="conn-status" id="kd-token-s">—</div>
        </div>
        <div class="conn-row">
          <div class="conn-dot" id="kd-market"></div>
          <div class="conn-label">Market</div>
          <div class="conn-status" id="kd-market-s">—</div>
        </div>
        <div style="margin-top:10px;font-family:var(--mono);font-size:10px;color:var(--txt2)" id="kite-reason"></div>
        <div style="margin-top:8px">
          <button class="btn btn-accent" style="width:100%;font-size:11px" onclick="showKiteModal()">⚡ Manage Kite Login</button>
        </div>
      </div>
    </div>

  </aside>
</div>
</div>

<!-- ══ KITE MODAL ════════════════════════════════════════════ -->
<div class="modal-overlay" id="kite-modal">
  <div class="modal fade-in">
    <div class="modal-title">⚡ Kite Connection
      <button class="modal-close" onclick="closeModal()">✕</button>
    </div>

    <!-- Step 1 -->
    <div id="modal-step1">
      <div style="font-family:var(--mono);font-size:11px;color:var(--txt2);margin-bottom:12px">Step 1 — Save API credentials</div>
      <div style="display:flex;flex-direction:column;gap:8px">
        <input class="inp" id="m-api-key" placeholder="Kite API Key">
        <input class="inp" id="m-api-secret" placeholder="Kite API Secret" type="password">
        <button class="btn btn-accent" style="width:100%" onclick="saveKiteConfig()">Save Credentials</button>
      </div>
      <div style="margin-top:14px;font-family:var(--mono);font-size:11px;color:var(--txt2);margin-bottom:8px">Step 2 — Login & get token</div>
      <button class="btn" style="width:100%" onclick="openKiteLogin()">Open Kite Login URL ↗</button>
      <div style="margin-top:10px">
        <input class="inp" id="m-req-token" placeholder="Paste request_token from redirect URL" style="width:100%;margin-bottom:6px">
        <button class="btn btn-accent" style="width:100%" onclick="submitKiteToken()">Generate Access Token</button>
      </div>
      <div id="modal-msg" style="margin-top:8px;font-family:var(--mono);font-size:11px;min-height:18px"></div>
    </div>
  </div>
</div>

<script>
// ════════════════════════════════════════════════════════
//  STATE
// ════════════════════════════════════════════════════════
let currentSymbol = 'NIFTY 50';
let currentInterval = '5m';
let chart = null, candleSeries = null, volSeries = null;
let equityChart = null, equitySeries = null;
let overviewData = null;
let autoRefreshTimer = null;

// ════════════════════════════════════════════════════════
//  HELPERS
// ════════════════════════════════════════════════════════
const fmt  = (n, d=2) => n == null ? '—' : Number(n).toLocaleString('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d});
const fmtK = n => n == null ? '—' : (Math.abs(n)>=1e5 ? (n/1e5).toFixed(1)+'L' : fmt(n,0));
const pnlClass = n => n > 0 ? 'bull-text' : n < 0 ? 'bear-text' : '';
const pnlSign  = n => n > 0 ? '+' : '';
const aiTag  = ai => {
  const a = (ai||'').toUpperCase();
  if(a.includes('CLAUDE')) return `<span class="tag tag-claude">Claude</span>`;
  if(a.includes('LOCAL'))  return `<span class="tag tag-local">Local</span>`;
  return `<span class="tag tag-algo">Algo</span>`;
};
const dirTag = sym => {
  if((sym||'').endsWith('CE')) return `<span class="tag tag-ce">CE</span>`;
  if((sym||'').endsWith('PE')) return `<span class="tag tag-pe">PE</span>`;
  return `<span class="tag tag-algo">${sym||'—'}</span>`;
};
function setTxt(id, txt, cls=''){
  const el = document.getElementById(id);
  if(!el) return;
  el.textContent = txt;
  el.className = cls;
}
function setHTML(id, html){ const el=document.getElementById(id); if(el) el.innerHTML=html; }

// ════════════════════════════════════════════════════════
//  CHART
// ════════════════════════════════════════════════════════
function initChart(){
  const container = document.getElementById('chart-container');
  if(chart){ chart.remove(); chart=null; }
  chart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: container.clientHeight,
    layout: { background:{type:'solid',color:'#0b0e11'}, textColor:'#9199af' },
    grid: { vertLines:{color:'#1c2030'}, horzLines:{color:'#1c2030'} },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal,
      vertLine:{color:'#3d4a6b',width:1,style:LightweightCharts.LineStyle.Dashed},
      horzLine:{color:'#3d4a6b',width:1,style:LightweightCharts.LineStyle.Dashed}},
    rightPriceScale: { borderColor:'#2e3650', scaleMargins:{top:.1,bottom:.2} },
    timeScale: { borderColor:'#2e3650', timeVisible:true, secondsVisible:false },
  });
  candleSeries = chart.addCandlestickSeries({
    upColor:'#26a69a', downColor:'#ef5350',
    borderUpColor:'#26a69a', borderDownColor:'#ef5350',
    wickUpColor:'#26a69a', wickDownColor:'#ef5350',
  });
  volSeries = chart.addHistogramSeries({
    priceFormat:{type:'volume'}, priceScaleId:'vol',
    scaleMargins:{top:.8,bottom:0},
  });
  chart.priceScale('vol').applyOptions({ scaleMargins:{top:.85,bottom:0} });
  chart.subscribeCrosshairMove(param => {
    if(!param.time || !param.seriesData) return;
    const d = param.seriesData.get(candleSeries);
    if(!d) return;
    setTxt('co-o', fmt(d.open)); setTxt('co-h', fmt(d.high));
    setTxt('co-l', fmt(d.low));  setTxt('co-c', fmt(d.close));
    const v = param.seriesData.get(volSeries);
    if(v) setTxt('co-v', fmtK(v.value));
  });
  new ResizeObserver(()=>{ if(chart) chart.applyOptions({width:container.clientWidth,height:container.clientHeight}); }).observe(container);
}

async function loadCandles(){
  document.getElementById('chart-error').style.display='none';
  try{
    const r = await fetch(`/api/candles?symbol=${encodeURIComponent(currentSymbol)}&interval=${currentInterval}`);
    const d = await r.json();
    if(d.error || !d.candles || !d.candles.length){
      document.getElementById('chart-error').style.display='flex';
      document.getElementById('chart-error-msg').textContent = d.error || 'No candle data';
      return;
    }
    if(!chart) initChart();
    const candles = d.candles.map(c=>({time:c.time,open:c.open,high:c.high,low:c.low,close:c.close}));
    const vols    = d.candles.map(c=>({time:c.time,value:c.volume,color:c.close>=c.open?'rgba(38,166,154,.4)':'rgba(239,83,80,.4)'}));
    candleSeries.setData(candles);
    volSeries.setData(vols);
    chart.timeScale().fitContent();
    const last = d.candles[d.candles.length-1];
    if(last){
      setTxt('co-o', fmt(last.open)); setTxt('co-h', fmt(last.high));
      setTxt('co-l', fmt(last.low));  setTxt('co-c', fmt(last.close));
      setTxt('co-v', fmtK(last.volume));
    }
  } catch(e){
    document.getElementById('chart-error').style.display='flex';
    document.getElementById('chart-error-msg').textContent='Kite not connected';
  }
}

function setInterval(iv){
  currentInterval = iv;
  document.querySelectorAll('.iv-btn').forEach(b=>b.classList.remove('active'));
  event.target.classList.add('active');
  loadCandles();
}

function selectSymbol(sym, label){
  currentSymbol = sym;
  setTxt('chart-sym', label);
  document.querySelectorAll('.wl-item').forEach(el=>el.classList.remove('active'));
  document.getElementById('wl-'+label)?.classList.add('active');
  loadCandles();
  loadDepth();
}

// ════════════════════════════════════════════════════════
//  EQUITY MINI CHART
// ════════════════════════════════════════════════════════
function initEquityMini(daily){
  const container = document.getElementById('equity-mini');
  if(equityChart){ equityChart.remove(); equityChart=null; }
  if(!daily||!daily.length) return;
  equityChart = LightweightCharts.createChart(container,{
    width:container.clientWidth, height:76,
    layout:{background:{type:'solid',color:'#0b0e11'},textColor:'#5d6480'},
    grid:{vertLines:{color:'transparent'},horzLines:{color:'#1c2030'}},
    rightPriceScale:{visible:false}, leftPriceScale:{visible:false},
    timeScale:{visible:false}, handleScroll:false, handleScale:false,
  });
  equitySeries = equityChart.addAreaSeries({
    lineColor:'#2962ff', topColor:'rgba(41,98,255,.2)',
    bottomColor:'rgba(41,98,255,0)', lineWidth:2,
  });
  const pts = daily.map(d=>({time:Math.floor(new Date(d.date).getTime()/1000), value:d.equity}));
  equitySeries.setData(pts);
  equityChart.timeScale().fitContent();
}

// ════════════════════════════════════════════════════════
//  OVERVIEW DATA
// ════════════════════════════════════════════════════════
async function loadOverview(){
  try{
    const r = await fetch('/api/overview');
    const d = await r.json();
    overviewData = d;
    renderOverview(d);
  } catch(e){ console.warn('Overview load failed', e); }
}

function renderOverview(d){
  const s = d.summary || {};
  const net = s.net_pnl || 0;
  const wr  = s.win_rate || 0;
  const pf  = s.profit_factor || 0;

  // Topbar
  setHTML('tp-net',   `<span class="${pnlClass(net)}">${pnlSign(net)}₹${fmt(Math.abs(net),0)}</span>`);
  setHTML('tp-wr',    `<span class="${wr>=55?'bull-text':wr>=45?'':'bear-text'}">${wr}%</span>`);
  setHTML('tp-today', `<span class="${pnlClass(s.today_pnl)}">${pnlSign(s.today_pnl)}₹${fmt(Math.abs(s.today_pnl||0),0)}</span>`);
  setHTML('tp-pf',    `<span class="${pf>=1.5?'bull-text':pf>=1?'':'bear-text'}">${pf}x</span>`);

  // KPIs
  setHTML('kpi-net',   `<span class="${pnlClass(net)}">${pnlSign(net)}₹${fmtK(net)}</span>`);
  setTxt('kpi-gross',  `Gross: ₹${fmtK(s.gross_pnl)} | Charges: ₹${fmtK(s.charges)}`);
  setHTML('kpi-wr',    `<span class="${wr>=55?'bull-text':wr>=45?'':'bear-text'}">${wr}%</span>`);
  document.getElementById('wr-bar').style.width = Math.min(wr,100)+'%';
  setHTML('kpi-pf',    `<span class="${pf>=1.5?'bull-text':pf>=1?'':'bear-text'}">${pf}x</span>`);
  setTxt('kpi-trades', s.total_trades||0);
  setTxt('kpi-open',   `Open: ${s.open_trades||0} | Closed: ${s.closed_trades||0}`);
  setHTML('kpi-best',  `+₹${fmt(s.best_trade||0,0)}`);
  setHTML('kpi-worst', `-₹${fmt(Math.abs(s.worst_trade||0),0)}`);
  const td = s.today_pnl||0;
  setHTML('kpi-today', `<span class="${pnlClass(td)}">${pnlSign(td)}₹${fmt(Math.abs(td),0)}</span>`);
  setTxt('kpi-today-wr', `${s.today_trades||0} trades, ${s.today_winners||0} wins`);
  setHTML('kpi-avg',   `<span class="${pnlClass(s.avg_pnl)}">${pnlSign(s.avg_pnl||0)}₹${fmt(Math.abs(s.avg_pnl||0),0)}</span>`);

  // Market
  const mkt = d.market || {};
  document.getElementById('market-dot').className = 'dot'+(mkt.is_open?' open':'');
  setTxt('market-label', mkt.label||'—');
  setTxt('market-time', (mkt.time||'').split(' ')[1]||'');
  setTxt('kd-market-s', mkt.label||'—');
  document.getElementById('kd-market').className = 'conn-dot '+(mkt.is_open?'conn-ok':'conn-warn');

  // Kite status
  const kite = d.kite || {};
  document.getElementById('kite-status-dot').className = 'conn-dot '+(kite.connected?'conn-ok':'conn-bad');
  setDot('kd-key',   kite.configured);
  setDot('kd-token', kite.token);
  setTxt('kd-key-s',   kite.configured?'Configured':'Missing');
  setTxt('kd-token-s', kite.token?'Valid':'Missing');
  setTxt('kite-reason', kite.reason||'');

  // Bot config
  const bot = d.bot || {};
  setTxt('cfg-cap',  bot.capital ? `₹${Number(bot.capital).toLocaleString('en-IN')}` : '—');
  setTxt('cfg-lots', bot.max_lots||'—');
  setTxt('cfg-sl',   bot.sl_pct  ? `${parseFloat(bot.sl_pct)*100}%` : '—');
  setTxt('cfg-tgt',  bot.target_pct ? `${parseFloat(bot.target_pct)*100}%` : '—');
  setTxt('cfg-sq',   bot.squareoff_time||'—');
  if(bot.paper_trade){ document.getElementById('paper-badge').style.display='inline-flex'; }

  // AI Sources
  renderSources(d.sources||[]);

  // Trades table
  renderTrades(d.recent_trades||[]);

  // Daily table + equity mini
  renderDaily(d.daily||[]);
  initEquityMini(d.daily||[]);

  // Symbol table
  renderSymbols(d.symbols||[]);

  // Log
  renderLog(d.logs||[]);

  // Watchlist quotes (try live, fallback to nothing)
  loadQuotes();
}

function setDot(id, ok){
  const el = document.getElementById(id);
  if(el) el.className = 'conn-dot '+(ok?'conn-ok':'conn-bad');
}

// ════════════════════════════════════════════════════════
//  TABLES
// ════════════════════════════════════════════════════════
function renderTrades(rows){
  const tbody = document.getElementById('trades-body');
  if(!tbody) return;
  if(!rows.length){ tbody.innerHTML=`<tr><td colspan="10" style="text-align:center;color:var(--txt2);padding:20px">No trades yet</td></tr>`; return; }
  tbody.innerHTML = rows.map((r,i)=>{
    const pnl = r.pnl||0; const cls = pnlClass(pnl);
    const isOpen = !r.close_price;
    return `<tr class="fade-in">
      <td style="color:var(--txt2)">${r.id}</td>
      <td style="color:var(--txt2)">${(r.date||'').slice(5)}</td>
      <td style="font-family:var(--mono);font-weight:600">${r.symbol||'—'}</td>
      <td>${dirTag(r.symbol)}</td>
      <td class="r">₹${fmt(r.price||0)}</td>
      <td class="r">${isOpen?'<span style="color:var(--gold)">OPEN</span>':'₹'+fmt(r.close_price)}</td>
      <td class="r">${r.qty||0}</td>
      <td class="r"><span class="${cls}" style="font-weight:600">${isOpen?'—':pnlSign(pnl)+'₹'+fmt(Math.abs(pnl),0)}</span></td>
      <td><span class="reason-tag">${r.reason||'—'}</span></td>
      <td>${aiTag(r.ai_decision)}</td>
    </tr>`;
  }).join('');
}

function renderDaily(rows){
  const tbody = document.getElementById('daily-body');
  if(!tbody) return;
  if(!rows.length){ tbody.innerHTML=`<tr><td colspan="6" style="text-align:center;color:var(--txt2);padding:20px">No daily data</td></tr>`; return; }
  tbody.innerHTML = [...rows].reverse().map(r=>{
    const pnl=r.pnl||0; const eq=r.equity||0;
    return `<tr>
      <td style="font-family:var(--mono)">${r.date}</td>
      <td class="r">${r.trades||0}</td>
      <td class="r" style="color:var(--bull)">${r.wins||0}</td>
      <td class="r"><span class="${r.win_rate>=55?'bull-text':r.win_rate>=45?'':'bear-text'}">${r.win_rate||0}%</span></td>
      <td class="r"><span class="${pnlClass(pnl)}" style="font-weight:600">${pnlSign(pnl)}₹${fmt(Math.abs(pnl),0)}</span></td>
      <td class="r"><span class="${pnlClass(eq)}">₹${fmt(Math.abs(eq),0)}</span></td>
    </tr>`;
  }).join('');
}

function renderSymbols(rows){
  const tbody = document.getElementById('symbols-body');
  if(!tbody) return;
  if(!rows.length){ tbody.innerHTML=`<tr><td colspan="7" style="text-align:center;color:var(--txt2);padding:20px">No data</td></tr>`; return; }
  tbody.innerHTML = rows.map(r=>{
    const pnl=r.pnl||0;
    return `<tr>
      <td style="font-family:var(--mono);font-weight:600">${r.symbol}</td>
      <td class="r">${r.trades}</td>
      <td class="r" style="color:var(--bull)">${r.wins||0}</td>
      <td class="r" style="color:var(--bear)">${r.losses||0}</td>
      <td class="r"><span class="${r.win_rate>=55?'bull-text':''}">${r.win_rate||0}%</span></td>
      <td class="r"><span class="${pnlClass(pnl)}" style="font-weight:600">${pnlSign(pnl)}₹${fmt(Math.abs(pnl),0)}</span></td>
      <td class="r"><span class="${pnlClass(r.avg_pnl)}">${pnlSign(r.avg_pnl||0)}₹${fmt(Math.abs(r.avg_pnl||0),0)}</span></td>
    </tr>`;
  }).join('');
}

function renderSources(rows){
  const el = document.getElementById('sources-body');
  if(!el) return;
  if(!rows.length){ el.innerHTML=`<div style="color:var(--txt2);font-family:var(--mono);font-size:11px">No data</div>`; return; }
  el.innerHTML = rows.map(r=>{
    const pnl=r.pnl||0;
    return `<div class="source-row">
      <div><div class="source-name">${aiTag(r.source)} ${r.source}</div>
      <div class="source-stats">${r.trades} trades · ${r.win_rate}% WR</div></div>
      <div class="${pnlClass(pnl)}" style="font-family:var(--mono);font-size:12px;font-weight:600">${pnlSign(pnl)}₹${fmt(Math.abs(pnl),0)}</div>
    </div>`;
  }).join('');
}

function renderLog(lines){
  const el = document.getElementById('log-body');
  if(!el) return;
  el.innerHTML = lines.slice().reverse().map(line=>{
    let cls='log-info';
    if(line.includes('[PAPER]')||line.includes('PAPER TRADE')) cls='log-paper';
    else if(line.includes('BUY')||line.includes('✅')) cls='log-buy';
    else if(line.includes('SELL')||line.includes('🔴')) cls='log-sell';
    else if(line.includes('ERROR')) cls='log-err';
    else if(line.includes('WARNING')||line.includes('⚠')) cls='log-warn';
    else if(line.includes('Claude')) cls='log-claude';
    const escaped = line.replace(/</g,'&lt;').replace(/>/g,'&gt;');
    return `<div class="log-line ${cls}">${escaped}</div>`;
  }).join('');
}

// ════════════════════════════════════════════════════════
//  LIVE QUOTES
// ════════════════════════════════════════════════════════
async function loadQuotes(){
  try{
    const r = await fetch('/api/quotes');
    const d = await r.json();
    if(d.error||!d.quotes) return;
    d.quotes.forEach(q=>{
      const label = q.label;
      const el = document.getElementById('wl-'+label);
      const chg = q.change||0; const pct = q.change_pct||0;
      const prevPrice = el?.querySelector('.price')?.textContent;
      const priceEl = document.getElementById('wl-price-'+label);
      if(priceEl){
        priceEl.textContent = '₹'+fmt(q.last_price,2);
        priceEl.className = 'price '+(chg>=0?'bull-text':'bear-text');
        if(prevPrice && prevPrice!=='—'){
          const prev=parseFloat(prevPrice.replace(/[₹,]/g,''));
          if(q.last_price>prev) el?.classList.add('flash-g');
          else if(q.last_price<prev) el?.classList.add('flash-r');
          setTimeout(()=>el?.classList.remove('flash-g','flash-r'),500);
        }
      }
      const chgEl = document.getElementById('wl-chg-'+label);
      if(chgEl){
        chgEl.textContent = `${chg>=0?'+':''}${fmt(chg,2)} (${pct>=0?'+':''}${pct}%)`;
        chgEl.className = 'chg '+(chg>=0?'bull-text':'bear-text');
      }
      const volEl = document.getElementById('wl-vol-'+label);
      if(volEl) volEl.textContent = 'Vol '+fmtK(q.volume);
    });
  } catch(e){}
}

async function loadDepth(){
  try{
    const r = await fetch(`/api/depth?symbol=${encodeURIComponent(currentSymbol)}`);
    const d = await r.json();
    if(d.error||!d.depth) return;
    renderDepth(d.depth);
  } catch(e){}
}

function renderDepth(depth){
  const buys  = (depth.buy  || []).slice(0,5);
  const sells = (depth.sell || []).slice(0,5);
  const maxQ = Math.max(...buys.map(b=>b.quantity||0), ...sells.map(s=>s.quantity||0), 1);
  document.getElementById('depth-buy').innerHTML = buys.map(b=>{
    const pct = (b.quantity/maxQ*100).toFixed(0);
    return `<div class="depth-row depth-buy">
      <div class="d-bar" style="width:${pct}%"></div>
      <div class="d-qty">${fmtK(b.quantity)}</div>
      <div class="d-price">₹${fmt(b.price,2)}</div>
      <div class="d-orders">${b.orders||0} ord</div>
    </div>`;
  }).join('');
  document.getElementById('depth-sell').innerHTML = sells.map(s=>{
    const pct = (s.quantity/maxQ*100).toFixed(0);
    return `<div class="depth-row depth-sell">
      <div class="d-qty">${fmtK(s.quantity)}</div>
      <div class="d-price">₹${fmt(s.price,2)}</div>
      <div class="d-orders">${s.orders||0} ord</div>
      <div class="d-bar" style="width:${pct}%"></div>
    </div>`;
  }).join('');
}

// ════════════════════════════════════════════════════════
//  TABS
// ════════════════════════════════════════════════════════
function showTab(name){
  document.querySelectorAll('.tab').forEach((t,i)=>{
    const tabs = ['trades','daily','symbols','depth','log'];
    t.classList.toggle('active', tabs[i]===name);
  });
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  document.getElementById('pane-'+name)?.classList.add('active');
  if(name==='depth') loadDepth();
}

// ════════════════════════════════════════════════════════
//  KITE MODAL
// ════════════════════════════════════════════════════════
function showKiteModal(){ document.getElementById('kite-modal').classList.add('show'); }
function closeModal(){ document.getElementById('kite-modal').classList.remove('show'); }
function modalMsg(msg, ok=false){
  const el = document.getElementById('modal-msg');
  el.textContent = msg;
  el.style.color = ok ? 'var(--bull)' : 'var(--bear)';
}

async function saveKiteConfig(){
  const k = document.getElementById('m-api-key').value.trim();
  const s = document.getElementById('m-api-secret').value.trim();
  if(!k||!s){ modalMsg('Enter both key and secret'); return; }
  try{
    const r = await fetch('/api/kite/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:k,api_secret:s})});
    const d = await r.json();
    if(d.ok) modalMsg('✓ Credentials saved', true);
    else modalMsg(d.error||'Failed');
  } catch(e){ modalMsg('Request failed'); }
}

async function openKiteLogin(){
  try{
    const r = await fetch('/api/kite/login-url');
    const d = await r.json();
    if(d.login_url) window.open(d.login_url, '_blank');
    else modalMsg(d.error||'Set API key first');
  } catch(e){ modalMsg('Failed to get login URL'); }
}

async function submitKiteToken(){
  const rt = document.getElementById('m-req-token').value.trim();
  if(!rt){ modalMsg('Enter request_token'); return; }
  try{
    const r = await fetch('/api/kite/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_token:rt})});
    const d = await r.json();
    if(d.ok){ modalMsg('✓ '+d.message, true); setTimeout(()=>{closeModal();refreshAll();}, 1500); }
    else modalMsg(d.error||'Failed');
  } catch(e){ modalMsg('Request failed'); }
}

// ════════════════════════════════════════════════════════
//  REFRESH
// ════════════════════════════════════════════════════════
async function refreshAll(){
  await loadOverview();
  await loadCandles();
}

// ════════════════════════════════════════════════════════
//  INIT
// ════════════════════════════════════════════════════════
window.addEventListener('load', ()=>{
  initChart();
  refreshAll();
  // Auto-refresh every 30s
  autoRefreshTimer = setInterval(()=>{
    loadOverview();
    loadQuotes();
    if(document.getElementById('pane-depth').classList.contains('active')) loadDepth();
  }, 30000);
  // Refresh candles every 60s
  setInterval(loadCandles, 60000);
});

window.addEventListener('click', e=>{
  if(e.target === document.getElementById('kite-modal')) closeModal();
});
</script>
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Trade Claude web dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5050)
    args = parser.parse_args()
    print(f"\n  Trade_Claude Dashboard → http://{args.host}:{args.port}\n")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()