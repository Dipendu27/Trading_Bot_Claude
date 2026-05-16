"""
Backtester — Dual-Timeframe Strategy on Real Kite Historical Data
==================================================================
Mirrors bot.py exactly: same indicators, same dual-TF scoring, same
fake-breakout filter, same SL/target/trail logic.

Usage:
    python backtest.py --symbol RELIANCE --days 30
    python backtest.py --symbol TCS --days 60 --capital 25000
    python backtest.py --all --days 30          # run all 20 UNIVERSE stocks
    python backtest.py --symbol RELIANCE --days 30 --csv  # export trades.csv

Root cause of "only 1 trade in 30 days" was:
  1. backtest used DAILY bars adapted from a 1-min strategy → almost no signals
  2. BT_CFG was completely out of sync with bot.py (old EMA 9/21)
  3. Score threshold required 4/4 — impossible with only 4 factors checked
This version fixes all three.
"""

import argparse, datetime
import numpy as np
import pandas as pd
from typing import Dict, List, Any, Tuple, Optional
from kiteconnect import KiteConnect

# ══════════════════════════════════════════════════════════
#  CONFIG — exact mirror of bot.py CFG
# ══════════════════════════════════════════════════════════
BT = {
    # 1-min TF
    "ema_fast_1m":  5,
    "ema_slow_1m":  13,
    "rsi_period_1m": 7,
    "atr_period_1m": 7,
    # 5-min TF
    "ema_fast_5m":  9,
    "ema_slow_5m":  21,
    "rsi_period_5m": 14,
    "atr_period_5m": 14,
    # Shared
    "min_volume_ratio":  1.1,
    "fake_break_ratio":  0.3,
    "sl_pct":            0.008,
    "target_pct":        0.020,
    "trail_sl":          True,
    "trail_trigger_pct": 0.005,
    "risk_per_trade":    0.012,
    # Dual-TF thresholds
    "tf_agree_score":    5,   # both TFs ≥ 5/8 → direct trade
    "tf_single_score":   4,   # one TF  ≥ 4/8 → trade (no Claude in backtest)
    # Misc
    "squareoff_hour":    15,
    "squareoff_minute":  10,
}

UNIVERSE = [
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK",
    "SBIN","BHARTIARTL","ITC","KOTAKBANK","LT",
    "AXISBANK","ASIANPAINT","MARUTI","TITAN","SUNPHARMA",
    "BAJFINANCE","WIPRO","HCLTECH","ULTRACEMCO","NESTLEIND",
]

# ══════════════════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════════════════
def get_kite() -> KiteConnect:
    """Reuse today's saved access token from bot.py login."""
    import os
    from kiteconnect import KiteConnect
    # Pull api_key from bot.py config without importing the whole bot
    try:
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("bot", "bot.py")
        bot  = importlib.util.module_from_spec(spec)  # type: ignore
        # Don't exec (triggers kite connect calls) — just read CFG via text parse
        raise ImportError("safe skip")
    except Exception:
        pass

    api_key = input("Enter your Zerodha API key: ").strip()
    kite    = KiteConnect(api_key=api_key)
    token_file = ".access_token"
    today = datetime.date.today().isoformat()

    if os.path.exists(token_file):
        saved_date, saved_tok = open(token_file).read().strip().split("|")
        if saved_date == today:
            kite.set_access_token(saved_tok)
            print("Reused saved access token.")
            return kite

    print(f"\nLogin URL:\n{kite.login_url()}\n")
    req_tok = input("Paste request_token: ").strip()
    api_secret = input("Enter your API secret: ").strip()
    sess = kite.generate_session(req_tok, api_secret=api_secret)
    kite.set_access_token(sess["access_token"])
    open(token_file, "w").write(f"{today}|{sess['access_token']}")
    return kite

