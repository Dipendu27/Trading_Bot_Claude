"""
Trade_Claude v2 — Backtester (Black-Scholes + Regime-Aware)
=============================================================
Mirrors bot.py v2 signal logic exactly:
  • ADX + Supertrend + VWAP bands
  • Regime detection (BULL / BEAR / SIDEWAYS) per bar
  • Regime-specific SL, target, score thresholds
  • Scaled exit (30% at 1:1 R:R)
  • Black-Scholes option pricing with real IV from Kite

Usage:
    python backtest.py --index "NIFTY 50" --days 30
    python backtest.py --index "NIFTY BANK" --days 60
    python backtest.py --all --days 30
    python backtest.py --index "NIFTY 50" --days 30 --csv
    python backtest.py --index "NIFTY 50" --days 30 --no-spread
"""

import argparse, datetime, math, os
import numpy as np
import pandas as pd
from typing import Dict, List, Any, Tuple, Optional
from kiteconnect import KiteConnect

try:
    from scipy.stats import norm
except ImportError:
    print("scipy required: pip install scipy"); exit(1)

# ══════════════════════════════════════════════════════════
#  CONFIG  (mirror of bot.py CFG + REGIME_PARAMS)
# ══════════════════════════════════════════════════════════
BT: Dict[str, Any] = {
    "ema_fast_1m": 5,   "ema_slow_1m": 13,  "rsi_period_1m": 7,  "atr_period_1m": 7,
    "ema_fast_5m": 9,   "ema_slow_5m": 21,  "rsi_period_5m": 14, "atr_period_5m": 14,
    "ema_trend_15m": 21,
    "adx_period":    14,
    "supertrend_mult": 2.5,
    "min_adx_trend":   20,
    "min_adx_sideways": 15,
    "min_volume_ratio": 1.1,
    "fake_break_ratio": 0.3,
    "tf_agree_score":   5,
    "tf_single_score":  4,
    "capital":          25000,
    "risk_per_trade":   0.06,
    "max_lots":         5,
    "lot_sizes": {"NIFTY 50": 25, "NIFTY BANK": 15},
    "scale_exit":         True,
    "scale_exit_ratio":   0.3,
    "risk_free_rate":     0.068,
    "iv_fallback":        0.15,
    "bid_ask_spread_pct": 0.02,
    "min_premium":        15.0,
    "max_premium":        450.0,
    "squareoff_hour":     15, "squareoff_minute": 10,
    "no_new_trades_after_hour": 14, "no_new_trades_after_minute": 45,
}

REGIME_PARAMS: Dict[str, Dict[str, Any]] = {
    "BULL": {
        "sl_pct": 0.25, "target_pct": 1.20, "trail_trigger_pct": 0.30,
        "preferred_dir": "CE", "min_adx": 20, "score_threshold": 4,
    },
    "BEAR": {
        "sl_pct": 0.25, "target_pct": 1.20, "trail_trigger_pct": 0.30,
        "preferred_dir": "PE", "min_adx": 20, "score_threshold": 4,
    },
    "SIDEWAYS": {
        "sl_pct": 0.15, "target_pct": 0.55, "trail_trigger_pct": 0.15,
        "preferred_dir": "BOTH", "min_adx": 15, "score_threshold": 5,
    },
}

INDICES = {"NIFTY 50": "NIFTY", "NIFTY BANK": "BANKNIFTY"}

# ══════════════════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════════════════
def get_kite() -> KiteConnect:
    api_key = input("Zerodha API key: ").strip()
    kite    = KiteConnect(api_key=api_key)
    token_file = ".access_token"
    today      = datetime.date.today().isoformat()
    if os.path.exists(token_file):
        parts = open(token_file).read().strip().split("|")
        if len(parts) == 2 and parts[0] == today:
            kite.set_access_token(parts[1])
            print("  ✅ Reused access token.")
            return kite
    print(f"\nLogin URL:\n{kite.login_url()}\n")
    req_tok    = input("request_token: ").strip()
    api_secret = input("API secret: ").strip()
    sess       = kite.generate_session(req_tok, api_secret=api_secret)
    kite.set_access_token(sess["access_token"])
    open(token_file, "w").write(f"{today}|{sess['access_token']}")
    return kite

