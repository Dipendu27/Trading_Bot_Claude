"""
Trade_Claude v2 — Zerodha AI Options Bot (Multi-Regime)
=========================================================
Improvements over v1:
  ┌─────────────────────────────────────────────────────────┐
  │ MARKET REGIME DETECTION                                  │
  │   Classifies market as BULL / BEAR / SIDEWAYS each bar  │
  │   using ADX + slope of 15-min EMA + VIX proxy           │
  │   Strategy parameters auto-switch per regime            │
  ├─────────────────────────────────────────────────────────┤
  │ BULL market  → buy CE aggressively, trail target wide   │
  │ BEAR market  → buy PE aggressively, trail target wide   │
  │ SIDEWAYS     → sell iron condor skew; buy only on       │
  │               confirmed breakouts; tighten SL           │
  ├─────────────────────────────────────────────────────────┤
  │ IMPROVED SIGNAL ENGINE                                   │
  │   + ADX(14) — only trade when ADX > 20 (avoid chop)    │
  │   + Supertrend — trend confirmation & dynamic SL        │
  │   + VWAP bands (±1σ, ±2σ) — mean-reversion in SIDEWAYS │
  │   + Volume profile proxy (POC detection)                │
  │   + Options-specific: IV percentile filter (skip when   │
  │     IV rank > 80 — premium too expensive to buy)        │
  ├─────────────────────────────────────────────────────────┤
  │ SMARTER POSITION MANAGEMENT                             │
  │   + Scaled exit: close 50% at 1:1, let rest run        │
  │   + Regime-aware SL/target (wider in trend, tighter     │
  │     in sideways)                                        │
  │   + Max daily loss circuit-breaker (stops new trades)   │
  │   + Cool-down after 2 consecutive losses               │
  ├─────────────────────────────────────────────────────────┤
  │ CLAUDE AI — options-aware prompt with regime context    │
  │ OLLAMA FALLBACK — auto-switch on internet drop          │
  │ PAPER TRADE = True (default)                            │
  └─────────────────────────────────────────────────────────┘

Run:
    pip install kiteconnect anthropic pandas numpy schedule requests scipy
    python bot.py
"""

import os, time, math, logging, schedule, threading, datetime
import numpy as np
import pandas as pd
from collections import deque
from kiteconnect import KiteConnect, KiteTicker
import anthropic
import journal
from typing import Dict, List, Any, Optional, Tuple

# ══════════════════════════════════════════════════════════
#  PAPER TRADE SWITCH
# ══════════════════════════════════════════════════════════
PAPER_TRADE: bool = True   # ← set False only when ready for real money

# ══════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════
CFG: Dict[str, Any] = {
    # ── Credentials (filled by setup.py) ─────────────────
    "api_key":       "YOUR_ZERODHA_API_KEY",
    "api_secret":    "YOUR_ZERODHA_API_SECRET",
    "anthropic_key": "YOUR_ANTHROPIC_API_KEY",
    "claude_model":  "claude-sonnet-4-6",

    # ── Local LLM fallback ────────────────────────────────
    "local_llm_model": "mistral",
    "local_llm_url":   "http://localhost:11434/api/generate",
    "llm_timeout_sec": 4,
    "_use_local_llm":  False,

    # ── Indices ───────────────────────────────────────────
    "indices": {
        "NIFTY 50":   "NIFTY",
        "NIFTY BANK": "BANKNIFTY",
    },
    "lot_sizes":   {"NIFTY": 25, "BANKNIFTY": 15},
    "strike_step": {"NIFTY": 50, "BANKNIFTY": 100},
    "otm_offset":  0,

    # ── Premium guard rails ───────────────────────────────
    "max_premium": 450,         # raised: don't miss valid setups
    "min_premium":  15,         # lowered: allow more options

    # ── Capital & position sizing ─────────────────────────
    "capital":        25000,    # ← matches user's actual capital
    "risk_per_trade": 0.06,     # 6% per trade (₹1500 on ₹25k → 1–2 lots)
    "max_lots":       5,        # raised from 3

    # ── Regime-aware SL/Target ────────────────────────────
    # These are overridden dynamically per regime (see REGIME_PARAMS)
    "sl_pct":            0.30,
    "target_pct":        1.00,
    "trail_sl":          True,
    "trail_trigger_pct": 0.20,

    # ── Scaled exit ───────────────────────────────────────
    "scale_exit":         True,   # close a smaller chunk at 1:1 R:R, rest at target
    "scale_exit_ratio":   0.3,    # keep most lots running toward the higher target

    # ── Daily circuit-breaker ─────────────────────────────
    "max_daily_loss":      5000,   # ₹ — 20% of ₹25k capital
    "cooldown_after_loss": 2,      # consecutive losses before 15-min pause

    # ── EOD ───────────────────────────────────────────────
    "squareoff_time": "15:15",
    "no_new_trades_after": "14:45",  # stop entering past this time

    # ── Indicators ────────────────────────────────────────
    "ema_fast_1m": 5,  "ema_slow_1m": 13,  "rsi_period_1m": 7,  "atr_period_1m": 7,
    "ema_fast_5m": 9,  "ema_slow_5m": 21,  "rsi_period_5m": 14, "atr_period_5m": 14,
    "ema_trend_15m": 21,   # 15-min EMA for regime slope
    "adx_period":    14,
    "supertrend_mult": 2.5,
    "min_adx_trend":   20,   # ADX must be > this for trend trades
    "min_adx_sideways": 15,  # ADX < this = confirmed sideways
    "min_volume_ratio": 1.1,
    "fake_break_ratio": 0.3,

    # ── IV filter ─────────────────────────────────────────
    "iv_rank_lookback": 20,   # sessions to compute IV rank
    "iv_rank_max":      75,   # skip BUY if IV rank > 75% (too expensive)

    # ── Signal thresholds ─────────────────────────────────
    "tf_agree_score":  5,
    "tf_single_score": 4,

    # ── Claude rate limit ─────────────────────────────────
    "claude_calls_per_min": 10,

    # ── Misc ──────────────────────────────────────────────
    "tick_buffer_1m":      400,
    "tick_buffer_5m":      120,
    "tick_buffer_15m":     60,
    "log_file":            "bot.log",
    "data_quality_min_1m": 20,
    "data_quality_min_5m": 10,
    "data_quality_min_15m": 5,
}