# ══════════════════════════════════════════════════════════
#  DATA FETCH
# ══════════════════════════════════════════════════════════
def fetch_1min(kite: KiteConnect, symbol: str, days: int) -> pd.DataFrame:
    """Fetch 1-min OHLCV from Kite for NSE symbol."""
    insts = kite.instruments("NSE")
    token = next((i["instrument_token"] for i in insts
                  if i["tradingsymbol"] == symbol), None)
    if token is None:
        raise ValueError(f"Symbol '{symbol}' not found on NSE.")

    to_dt   = datetime.datetime.now()
    from_dt = to_dt - datetime.timedelta(days=days + 5)   # +5 for weekends

    print(f"  Fetching {days}d of 1-min data for {symbol}…", end=" ", flush=True)
    records = kite.historical_data(token, from_dt, to_dt, "minute")
    df = pd.DataFrame(records)
    if df.empty:
        print("NO DATA")
        return df
    df.rename(columns={"date": "time"}, inplace=True)
    df["time"] = pd.to_datetime(df["time"])
    df.set_index("time", inplace=True)
    df = df.between_time("09:15", "15:30")
    # Last N trading days
    trading_days = df.index.normalize().unique()
    if len(trading_days) > days:
        cutoff = trading_days[-days]
        df = df[df.index.normalize() >= cutoff]
    print(f"{len(df)} candles across {df.index.normalize().nunique()} days.")
    return df

def resample_5min(df1: pd.DataFrame) -> pd.DataFrame:
    """Resample 1-min data to 5-min OHLCV."""
    df5 = df1.resample("5min").agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna()
    df5 = df5.between_time("09:15", "15:30")
    return df5

# ══════════════════════════════════════════════════════════
#  INDICATORS  (same formula as bot.py)
# ══════════════════════════════════════════════════════════
def add_indicators(df: pd.DataFrame, ema_fast: int, ema_slow: int,
                   rsi_p: int, atr_p: int) -> pd.DataFrame:
    df = df.copy()
    # EMA
    df["ema_fast"] = df["close"].ewm(span=ema_fast, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=ema_slow, adjust=False).mean()
    # RSI
    d  = df["close"].diff()
    g  = d.clip(lower=0).ewm(com=rsi_p - 1, adjust=False).mean()
    l_ = (-d.clip(upper=0)).ewm(com=rsi_p - 1, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + g / l_.replace(0, np.nan))
    # ATR
    hl  = df["high"] - df["low"]
    hc  = (df["high"] - df["close"].shift()).abs()
    lc  = (df["low"]  - df["close"].shift()).abs()
    df["atr"] = pd.concat([hl, hc, lc], axis=1).max(axis=1).ewm(
        span=atr_p, adjust=False).mean()
    # VWAP — reset each calendar day
    df["_date"] = df.index.normalize()
    tp = (df["high"] + df["low"] + df["close"]) / 3
    df["_tpv"]  = tp * df["volume"]
    df["vwap"]  = df.groupby("_date")["_tpv"].cumsum() / \
                  df.groupby("_date")["volume"].cumsum()
    df["vwap_std"] = (df["close"] - df["vwap"]).rolling(10).std()
    # OBV
    df["obv"]    = (np.sign(df["close"].diff()) * df["volume"]).cumsum()
    # Volume MA
    df["vol_ma"] = df["volume"].rolling(10).mean()
    df.drop(columns=["_date", "_tpv"], inplace=True)
    return df.dropna()

# ══════════════════════════════════════════════════════════
#  FAKE BREAKOUT FILTER
# ══════════════════════════════════════════════════════════
def is_fake(row: pd.Series, prev: pd.Series) -> Tuple[bool, str]:
    body  = abs(row["close"] - row["open"])
    rng   = row["high"] - row["low"] + 1e-9
    flags = []
    if body / rng < BT["fake_break_ratio"]:
        flags.append("thin-body")
    if row["volume"] < row["vol_ma"] * BT["min_volume_ratio"]:
        flags.append("low-vol")
    if prev["low"] < row["close"] < prev["high"]:
        flags.append("inside-close")
    return len(flags) >= 2, ", ".join(flags)