# ══════════════════════════════════════════════════════════
#  BLACK-SCHOLES
# ══════════════════════════════════════════════════════════
def bs_price(S, K, T, r, sigma, opt_type):
    if T <= 0:
        return max(0.0, S - K) if opt_type == "CE" else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt_type == "CE":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

def implied_vol(mkt_price, S, K, T, r, opt_type):
    if mkt_price <= 0 or T <= 0:
        return BT["iv_fallback"]
    sigma = 0.20
    for _ in range(100):
        p    = bs_price(S, K, T, r, sigma, opt_type)
        vega = S * norm.pdf((math.log(S/K) + (r + 0.5*sigma**2)*T)/(sigma*math.sqrt(T))) * math.sqrt(T)
        diff = p - mkt_price
        if abs(diff) < 1e-5: break
        if abs(vega) < 1e-10: break
        sigma -= diff / vega
        sigma  = max(0.01, min(sigma, 5.0))
    return sigma if 0.01 < sigma < 5.0 else BT["iv_fallback"]

def bar_premium(spot, strike, bar_ts, expiry, iv, opt_type):
    exp_dt = datetime.datetime.combine(expiry, datetime.time(15, 30))
    T      = max((exp_dt - bar_ts).total_seconds() / (365 * 24 * 3600), 1/365)
    return max(1.0, bs_price(spot, strike, T, BT["risk_free_rate"], iv, opt_type))

# ══════════════════════════════════════════════════════════
#  FETCH IV FROM KITE
# ══════════════════════════════════════════════════════════
def fetch_atm_iv(kite: KiteConnect, index_name: str) -> Dict[str, Any]:
    root  = INDICES[index_name]
    today = datetime.date.today()
    fb    = {"iv_ce": BT["iv_fallback"], "iv_pe": BT["iv_fallback"],
             "strike": 0, "expiry": today + datetime.timedelta(days=7),
             "spot": 0, "source": "fallback"}
    try:
        q    = kite.quote(f"NSE:{index_name}")
        spot = q[f"NSE:{index_name}"]["last_price"]
        nfo  = kite.instruments("NFO")
        exps = sorted({i["expiry"] for i in nfo
                       if i["name"] == root and i["expiry"] and i["expiry"] >= today})
        if not exps: return fb
        expiry      = exps[0]
        days_to_exp = (expiry - today).days
        T           = max(days_to_exp / 365, 1/365)
        step        = 50 if root == "NIFTY" else 100
        strike      = round(spot / step) * step
        ce = next((i for i in nfo if i["name"]==root and i["expiry"]==expiry
                   and abs(i["strike"]-strike)<0.01 and i["instrument_type"]=="CE"), None)
        pe = next((i for i in nfo if i["name"]==root and i["expiry"]==expiry
                   and abs(i["strike"]-strike)<0.01 and i["instrument_type"]=="PE"), None)
        if not ce or not pe: return fb
        qs     = kite.quote([f"NFO:{ce['tradingsymbol']}", f"NFO:{pe['tradingsymbol']}"])
        ce_ltp = qs.get(f"NFO:{ce['tradingsymbol']}", {}).get("last_price", 0)
        pe_ltp = qs.get(f"NFO:{pe['tradingsymbol']}", {}).get("last_price", 0)
        if not ce_ltp or not pe_ltp: return fb
        iv_ce  = implied_vol(ce_ltp, spot, strike, T, BT["risk_free_rate"], "CE")
        iv_pe  = implied_vol(pe_ltp, spot, strike, T, BT["risk_free_rate"], "PE")
        print(f"  📊 {index_name} spot=₹{spot:.0f} strike={strike} expiry={expiry} "
              f"T={days_to_exp}d IV_CE={iv_ce*100:.1f}% IV_PE={iv_pe*100:.1f}% "
              f"CE=₹{ce_ltp:.2f} PE=₹{pe_ltp:.2f}")
        return {"iv_ce": iv_ce, "iv_pe": iv_pe, "iv_avg": (iv_ce+iv_pe)/2,
                "strike": strike, "expiry": expiry, "spot": spot, "source": "live"}
    except Exception as e:
        print(f"  ⚠  IV fetch error: {e} — using fallback.")
        return fb

