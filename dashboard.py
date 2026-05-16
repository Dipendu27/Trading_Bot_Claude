"""
Live Dashboard — Terminal UI for the AI Trading Bot
=====================================================
Run in a second terminal alongside bot.py:
    python dashboard.py

Reads the shared state files written by bot.py every few seconds
and shows a live, colour-coded terminal view.

Requires: pip install rich
"""

import time, json, os, datetime, sqlite3
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich import box

console = Console()
DB_FILE  = "trades.db"
LOG_FILE = "bot.log"

# ─────────────────────────────────────────────
#  READ LAST N LINES FROM LOG
# ─────────────────────────────────────────────
def tail_log(n=12) -> list[str]:
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        buf  = b""
        pos  = size
        lines_found = 0
        while pos > 0 and lines_found < n + 1:
            pos   = max(0, pos - 4096)
            f.seek(pos)
            chunk = f.read(min(4096, size - pos))
            buf   = chunk + buf
            lines_found = buf.count(b"\n")
        return buf.decode(errors="replace").strip().split("\n")[-n:]

# ─────────────────────────────────────────────
#  READ DB
# ─────────────────────────────────────────────
def get_today_trades():
    if not os.path.exists(DB_FILE):
        return []
    today = datetime.date.today().isoformat()
    con   = sqlite3.connect(DB_FILE)
    rows  = con.execute("""
        SELECT symbol, qty, price, close_price, reason, pnl, ai_decision, created_at
        FROM trades WHERE date=? ORDER BY id
    """, (today,)).fetchall()
    con.close()
    return rows

def get_alltime_net():
    if not os.path.exists(DB_FILE):
        return 0
    con = sqlite3.connect(DB_FILE)
    r   = con.execute("SELECT SUM(net_pnl) FROM daily_summary").fetchone()
    con.close()
    return r[0] or 0

# ─────────────────────────────────────────────
#  BUILD DISPLAY
# ─────────────────────────────────────────────
def build_display():
    now    = datetime.datetime.now().strftime("%H:%M:%S")
    trades = get_today_trades()
    logs   = tail_log(10)

    # ── Header ────────────────────────────────
    header = Panel(
        Text.assemble(
            ("  🤖 Zerodha AI Bot  ", "bold cyan"),
            (f"│  {datetime.date.today()}  {now}  ", "dim"),
            ("│  Powered by Claude claude-sonnet-4-6", "bold green"),
        ),
        box=box.HEAVY, style="cyan"
    )

    # ── Today's Trades ────────────────────────
    trade_table = Table(
        title="Today's Trades", box=box.SIMPLE_HEAVY,
        show_header=True, header_style="bold yellow",
        min_width=70,
    )
    trade_table.add_column("Symbol",    style="bold white", width=12)
    trade_table.add_column("Qty",       justify="right", width=5)
    trade_table.add_column("Entry",     justify="right", width=9)
    trade_table.add_column("Exit",      justify="right", width=9)
    trade_table.add_column("Reason",    width=14)
    trade_table.add_column("P&L",       justify="right", width=9)
    trade_table.add_column("AI",        width=8)

    gross_pnl = 0
    open_count = 0
    for t in trades:
        sym, qty, entry, exit_, reason, pnl, ai, created = t
        pnl     = pnl or 0
        gross_pnl += pnl
        is_open = exit_ is None
        if is_open:
            open_count += 1

        pnl_str  = f"₹{pnl:+.0f}" if not is_open else "OPEN"
        pnl_col  = "green" if pnl > 0 else ("red" if pnl < 0 else "white")
        exit_str = f"₹{exit_:.2f}" if exit_ else "[dim]–[/dim]"

        trade_table.add_row(
            sym,
            str(qty),
            f"₹{entry:.2f}",
            exit_str,
            reason or "OPEN",
            f"[{pnl_col}]{pnl_str}[/{pnl_col}]",
            ai or "ALGO",
        )

    if not trades:
        trade_table.add_row("[dim]No trades today[/dim]", "", "", "", "", "", "")

    # ── Stats panel ───────────────────────────
    closed = [t for t in trades if t[3] is not None]
    winners = sum(1 for t in closed if (t[5] or 0) > 0)
    wr = f"{winners/len(closed)*100:.1f}%" if closed else "–"
    charges = len(closed) * 40
    net = gross_pnl - charges
    alltime = get_alltime_net()

    stats = Table(box=box.SIMPLE, show_header=False, min_width=30)
    stats.add_column("Key",   style="dim", width=18)
    stats.add_column("Value", style="bold white", width=12)
    stats.add_row("Trades today",   str(len(trades)))
    stats.add_row("Open positions", f"[yellow]{open_count}[/yellow]")
    stats.add_row("Win rate",       wr)
    stats.add_row("Gross P&L",      f"₹{gross_pnl:+.0f}")
    stats.add_row("Est. charges",   f"₹{charges:.0f}")
    net_col = "green" if net >= 0 else "red"
    stats.add_row("Net P&L today",  f"[{net_col}]₹{net:+.0f}[/{net_col}]")
    all_col = "green" if alltime >= 0 else "red"
    stats.add_row("All-time Net",   f"[{all_col}]₹{alltime:+.0f}[/{all_col}]")

    stats_panel = Panel(stats, title="Stats", box=box.SIMPLE_HEAVY, style="blue")

    # ── Log tail ──────────────────────────────
    log_text = Text()
    for line in logs:
        if "BUY" in line or "✅" in line:
            log_text.append(line + "\n", style="green")
        elif "SELL" in line or "🔴" in line:
            log_text.append(line + "\n", style="red")
        elif "ERROR" in line:
            log_text.append(line + "\n", style="bold red")
        elif "WARNING" in line or "⚠" in line:
            log_text.append(line + "\n", style="yellow")
        elif "Claude" in line:
            log_text.append(line + "\n", style="cyan")
        else:
            log_text.append(line + "\n", style="dim")

    log_panel = Panel(log_text, title="Bot Log (live)", box=box.SIMPLE_HEAVY, style="dim")

    return Columns([
        Panel(trade_table, box=box.MINIMAL),
        Columns([stats_panel, log_panel], equal=False),
    ])

# ─────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────
def main():
    console.print("[bold cyan]Starting live dashboard… (Ctrl+C to exit)[/bold cyan]")
    with Live(build_display(), refresh_per_second=0.5, screen=True) as live:
        while True:
            live.update(build_display())
            time.sleep(2)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Dashboard stopped.[/yellow]")