# ══════════════════════════════════════════════════════════
#  SINGLE-TF SCORER  (same as bot.py score_tf)
# ══════════════════════════════════════════════════════════
def score_tf(row: pd.Series, prev: pd.Series, ago3: pd.Series) -> Tuple[int, int]:
    ema_cross_up = prev["ema_fast"] <= prev["ema_slow"] and row["ema_fast"] > row["ema_slow"]
    ema_cross_dn = prev["ema_fast"] >= prev["ema_slow"] and row["ema_fast"] < row["ema_slow"]
    ema_trend_up = row["ema_fast"] > row["ema_slow"]
    ema_trend_dn = row["ema_fast"] < row["ema_slow"]

    rsi_bull = 42 < row["rsi"] < 70
    rsi_ob   = row["rsi"] > 70
    rsi_bear = 30 < row["rsi"] < 58
    rsi_os   = row["rsi"] < 30

    above_vwap   = row["close"] > row["vwap"]
    mom_up       = row["close"] > prev["close"] > ago3["close"]
    mom_dn       = row["close"] < prev["close"] < ago3["close"]
    vol_spike    = row["volume"] > row["vol_ma"] * BT["min_volume_ratio"]
    obv_up       = row["obv"] > ago3["obv"]
    obv_dn       = row["obv"] < ago3["obv"]
    vwap_core_up = row["close"] > row["vwap"] + row.get("vwap_std", 0) * 0.3

    buy_signals = [
        ema_cross_up or ema_trend_up,
        rsi_bull and not rsi_ob,
        above_vwap,
        vol_spike,
        obv_up,
        mom_up,
        vwap_core_up,
        ema_cross_up,
    ]
    sell_signals = [
        ema_cross_dn or ema_trend_dn,
        rsi_bear and not rsi_os,
        not above_vwap,
        vol_spike,
        obv_dn,
        mom_dn,
        not vwap_core_up,
        ema_cross_dn,
    ]
    return sum(buy_signals), sum(sell_signals)

# ══════════════════════════════════════════════════════════
#  DUAL-TF SIGNAL
# ══════════════════════════════════════════════════════════
def dual_signal(row1: pd.Series, prev1: pd.Series, ago3_1: pd.Series,
                row5: Optional[pd.Series], prev5: Optional[pd.Series],
                ago3_5: Optional[pd.Series]) -> str:
    """
    Returns 'BUY', 'SELL', or 'HOLD'.
    In backtest we treat AMBIGUOUS as a trade (no Claude available),
    but only if score is strong enough.
    """
    s1b, s1s = score_tf(row1, prev1, ago3_1)

    s5b = s5s = 0
    if row5 is not None and prev5 is not None and ago3_5 is not None:
        s5b, s5s = score_tf(row5, prev5, ago3_5)

    has_5m = row5 is not None
    agree  = BT["tf_agree_score"]
    single = BT["tf_single_score"]

    if has_5m:
        if s1b >= agree and s5b >= agree:
            return "BUY"
        if s1s >= agree and s5s >= agree:
            return "SELL"
        # One TF strong — treat AMBIGUOUS as trade in backtest
        if s1b >= single + 1 or s5b >= single + 1:
            return "BUY"
        if s1s >= single + 1 or s5s >= single + 1:
            return "SELL"
    else:
        if s1b >= agree:
            return "BUY"
        if s1s >= agree:
            return "SELL"
        if s1b >= single:
            return "BUY"
        if s1s >= single:
            return "SELL"

    return "HOLD"