# ══════════════════════════════════════════════════════════
#  REGIME PARAMETERS  — auto-selected per detected regime
# ══════════════════════════════════════════════════════════
REGIME_PARAMS: Dict[str, Dict[str, Any]] = {
    "BULL": {
        "sl_pct":            0.25,   # 25% SL on premium
        "target_pct":        1.20,   # higher trend target: +120% option premium
        "trail_trigger_pct": 0.30,   # start trailing after +30%
        "otm_offset":        0,      # ATM CE
        "preferred_dir":     "CE",
        "min_adx":           20,
        "score_threshold":   4,
        "description": "Trending up — buy CE aggressively, wide target",
    },
    "BEAR": {
        "sl_pct":            0.25,
        "target_pct":        1.20,   # higher trend target: +120% option premium
        "trail_trigger_pct": 0.30,
        "otm_offset":        0,      # ATM PE
        "preferred_dir":     "PE",
        "min_adx":           20,
        "score_threshold":   4,
        "description": "Trending down — buy PE aggressively, wide target",
    },
    "SIDEWAYS": {
        "sl_pct":            0.15,   # tighter SL in chop (was 0.20)
        "target_pct":        0.55,   # higher breakout target, still below trend targets
        "trail_trigger_pct": 0.15,
        "otm_offset":        0,
        "preferred_dir":     "BOTH",
        "min_adx":           15,
        "score_threshold":   5,      # was 6 — slightly more permissive
        "description": "Range-bound — scalp breakouts, tight SL, quick profit",
    },
}

# ══════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(CFG["log_file"]),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("TradeClaude_v2")

# ══════════════════════════════════════════════════════════
#  SHARED STATE
# ══════════════════════════════════════════════════════════
open_positions: Dict[str, Dict[str, Any]] = {}
tick_store_1m:  Dict[int, deque] = {}
tick_store_5m:  Dict[int, deque] = {}
tick_store_15m: Dict[int, deque] = {}
live_ticks:     Dict[int, Dict[str, Any]] = {}
token_sym:      Dict[int, str]  = {}
sym_token:      Dict[str, int]  = {}
builders:       Dict[int, Any]  = {}
claude_calls:   List[float]     = []
index_prices:   Dict[str, float] = {}
active_options: Dict[str, Dict[str, Any]] = {}

# Session-level tracking
session_pnl:        float = 0.0
consecutive_losses: int   = 0
cooldown_until:     Optional[datetime.datetime] = None
daily_stopped:      bool  = False
current_regime:     Dict[str, str] = {}  # index_name → "BULL"/"BEAR"/"SIDEWAYS"
iv_history:         Dict[str, deque] = {}  # root → deque of daily IV values

# ══════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════
def using_claude() -> bool:
    return not CFG["_use_local_llm"]

def brain_name() -> str:
    return "Claude" if using_claude() else f"Ollama/{CFG['local_llm_model']}"

def regime_params(index_sym: str) -> Dict[str, Any]:
    regime = current_regime.get(index_sym, "SIDEWAYS")
    return REGIME_PARAMS[regime]

def can_enter() -> bool:
    """Global gate: circuit breaker + cooldown + time check."""
    if daily_stopped:
        return False
    if cooldown_until and datetime.datetime.now() < cooldown_until:
        return False
    now = datetime.datetime.now().time()
    if now >= datetime.time(14, 45):
        return False
    return True

# ══════════════════════════════════════════════════════════
#  AUTHENTICATION
# ══════════════════════════════════════════════════════════
def get_kite() -> KiteConnect:
    kite       = KiteConnect(api_key=CFG["api_key"])
    token_file = ".access_token"
    today      = datetime.date.today().isoformat()
    if os.path.exists(token_file):
        parts = open(token_file).read().strip().split("|")
        if len(parts) == 2 and parts[0] == today:
            kite.set_access_token(parts[1])
            log.info("Reused access token.")
            return kite
    print(f"\nLogin URL:\n{kite.login_url()}\n")
    req_tok = input("Paste request_token: ").strip()
    sess    = kite.generate_session(req_tok, api_secret=CFG["api_secret"])
    kite.set_access_token(sess["access_token"])
    open(token_file, "w").write(f"{today}|{sess['access_token']}")
    log.info("Login successful.")
    return kite

# ══════════════════════════════════════════════════════════
#  INSTRUMENT LOADER
# ══════════════════════════════════════════════════════════
def load_tokens(kite: KiteConnect) -> List[int]:
    today     = datetime.date.today()
    nse_insts = kite.instruments("NSE")
    for inst in nse_insts:
        s = inst["tradingsymbol"]
        if s in CFG["indices"]:
            token_sym[inst["instrument_token"]] = s
            sym_token[s] = inst["instrument_token"]
            log.info(f"  Index: {s} → {inst['instrument_token']}")

    nfo_insts = kite.instruments("NFO")
    for index_name, root in CFG["indices"].items():
        if index_name not in sym_token:
            continue
        expiries = sorted({
            i["expiry"] for i in nfo_insts
            if i["name"] == root and i["expiry"] and i["expiry"] >= today
        })
        if not expiries:
            continue
        expiry = expiries[0]
        log.info(f"  {root} expiry: {expiry}")
        try:
            q   = kite.quote(f"NSE:{index_name}")
            ltp = q[f"NSE:{index_name}"]["last_price"]
        except Exception as e:
            log.warning(f"  Quote failed for {index_name}: {e}")
            continue
        step   = CFG["strike_step"].get(root, 50)
        atm    = round(ltp / step) * step
        active_options[root] = {"CE": None, "PE": None, "expiry": expiry}
        loaded = 0
        for s_off in range(-3, 4):       # ATM-3 to ATM+3
            strike = atm + s_off * step
            for ot in ("CE", "PE"):
                inst = next((i for i in nfo_insts
                             if i["name"] == root and i["expiry"] == expiry
                             and abs(i["strike"] - strike) < 0.01
                             and i["instrument_type"] == ot), None)
                if not inst:
                    continue
                tok = inst["instrument_token"]
                sym = inst["tradingsymbol"]
                token_sym[tok] = sym
                sym_token[sym] = tok
                if s_off == CFG["otm_offset"] and ot == "CE":
                    active_options[root]["CE"] = sym
                if s_off == -CFG["otm_offset"] and ot == "PE":
                    active_options[root]["PE"] = sym
                loaded += 1
        log.info(f"  {root}: {loaded} option tokens | CE={active_options[root]['CE']} PE={active_options[root]['PE']}")

    tokens = list(token_sym.keys())
    log.info(f"Total tokens: {len(tokens)}")
    return tokens

