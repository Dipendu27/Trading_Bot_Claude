#!/usr/bin/env python3
"""
Web monitor for the main Trade Claude bot.

This does not place orders. It reads the local journal database and bot log,
then renders a browser dashboard with a TradingView chart widget.

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

from flask import Flask, jsonify, render_template

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python <3.9 fallback
    ZoneInfo = None


PROJECT_ROOT = Path(__file__).resolve().parent
DB_FILE = PROJECT_ROOT / "trades.db"
LOG_FILE = PROJECT_ROOT / "bot.log"
BOT_FILE = PROJECT_ROOT / "bot.py"

WATCHLIST = [
    {"label": "RELIANCE", "tv": "BSE:RELIANCE"},
    {"label": "TCS", "tv": "BSE:TCS"},
    {"label": "HDFCBANK", "tv": "BSE:HDFCBANK"},
    {"label": "INFY", "tv": "BSE:INFY"},
    {"label": "ICICIBANK", "tv": "BSE:ICICIBANK"},
    {"label": "SBIN", "tv": "BSE:SBIN"},
    {"label": "ITC", "tv": "BSE:ITC"},
    {"label": "LT", "tv": "BSE:LT"},
]

app = Flask(__name__)


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
    if not BOT_FILE.exists():
        return {}
    text = BOT_FILE.read_text()

    def find_value(key: str) -> str | None:
        match = re.search(rf'"{re.escape(key)}"\s*:\s*([^,\n#]+)', text)
        if not match:
            return None
        return match.group(1).strip().strip('"').strip("'")

    paper_match = re.search(r"PAPER_TRADE\s*:\s*bool\s*=\s*(True|False)", text)
    indices = re.findall(r'"(NIFTY 50|NIFTY BANK)"\s*:', text)
    return {
        "paper_trade": paper_match.group(1) == "True" if paper_match else None,
        "capital": find_value("capital"),
        "max_lots": find_value("max_lots"),
        "sl_pct": find_value("sl_pct"),
        "target_pct": find_value("target_pct"),
        "squareoff_time": find_value("squareoff_time"),
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


@app.route("/api/health")
def api_health():
    return jsonify(
        {
            "ok": True,
            "database": DB_FILE.exists(),
            "log": LOG_FILE.exists(),
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