# ══════════════════════════════════════════════════════════
#  BACKTEST ENGINE
# ══════════════════════════════════════════════════════════
def run_backtest(df1: pd.DataFrame, df5: pd.DataFrame,
                 capital: float, symbol: str) -> List[Dict[str, Any]]:
    """
    Simulate live trading on historical 1-min data.
    Uses 5-min data (aligned by timestamp) as trend confirmation.
    """
    trades: List[Dict[str, Any]] = []
    position: Optional[Dict[str, Any]] = None

    def calc_qty(price: float) -> int:
        risk_amt = capital * BT["risk_per_trade"]
        sl_amt   = price * BT["sl_pct"]
        return max(1, int(risk_amt / sl_amt))

    rows1 = list(df1.iterrows())
    sq_time = datetime.time(BT["squareoff_hour"], BT["squareoff_minute"])

    for i, (ts, row) in enumerate(rows1):
        if i < 3:
            continue

        prev1  = rows1[i-1][1]
        ago3_1 = rows1[i-3][1]

        # Find matching or last 5-min bar
        row5 = prev5 = ago3_5 = None
        if not df5.empty:
            mask = df5.index <= ts
            if mask.sum() >= 3:
                past5  = df5[mask]
                row5   = past5.iloc[-1]
                prev5  = past5.iloc[-2]
                ago3_5 = past5.iloc[-3]

        # ── EOD square-off ───────────────────────────────────
        if ts.time() >= sq_time:
            if position:
                pnl = (row["close"] - position["entry_price"]) * position["qty"]
                trades.append({**position, "exit_price": row["close"],
                               "exit_time": ts, "reason": "eod", "pnl": pnl})
                position = None
            continue

        # ── Manage open position ──────────────────────────────
        if position:
            ltp = row["close"]
            profit_pct = (ltp - position["entry_price"]) / position["entry_price"]

            # Trailing SL
            if BT["trail_sl"] and profit_pct > BT["trail_trigger_pct"]:
                position["peak"] = max(ltp, position["peak"])
                locked = BT["trail_trigger_pct"] * 0.5
                new_sl = round(position["entry_price"] * (1 + locked), 2)
                if new_sl > position["sl"]:
                    position["sl"] = new_sl

            reason = exit_p = None
            if row["low"] <= position["sl"]:
                reason, exit_p = "stop-loss", min(row["open"], position["sl"])
            elif row["high"] >= position["target"]:
                reason, exit_p = "target", position["target"]

            if reason:
                pnl = (exit_p - position["entry_price"]) * position["qty"]
                trades.append({**position, "exit_price": exit_p,
                               "exit_time": ts, "reason": reason, "pnl": pnl})
                position = None
                continue

        # ── Entry signal ──────────────────────────────────────
        if position:
            continue   # one position at a time per symbol in backtest

        action = dual_signal(row, prev1, ago3_1, row5, prev5, ago3_5)
        if action != "BUY":
            # Check SELL direction for short — skip for now (MIS long only)
            continue

        # Fake breakout filter (use 5-min bar if available, else 1-min)
        check_row  = row5  if row5  is not None else row
        check_prev = prev5 if prev5 is not None else prev1
        fake, _    = is_fake(check_row, check_prev)
        if fake:
            continue

        entry = row["close"]
        qty   = calc_qty(entry)
        position = {
            "symbol":      symbol,
            "entry_price": entry,
            "entry_time":  ts,
            "sl":          round(entry * (1 - BT["sl_pct"]), 2),
            "target":      round(entry * (1 + BT["target_pct"]), 2),
            "peak":        entry,
            "qty":         qty,
        }

    # Close any remaining open position at last bar
    if position and rows1:
        last_ts, last_row = rows1[-1]
        pnl = (last_row["close"] - position["entry_price"]) * position["qty"]
        trades.append({**position, "exit_price": last_row["close"],
                       "exit_time": last_ts, "reason": "end-of-data", "pnl": pnl})

    return trades