# ══════════════════════════════════════════════════════════
#  DATA FETCH
# ══════════════════════════════════════════════════════════
def fetch_1min(kite: KiteConnect, index_name: str, days: int) -> pd.DataFrame:
    insts = kite.instruments("NSE")
    token = next((i["instrument_token"] for i in insts
                  if i["tradingsymbol"] == index_name), None)
    if not token:
        raise ValueError(f"'{index_name}' not found in NSE instruments.")
    to_dt   = datetime.datetime.now()
    from_dt = to_dt - datetime.timedelta(days=days + 7)
    print(f"  Fetching {days}d 1-min data for {index_name}…", end=" ", flush=True)
    recs = kite.historical_data(token, from_dt, to_dt, "minute")
    df   = pd.DataFrame(recs)
    if df.empty:
        print("NO DATA"); return df
    df.rename(columns={"date": "time"}, inplace=True)
    df["time"] = pd.to_datetime(df["time"])
    df.set_index("time", inplace=True)
    df = df.between_time("09:15", "15:30")
    td = df.index.normalize().unique()
    if len(td) > days:
        df = df[df.index.normalize() >= td[-days]]
    print(f"{len(df)} bars | {df.index.normalize().nunique()} days.")
    return df

def resample(df1: pd.DataFrame, minutes: int) -> pd.DataFrame:
    df = df1.resample(f"{minutes}min").agg(
        {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
    ).dropna()
    return df.between_time("09:15", "15:30")

# ══════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════
def add_indicators(df: pd.DataFrame, ef, es, rp, ap) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=ef, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=es, adjust=False).mean()
    d   = df["close"].diff()
    g   = d.clip(lower=0).ewm(com=rp-1, adjust=False).mean()
    l_  = (-d.clip(upper=0)).ewm(com=rp-1, adjust=False).mean().replace(0, float("nan"))
    df["rsi"] = 100 - 100 / (1 + g / l_)
    hl  = df["high"] - df["low"]
    hc  = (df["high"] - df["close"].shift()).abs()
    lc  = (df["low"]  - df["close"].shift()).abs()
    df["atr"] = pd.concat([hl, hc, lc], axis=1).max(axis=1).ewm(span=ap, adjust=False).mean()
    # VWAP
    df["_date"] = df.index.normalize()
    tp = (df["high"] + df["low"] + df["close"]) / 3
    df["vwap"]   = (tp*df["volume"]).groupby(df["_date"]).cumsum() / \
                    df["volume"].groupby(df["_date"]).cumsum()
    df["vwap_std"] = (df["close"] - df["vwap"]).rolling(20).std()
    df["vwap_u1"]  = df["vwap"] + df["vwap_std"]
    df["vwap_d1"]  = df["vwap"] - df["vwap_std"]
    df["vwap_u2"]  = df["vwap"] + 2 * df["vwap_std"]
    df["vwap_d2"]  = df["vwap"] - 2 * df["vwap_std"]
    df["obv"]    = (np.sign(df["close"].diff()) * df["volume"]).cumsum()
    df["vol_ma"] = df["volume"].rolling(20).mean()
    df.drop(columns=["_date"], inplace=True)
    # ADX
    hi, lo, cl = df["high"], df["low"], df["close"]
    up   = hi.diff(); down = -lo.diff()
    pdm  = np.where((up > down) & (up > 0), up, 0.0)
    ndm  = np.where((down > up) & (down > 0), down, 0.0)
    tr   = pd.concat([hi-lo, (hi-cl.shift()).abs(), (lo-cl.shift()).abs()], axis=1).max(axis=1)
    atr14 = tr.ewm(span=14, adjust=False).mean()
    pdi  = 100 * pd.Series(pdm, index=df.index).ewm(span=14, adjust=False).mean() / atr14
    ndi  = 100 * pd.Series(ndm, index=df.index).ewm(span=14, adjust=False).mean() / atr14
    dx   = 100 * (pdi - ndi).abs() / (pdi + ndi + 1e-9)
    df["adx"] = dx.ewm(span=14, adjust=False).mean()
    df["+di"] = pdi; df["-di"] = ndi
    # Supertrend
    hl2  = (df["high"] + df["low"]) / 2
    mult = BT["supertrend_mult"]
    up_b = hl2 + mult * df["atr"]
    dn_b = hl2 - mult * df["atr"]
    st      = pd.Series(index=df.index, dtype=float)
    st_dir  = pd.Series(1, index=df.index, dtype=int)
    st.iloc[0] = dn_b.iloc[0]
    for i in range(1, len(df)):
        if df["close"].iloc[i] > up_b.iloc[i-1]:
            st.iloc[i] = dn_b.iloc[i]; st_dir.iloc[i] = 1
        elif df["close"].iloc[i] < dn_b.iloc[i-1]:
            st.iloc[i] = up_b.iloc[i]; st_dir.iloc[i] = -1
        else:
            st_dir.iloc[i] = st_dir.iloc[i-1]
            if st_dir.iloc[i] == 1:
                st.iloc[i] = max(dn_b.iloc[i], st.iloc[i-1])
            else:
                st.iloc[i] = min(up_b.iloc[i], st.iloc[i-1])
    df["supertrend_dir"] = st_dir
    return df.dropna()