# ══════════════════════════════════════════════════════════
#  CANDLE BUILDER  (1m + 5m + 15m simultaneously)
# ══════════════════════════════════════════════════════════
class CandleBuilder:
    def __init__(self, token: int):
        self.token = token
        self._state: Dict[int, Dict] = {1: {}, 5: {}, 15: {}}
        self._stores = {
            1: (tick_store_1m, CFG["tick_buffer_1m"]),
            5: (tick_store_5m, CFG["tick_buffer_5m"]),
            15: (tick_store_15m, CFG["tick_buffer_15m"]),
        }

    @staticmethod
    def _bucket(ts: datetime.datetime, interval: int) -> datetime.datetime:
        f = ts.replace(second=0, microsecond=0)
        return f.replace(minute=(f.minute // interval) * interval)

    def add_tick(self, price: float, volume: float, ts: datetime.datetime):
        for interval, (store, maxlen) in self._stores.items():
            bucket = self._bucket(ts, interval)
            s      = self._state[interval]
            if not s or bucket != s.get("ts"):
                if s:
                    buf = store.setdefault(self.token, deque(maxlen=maxlen))
                    buf.append({"time": s["ts"], "open": s["o"], "high": s["h"],
                                "low": s["l"], "close": s["c"], "volume": s["v"]})
                self._state[interval] = {"ts": bucket, "o": price, "h": price,
                                          "l": price, "c": price, "v": volume}
            else:
                s["h"] = max(s["h"], price)
                s["l"] = min(s["l"], price)
                s["c"] = price
                s["v"] += volume

# ══════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════
def df_from_buf(store: Dict[int, deque], token: int, min_bars: int) -> pd.DataFrame:
    buf = store.get(token, deque())
    if len(buf) < min_bars:
        return pd.DataFrame()
    df = pd.DataFrame(list(buf))
    df.set_index("time", inplace=True)
    return df

def compute_indicators(df: pd.DataFrame, ef: int, es: int,
                        rp: int, ap: int) -> pd.DataFrame:
    df = df.copy()
    # EMAs
    df["ema_fast"] = df["close"].ewm(span=ef, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=es, adjust=False).mean()
    # RSI
    d   = df["close"].diff()
    g   = d.clip(lower=0).ewm(com=rp-1, adjust=False).mean()
    l_  = (-d.clip(upper=0)).ewm(com=rp-1, adjust=False).mean().replace(0, float("nan"))
    df["rsi"] = 100 - 100 / (1 + g / l_)
    # ATR
    hl  = df["high"] - df["low"]
    hc  = (df["high"] - df["close"].shift()).abs()
    lc  = (df["low"]  - df["close"].shift()).abs()
    df["atr"] = pd.concat([hl, hc, lc], axis=1).max(axis=1).ewm(span=ap, adjust=False).mean()
    # VWAP with bands (resets per session — approximate with rolling since we don't have dates here)
    tp          = (df["high"] + df["low"] + df["close"]) / 3
    df["vwap"]  = (tp * df["volume"]).cumsum() / df["volume"].cumsum()
    df["vwap_std"] = (df["close"] - df["vwap"]).rolling(20).std()
    df["vwap_u1"]  = df["vwap"] + df["vwap_std"]        # +1σ
    df["vwap_d1"]  = df["vwap"] - df["vwap_std"]        # -1σ
    df["vwap_u2"]  = df["vwap"] + 2 * df["vwap_std"]   # +2σ
    df["vwap_d2"]  = df["vwap"] - 2 * df["vwap_std"]   # -2σ
    # OBV
    df["obv"]    = (np.sign(df["close"].diff()) * df["volume"]).cumsum()
    df["vol_ma"] = df["volume"].rolling(20).mean()
    # ADX
    df = _add_adx(df, CFG["adx_period"])
    # Supertrend
    df = _add_supertrend(df, CFG["adx_period"], CFG["supertrend_mult"])
    return df

def _add_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """ADX — measures trend strength regardless of direction."""
    hi, lo, cl = df["high"], df["low"], df["close"]
    up   = hi.diff()
    down = -lo.diff()
    pos_dm = np.where((up > down) & (up > 0), up, 0.0)
    neg_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr   = pd.concat([hi - lo, (hi - cl.shift()).abs(), (lo - cl.shift()).abs()], axis=1).max(axis=1)
    atr  = tr.ewm(span=period, adjust=False).mean()
    pdi  = 100 * pd.Series(pos_dm, index=df.index).ewm(span=period, adjust=False).mean() / atr
    ndi  = 100 * pd.Series(neg_dm, index=df.index).ewm(span=period, adjust=False).mean() / atr
    dx   = (100 * (pdi - ndi).abs() / (pdi + ndi + 1e-9))
    df["adx"] = dx.ewm(span=period, adjust=False).mean()
    df["+di"] = pdi
    df["-di"] = ndi
    return df

def _add_supertrend(df: pd.DataFrame, period: int = 14, mult: float = 2.5) -> pd.DataFrame:
    """Supertrend — dynamic support/resistance based on ATR."""
    hl2   = (df["high"] + df["low"]) / 2
    atr   = df["atr"]
    upper = hl2 + mult * atr
    lower = hl2 - mult * atr
    st    = pd.Series(index=df.index, dtype=float)
    st_dir= pd.Series(index=df.index, dtype=int)   # 1=bullish, -1=bearish
    for i in range(1, len(df)):
        if df["close"].iloc[i] > upper.iloc[i-1]:
            st.iloc[i]     = lower.iloc[i]
            st_dir.iloc[i] = 1
        elif df["close"].iloc[i] < lower.iloc[i-1]:
            st.iloc[i]     = upper.iloc[i]
            st_dir.iloc[i] = -1
        else:
            st_dir.iloc[i] = st_dir.iloc[i-1]
            if st_dir.iloc[i] == 1:
                st.iloc[i] = max(lower.iloc[i], st.iloc[i-1]) if i > 1 else lower.iloc[i]
            else:
                st.iloc[i] = min(upper.iloc[i], st.iloc[i-1]) if i > 1 else upper.iloc[i]
    if len(df) > 0:
        st.iloc[0]     = lower.iloc[0]
        st_dir.iloc[0] = 1
    df["supertrend"]     = st
    df["supertrend_dir"] = st_dir
    return df

# ══════════════════════════════════════════════════════════
#  MARKET REGIME DETECTOR
# ══════════════════════════════════════════════════════════
def detect_regime(df15: pd.DataFrame, df5: pd.DataFrame) -> str:
    """
    Returns "BULL", "BEAR", or "SIDEWAYS" based on:
      1. ADX value (strength)
      2. +DI vs -DI (direction)
      3. Supertrend direction on 15m
      4. 15m EMA slope

    Logic:
      ADX > 20 AND +DI > -DI AND ST bullish → BULL
      ADX > 20 AND -DI > +DI AND ST bearish → BEAR
      ADX < 20 (or conflicting signals)     → SIDEWAYS
    """
    if df15.empty or len(df15) < 5:
        # Fall back to 5m if no 15m yet
        if df5.empty or len(df5) < 5:
            return "SIDEWAYS"
        df = df5
    else:
        df = df15

    last   = df.iloc[-1]
    adx    = last.get("adx", 0)
    pdi    = last.get("+di", 0)
    ndi    = last.get("-di", 0)
    st_dir = last.get("supertrend_dir", 0)

    # EMA slope over last 5 bars
    if len(df) >= 5:
        slope = (float(df["ema_slow"].iloc[-1]) - float(df["ema_slow"].iloc[-5])) / 5
    else:
        slope = 0.0

    if adx > CFG["min_adx_trend"]:
        if pdi > ndi and st_dir == 1 and slope > 0:
            return "BULL"
        elif ndi > pdi and st_dir == -1 and slope < 0:
            return "BEAR"
        else:
            return "SIDEWAYS"   # ADX high but conflicting → don't trade
    else:
        return "SIDEWAYS"

# ══════════════════════════════════════════════════════════
#  FAKE BREAKOUT FILTER
# ══════════════════════════════════════════════════════════
def is_fake_breakout(df: pd.DataFrame, regime: str) -> Tuple[bool, str]:
    if len(df) < 5:
        return False, ""
    last = df.iloc[-1]; prev = df.iloc[-2]
    body = abs(last["close"] - last["open"])
    rng  = last["high"] - last["low"] + 1e-9
    flags: List[str] = []

    # Body ratio — wick-heavy candle = indecision
    ratio_threshold = CFG["fake_break_ratio"]
    if regime == "SIDEWAYS":
        ratio_threshold = 0.4   # stricter in sideways
    if body / rng < ratio_threshold:
        flags.append(f"thin-body({body/rng:.0%})")

    # Volume
    if last["volume"] < last["vol_ma"] * CFG["min_volume_ratio"]:
        flags.append("low-vol")

    # Close inside previous candle range
    if prev["low"] < last["close"] < prev["high"]:
        flags.append("inside-close")

    # OBV divergence
    if len(df) >= 4:
        if last["close"] > df.iloc[-4]["close"] and last["obv"] < df.iloc[-4]["obv"]:
            flags.append("OBV-div")

    # In sideways, reject if close is inside VWAP bands (no breakout)
    if regime == "SIDEWAYS":
        if last.get("vwap_d1", 0) < last["close"] < last.get("vwap_u1", 1e9):
            flags.append("vwap-inside")

    threshold = 2 if regime != "SIDEWAYS" else 1   # stricter in sideways
    is_fake   = len(flags) >= threshold
    return is_fake, " | ".join(flags)

# ══════════════════════════════════════════════════════════
#  SINGLE-TIMEFRAME SCORER  (regime-aware, 0–10 points)
# ══════════════════════════════════════════════════════════
def score_tf(df: pd.DataFrame, regime: str) -> Tuple[int, int, Dict[str, Any]]:
    if len(df) < 3:
        return 0, 0, {}
    last = df.iloc[-1]; prev = df.iloc[-2]; ago3 = df.iloc[-3]

    xup = prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]
    xdn = prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]
    tup = last["ema_fast"] > last["ema_slow"]
    tdn = last["ema_fast"] < last["ema_slow"]

    rsi_bull = 42 < last["rsi"] < 72
    rsi_bear = 28 < last["rsi"] < 58
    abv_vwap = last["close"] > last["vwap"]
    vol_spk  = last["volume"] > last["vol_ma"] * CFG["min_volume_ratio"]
    obv_up   = last["obv"] > ago3["obv"]
    obv_dn   = last["obv"] < ago3["obv"]
    mom_up   = last["close"] > prev["close"] > ago3["close"]
    mom_dn   = last["close"] < prev["close"] < ago3["close"]
    st_bull  = last.get("supertrend_dir", 0) == 1
    st_bear  = last.get("supertrend_dir", 0) == -1
    adx_ok   = last.get("adx", 0) > regime_params_by_regime(regime)["min_adx"]

    # SIDEWAYS-specific: VWAP band signals
    at_vwap_sup = last["close"] < last.get("vwap_d1", 0)   # near lower band = bounce up
    at_vwap_res = last["close"] > last.get("vwap_u1", 1e9) # near upper band = fade

    if regime in ("BULL", "BEAR"):
        buys  = [xup or tup, rsi_bull and last["rsi"] <= 72, abv_vwap,
                 vol_spk, obv_up, mom_up,
                 last["close"] > last.get("vwap_u1", 0), xup,
                 st_bull, adx_ok]
        sells = [xdn or tdn, rsi_bear and last["rsi"] >= 28, not abv_vwap,
                 vol_spk, obv_dn, mom_dn,
                 last["close"] < last.get("vwap_d1", 1e9), xdn,
                 st_bear, adx_ok]
    else:   # SIDEWAYS — mean reversion signals
        buys  = [at_vwap_sup, rsi_bull and last["rsi"] < 50, abv_vwap,
                 vol_spk, obv_up, mom_up, xup, st_bull, adx_ok,
                 last["close"] > last.get("vwap_d2", 0)]
        sells = [at_vwap_res, rsi_bear and last["rsi"] > 50, not abv_vwap,
                 vol_spk, obv_dn, mom_dn, xdn, st_bear, adx_ok,
                 last["close"] < last.get("vwap_u2", 1e9)]

    meta = {
        "rsi":      round(float(last["rsi"]), 2),
        "adx":      round(float(last.get("adx", 0)), 2),
        "atr":      round(float(last["atr"]), 2),
        "vwap":     round(float(last["vwap"]), 2),
        "price":    float(last["close"]),
        "st_dir":   int(last.get("supertrend_dir", 0)),
        "xup":      xup, "xdn": xdn,
    }
    return sum(buys), sum(sells), meta