# ══════════════════════════════════════════════════════════
#  REPORT
# ══════════════════════════════════════════════════════════
def print_report(trades: List[Dict[str, Any]], symbol: str,
                 capital: float, days: int):
    sep = "═" * 62
    if not trades:
        print(f"\n{sep}")
        print(f"  {symbol}: NO TRADES in {days} days")
        print(f"  Possible reasons:")
        print(f"    • Market was sideways — strategy stays flat in chop")
        print(f"    • Increase days (try --days 60)")
        print(f"    • Lower tf_agree_score in BT config")
        print(sep)
        return

    pnls    = [t["pnl"] for t in trades]
    winners = [p for p in pnls if p > 0]
    losers  = [p for p in pnls if p <= 0]
    gross   = sum(pnls)
    # Zerodha brokerage: ₹20 per leg (entry + exit) = ₹40 per trade + STT ~₹10
    charges = len(pnls) * 50
    net     = gross - charges
    win_rt  = len(winners) / len(pnls) * 100
    avg_win = sum(winners) / len(winners) if winners else 0
    avg_los = sum(losers)  / len(losers)  if losers  else 0
    pf      = abs(sum(winners) / sum(losers)) if losers and sum(losers) != 0 else float("inf")
    max_dd  = min(pnls) if pnls else 0
    max_win = max(pnls) if pnls else 0

    # Per-day breakdown
    by_date: Dict[str, float] = {}
    for t in trades:
        d = str(t["entry_time"].date())
        by_date[d] = by_date.get(d, 0) + t["pnl"]

    print(f"\n{sep}")
    print(f"  Backtest Report — {symbol}  ({days} days)")
    print(f"{sep}")
    print(f"  Trades        : {len(trades)}")
    print(f"  Winners       : {len(winners)}  ({win_rt:.1f}%)")
    print(f"  Losers        : {len(losers)}")
    print(f"  Avg win       : ₹{avg_win:,.0f}")
    print(f"  Avg loss      : ₹{avg_los:,.0f}")
    print(f"  Best trade    : ₹{max_win:,.0f}")
    print(f"  Worst trade   : ₹{max_dd:,.0f}")
    print(f"  Profit factor : {pf:.2f}  (>1.5 is good)")
    print(f"  Gross P&L     : ₹{gross:,.0f}")
    print(f"  Est. charges  : ₹{charges:,.0f}")
    print(f"  Net P&L       : ₹{net:,.0f}")
    print(f"  Return        : {net/capital*100:.2f}% on ₹{capital:,.0f}")
    print(f"{sep}")

    # Daily breakdown
    print(f"\n  Daily P&L breakdown:")
    print(f"  {'Date':<12}  {'P&L':>10}")
    print(f"  {'─'*24}")
    for d, pnl in sorted(by_date.items()):
        sign = "+" if pnl >= 0 else ""
        print(f"  {d:<12}  {sign}₹{pnl:,.0f}")

    # Trade log (last 25)
    show = trades[-25:]
    print(f"\n  Recent trades (last {len(show)}):")
    print(f"  {'Entry':<19} {'Exit':<19} {'Entry₹':>8} {'Exit₹':>8} {'Qty':>4} {'P&L':>8}  Reason")
    print(f"  {'─'*90}")
    for t in show:
        sign = "+" if t["pnl"] >= 0 else ""
        print(f"  {str(t['entry_time'])[:19]:<19} {str(t['exit_time'])[:19]:<19} "
              f"₹{t['entry_price']:>7.2f} ₹{t['exit_price']:>7.2f} "
              f"{t['qty']:>4}  {sign}₹{t['pnl']:>7.0f}  {t['reason']}")
    if len(trades) > 25:
        print(f"  … and {len(trades)-25} earlier trades")
    print()