# ══════════════════════════════════════════════════════════
#  REGIME DETECTOR
# ══════════════════════════════════════════════════════════
def detect_regime(row15, row5) -> str:
    row = row15 if row15 is not None else row5
    if row is None: return "SIDEWAYS"
    adx    = row.get("adx", 0)
    pdi    = row.get("+di", 0)
    ndi    = row.get("-di", 0)
    st_dir = row.get("supertrend_dir", 0)
    if adx > BT["min_adx_trend"]:
        if pdi > ndi and st_dir == 1: return "BULL"
        if ndi > pdi and st_dir == -1: return "BEAR"
    return "SIDEWAYS"

# ══════════════════════════════════════════════════════════
#  FAKE BREAKOUT
# ══════════════════════════════════════════════════════════
def is_fake(row, prev, regime) -> bool:
    body  = abs(row["close"] - row["open"])
    rng   = row["high"] - row["low"] + 1e-9
    ratio = 0.4 if regime == "SIDEWAYS" else BT["fake_break_ratio"]
    flags = 0
    if body / rng < ratio:                                        flags += 1
    if row["volume"] < row["vol_ma"] * BT["min_volume_ratio"]:   flags += 1
    if prev["low"] < row["close"] < prev["high"]:                 flags += 1
    if regime == "SIDEWAYS":
        vwap_d1 = row.get("vwap_d1", 0)
        vwap_u1 = row.get("vwap_u1", 1e9)
        if vwap_d1 < row["close"] < vwap_u1:                     flags += 1
    threshold = 1 if regime == "SIDEWAYS" else 2
    return flags >= threshold

# ══════════════════════════════════════════════════════════
#  SCORER  (regime-aware, 0–10)
# ══════════════════════════════════════════════════════════
def score_tf(row, prev, ago3, regime) -> Tuple[int, int]:
    xup = prev["ema_fast"] <= prev["ema_slow"] and row["ema_fast"] > row["ema_slow"]
    xdn = prev["ema_fast"] >= prev["ema_slow"] and row["ema_fast"] < row["ema_slow"]
    tup = row["ema_fast"] > row["ema_slow"]
    tdn = row["ema_fast"] < row["ema_slow"]
    rsi_bull = 42 < row["rsi"] < 72
    rsi_bear = 28 < row["rsi"] < 58
    abv  = row["close"] > row["vwap"]
    vol  = row["volume"] > row["vol_ma"] * BT["min_volume_ratio"]
    obu  = row["obv"] > ago3["obv"]
    obd  = row["obv"] < ago3["obv"]
    mup  = row["close"] > prev["close"] > ago3["close"]
    mdn  = row["close"] < prev["close"] < ago3["close"]
    stb  = row.get("supertrend_dir", 0) == 1
    stbe = row.get("supertrend_dir", 0) == -1
    adxok = row.get("adx", 0) > REGIME_PARAMS.get(regime, REGIME_PARAMS["SIDEWAYS"])["min_adx"]

    at_sup = row["close"] < row.get("vwap_d1", 0)
    at_res = row["close"] > row.get("vwap_u1", 1e9)

    if regime in ("BULL", "BEAR"):
        buys  = [xup or tup, rsi_bull, abv, vol, obu, mup,
                 row["close"] > row.get("vwap_u1", 0), xup, stb, adxok]
        sells = [xdn or tdn, rsi_bear, not abv, vol, obd, mdn,
                 row["close"] < row.get("vwap_d1", 1e9), xdn, stbe, adxok]
    else:
        buys  = [at_sup, rsi_bull and row["rsi"] < 50, abv, vol, obu, mup,
                 xup, stb, adxok, row["close"] > row.get("vwap_d2", 0)]
        sells = [at_res, rsi_bear and row["rsi"] > 50, not abv, vol, obd, mdn,
                 xdn, stbe, adxok, row["close"] < row.get("vwap_u2", 1e9)]
    return sum(buys), sum(sells)

