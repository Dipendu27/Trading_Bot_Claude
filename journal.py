"""
Trade Journal — SQLite-backed P&L tracker for Trade_Claude
============================================================
Imported by bot.py automatically. Also run standalone for reports.

Usage:
    python journal.py              # today's summary
    python journal.py --all        # all-time summary
    python journal.py --csv        # export to trades.csv
"""

import sqlite3, datetime, argparse, csv, os

DB_FILE = "trades.db"

# ──────────────────────────────────────────────
#  SCHEMA
# ──────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_FILE)
    con.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT,
            symbol      TEXT,
            side        TEXT,        -- BUY / SELL
            qty         INTEGER,
            price       REAL,        -- entry premium (options) or entry price (equity)
            sl          REAL,
            target      REAL,
            close_price REAL,
            close_time  TEXT,
            reason      TEXT,        -- signal / stop-loss / target / eod-squareoff / signal-reversal
            pnl         REAL,
            ai_decision TEXT,        -- CLAUDE / LOCAL / ALGO
            created_at  TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS daily_summary (
            date        TEXT PRIMARY KEY,
            trades      INTEGER,
            winners     INTEGER,
            losers      INTEGER,
            gross_pnl   REAL,
            charges     REAL,
            net_pnl     REAL,
            win_rate    REAL
        )
    """)
    con.commit()
    con.close()

# ──────────────────────────────────────────────
#  WRITE
# ──────────────────────────────────────────────
def log_entry(symbol: str, qty: int, price: float, sl: float, target: float,
              ai_decision: str = "ALGO", **kwargs) -> int:
    """Log a new trade entry. Returns the row id."""
    con = sqlite3.connect(DB_FILE)
    con.execute("""
        INSERT INTO trades (date, symbol, side, qty, price, sl, target, ai_decision)
        VALUES (?, ?, 'BUY', ?, ?, ?, ?, ?)
    """, (datetime.date.today().isoformat(), symbol, qty, price, sl, target, ai_decision))
    con.commit()
    last_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.close()
    return last_id

def log_exit(trade_id: int, close_price: float, reason: str, pnl: float):
    con = sqlite3.connect(DB_FILE)
    con.execute("""
        UPDATE trades
        SET close_price=?, close_time=datetime('now','localtime'), reason=?, pnl=?
        WHERE id=?
    """, (close_price, reason, pnl, trade_id))
    con.commit()
    con.close()

def update_daily_summary():
    today = datetime.date.today().isoformat()
    con   = sqlite3.connect(DB_FILE)
    rows  = con.execute("""
        SELECT pnl FROM trades
        WHERE date=? AND close_price IS NOT NULL
    """, (today,)).fetchall()
    if not rows:
        con.close()
        return
    pnls    = [r[0] for r in rows]
    winners = sum(1 for p in pnls if p > 0)
    losers  = sum(1 for p in pnls if p <= 0)
    gross   = sum(pnls)
    # Options brokerage: ₹20 flat × 2 legs + STT ≈ ₹50 per trade (rough estimate)
    charges = len(pnls) * 50
    net     = gross - charges
    win_rate = winners / len(pnls) if pnls else 0

    con.execute("""
        INSERT INTO daily_summary (date, trades, winners, losers, gross_pnl, charges, net_pnl, win_rate)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            trades=excluded.trades, winners=excluded.winners,
            losers=excluded.losers, gross_pnl=excluded.gross_pnl,
            charges=excluded.charges, net_pnl=excluded.net_pnl,
            win_rate=excluded.win_rate
    """, (today, len(pnls), winners, losers, gross, charges, net, win_rate))
    con.commit()
    con.close()

# ──────────────────────────────────────────────
#  REPORTS
# ──────────────────────────────────────────────
def today_report():
    today = datetime.date.today().isoformat()
    con   = sqlite3.connect(DB_FILE)
    rows  = con.execute("""
        SELECT symbol, qty, price, close_price, reason, pnl, ai_decision
        FROM trades WHERE date=? ORDER BY id
    """, (today,)).fetchall()
    con.close()

    print(f"\n{'─'*80}")
    print(f"  Trade Journal — {today}  (options mode)")
    print(f"{'─'*80}")
    print(f"  {'Symbol':<32} {'Qty':>5} {'Entry':>8} {'Exit':>8} {'Reason':<16} {'P&L':>8}  AI")
    print(f"{'─'*80}")
    total = 0
    for sym, qty, entry, exit_, reason, pnl, ai in rows:
        pnl   = pnl or 0
        total += pnl
        exit_str = f"₹{exit_:.2f}" if exit_ else "OPEN"
        print(f"  {sym:<32} {qty:>5} ₹{entry:>7.2f} {exit_str:>8} "
              f"{(reason or 'OPEN'):<16} ₹{pnl:>7.0f}  {ai}")
    closed = sum(1 for r in rows if r[3] is not None)
    charges = closed * 50
    print(f"{'─'*80}")
    print(f"  Gross P&L     : ₹{total:.0f}")
    print(f"  Est. charges  : ₹{charges:.0f}  ({closed} closed trades × ₹50)")
    print(f"  Net P&L       : ₹{total - charges:.0f}")
    print(f"{'─'*80}\n")

def alltime_report():
    con  = sqlite3.connect(DB_FILE)
    rows = con.execute("""
        SELECT date, trades, winners, losers, gross_pnl, net_pnl, win_rate
        FROM daily_summary ORDER BY date
    """).fetchall()
    con.close()

    print(f"\n{'─'*72}")
    print(f"  All-Time Summary")
    print(f"{'─'*72}")
    print(f"  {'Date':<12} {'Trades':>6} {'W':>4} {'L':>4} {'Gross P&L':>11} {'Net P&L':>10} {'WR':>6}")
    print(f"{'─'*72}")
    total_net = 0
    for date, trades, w, l, gross, net, wr in rows:
        total_net += net or 0
        print(f"  {date:<12} {trades:>6} {w:>4} {l:>4} "
              f"₹{gross:>10.0f} ₹{net:>9.0f} {wr*100:>5.1f}%")
    print(f"{'─'*72}")
    print(f"  Cumulative Net P&L: ₹{total_net:.0f}")
    print(f"{'─'*72}\n")

def export_csv():
    con  = sqlite3.connect(DB_FILE)
    rows = con.execute("SELECT * FROM trades ORDER BY id").fetchall()
    cols = [d[0] for d in con.description]
    con.close()
    with open("trades.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    print(f"Exported {len(rows)} trades → trades.csv")

# ──────────────────────────────────────────────
#  INIT ON IMPORT
# ──────────────────────────────────────────────
init_db()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="All-time summary")
    ap.add_argument("--csv", action="store_true", help="Export to trades.csv")
    args = ap.parse_args()
    if args.all:
        alltime_report()
    elif args.csv:
        export_csv()
    else:
        today_report()