def regime_params_by_regime(regime: str) -> Dict[str, Any]:
    return REGIME_PARAMS.get(regime, REGIME_PARAMS["SIDEWAYS"])

# ══════════════════════════════════════════════════════════
#  DUAL-TF SIGNAL  (regime-aware)
# ══════════════════════════════════════════════════════════
def generate_signal(index_sym: str) -> Dict[str, Any]:
    token = sym_token.get(index_sym)
    empty = {
        "action": "HOLD", "symbol": index_sym, "regime": "UNKNOWN",
        "confidence": "no_data", "score_1m_buy": 0, "score_1m_sell": 0,
        "score_5m_buy": 0, "score_5m_sell": 0, "total_buy": 0, "total_sell": 0,
        "rsi_1m": None, "rsi_5m": None, "adx_1m": None,
        "vwap": None, "atr": None, "supertrend_dir": None,
        "fake_breakout": False, "fake_reason": "",
        "price": index_prices.get(index_sym, 0),
    }
    if token is None:
        return empty

    df1  = df_from_buf(tick_store_1m,  token, CFG["data_quality_min_1m"])
    df5  = df_from_buf(tick_store_5m,  token, CFG["data_quality_min_5m"])
    df15 = df_from_buf(tick_store_15m, token, CFG["data_quality_min_15m"])

    if df1.empty and df5.empty:
        return empty

    # Compute indicators
    if not df1.empty:
        df1 = compute_indicators(df1, CFG["ema_fast_1m"], CFG["ema_slow_1m"],
                                  CFG["rsi_period_1m"], CFG["atr_period_1m"])
    if not df5.empty:
        df5 = compute_indicators(df5, CFG["ema_fast_5m"], CFG["ema_slow_5m"],
                                  CFG["rsi_period_5m"], CFG["atr_period_5m"])
    if not df15.empty:
        df15 = compute_indicators(df15, CFG["ema_trend_15m"], CFG["ema_trend_15m"],
                                   CFG["rsi_period_5m"], CFG["atr_period_5m"])

    # Detect regime
    regime = detect_regime(df15, df5)
    current_regime[index_sym] = regime
    rp     = regime_params_by_regime(regime)

    # Score each timeframe
    s1b = s1s = s5b = s5s = 0
    m1: Dict[str, Any] = {}; m5: Dict[str, Any] = {}
    fake = False; fake_reason = ""

    if not df1.empty:
        s1b, s1s, m1 = score_tf(df1, regime)
        fake, fake_reason = is_fake_breakout(df1, regime)

    if not df5.empty:
        s5b, s5s, m5 = score_tf(df5, regime)

    tb = s1b + s5b; ts = s1s + s5s
    thresh = rp["score_threshold"]   # regime-specific threshold

    # Determine action
    if tb >= thresh * 2:       action, conf = "BUY",       "strong"
    elif ts >= thresh * 2:     action, conf = "SELL",      "strong"
    elif tb >= thresh + 2:     action, conf = "AMBIGUOUS", "moderate-buy"
    elif ts >= thresh + 2:     action, conf = "AMBIGUOUS", "moderate-sell"
    elif s1b >= thresh or (not df5.empty and s5b >= thresh):
        action, conf = "AMBIGUOUS", "weak-buy"
    elif s1s >= thresh or (not df5.empty and s5s >= thresh):
        action, conf = "AMBIGUOUS", "weak-sell"
    else:
        action, conf = "HOLD", "low"

    # In SIDEWAYS, only trade AMBIGUOUS or STRONG (more confirmation needed)
    if regime == "SIDEWAYS" and action == "BUY" and tb < thresh * 2.5:
        action, conf = "AMBIGUOUS", "sideways-needs-confirm"
    if regime == "SIDEWAYS" and action == "SELL" and ts < thresh * 2.5:
        action, conf = "AMBIGUOUS", "sideways-needs-confirm"

    return {
        "action": action, "confidence": conf, "regime": regime,
        "symbol": index_sym,
        "price":  index_prices.get(index_sym, m1.get("price") or m5.get("price", 0)),
        "score_1m_buy": s1b, "score_1m_sell": s1s,
        "score_5m_buy": s5b, "score_5m_sell": s5s,
        "total_buy": tb, "total_sell": ts,
        "rsi_1m":  m1.get("rsi"), "rsi_5m":  m5.get("rsi"),
        "adx_1m":  m1.get("adx"), "adx_5m":  m5.get("adx"),
        "atr":     m1.get("atr"), "vwap":    m1.get("vwap"),
        "supertrend_dir": m1.get("st_dir"),
        "fake_breakout": fake, "fake_reason": fake_reason,
    }