# ══════════════════════════════════════════════════════════
#  DUAL-TF SIGNAL
# ══════════════════════════════════════════════════════════
def dual_signal(r1, p1, a1, r5, p5, a5, r15, regime) -> str:
    s1b, s1s = score_tf(r1, p1, a1, regime)
    s5b = s5s = 0
    if r5 is not None:
        s5b, s5s = score_tf(r5, p5, a5, regime)
    tb = s1b + s5b; ts = s1s + s5s
    rp  = REGIME_PARAMS.get(regime, REGIME_PARAMS["SIDEWAYS"])
    thr = rp["score_threshold"]

    if tb >= thr * 2:   return "BUY"
    if ts >= thr * 2:   return "SELL"
    if tb >= thr + 2:   return "BUY"
    if ts >= thr + 2:   return "SELL"
    if s1b >= thr or (r5 is not None and s5b >= thr): return "BUY"
    if s1s >= thr or (r5 is not None and s5s >= thr): return "SELL"
    return "HOLD"


def trailing_stop(position: Dict[str, Any], current_premium: float,
                  trigger_pct: float) -> None:
    """Raise stop-loss once an option premium moves enough in favour."""
    entry = position.get("fill_price", 0)
    if not entry:
        return
    pct = (current_premium - entry) / entry
    if pct <= trigger_pct:
        return

    new_sl = round(entry * (1 + trigger_pct * 0.5), 2)
    if new_sl > position["sl"]:
        position["sl"] = new_sl