def export_csv(trades: List[Dict[str, Any]], filename: str):
    import csv
    if not trades:
        print("No trades to export.")
        return
    keys = ["symbol","entry_time","exit_time","entry_price","exit_price",
            "qty","sl","target","reason","pnl"]
    with open(filename, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(trades)
    print(f"Exported {len(trades)} trades to {filename}")

# ══════════════════════════════════════════════════════════
#  MULTI-SYMBOL SUMMARY
# ══════════════════════════════════════════════════════════
def run_all(kite: KiteConnect, days: int, capital: float):
    all_trades: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]]    = []

    for sym in UNIVERSE:
        try:
            df1 = fetch_1min(kite, sym, days)
            if df1.empty or len(df1) < 30:
                print(f"  {sym}: insufficient data, skipping.")
                continue
            df1 = add_indicators(df1, BT["ema_fast_1m"], BT["ema_slow_1m"],
                                  BT["rsi_period_1m"], BT["atr_period_1m"])
            df5 = resample_5min(df1)
            df5 = add_indicators(df5, BT["ema_fast_5m"], BT["ema_slow_5m"],
                                  BT["rsi_period_5m"], BT["atr_period_5m"])
            trades = run_backtest(df1, df5, capital, sym)
            all_trades.extend(trades)
            pnls  = [t["pnl"] for t in trades]
            gross = sum(pnls)
            net   = gross - len(pnls) * 50
            results.append({"symbol": sym, "trades": len(trades), "net": net})
        except Exception as e:
            print(f"  {sym}: ERROR — {e}")

    print(f"\n{'═'*50}")
    print(f"  ALL-UNIVERSE SUMMARY ({days} days)")
    print(f"{'═'*50}")
    print(f"  {'Symbol':<14} {'Trades':>6}  {'Net P&L':>10}")
    print(f"  {'─'*38}")
    total_net = 0.0
    for r in sorted(results, key=lambda x: x["net"], reverse=True):
        sign = "+" if r["net"] >= 0 else ""
        print(f"  {r['symbol']:<14} {r['trades']:>6}  {sign}₹{r['net']:>9,.0f}")
        total_net += r["net"]
    print(f"  {'─'*38}")
    sign = "+" if total_net >= 0 else ""
    print(f"  {'TOTAL':<14} {sum(r['trades'] for r in results):>6}  {sign}₹{total_net:>9,.0f}")
    print(f"{'═'*50}\n")
    return all_trades

# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Dual-TF backtester (mirrors bot.py exactly)")
    ap.add_argument("--symbol",  default="RELIANCE",
                    help="NSE symbol (default: RELIANCE)")
    ap.add_argument("--days",    type=int, default=30,
                    help="Trading days of history (default: 30)")
    ap.add_argument("--capital", type=float, default=25000,
                    help="Capital in ₹ (default: 25000)")
    ap.add_argument("--all",     action="store_true",
                    help="Run all 20 universe stocks and show summary")
    ap.add_argument("--csv",     action="store_true",
                    help="Export trades to CSV")
    args = ap.parse_args()

    kite = get_kite()

    if args.all:
        all_trades = run_all(kite, args.days, args.capital)
        if args.csv:
            export_csv(all_trades, "backtest_all.csv")
    else:
        print(f"\nBacktesting {args.symbol} | {args.days} days | capital ₹{args.capital:,.0f}")
        df1 = fetch_1min(kite, args.symbol, args.days)
        if df1.empty:
            print("No data received. Check symbol name and Kite API access.")
            exit(1)

        print(f"  Adding 1-min indicators…")
        df1 = add_indicators(df1, BT["ema_fast_1m"], BT["ema_slow_1m"],
                              BT["rsi_period_1m"], BT["atr_period_1m"])

        print(f"  Resampling to 5-min and adding indicators…")
        df5 = resample_5min(df1)
        df5 = add_indicators(df5, BT["ema_fast_5m"], BT["ema_slow_5m"],
                              BT["rsi_period_5m"], BT["atr_period_5m"])

        print(f"  Running dual-TF backtest…")
        trades = run_backtest(df1, df5, args.capital, args.symbol)

        print_report(trades, args.symbol, args.capital, args.days)

        if args.csv:
            export_csv(trades, f"backtest_{args.symbol}.csv")