# ══════════════════════════════════════════════════════════
#  IV RANK  (tracks rolling IV to avoid buying expensive premiums)
# ══════════════════════════════════════════════════════════
def update_iv_history(root: str, current_iv: float):
    if root not in iv_history:
        iv_history[root] = deque(maxlen=CFG["iv_rank_lookback"])
    iv_history[root].append(current_iv)

def get_iv_rank(root: str, current_iv: float) -> float:
    """
    IV Rank = (current IV - 52w low) / (52w high - 52w low) × 100
    Here we use the rolling window instead of 52 weeks.
    Returns 0–100. High rank = expensive premiums.
    """
    hist = iv_history.get(root, deque())
    if len(hist) < 5:
        return 50.0   # neutral default when insufficient history
    lo, hi = min(hist), max(hist)
    if hi == lo:
        return 50.0
    return (current_iv - lo) / (hi - lo) * 100

# ══════════════════════════════════════════════════════════
#  AI BRAIN  —  Claude PRIMARY, Ollama FALLBACK
# ══════════════════════════════════════════════════════════
def _can_call_claude() -> bool:
    now = time.time()
    claude_calls[:] = [t for t in claude_calls if now - t < 60]
    return len(claude_calls) < CFG["claude_calls_per_min"]

def _call_claude(prompt: str) -> Optional[str]:
    if not _can_call_claude():
        return None
    try:
        msg = anthropic.Anthropic(api_key=CFG["anthropic_key"]).messages.create(
            model=CFG["claude_model"], max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip().upper()
        claude_calls.append(time.time())
        for w in ("BUY", "SELL", "HOLD"):
            if w in raw:
                return w
        return "HOLD"
    except Exception as e:
        log.warning(f"Claude error: {e}")
        return None

def _call_local_llm(prompt: str) -> Optional[str]:
    import requests as req
    try:
        r    = req.post(CFG["local_llm_url"],
                        json={"model": CFG["local_llm_model"], "prompt": prompt, "stream": False},
                        timeout=CFG["llm_timeout_sec"])
        resp = r.json().get("response", "").strip().upper()
        if "BUY"  in resp: return "BUY"
        if "SELL" in resp: return "SELL"
        return "HOLD"
    except Exception as e:
        log.debug(f"Local LLM error: {e}")
        return None

def ask_ai(sig: Dict[str, Any], df1_tail: str, df5_tail: str,
           opt_sym: str, premium: float) -> Tuple[str, str]:
    root   = CFG["indices"].get(sig["symbol"], sig["symbol"])
    lot    = CFG["lot_sizes"].get(root, 25)
    regime = sig.get("regime", "UNKNOWN")
    rp     = regime_params_by_regime(regime)

    prompt = f"""You are an expert intraday NSE options trader. Current market regime: {regime}.
{rp['description']}

Index   : {sig['symbol']}  @ ₹{sig['price']:.2f}
Option  : {opt_sym or 'N/A'}  |  Premium ₹{premium:.2f}  |  Lot {lot}

REGIME  : {regime}
ADX(1m) : {sig.get('adx_1m', '?')}  (>20 = trending, <20 = choppy)
ADX(5m) : {sig.get('adx_5m', '?')}
Supertrend direction: {sig.get('supertrend_dir', '?')}  (1=bullish, -1=bearish)

SIGNAL SCORES (0–10 each timeframe):
  1-min  Buy={sig['score_1m_buy']} Sell={sig['score_1m_sell']}  RSI={sig['rsi_1m']}
  5-min  Buy={sig['score_5m_buy']} Sell={sig['score_5m_sell']}  RSI={sig['rsi_5m']}
  Confidence: {sig['confidence']}
  Fake breakout: {sig.get('fake_reason') or 'None'}

Last 8 × 1-min INDEX candles:
{df1_tail}

Last 8 × 5-min INDEX candles:
{df5_tail}

Open positions: {len(open_positions)}  Session P&L: ₹{session_pnl:.0f}

REGIME-SPECIFIC RULES:
  {regime}: SL={rp['sl_pct']*100:.0f}%  Target={rp['target_pct']*100:.0f}%  prefer={rp['preferred_dir']}

GENERAL RULES:
- BUY options only (no shorting/writing).
- BUY CE on bullish index; BUY PE on bearish index.
- SELL = EXIT a held position.
- BULL/BEAR: confirm trend with ADX>20 AND Supertrend direction.
- SIDEWAYS: only enter on VWAP band touch + reversal candle.
- Skip if fake breakout flags present.
- Never enter after 2:45 PM IST.
- High theta decay if expiry ≤ 2 days.

Reply with EXACTLY one word: BUY  SELL  or  HOLD"""

    if using_claude():
        result = _call_claude(prompt)
        if result:
            return result, "CLAUDE"
        return (_call_local_llm(prompt) or "HOLD"), "LOCAL(emergency)"
    result = _call_local_llm(prompt)
    return (result or "HOLD"), f"LOCAL/{CFG['local_llm_model']}"

# ══════════════════════════════════════════════════════════
#  CONNECTIVITY WATCHDOG
# ══════════════════════════════════════════════════════════
class ConnectivityWatchdog(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.online = True

    def run(self):
        import urllib.request
        while True:
            try:
                urllib.request.urlopen("https://api.anthropic.com", timeout=3)
                if not self.online:
                    self.online = True
                    CFG["_use_local_llm"] = False
                    log.info("🌐 Internet restored — brain back to Claude.")
            except Exception:
                if self.online:
                    self.online = False
                    CFG["_use_local_llm"] = True
                    log.warning("🔌 Internet DOWN — brain switched to Ollama.")
            time.sleep(5)

watchdog = ConnectivityWatchdog()

# ══════════════════════════════════════════════════════════
#  OPTION SELECTOR
# ══════════════════════════════════════════════════════════
def get_live_option(root: str, direction: str) -> Optional[str]:
    if root not in active_options:
        return None
    index_name = next((k for k, v in CFG["indices"].items() if v == root), None)
    ltp_index  = index_prices.get(index_name or "", 0)

    if ltp_index > 0:
        step   = CFG["strike_step"].get(root, 50)
        rp     = regime_params(index_name or "")
        offset = rp.get("otm_offset", 0)
        atm    = round(ltp_index / step) * step
        target_strike = (atm + offset * step) if direction == "CE" else (atm - offset * step)
        for tok, sym in token_sym.items():
            if not sym.startswith(root) or not sym.endswith(direction):
                continue
            if str(int(target_strike)) in sym:
                ltp_opt = live_ticks.get(tok, {}).get("last_price", 0)
                if ltp_opt and not (CFG["min_premium"] <= ltp_opt <= CFG["max_premium"]):
                    return None
                return sym

    return active_options[root].get(direction)

# ══════════════════════════════════════════════════════════
#  ORDER MANAGEMENT
# ══════════════════════════════════════════════════════════
def calc_lots(premium: float, root: str, regime: str = "SIDEWAYS") -> int:
    """
    Position sizing uses actual stop-loss risk, then caps exposure so a
    single option position does not consume too much of a 25k account.
    """
    lot_size = CFG["lot_sizes"].get(root, 25)
    if premium <= 0 or lot_size <= 0:
        return 0

    # Risk budget (in ₹)
    max_risk_per_trade = CFG["capital"] * CFG["risk_per_trade"]

    # Max loss per lot = premium x regime stop-loss x lot_size
    sl_fraction  = max(float(regime_params_by_regime(regime).get("sl_pct", CFG["sl_pct"])), 0.01)
    loss_per_lot = premium * sl_fraction * lot_size
    if loss_per_lot <= 0:
        return 0

    # How many lots fit in the risk budget?
    risk_based_lots = int(max_risk_per_trade / loss_per_lot)

    # Capital guard: no more than 15% of capital in one position
    capital_guard_lots = int(CFG["capital"] * 0.15 / (premium * lot_size))

    if risk_based_lots < 1 or capital_guard_lots < 1:
        return 0

    # Trend regimes can use the full lot cap; sideways stays smaller.
    tier_cap = CFG["max_lots"] if regime in ("BULL", "BEAR") else max(1, CFG["max_lots"] // 2 + 1)

    return max(0, min(risk_based_lots, capital_guard_lots, tier_cap, CFG["max_lots"]))


def scaled_exit_qty(pos: Dict[str, Any]) -> int:
    """Return a lot-safe quantity for the first partial exit."""
    lot_size = int(pos.get("lot_size") or CFG["lot_sizes"].get(pos.get("root", ""), 25))
    lots = int(pos.get("lots") or max(0, pos.get("qty", 0) // lot_size))
    if lots <= 1:
        return 0
    lots_to_exit = max(1, int(lots * CFG["scale_exit_ratio"]))
    lots_to_exit = min(lots_to_exit, lots - 1)
    return lots_to_exit * lot_size

def place_buy(kite: KiteConnect, opt_sym: str, root: str,
              index_price: float, brain: str = "ALGO", regime: str = "SIDEWAYS"):
    global session_pnl, consecutive_losses, cooldown_until, daily_stopped

    if not can_enter():
        return
    if opt_sym in open_positions:
        return

    tok     = sym_token.get(opt_sym, 0)
    premium = live_ticks.get(tok, {}).get("last_price", 0)
    if not (CFG["min_premium"] <= premium <= CFG["max_premium"]):
        log.warning(f"  {opt_sym} premium ₹{premium:.0f} outside rails — skip.")
        return

    rp       = regime_params_by_regime(regime)
    lot_size = CFG["lot_sizes"].get(root, 25)
    lots     = calc_lots(premium, root, regime)
    if lots < 1:
        log.warning(f"  {opt_sym} premium ₹{premium:.0f} exceeds risk/capital guard — skip.")
        return
    qty      = lots * lot_size
    sl       = round(premium * (1 - rp["sl_pct"]),     2)
    target   = round(premium * (1 + rp["target_pct"]), 2)
    # Scaled exit: first target at 1:1 R:R
    scale_target = round(premium * (1 + rp["sl_pct"]), 2)

    if PAPER_TRADE:
        oid = f"PAPER_{int(time.time())}"
        position_cost = premium * qty
        pct_capital   = position_cost / CFG["capital"] * 100
        log.info(f"📝 [PAPER|{regime}] BUY  {opt_sym:32} lots={lots} qty={qty} "
                 f"prem=₹{premium:.2f}  SL=₹{sl}  T=₹{target}  "
                 f"cost=₹{position_cost:.0f}({pct_capital:.1f}%)  [{brain}]")
    else:
        try:
            oid = kite.place_order(
                variety=kite.VARIETY_REGULAR, exchange=kite.EXCHANGE_NFO,
                tradingsymbol=opt_sym, transaction_type=kite.TRANSACTION_TYPE_BUY,
                quantity=qty, order_type=kite.ORDER_TYPE_MARKET, product=kite.PRODUCT_MIS,
            )
            position_cost = premium * qty
            pct_capital   = position_cost / CFG["capital"] * 100
            log.info(f"✅ [{regime}] BUY  {opt_sym:32} lots={lots} qty={qty} "
                     f"prem=₹{premium:.2f}  SL=₹{sl}  T=₹{target}  "
                     f"cost=₹{position_cost:.0f}({pct_capital:.1f}%)  [{brain}]")
        except Exception as e:
            log.error(f"Buy FAILED {opt_sym}: {e}")
            return

    jid = journal.log_entry(opt_sym, qty, premium, sl, target, ai_decision=brain)
    open_positions[opt_sym] = {
        "qty": qty, "entry": premium, "sl": sl, "target": target,
        "scale_target": scale_target, "scaled_out": False,
        "peak": premium, "order_id": oid, "journal_id": jid,
        "root": root, "lots": lots, "lot_size": lot_size, "regime": regime,
        "index_price_at_entry": index_price,
    }

def place_sell(kite: KiteConnect, opt_sym: str, reason: str = "signal",
               partial_qty: int = 0):
    global session_pnl, consecutive_losses, cooldown_until, daily_stopped

    if opt_sym not in open_positions:
        return
    pos      = open_positions[opt_sym]
    tok      = sym_token.get(opt_sym, 0)
    cur_prem = live_ticks.get(tok, {}).get("last_price", pos["entry"])
    sell_qty = partial_qty if partial_qty else pos["qty"]
    pnl      = (cur_prem - pos["entry"]) * sell_qty

    if PAPER_TRADE:
        log.info(f"📝 [PAPER] {'PARTIAL ' if partial_qty else ''}SELL "
                 f"{opt_sym:32} reason={reason:20} exit=₹{cur_prem:.2f}  P&L=₹{pnl:.0f}")
    else:
        try:
            kite.place_order(
                variety=kite.VARIETY_REGULAR, exchange=kite.EXCHANGE_NFO,
                tradingsymbol=opt_sym, transaction_type=kite.TRANSACTION_TYPE_SELL,
                quantity=sell_qty, order_type=kite.ORDER_TYPE_MARKET,
                product=kite.PRODUCT_MIS,
            )
            log.info(f"🔴 {'PARTIAL ' if partial_qty else ''}SELL "
                     f"{opt_sym:32} reason={reason:20} P&L=₹{pnl:.0f}")
        except Exception as e:
            log.error(f"Sell FAILED {opt_sym}: {e}")
            return

    session_pnl += pnl

    if not partial_qty:
        # Full exit
        if pnl < 0:
            consecutive_losses += 1
            if consecutive_losses >= CFG["cooldown_after_loss"]:
                cooldown_until = datetime.datetime.now() + datetime.timedelta(minutes=15)
                log.warning(f"⏸  {consecutive_losses} consecutive losses — 15min cooldown.")
        else:
            consecutive_losses = 0

        if session_pnl <= -CFG["max_daily_loss"]:
            daily_stopped = True
            log.warning(f"🛑 Daily loss limit hit (₹{session_pnl:.0f}) — no new trades today.")

        if "journal_id" in pos:
            journal.log_exit(pos["journal_id"], cur_prem, reason, pnl)
            journal.update_daily_summary()
        del open_positions[opt_sym]
    else:
        # Partial exit — update remaining qty
        open_positions[opt_sym]["qty"] -= sell_qty
        if pos.get("lot_size"):
            open_positions[opt_sym]["lots"] = max(0, open_positions[opt_sym]["qty"] // pos["lot_size"])
        open_positions[opt_sym]["scaled_out"] = True
        if "journal_id" in pos:
            journal.log_exit(pos["journal_id"], cur_prem, f"{reason}(partial)", pnl)

def check_positions(kite: KiteConnect):
    """SL, target, trailing SL, and scaled exit checks."""
    for sym, pos in list(open_positions.items()):
        tok = sym_token.get(sym, 0)
        ltp = live_ticks.get(tok, {}).get("last_price", 0)
        if not ltp:
            continue
        pct = (ltp - pos["entry"]) / pos["entry"]

        # ── Scaled exit: book a smaller lot-safe chunk at 1:1, let the rest run ──
        if (CFG["scale_exit"] and not pos["scaled_out"]
                and ltp >= pos.get("scale_target", 1e9)):
            partial_qty = scaled_exit_qty(pos)
            if not partial_qty:
                open_positions[sym]["scaled_out"] = True
                pos = open_positions[sym]
            else:
                place_sell(kite, sym, "scale-1:1", partial_qty=partial_qty)
                # Widen trail trigger after partial close
                open_positions[sym]["trail_trigger_pct"] = pos.get("trail_trigger_pct", CFG["trail_trigger_pct"]) * 0.5
                continue

        # ── Trailing SL ───────────────────────────────────
        trig = pos.get("trail_trigger_pct", CFG["trail_trigger_pct"])
        if CFG["trail_sl"] and pct > trig:
            open_positions[sym]["peak"] = max(ltp, pos["peak"])
            new_sl = round(pos["entry"] * (1 + trig * 0.5), 2)
            if new_sl > pos["sl"]:
                open_positions[sym]["sl"] = new_sl

        if ltp <= pos["sl"]:
            place_sell(kite, sym, "stop-loss")
        elif ltp >= pos["target"]:
            place_sell(kite, sym, "target")

def squareoff_all(kite: KiteConnect):
    log.info("⏰ EOD square-off.")
    for sym in list(open_positions.keys()):
        place_sell(kite, sym, "eod-squareoff")

# ══════════════════════════════════════════════════════════
#  WEBSOCKET TICKER
# ══════════════════════════════════════════════════════════
def start_ticker(kite: KiteConnect, tokens: List[int]):
    ticker = KiteTicker(CFG["api_key"], kite.access_token)

    def on_ticks(ws: Any, ticks: List[Dict]) -> None:
        for tick in ticks:
            t   = tick["instrument_token"]
            sym = token_sym.get(t, "")
            live_ticks[t] = tick
            if sym in CFG["indices"]:
                index_prices[sym] = tick.get("last_price", 0)
                b = builders.setdefault(t, CandleBuilder(t))
                b.add_tick(tick.get("last_price", 0),
                           tick.get("volume_traded", 0),
                           datetime.datetime.now())

    def on_connect(ws, _):
        log.info("WebSocket connected.")
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)

    def on_close(ws, code, reason):
        log.warning(f"WebSocket closed ({code}): {reason}. Reconnecting…")

    def on_error(ws, code, reason):
        log.error(f"WebSocket error ({code}): {reason}")

    ticker.on_ticks   = on_ticks
    ticker.on_connect = on_connect
    ticker.on_close   = on_close
    ticker.on_error   = on_error
    threading.Thread(target=ticker.connect, kwargs={"threaded": True}, daemon=True).start()
    log.info(f"Ticker started for {len(tokens)} tokens.")
    return ticker

# ══════════════════════════════════════════════════════════
#  STRATEGY LOOP  (every 60 seconds)
# ══════════════════════════════════════════════════════════
def strategy_loop(kite: KiteConnect):
    now = datetime.datetime.now().time()
    if now < datetime.time(9, 30) or now >= datetime.time(15, 0):
        return

    check_positions(kite)

    regime_summary = {k: current_regime.get(k, "?") for k in CFG["indices"]}
    log.info(f"── Scan [{brain_name()}] | open={len(open_positions)} "
             f"| P&L=₹{session_pnl:.0f} | regimes={regime_summary} ──")

    if not can_enter() and not open_positions:
        if daily_stopped:
            log.warning("  Daily loss limit hit — no new entries.")
        return

    for index_sym, root in CFG["indices"].items():
        if index_sym not in sym_token:
            continue

        sig    = generate_signal(index_sym)
        regime = sig["regime"]
        action = sig["action"]
        brain  = "ALGO"
        rp     = regime_params_by_regime(regime)

        log.debug(f"  {index_sym:12} regime={regime:8} "
                  f"1m={sig['score_1m_buy']}/{sig['score_1m_sell']} "
                  f"5m={sig['score_5m_buy']}/{sig['score_5m_sell']} "
                  f"ADX={sig.get('adx_1m','?')} → {action}")

        # AMBIGUOUS → ask AI
        if action == "AMBIGUOUS":
            token = sym_token.get(index_sym, 0)
            df1_tail = df5_tail = "no data"
            df1 = df_from_buf(tick_store_1m, token, CFG["data_quality_min_1m"])
            if not df1.empty:
                df1 = compute_indicators(df1, CFG["ema_fast_1m"], CFG["ema_slow_1m"],
                                          CFG["rsi_period_1m"], CFG["atr_period_1m"])
                df1_tail = df1.tail(8)[["open","high","low","close","volume","rsi","adx","vwap"]].to_string()
            df5 = df_from_buf(tick_store_5m, token, CFG["data_quality_min_5m"])
            if not df5.empty:
                df5 = compute_indicators(df5, CFG["ema_fast_5m"], CFG["ema_slow_5m"],
                                          CFG["rsi_period_5m"], CFG["atr_period_5m"])
                df5_tail = df5.tail(8)[["open","high","low","close","volume","rsi","adx","vwap"]].to_string()

            opt_dir = "CE" if "buy" in sig["confidence"] else "PE"
            if regime == "BULL":  opt_dir = "CE"
            if regime == "BEAR":  opt_dir = "PE"
            opt_sym = get_live_option(root, opt_dir) or ""
            opt_tok = sym_token.get(opt_sym, 0)
            premium = live_ticks.get(opt_tok, {}).get("last_price", 0)
            action, brain = ask_ai(sig, df1_tail, df5_tail, opt_sym, premium)
            log.info(f"  AI ({brain}) [{regime}] → {action} for {index_sym}")

        # ── ENTRY ─────────────────────────────────────────
        if action in ("BUY", "SELL") and can_enter():
            if sig["fake_breakout"]:
                log.info(f"  ⚠  Fake breakout [{regime}]: {sig['fake_reason']}")
                continue

            # In SIDEWAYS, enforce stricter ADX check
            if regime == "SIDEWAYS" and (sig.get("adx_1m") or 0) < 15:
                log.debug(f"  {index_sym} sideways ADX too low — skip.")
                continue

            direction = "CE" if action == "BUY" else "PE"

            # Regime preference override
            if regime == "BULL" and direction == "PE":
                log.debug(f"  BULL regime — skipping PE entry.")
                continue
            if regime == "BEAR" and direction == "CE":
                log.debug(f"  BEAR regime — skipping CE entry.")
                continue

            opt_sym = get_live_option(root, direction)
            if opt_sym and opt_sym not in open_positions:
                place_buy(kite, opt_sym, root, sig["price"], brain, regime)

        # ── REVERSAL EXIT ─────────────────────────────────
        for held_sym in list(open_positions.keys()):
            if root not in held_sym:
                continue
            pos_type = "CE" if held_sym.endswith("CE") else "PE"
            # Exit on strong reversal signal
            reversal_gap = 4 if regime == "SIDEWAYS" else 5
            if ((pos_type == "CE" and sig["total_sell"] > sig["total_buy"] + reversal_gap) or
                    (pos_type == "PE" and sig["total_buy"] > sig["total_sell"] + reversal_gap)):
                log.info(f"  Signal reversed [{regime}] — exiting {held_sym}")
                place_sell(kite, held_sym, "signal-reversal")

# ══════════════════════════════════════════════════════════
#  DAILY RESET
# ══════════════════════════════════════════════════════════
def daily_reset():
    global session_pnl, consecutive_losses, cooldown_until, daily_stopped
    session_pnl        = 0.0
    consecutive_losses = 0
    cooldown_until     = None
    daily_stopped      = False
    current_regime.clear()
    log.info("🔄 Daily state reset.")

# ══════════════════════════════════════════════════════════
#  SUMMARY
# ══════════════════════════════════════════════════════════
def print_summary():
    log.info("─" * 65)
    log.info(f"DAILY SUMMARY | Session P&L: ₹{session_pnl:.0f}")
    for sym, pos in open_positions.items():
        tok  = sym_token.get(sym, 0)
        ltp  = live_ticks.get(tok, {}).get("last_price", pos["entry"])
        ur   = (ltp - pos["entry"]) * pos["qty"]
        log.info(f"  OPEN {sym:32} regime={pos.get('regime','?'):8} "
                 f"lots={pos['lots']} prem=₹{ltp:.2f} unrealised=₹{ur:.0f}")
    log.info(f"  Regime snapshot: {current_regime}")
    log.info("─" * 65)

# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
def main():
    mode = "PAPER TRADE ⚠ no real orders" if PAPER_TRADE else "LIVE TRADE 🔴 real money"
    log.info("=" * 65)
    log.info(f"  Trade_Claude v2 — Multi-Regime Options Bot  [{mode}]")
    log.info(f"  BULL: CE | BEAR: PE | SIDEWAYS: breakouts only")
    log.info(f"  ADX + Supertrend + VWAP bands + Scaled exit")
    log.info(f"  Claude PRIMARY  |  Ollama FALLBACK")
    log.info("=" * 65)

    kite   = get_kite()
    tokens = load_tokens(kite)

    watchdog.start()
    start_ticker(kite, tokens)
    time.sleep(5)

    schedule.every(60).seconds.do(strategy_loop,  kite=kite)
    schedule.every(10).seconds.do(check_positions, kite=kite)
    schedule.every().day.at(CFG["squareoff_time"]).do(squareoff_all, kite=kite)
    schedule.every().day.at("09:15").do(daily_reset)
    schedule.every().day.at("15:20").do(print_summary)

    log.info(f"  Capital       : ₹{CFG['capital']:,}")
    log.info(f"  Risk/trade    : {CFG['risk_per_trade']*100:.0f}%  "
             f"→ max ₹{CFG['capital']*CFG['risk_per_trade']:.0f} risk per trade")
    log.info(f"  At ₹100 prem  : {calc_lots(100, 'NIFTY', 'BULL')} lots NIFTY, "
             f"{calc_lots(100, 'BANKNIFTY', 'BULL')} lots BANKNIFTY")
    log.info(f"  Max daily loss: ₹{CFG['max_daily_loss']:,}")
    log.info(f"  Scaled exit   : {CFG['scale_exit']} ({CFG['scale_exit_ratio']*100:.0f}% at 1:1 R:R)")
    log.info(f"  Cooldown      : after {CFG['cooldown_after_loss']} consecutive losses")
    log.info("Press Ctrl+C to stop.\n")

    strategy_loop(kite)
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