# ══════════════════════════════════════════════════════════
#  BACKTEST ENGINE
# ══════════════════════════════════════════════════════════
def run_backtest(df1: pd.DataFrame, df5: pd.DataFrame, df15: pd.DataFrame,
                 index_name: str, iv_info: Dict[str, Any],
                 use_spread: bool = True) -> List[Dict[str, Any]]:
    lot_size = BT["lot_sizes"].get(index_name, 25)
    sq_time  = datetime.time(BT["squareoff_hour"],   BT["squareoff_minute"])
    no_entry = datetime.time(BT["no_new_trades_after_hour"], BT["no_new_trades_after_minute"])
    strike   = iv_info["strike"]
    expiry   = iv_info["expiry"]
    iv_ce    = iv_info["iv_ce"]
    iv_pe    = iv_info["iv_pe"]
    r        = BT["risk_free_rate"]

    trades: List[Dict[str, Any]] = []
    position: Optional[Dict[str, Any]] = None
    session_losses = 0
    session_pnl    = 0.0

    def get_premium(spot, ts, direction, raw=False):
        iv  = iv_ce if direction == "CE" else iv_pe
        p   = bar_premium(spot, strike, ts, expiry, iv, direction)
        if not raw and use_spread:
            p = p * (1 + BT["bid_ask_spread_pct"] / 2)   # entry: pay ask
        elif raw and use_spread:
            p = p * (1 - BT["bid_ask_spread_pct"] / 2)   # exit: receive bid
        return p

    def calc_lots(prem, regime):
        if prem <= 0 or lot_size <= 0:
            return 0
        rp = REGIME_PARAMS[regime]
        loss_per_lot = prem * max(float(rp["sl_pct"]), 0.01) * lot_size
        risk_based_lots = int((BT["capital"] * BT["risk_per_trade"]) / loss_per_lot)
        capital_guard_lots = int((BT["capital"] * 0.15) / (prem * lot_size))
        if risk_based_lots < 1 or capital_guard_lots < 1:
            return 0
        tier_cap = BT["max_lots"] if regime in ("BULL", "BEAR") else max(1, BT["max_lots"] // 2 + 1)
        return max(0, min(risk_based_lots, capital_guard_lots, tier_cap, BT["max_lots"]))

    rows1  = list(df1.iterrows())

    for i, (ts, row) in enumerate(rows1):
        if i < 3: continue
        prev1  = rows1[i-1][1]; ago3_1 = rows1[i-3][1]

        row5 = prev5 = ago3_5 = None
        if not df5.empty:
            mask = df5.index <= ts
            if mask.sum() >= 3:
                p5 = df5[mask]; row5 = p5.iloc[-1]; prev5 = p5.iloc[-2]; ago3_5 = p5.iloc[-3]

        row15 = None
        if not df15.empty:
            mask15 = df15.index <= ts
            if mask15.sum() >= 1:
                row15 = df15[mask15].iloc[-1]

        spot   = float(row["close"])
        regime = detect_regime(row15, row5 if row5 is not None else row)
        rp     = REGIME_PARAMS[regime]

        # ── EOD square-off ─────────────────────────────────
        if ts.time() >= sq_time and position:
            exit_p = get_premium(spot, ts, position["direction"], raw=True)
            pnl    = (exit_p - position["fill_price"]) * position["qty"]
            trades.append({**position, "exit_premium": exit_p, "exit_spot": spot,
                           "exit_time": ts, "reason": "eod", "pnl": pnl, "regime": regime})
            session_pnl += pnl
            position = None
            continue

        # ── Manage open position ────────────────────────────
        if position:
            iv      = iv_ce if position["direction"] == "CE" else iv_pe
            cur_p   = bar_premium(spot, strike, ts, expiry, iv, position["direction"])

            # Scaled exit at 1:1: book a smaller lot-safe chunk, let the rest run.
            if (BT["scale_exit"] and not position.get("scaled_out")
                    and cur_p >= position.get("scale_target", 1e9)
                    and position["lots"] > 1):
                lots_to_exit = max(1, int(position["lots"] * BT["scale_exit_ratio"]))
                lots_to_exit = min(lots_to_exit, position["lots"] - 1)
                partial_qty = lots_to_exit * lot_size
                partial_pnl = (cur_p - position["fill_price"]) * partial_qty
                trades.append({**position, "exit_premium": cur_p, "exit_spot": spot,
                               "exit_time": ts, "reason": "scale-1:1",
                               "pnl": partial_pnl, "qty": partial_qty, "regime": regime})
                session_pnl += partial_pnl
                position["qty"] -= partial_qty
                position["lots"] -= lots_to_exit
                position["scaled_out"] = True

            trailing_stop(position, cur_p, rp["trail_trigger_pct"])

            reason = exit_p = None
            if cur_p <= position["sl"]:
                reason, exit_p = "stop-loss", position["sl"]
            elif cur_p >= position["target"]:
                reason, exit_p = "target", position["target"]

            if reason:
                exit_spread = exit_p * (1 - BT["bid_ask_spread_pct"]/2) if use_spread else exit_p
                pnl = (exit_spread - position["fill_price"]) * position["qty"]
                trades.append({**position, "exit_premium": exit_spread, "exit_spot": spot,
                               "exit_time": ts, "reason": reason, "pnl": pnl, "regime": regime})
                session_pnl += pnl
                if pnl < 0: session_losses += 1
                else:       session_losses = 0
                position = None
                continue

        if position: continue
        if ts.time() >= no_entry: continue

        # ── Entry signal ────────────────────────────────────
        action = dual_signal(row, prev1, ago3_1, row5, prev5, ago3_5, row15, regime)
        if action not in ("BUY", "SELL"): continue

        check_r = row5 if row5 is not None else row
        check_p = prev5 if prev5 is not None else prev1
        if is_fake(check_r, check_p, regime): continue

        # Regime directional filter
        direction = "CE" if action == "BUY" else "PE"
        if regime == "BULL" and direction == "PE": continue
        if regime == "BEAR" and direction == "CE": continue

        # SIDEWAYS ADX check
        if regime == "SIDEWAYS" and row.get("adx", 0) < 15: continue

        fill_p = get_premium(spot, ts, direction, raw=False)
        if not (BT["min_premium"] <= fill_p <= BT["max_premium"]): continue

        lots  = calc_lots(fill_p, regime)
        if lots < 1:
            continue
        qty   = lots * lot_size
        sl    = round(fill_p * (1 - rp["sl_pct"]),     2)
        target= round(fill_p * (1 + rp["target_pct"]), 2)
        scale = round(fill_p * (1 + rp["sl_pct"]),     2)  # 1:1 R:R

        position = {
            "index_name":  index_name, "direction": direction, "signal": action,
            "entry_time":  ts, "entry_spot": spot, "strike": strike,
            "expiry":      expiry, "fill_price": fill_p,
            "sl":          sl, "target": target, "scale_target": scale,
            "lots":        lots, "qty": qty, "scaled_out": False,
            "regime_at_entry": regime,
        }

    # Close open at last bar
    if position and rows1:
        last_ts, last_row = rows1[-1]
        exit_p = get_premium(float(last_row["close"]), last_ts, position["direction"], raw=True)
        pnl    = (exit_p - position["fill_price"]) * position["qty"]
        trades.append({**position, "exit_premium": exit_p, "exit_spot": float(last_row["close"]),
                       "exit_time": last_ts, "reason": "end-of-data", "pnl": pnl,
                       "regime": "end"})

    return trades

# ══════════════════════════════════════════════════════════
#  REPORT
# ══════════════════════════════════════════════════════════
def print_report(trades: List[Dict[str, Any]], index_name: str,
                 days: int, iv_info: Dict[str, Any]):
    sep = "═" * 70
    if not trades:
        print(f"\n{sep}\n  {index_name}: NO TRADES in {days} days.\n{sep}")
        return

    # Exclude partial trades (scale-1:1) from main stats
    full  = [t for t in trades if t["reason"] != "scale-1:1"]
    pnls  = [t["pnl"] for t in trades]          # all cash flows
    wins  = [p for p in [t["pnl"] for t in full] if p > 0]
    losses= [p for p in [t["pnl"] for t in full] if p <= 0]
    gross = sum(pnls)
    charges = len(full) * 50
    net   = gross - charges

    # Per-regime breakdown
    by_regime: Dict[str, List[float]] = {}
    for t in full:
        rg = t.get("regime_at_entry", t.get("regime", "?"))
        by_regime.setdefault(rg, []).append(t["pnl"])

    print(f"\n{sep}")
    print(f"  Backtest v2 — {index_name}  ({days}d | BS pricing | regime-aware)")
    print(f"{sep}")
    print(f"  IV source     : {iv_info.get('source','?')}")
    print(f"  IV CE / PE    : {iv_info['iv_ce']*100:.1f}% / {iv_info['iv_pe']*100:.1f}%")
    print(f"  Strike / Expiry: {iv_info['strike']:.0f} / {iv_info['expiry']}")
    print(f"{sep}")
    print(f"  Total trades  : {len(full)}")
    print(f"  Winners       : {len(wins)}  ({len(wins)/len(full)*100:.1f}%)")
    print(f"  Losers        : {len(losses)}")
    print(f"  Avg win       : ₹{sum(wins)/len(wins) if wins else 0:>10,.0f}")
    print(f"  Avg loss      : ₹{sum(losses)/len(losses) if losses else 0:>10,.0f}")
    pf = abs(sum(wins)/sum(losses)) if losses and sum(losses) != 0 else float("inf")
    print(f"  Profit factor : {pf:.2f}  (target >1.5)")
    print(f"  Gross P&L     : ₹{gross:>10,.0f}")
    print(f"  Est. charges  : ₹{charges:>10,.0f}")
    print(f"  Net P&L       : ₹{net:>10,.0f}")
    print(f"  Net return    : {net/BT['capital']*100:.2f}% on ₹{BT['capital']:,}")
    print(f"{sep}")
    print(f"\n  BY REGIME:")
    print(f"  {'Regime':<10} {'Trades':>7} {'W':>4} {'L':>4} {'Total P&L':>12} {'Avg P&L':>10} {'Win%':>6}")
    print(f"  {'─'*60}")
    for rg, rg_pnls in sorted(by_regime.items()):
        rg_w = sum(1 for p in rg_pnls if p > 0)
        rg_l = sum(1 for p in rg_pnls if p <= 0)
        wr   = rg_w / len(rg_pnls) * 100 if rg_pnls else 0
        avg  = sum(rg_pnls) / len(rg_pnls) if rg_pnls else 0
        print(f"  {rg:<10} {len(rg_pnls):>7} {rg_w:>4} {rg_l:>4} "
              f"₹{sum(rg_pnls):>11,.0f} ₹{avg:>9,.0f} {wr:>5.1f}%")

    # Exit reason breakdown
    print(f"\n  EXIT REASONS:")
    by_exit: Dict[str, List[float]] = {}
    for t in trades:
        by_exit.setdefault(t["reason"], []).append(t["pnl"])
    for reason, epnls in sorted(by_exit.items()):
        print(f"  {reason:<22} {len(epnls):>3} trades  avg ₹{sum(epnls)/len(epnls):>8,.0f}")

    # Daily P&L
    by_date: Dict[str, float] = {}
    for t in trades:
        d = str(t["entry_time"].date())
        by_date[d] = by_date.get(d, 0) + t["pnl"]
    print(f"\n  DAILY P&L:")
    profitable_days = sum(1 for p in by_date.values() if p > 0)
    print(f"  Profitable days: {profitable_days}/{len(by_date)}")
    for d, pnl in sorted(by_date.items()):
        bar  = "█" * min(int(abs(pnl) / 1000), 25)
        sign = "+" if pnl >= 0 else "-"
        print(f"  {d}  {sign}₹{abs(pnl):>8,.0f}  {bar}")

    # Recent trade log
    show = trades[-20:]
    print(f"\n  RECENT TRADES (last {len(show)} of {len(trades)}):")
    print(f"  {'Entry time':<20} {'Dir':<4} {'Regime':<9} {'EntPrem':>8} "
          f"{'ExPrem':>8} {'Lots':>4} {'P&L':>10}  Reason")
    print(f"  {'─'*85}")
    for t in show:
        s = "+" if t["pnl"] >= 0 else ""
        rg = t.get("regime_at_entry", t.get("regime", "?"))
        print(f"  {str(t['entry_time'])[:19]:<20} {t['direction']:<4} {rg:<9} "
              f"₹{t['fill_price']:>7.2f} ₹{t['exit_premium']:>7.2f} "
              f"{t['lots']:>4}  {s}₹{t['pnl']:>9,.0f}  {t['reason']}")
    print()

def export_csv(trades, filename):
    if not trades: return
    import csv
    keys = ["index_name","direction","regime_at_entry","entry_time","exit_time",
            "entry_spot","exit_spot","strike","expiry","fill_price","exit_premium",
            "lots","qty","sl","target","reason","pnl"]
    with open(filename, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader(); w.writerows(trades)
    print(f"  Exported {len(trades)} trades → {filename}")

# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Backtest v2 — regime-aware BS pricing")
    ap.add_argument("--index",     default="NIFTY 50")
    ap.add_argument("--days",      type=int, default=30)
    ap.add_argument("--all",       action="store_true")
    ap.add_argument("--csv",       action="store_true")
    ap.add_argument("--no-spread", action="store_true")
    args = ap.parse_args()

    kite       = get_kite()
    use_spread = not args.no_spread

    def run_one(idx):
        print(f"\n{'─'*70}\n  {idx}\n{'─'*70}")
        iv_info = fetch_atm_iv(kite, idx)
        df1     = fetch_1min(kite, idx, args.days)
        if df1.empty or len(df1) < 30:
            print(f"  Insufficient data."); return
        print(f"  Computing 1-min indicators…")
        df1  = add_indicators(df1, BT["ema_fast_1m"], BT["ema_slow_1m"],
                               BT["rsi_period_1m"], BT["atr_period_1m"])
        print(f"  Computing 5-min indicators…")
        df5  = resample(df1, 5)
        df5  = add_indicators(df5, BT["ema_fast_5m"], BT["ema_slow_5m"],
                               BT["rsi_period_5m"], BT["atr_period_5m"])
        print(f"  Computing 15-min indicators…")
        df15 = resample(df1, 15)
        df15 = add_indicators(df15, BT["ema_trend_15m"], BT["ema_trend_15m"],
                               BT["rsi_period_5m"], BT["atr_period_5m"])
        print(f"  Running regime-aware backtest…")
        trades = run_backtest(df1, df5, df15, idx, iv_info, use_spread)
        print_report(trades, idx, args.days, iv_info)
        if args.csv:
            export_csv(trades, f"backtest_{idx.replace(' ','_')}.csv")

    if args.all:
        for idx in INDICES:
            try:   run_one(idx)
            except Exception as e: print(f"  ERROR: {e}")
    else:
        run_one(args.index)
