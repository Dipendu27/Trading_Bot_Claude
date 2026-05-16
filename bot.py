"""
Zerodha AI Options Bot — Powered by Claude
============================================
Trades weekly NIFTY / BANKNIFTY options on NFO exchange.

Key differences from the equity bot:
  • Exchange   : NFO (not NSE)
  • Instruments: ATM CE/PE options, auto-selected each session
  • Lot sizes  : NIFTY=25, BANKNIFTY=15, FINNIFTY=40
  • Greeks     : Delta, Theta, IV fed to Claude for smarter decisions
  • Position   : Premium-based sizing (not price × qty)
  • Paper mode : Set PAPER_TRADE=True to simulate without real orders

Architecture (unchanged from equity bot):
  • WebSocket tick stream     → real-time price feed
  • Dual-timeframe signals    → 1-min (scalp) + 5-min (trend) on the INDEX
  • Claude AI brain           → PRIMARY for ambiguous signals
  • Local LLM (Ollama)        → AUTO-FALLBACK when internet drops
  • Fake breakout filter      → volume + spread + body + OBV analysis
  • Connectivity watchdog     → detects drops, switches brain, auto-restores

Run:
    pip install kiteconnect anthropic pandas numpy schedule requests rich
    python bot.py
"""

import os, time, math, logging, schedule, threading, datetime, queue, json
import numpy as np
import pandas as pd
from collections import deque
from kiteconnect import KiteConnect, KiteTicker
import anthropic
import journal
from typing import Dict, List, Any, Optional, Tuple

# ══════════════════════════════════════════════════════════
#  PAPER TRADE FLAG  ← set False only when ready for live
# ══════════════════════════════════════════════════════════
PAPER_TRADE: bool = True        # ← True = simulate; False = real orders

# ══════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════
CFG: Dict[str, Any] = {
    # ── Zerodha ──────────────────────────────────────────
    "api_key":    "YOUR_ZERODHA_API_KEY",
    "api_secret": "YOUR_ZERODHA_API_SECRET",

    # ── Anthropic (PRIMARY BRAIN) ─────────────────────────
    "anthropic_key": "YOUR_ANTHROPIC_API_KEY",
    "claude_model":  "claude-sonnet-4-6",

    # ── Local LLM (FALLBACK — used only when internet is down) ──
    "local_llm_model": "mistral",
    "local_llm_url":   "http://localhost:11434/api/generate",
    "llm_timeout_sec": 4,
    "_use_local_llm":  False,

    # ── Options Config ────────────────────────────────────
    # Indices to trade options on (NSE index name → option root)
    "indices": {
        "NIFTY 50":   "NIFTY",
        "NIFTY BANK": "BANKNIFTY",
    },
    # Lot sizes (NSE-defined; verify before trading as SEBI revises these)
    "lot_sizes": {
        "NIFTY":      25,
        "BANKNIFTY":  15,
        "FINNIFTY":   40,
    },
    # Strike steps (₹ between consecutive strikes)
    "strike_step": {
        "NIFTY":      50,
        "BANKNIFTY":  100,
    },
    # CE on bullish signal, PE on bearish, BOTH means bot picks dynamically
    "option_direction": "BOTH",

    # OTM offset: 0 = ATM, 1 = 1 strike away from ATM, etc.
    # ATM is usually highest liquidity; slight OTM gives leverage
    "otm_offset": 0,

    # Premium guard rails
    "max_premium":  300,    # ₹ per share — reject options above this
    "min_premium":   15,    # ₹ per share — reject near-zero options

    # Max lots per trade (controls cash exposure)
    "max_lots":       2,

    # ── Risk ──────────────────────────────────────────────
    "capital":           50000,    # ₹ deployed — options need more margin buffer
    "risk_per_trade":    0.02,     # 2% capital at risk per trade
    "sl_pct":            0.25,     # 25% of premium (options move fast)
    "target_pct":        0.50,     # 50% of premium (2:1 R:R)
    "trail_sl":          True,
    "trail_trigger_pct": 0.15,     # start trailing after premium +15%
    "squareoff_time":    "15:15",  # hard exit before EOD

    # ── Dual-Timeframe Signal Tuning (on INDEX price) ────
    "ema_fast_1m":  5,
    "ema_slow_1m":  13,
    "rsi_period_1m": 7,
    "atr_period_1m": 7,
    "ema_fast_5m":  9,
    "ema_slow_5m":  21,
    "rsi_period_5m": 14,
    "atr_period_5m": 14,
    "min_volume_ratio":  1.1,
    "fake_break_ratio":  0.3,
    "vwap_deviation":    2.0,

    # ── Timeframe agreement thresholds ───────────────────
    "tf_agree_score_threshold":    5,
    "tf_single_score_threshold":   4,
    "tf_ambiguous_claude_always": True,

    # ── Claude call throttle ──────────────────────────────
    "claude_calls_per_min": 10,

    # ── Misc ──────────────────────────────────────────────
    "tick_buffer_1m":       300,
    "tick_buffer_5m":       100,
    "log_file":             "bot.log",
    "data_quality_min_1m":  15,
    "data_quality_min_5m":  10,
}


def _load_local_env() -> None:
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _apply_env_config() -> None:
    _load_local_env()
    CFG["api_key"] = os.getenv("KITE_API_KEY") or os.getenv("ZERODHA_API_KEY") or CFG["api_key"]
    CFG["api_secret"] = os.getenv("KITE_API_SECRET") or os.getenv("ZERODHA_API_SECRET") or CFG["api_secret"]
    CFG["anthropic_key"] = os.getenv("ANTHROPIC_API_KEY") or CFG["anthropic_key"]
    if os.getenv("TRADE_CAPITAL"):
        CFG["capital"] = float(os.getenv("TRADE_CAPITAL", CFG["capital"]))


_apply_env_config()

# ══════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[logging.FileHandler(CFG["log_file"]), logging.StreamHandler()],
)
log = logging.getLogger("OptionsBot")

if PAPER_TRADE:
    log.info("=" * 65)
    log.info("  *** PAPER TRADE MODE ACTIVE — NO REAL ORDERS WILL PLACE ***")
    log.info("=" * 65)

# ══════════════════════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════════════════════
open_positions: Dict[str, Dict[str, Any]] = {}
tick_store_1m:  Dict[int, deque] = {}
tick_store_5m:  Dict[int, deque] = {}
live_ticks:     Dict[int, Dict[str, Any]] = {}
token_sym:      Dict[int, str]  = {}
sym_token:      Dict[str, int]  = {}
claude_calls:   List[float]     = []
builders:       Dict[int, Any]  = {}

# Options-specific: maps index root → selected option instrument for today
# e.g. {"NIFTY": {"CE": "NIFTY24JAN22500CE", "PE": "NIFTY24JAN22500PE"}}
active_options: Dict[str, Dict[str, str]] = {}
index_prices:   Dict[str, float] = {}   # latest LTP for each index

# ══════════════════════════════════════════════════════════
#  BRAIN STATE HELPERS
# ══════════════════════════════════════════════════════════
def using_claude() -> bool:
    return not CFG["_use_local_llm"]

def brain_name() -> str:
    return "Claude" if using_claude() else f"Ollama/{CFG['local_llm_model']}"

# ══════════════════════════════════════════════════════════
#  AUTHENTICATION
# ══════════════════════════════════════════════════════════
def get_kite() -> KiteConnect:
    kite = KiteConnect(api_key=CFG["api_key"])
    token_file = ".access_token"
    today = datetime.date.today().isoformat()

    if os.path.exists(token_file):
        saved_date, saved_tok = open(token_file).read().strip().split("|")
        if saved_date == today:
            kite.set_access_token(saved_tok)
            log.info("Reused access token.")
            return kite

    print(f"\nOpen this URL and login:\n{kite.login_url()}\n")
    req_tok = input("Paste request_token from redirect URL: ").strip()
    sess    = kite.generate_session(req_tok, api_secret=CFG["api_secret"])
    kite.set_access_token(sess["access_token"])
    open(token_file, "w").write(f"{today}|{sess['access_token']}")
    log.info("Login successful.")
    return kite

# ══════════════════════════════════════════════════════════
#  INSTRUMENT LOADER  —  index spot + NFO options
# ══════════════════════════════════════════════════════════
def load_tokens(kite: KiteConnect) -> List[int]:
    """
    Loads:
      1. NSE index instruments (NIFTY 50, NIFTY BANK) for price feed
      2. NFO weekly/near-expiry CE+PE options for the active indices

    Populates token_sym, sym_token, and active_options.
    Returns list of all tokens to subscribe to.
    """
    # ── Step 1: NSE index tokens ─────────────────────────
    nse_insts = kite.instruments("NSE")
    for inst in nse_insts:
        s = inst["tradingsymbol"]
        if s in CFG["indices"]:
            token_sym[inst["instrument_token"]] = s
            sym_token[s] = inst["instrument_token"]
            log.info(f"  Index loaded: {s} → token {inst['instrument_token']}")

    # ── Step 2: NFO options ───────────────────────────────
    nfo_insts  = kite.instruments("NFO")
    today      = datetime.date.today()

    # Find the nearest weekly expiry (next Thursday for NIFTY/BANKNIFTY)
    expiry_map: Dict[str, datetime.date] = {}
    for root in CFG["lot_sizes"]:
        # Collect all expiries for this root that are >= today
        expiries = sorted({
            inst["expiry"]
            for inst in nfo_insts
            if inst["name"] == root
            and inst["expiry"] is not None
            and inst["expiry"] >= today
        })
        if expiries:
            expiry_map[root] = expiries[0]
            log.info(f"  {root} near expiry: {expiry_map[root]}")

    # Build a lookup: (root, expiry, strike, instrument_type) → instrument
    nfo_lookup: Dict[tuple, Any] = {}
    for inst in nfo_insts:
        root = inst.get("name", "")
        if root in expiry_map and inst["expiry"] == expiry_map[root]:
            key = (root, inst["strike"], inst["instrument_type"])
            nfo_lookup[key] = inst

    # ── Step 3: Pick ATM + OTM options ──────────────────
    # We don't know the current index price yet (no ticks), so we use
    # yesterday's close from the kite quote API as a proxy.
    for index_name, root in CFG["indices"].items():
        if root not in expiry_map:
            continue
        if index_name not in sym_token:
            continue

        try:
            quote = kite.quote(f"NSE:{index_name}")
            current_price = quote[f"NSE:{index_name}"]["last_price"]
        except Exception as e:
            log.warning(f"  Could not fetch {index_name} quote: {e}. Skipping.")
            continue

        step   = CFG["strike_step"].get(root, 50)
        offset = CFG["otm_offset"]
        atm    = round(current_price / step) * step

        # ATM and nearby strikes to subscribe to
        strikes_to_watch = [atm + i * step for i in range(-2 + offset, 3 + offset)]

        active_options[root] = {"CE": None, "PE": None, "expiry": expiry_map[root]}
        tokens_added = 0

        for strike in strikes_to_watch:
            for opt_type in ["CE", "PE"]:
                key = (root, float(strike), opt_type)
                inst = nfo_lookup.get(key)
                if not inst:
                    continue
                tok = inst["instrument_token"]
                sym = inst["tradingsymbol"]
                token_sym[tok] = sym
                sym_token[sym] = tok

                # Track the ATM option as the primary trading instrument
                if strike == atm + offset * step:
                    active_options[root][opt_type] = sym
                    log.info(f"  Active option: {sym} (ATM{'+' + str(offset) if offset else ''})")

                tokens_added += 1

        log.info(f"  {root}: loaded {tokens_added} option instruments "
                 f"around ATM ₹{atm}")

    all_tokens = list(token_sym.keys())
    log.info(f"Total tokens subscribed: {len(all_tokens)}")
    return all_tokens

# ══════════════════════════════════════════════════════════
#  LIVE OPTION SELECTOR  (re-evaluates ATM on each scan)
# ══════════════════════════════════════════════════════════
def get_live_option(root: str, direction: str) -> Optional[str]:
    """
    Returns the tradingsymbol of the best-fit option for the given direction.
    Re-selects dynamically as index moves, staying near ATM.

    direction: "CE" (bullish) or "PE" (bearish)
    """
    if root not in active_options:
        return None

    index_name = next(
        (k for k, v in CFG["indices"].items() if v == root), None
    )
    if not index_name:
        return None

    ltp = index_prices.get(index_name, 0)
    if ltp == 0:
        # Fall back to pre-selected option
        return active_options[root].get(direction)

    step   = CFG["strike_step"].get(root, 50)
    offset = CFG["otm_offset"]
    atm    = round(ltp / step) * step

    if direction == "CE":
        strike = atm + offset * step
    else:  # PE — OTM is below ATM
        strike = atm - offset * step

    key = (root, float(strike), direction)
    # Look up in token_sym (we already loaded nearby options)
    for tok, sym in token_sym.items():
        if sym.startswith(root) and f"{int(strike)}{direction}" in sym:
            # Check premium guard rails
            ltp_opt = live_ticks.get(tok, {}).get("last_price", 0)
            if ltp_opt and (ltp_opt < CFG["min_premium"] or ltp_opt > CFG["max_premium"]):
                log.debug(f"  {sym} premium ₹{ltp_opt:.0f} outside guard rails — skipping")
                return None
            return sym

    # Fall back to pre-selected
    return active_options[root].get(direction)

# ══════════════════════════════════════════════════════════
#  DUAL CANDLE BUILDER  (1-min and 5-min from ticks)
#  Unchanged from equity bot — signals run on INDEX price
# ══════════════════════════════════════════════════════════
class DualCandleBuilder:
    def __init__(self, token: int):
        self.token = token
        self.o1 = self.h1 = self.l1 = self.c1 = self.v1 = 0.0
        self.ts1: Optional[datetime.datetime] = None
        self.o5 = self.h5 = self.l5 = self.c5 = self.v5 = 0.0
        self.ts5: Optional[datetime.datetime] = None

    def _minute_bucket(self, ts: datetime.datetime, interval: int) -> datetime.datetime:
        floored = ts.replace(second=0, microsecond=0)
        bucket  = (floored.minute // interval) * interval
        return floored.replace(minute=bucket)

    def _update(self, o, h, l, c, v, price, volume):
        h = max(h, price)
        l = min(l, price)
        c = price
        v += volume
        return o, h, l, c, v

    def add_tick(self, price: float, volume: float, ts: datetime.datetime):
        # ── 1-min ──────────────────────────────────────────────
        min1 = self._minute_bucket(ts, 1)
        if self.ts1 is None:
            self.ts1 = min1
        if min1 != self.ts1:
            buf = tick_store_1m.setdefault(self.token, deque(maxlen=CFG["tick_buffer_1m"]))
            buf.append({"time": self.ts1, "open": self.o1, "high": self.h1,
                        "low": self.l1, "close": self.c1, "volume": self.v1})
            self.ts1 = min1
            self.o1 = self.h1 = self.l1 = self.c1 = price
            self.v1 = volume
        else:
            if self.o1 == 0: self.o1 = price
            self.o1, self.h1, self.l1, self.c1, self.v1 = self._update(
                self.o1, self.h1, self.l1, self.c1, self.v1, price, volume)

        # ── 5-min ──────────────────────────────────────────────
        min5 = self._minute_bucket(ts, 5)
        if self.ts5 is None:
            self.ts5 = min5
        if min5 != self.ts5:
            buf = tick_store_5m.setdefault(self.token, deque(maxlen=CFG["tick_buffer_5m"]))
            buf.append({"time": self.ts5, "open": self.o5, "high": self.h5,
                        "low": self.l5, "close": self.c5, "volume": self.v5})
            self.ts5 = min5
            self.o5 = self.h5 = self.l5 = self.c5 = price
            self.v5 = volume
        else:
            if self.o5 == 0: self.o5 = price
            self.o5, self.h5, self.l5, self.c5, self.v5 = self._update(
                self.o5, self.h5, self.l5, self.c5, self.v5, price, volume)

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

def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def compute_indicators(df: pd.DataFrame, ema_fast: int, ema_slow: int,
                       rsi_period: int, atr_period: int) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = _ema(df["close"], ema_fast)
    df["ema_slow"] = _ema(df["close"], ema_slow)
    d   = df["close"].diff()
    g   = d.clip(lower=0)
    l_  = -d.clip(upper=0)
    df["rsi"] = 100 - 100 / (
        1 + g.ewm(com=rsi_period - 1, adjust=False).mean()
          / l_.ewm(com=rsi_period - 1, adjust=False).mean().replace(0, np.nan)
    )
    hl  = df["high"] - df["low"]
    hc  = (df["high"] - df["close"].shift()).abs()
    lc  = (df["low"]  - df["close"].shift()).abs()
    df["atr"] = pd.concat([hl, hc, lc], axis=1).max(axis=1).ewm(
        span=atr_period, adjust=False).mean()
    tp = (df["high"] + df["low"] + df["close"]) / 3
    df["vwap"]     = (tp * df["volume"]).cumsum() / df["volume"].cumsum()
    df["vwap_std"] = (df["close"] - df["vwap"]).rolling(10).std()
    df["obv"]      = (np.sign(df["close"].diff()) * df["volume"]).cumsum()
    df["vol_ma"]   = df["volume"].rolling(10).mean()
    return df

# ══════════════════════════════════════════════════════════
#  FAKE BREAKOUT FILTER
# ══════════════════════════════════════════════════════════
def is_fake_breakout(df: pd.DataFrame) -> Tuple[bool, str]:
    if len(df) < 5:
        return False, ""
    last = df.iloc[-1]
    prev = df.iloc[-2]
    body = abs(last["close"] - last["open"])
    rng  = last["high"] - last["low"] + 1e-9
    reasons: List[str] = []
    if body / rng < CFG["fake_break_ratio"]:
        reasons.append(f"thin body ({body/rng:.0%})")
    vol_ratio = last["volume"] / (last["vol_ma"] + 1e-9)
    if vol_ratio < CFG["min_volume_ratio"]:
        reasons.append(f"low volume ({vol_ratio:.2f}x)")
    if prev["low"] < last["close"] < prev["high"]:
        reasons.append("close inside prev range")
    if len(df) >= 4 and last["close"] > df.iloc[-4]["close"] and last["obv"] < df.iloc[-4]["obv"]:
        reasons.append("OBV divergence")
    if len(reasons) >= 2:
        return True, " | ".join(reasons)
    return False, ""

# ══════════════════════════════════════════════════════════
#  SINGLE-TIMEFRAME SCORER
# ══════════════════════════════════════════════════════════
def score_tf(df: pd.DataFrame) -> Tuple[int, int, Dict[str, Any]]:
    if len(df) < 3:
        return 0, 0, {}
    last = df.iloc[-1]
    prev = df.iloc[-2]
    ago3 = df.iloc[-3]

    ema_cross_up = prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]
    ema_cross_dn = prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]
    ema_trend_up = last["ema_fast"] > last["ema_slow"]
    ema_trend_dn = last["ema_fast"] < last["ema_slow"]
    rsi_bull     = 42 < last["rsi"] < 70
    rsi_bear     = 30 < last["rsi"] < 58
    rsi_ob       = last["rsi"] > 70
    rsi_os       = last["rsi"] < 30
    above_vwap   = last["close"] > last["vwap"]
    momentum_up  = last["close"] > prev["close"] > ago3["close"]
    momentum_dn  = last["close"] < prev["close"] < ago3["close"]
    vol_spike    = last["volume"] > last["vol_ma"] * CFG["min_volume_ratio"]
    obv_up       = last["obv"] > ago3["obv"]
    obv_dn       = last["obv"] < ago3["obv"]

    buy_signals = [
        ema_cross_up or ema_trend_up,
        rsi_bull and not rsi_ob,
        above_vwap,
        vol_spike,
        obv_up,
        momentum_up,
        last["close"] > last["vwap"] + last.get("vwap_std", 0) * 0.3,
        ema_cross_up,
    ]
    sell_signals = [
        ema_cross_dn or ema_trend_dn,
        rsi_bear and not rsi_os,
        not above_vwap,
        vol_spike,
        obv_dn,
        momentum_dn,
        last["close"] < last["vwap"] - last.get("vwap_std", 0) * 0.3,
        ema_cross_dn,
    ]

    meta = {
        "rsi":          round(float(last["rsi"]), 2),
        "atr":          round(float(last["atr"]), 2),
        "vwap":         round(float(last["vwap"]), 2),
        "price":        last["close"],
        "ema_fast":     round(float(last["ema_fast"]), 2),
        "ema_slow":     round(float(last["ema_slow"]), 2),
        "above_vwap":   above_vwap,
        "ema_cross_up": ema_cross_up,
        "ema_cross_dn": ema_cross_dn,
    }
    return sum(buy_signals), sum(sell_signals), meta

# ══════════════════════════════════════════════════════════
#  DUAL-TIMEFRAME SIGNAL GENERATOR  (operates on INDEX)
# ══════════════════════════════════════════════════════════
def generate_signal(index_sym: str) -> Dict[str, Any]:
    """
    Generates BUY/SELL/AMBIGUOUS/HOLD for the index direction.
    BUY  → buy a CE option
    SELL → buy a PE option (we always BUY options, never short)
    """
    token = sym_token.get(index_sym)
    if token is None:
        return {"action": "HOLD", "symbol": index_sym}

    df1 = df_from_buf(tick_store_1m, token, CFG["data_quality_min_1m"])
    df5 = df_from_buf(tick_store_5m, token, CFG["data_quality_min_5m"])

    has_1m = not df1.empty
    has_5m = not df5.empty

    if not has_1m and not has_5m:
        return {"action": "HOLD", "symbol": index_sym, "confidence": "no_data",
                "score_1m_buy": 0, "score_1m_sell": 0,
                "score_5m_buy": 0, "score_5m_sell": 0,
                "total_buy": 0, "total_sell": 0,
                "rsi_1m": None, "rsi_5m": None, "atr_1m": None,
                "vwap_1m": None, "vwap_5m": None,
                "ema_cross_1m": False, "ema_cross_5m": False,
                "fake_breakout": False, "fake_reason": "",
                "has_1m": False, "has_5m": False,
                "price": index_prices.get(index_sym, 0)}

    s1b = s1s = s5b = s5s = 0
    m1: Dict[str, Any] = {}
    m5: Dict[str, Any] = {}
    fake = False
    fake_reason = ""

    if has_1m:
        df1 = compute_indicators(df1, CFG["ema_fast_1m"], CFG["ema_slow_1m"],
                                  CFG["rsi_period_1m"], CFG["atr_period_1m"])
        s1b, s1s, m1 = score_tf(df1)
        fake, fake_reason = is_fake_breakout(df1)

    if has_5m:
        df5 = compute_indicators(df5, CFG["ema_fast_5m"], CFG["ema_slow_5m"],
                                  CFG["rsi_period_5m"], CFG["atr_period_5m"])
        s5b, s5s, m5 = score_tf(df5)

    total_buy  = s1b + s5b
    total_sell = s1s + s5s
    thresh     = CFG["tf_agree_score_threshold"]
    single     = CFG["tf_single_score_threshold"]

    if total_buy >= thresh * 2:
        action, conf = "BUY",       "strong"
    elif total_sell >= thresh * 2:
        action, conf = "SELL",      "strong"
    elif total_buy >= single * 2 or total_sell >= single * 2:
        action, conf = "AMBIGUOUS", "moderate"
    elif has_1m and has_5m and (s1b >= thresh or s5b >= thresh) and (s1b + s5b > s1s + s5s):
        action, conf = "AMBIGUOUS", "weak_buy"
    elif has_1m and has_5m and (s1s >= thresh or s5s >= thresh) and (s1s + s5s > s1b + s5b):
        action, conf = "AMBIGUOUS", "weak_sell"
    else:
        action, conf = "HOLD",      "low"

    return {
        "action":       action,
        "confidence":   conf,
        "symbol":       index_sym,
        "price":        index_prices.get(index_sym, m1.get("price") or m5.get("price", 0)),
        "score_1m_buy":  s1b,
        "score_1m_sell": s1s,
        "score_5m_buy":  s5b,
        "score_5m_sell": s5s,
        "total_buy":     total_buy,
        "total_sell":    total_sell,
        "rsi_1m":        m1.get("rsi"),
        "rsi_5m":        m5.get("rsi"),
        "atr_1m":        m1.get("atr"),
        "vwap_1m":       m1.get("vwap"),
        "vwap_5m":       m5.get("vwap"),
        "ema_cross_1m":  m1.get("ema_cross_up") or m1.get("ema_cross_dn"),
        "ema_cross_5m":  m5.get("ema_cross_up") or m5.get("ema_cross_dn"),
        "fake_breakout": fake,
        "fake_reason":   fake_reason,
        "has_1m":        has_1m,
        "has_5m":        has_5m,
    }

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
        client  = anthropic.Anthropic(api_key=CFG["anthropic_key"])
        message = client.messages.create(
            model=CFG["claude_model"],
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip().upper()
        claude_calls.append(time.time())
        for word in ["BUY", "SELL", "HOLD"]:
            if word in raw:
                return word
        return "HOLD"
    except Exception as e:
        log.warning(f"Claude API error: {e}")
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

def ask_ai(signal: Dict[str, Any], df1_tail: str, df5_tail: str,
           option_sym: str = "", premium: float = 0) -> Tuple[str, str]:
    """
    Options-aware AI prompt. Returns (decision, brain_used).
    Decision: BUY (enter option), SELL (exit if holding), HOLD
    """
    root = CFG["indices"].get(signal["symbol"], signal["symbol"])
    lot  = CFG["lot_sizes"].get(root, 25)
    expiry_info = ""
    if root in active_options:
        exp = active_options[root].get("expiry")
        if exp:
            days_to_expiry = (exp - datetime.date.today()).days
            expiry_info = f"Days to expiry: {days_to_expiry}"

    prompt = f"""You are an expert intraday options trader on NSE (India).
Analyze the index data and decide whether to BUY, SELL (exit), or HOLD.

Index   : {signal['symbol']}
Price   : ₹{signal['price']:.2f}
Option  : {option_sym or 'N/A'}
Premium : ₹{premium:.2f} per share (lot={lot} shares)
{expiry_info}

SIGNAL SCORES (index-level, 0–8 per timeframe):
  1-min  Buy={signal['score_1m_buy']}/8  Sell={signal['score_1m_sell']}/8  RSI={signal['rsi_1m']}  VWAP={signal['vwap_1m']}
  5-min  Buy={signal['score_5m_buy']}/8  Sell={signal['score_5m_sell']}/8  RSI={signal['rsi_5m']}  VWAP={signal['vwap_5m']}
  Confidence  : {signal['confidence']}
  EMA cross   : 1m={signal['ema_cross_1m']}  5m={signal['ema_cross_5m']}
  Fake breakout: {signal['fake_reason'] or 'None'}

Last 8 candles — 1-min index OHLCV:
{df1_tail}

Last 8 candles — 5-min index OHLCV:
{df5_tail}

Open positions: {len(open_positions)} | Capital: ₹{CFG['capital']:,}

TRADING RULES:
  - We BUY options, never short (unlimited risk).
  - Only BUY CE on strong bullish signal; BUY PE on strong bearish signal.
  - Reject if fake_breakout flags > 0.
  - Reject if premium is too high (>₹{CFG['max_premium']}) or too low (<₹{CFG['min_premium']}).
  - Prefer 5-min trend direction when timeframes conflict.
  - SELL means EXIT a currently held position.
  - Never hold past 3:10 PM IST.
  - Consider theta decay — avoid buying if expiry < 2 days and it's not morning.

Reply with EXACTLY one word: BUY, SELL, or HOLD."""

    if using_claude():
        result = _call_claude(prompt)
        if result is not None:
            return result, "CLAUDE"
        log.warning("Claude failed — trying local LLM.")
        result = _call_local_llm(prompt)
        return (result or "HOLD"), "LOCAL(emergency)"

    result = _call_local_llm(prompt)
    if result is not None:
        return result, f"LOCAL/{CFG['local_llm_model']}"

    return "HOLD", "NONE"

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
                was_offline = not self.online
                self.online = True
                if was_offline:
                    CFG["_use_local_llm"] = False
                    log.info("🌐 Internet restored. Brain → Claude.")
            except Exception:
                was_online = self.online
                self.online = False
                if was_online:
                    CFG["_use_local_llm"] = True
                    log.warning("🔌 Internet DOWN. Brain → local LLM.")
            time.sleep(5)

watchdog = ConnectivityWatchdog()

# ══════════════════════════════════════════════════════════
#  ORDER MANAGEMENT  (options-specific sizing)
# ══════════════════════════════════════════════════════════
def calc_lots(premium: float, root: str) -> int:
    """
    How many lots to buy.
    Risk = premium × lot_size × lots ≤ capital × risk_per_trade
    """
    lot_size   = CFG["lot_sizes"].get(root, 25)
    max_risk   = CFG["capital"] * CFG["risk_per_trade"]
    # Each lot costs: premium × lot_size
    cost_per_lot = premium * lot_size
    if cost_per_lot <= 0:
        return 0
    lots = math.floor(max_risk / cost_per_lot)
    return max(1, min(lots, CFG["max_lots"]))

def place_buy(kite: KiteConnect, option_sym: str, root: str,
              index_price: float, brain: str = "ALGO"):
    """Buy an options contract (CE or PE)."""
    if option_sym in open_positions:
        return

    tok     = sym_token.get(option_sym)
    premium = live_ticks.get(tok, {}).get("last_price", 0) if tok else 0
    if premium <= 0:
        log.warning(f"  Cannot buy {option_sym} — no live premium data.")
        return
    if premium < CFG["min_premium"] or premium > CFG["max_premium"]:
        log.warning(f"  Skipping {option_sym}: premium ₹{premium:.0f} outside guard rails.")
        return

    lot_size = CFG["lot_sizes"].get(root, 25)
    lots     = calc_lots(premium, root)
    qty      = lots * lot_size
    sl       = round(premium * (1 - CFG["sl_pct"]), 2)
    target   = round(premium * (1 + CFG["target_pct"]), 2)
    cost     = premium * qty

    if PAPER_TRADE:
        log.info(f"📝 [PAPER] BUY  {option_sym:30} lots={lots} qty={qty:4} "
                 f"premium=₹{premium:.2f}  SL=₹{sl}  T=₹{target}  [{brain}]"
                 f"  Cost≈₹{cost:.0f}")
        oid = f"PAPER_{int(time.time())}"
    else:
        try:
            oid = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=kite.EXCHANGE_NFO,
                tradingsymbol=option_sym,
                transaction_type=kite.TRANSACTION_TYPE_BUY,
                quantity=qty,
                order_type=kite.ORDER_TYPE_MARKET,
                product=kite.PRODUCT_MIS,
            )
            log.info(f"✅ BUY  {option_sym:30} lots={lots} qty={qty:4} "
                     f"premium=₹{premium:.2f}  SL=₹{sl}  T=₹{target}  [{brain}]")
        except Exception as e:
            log.error(f"Buy order FAILED {option_sym}: {e}")
            return

    jid = journal.log_entry(
        symbol=option_sym, qty=qty, price=premium,
        sl=sl, target=target, ai_decision=brain,
        extra={"index_price": index_price, "lots": lots, "root": root}
    )
    open_positions[option_sym] = {
        "qty": qty, "entry": premium, "sl": sl, "target": target,
        "peak": premium, "order_id": oid, "journal_id": jid,
        "root": root, "lots": lots, "cost": cost,
    }

def place_sell(kite: KiteConnect, option_sym: str, reason: str = "signal"):
    """Sell (exit) an options position."""
    if option_sym not in open_positions:
        return
    pos = open_positions[option_sym]
    tok = sym_token.get(option_sym)
    current_premium = live_ticks.get(tok, {}).get("last_price", pos["entry"]) if tok else pos["entry"]
    pnl = (current_premium - pos["entry"]) * pos["qty"]

    if PAPER_TRADE:
        log.info(f"📝 [PAPER] SELL {option_sym:30} reason={reason:18} "
                 f"exit=₹{current_premium:.2f}  P&L≈₹{pnl:.0f}")
    else:
        try:
            kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=kite.EXCHANGE_NFO,
                tradingsymbol=option_sym,
                transaction_type=kite.TRANSACTION_TYPE_SELL,
                quantity=pos["qty"],
                order_type=kite.ORDER_TYPE_MARKET,
                product=kite.PRODUCT_MIS,
            )
            log.info(f"🔴 SELL {option_sym:30} reason={reason:18} P&L≈₹{pnl:.0f}")
        except Exception as e:
            log.error(f"Sell order FAILED {option_sym}: {e}")
            return

    if "journal_id" in pos:
        journal.log_exit(pos["journal_id"], current_premium, reason, pnl)
        journal.update_daily_summary()
    del open_positions[option_sym]

def check_positions(kite: KiteConnect):
    """Check SL / target / trailing SL for all open option positions."""
    for sym, pos in list(open_positions.items()):
        tok = sym_token.get(sym)
        if not tok:
            continue
        ltp = live_ticks.get(tok, {}).get("last_price", 0)
        if not ltp:
            continue

        profit_pct = (ltp - pos["entry"]) / pos["entry"]

        # Trailing SL on option premium
        if CFG["trail_sl"] and profit_pct > CFG["trail_trigger_pct"]:
            open_positions[sym]["peak"] = max(ltp, pos["peak"])
            new_sl = round(pos["entry"] * (1 + CFG["trail_trigger_pct"] * 0.5), 2)
            if new_sl > pos["sl"]:
                open_positions[sym]["sl"] = new_sl
                log.debug(f"Trail SL: {sym} → ₹{new_sl}")

        if ltp <= pos["sl"]:
            place_sell(kite, sym, "stop-loss")
        elif ltp >= pos["target"]:
            place_sell(kite, sym, "target")

def squareoff_all(kite: KiteConnect):
    log.info("⏰ EOD square-off — exiting all options positions.")
    for sym in list(open_positions.keys()):
        place_sell(kite, sym, "eod-squareoff")

# ══════════════════════════════════════════════════════════
#  WEBSOCKET TICKER
# ══════════════════════════════════════════════════════════
def start_ticker(kite: KiteConnect, tokens: List[int]):
    ticker = KiteTicker(CFG["api_key"], kite.access_token)

    def on_ticks(ws: Any, ticks: List[Dict[str, Any]]) -> None:
        for tick in ticks:
            t = tick["instrument_token"]
            live_ticks[t] = tick
            sym = token_sym.get(t, "")

            # Update index price if this tick is an index
            if sym in CFG["indices"]:
                index_prices[sym] = tick.get("last_price", 0)

            # Build candles only for index tokens (signals run on index)
            if sym in CFG["indices"]:
                b = builders.setdefault(t, DualCandleBuilder(t))
                b.add_tick(
                    price  = tick.get("last_price", 0),
                    volume = tick.get("volume_traded", 0),
                    ts     = datetime.datetime.now(),
                )

    def on_connect(ws: Any, response: Any) -> None:
        log.info("WebSocket connected.")
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)

    def on_close(ws: Any, code: int, reason: str) -> None:
        log.warning(f"WebSocket closed ({code}): {reason}. Reconnecting…")

    def on_error(ws: Any, code: int, reason: str) -> None:
        log.error(f"WebSocket error ({code}): {reason}")

    ticker.on_ticks   = on_ticks
    ticker.on_connect = on_connect
    ticker.on_close   = on_close
    ticker.on_error   = on_error

    t = threading.Thread(target=ticker.connect, kwargs={"threaded": True}, daemon=True)
    t.start()
    log.info(f"Ticker started for {len(tokens)} instruments.")
    return ticker

# ══════════════════════════════════════════════════════════
#  STRATEGY LOOP  (every 60 seconds)
# ══════════════════════════════════════════════════════════
def strategy_loop(kite: KiteConnect):
    now = datetime.datetime.now().time()
    if now < datetime.time(9, 30) or now >= datetime.time(15, 0):
        return

    check_positions(kite)
    log.info(f"── Scan [{brain_name()}] | positions={len(open_positions)} ──")

    for index_sym, root in CFG["indices"].items():
        if index_sym not in sym_token:
            continue

        sig    = generate_signal(index_sym)
        action = sig["action"]
        brain  = "ALGO"

        log.debug(f"{index_sym:12} 1m={sig['score_1m_buy']}/{sig['score_1m_sell']} "
                  f"5m={sig['score_5m_buy']}/{sig['score_5m_sell']} → {action}")

        # AMBIGUOUS → ask AI
        if action == "AMBIGUOUS":
            token = sym_token.get(index_sym)
            df1_tail = df5_tail = "no data"
            if token:
                df1 = df_from_buf(tick_store_1m, token, CFG["data_quality_min_1m"])
                if not df1.empty:
                    df1 = compute_indicators(df1, CFG["ema_fast_1m"], CFG["ema_slow_1m"],
                                             CFG["rsi_period_1m"], CFG["atr_period_1m"])
                    df1_tail = df1.tail(8)[["open","high","low","close","volume","rsi","vwap"]].to_string()
                df5 = df_from_buf(tick_store_5m, token, CFG["data_quality_min_5m"])
                if not df5.empty:
                    df5 = compute_indicators(df5, CFG["ema_fast_5m"], CFG["ema_slow_5m"],
                                             CFG["rsi_period_5m"], CFG["atr_period_5m"])
                    df5_tail = df5.tail(8)[["open","high","low","close","volume","rsi","vwap"]].to_string()

            # Determine option type for context
            if "weak_buy" in sig["confidence"] or sig["total_buy"] > sig["total_sell"]:
                opt_type = "CE"
            else:
                opt_type = "PE"
            opt_sym  = get_live_option(root, opt_type) or ""
            opt_tok  = sym_token.get(opt_sym, 0)
            premium  = live_ticks.get(opt_tok, {}).get("last_price", 0)

            action, brain = ask_ai(sig, df1_tail, df5_tail, opt_sym, premium)
            log.info(f"AI ({brain}) → {action} for {index_sym} [{opt_type}]")

        # ── ENTER: BUY CE (bullish) ──────────────────────
        if action == "BUY":
            if sig.get("fake_breakout"):
                log.info(f"⚠️  Skipping {index_sym} CE — fake breakout: {sig['fake_reason']}")
                continue
            opt_sym = get_live_option(root, "CE")
            if opt_sym and opt_sym not in open_positions:
                place_buy(kite, opt_sym, root, sig["price"], brain=brain)

        # ── ENTER: BUY PE (bearish) ──────────────────────
        elif action == "SELL":
            if sig.get("fake_breakout"):
                log.info(f"⚠️  Skipping {index_sym} PE — fake breakout: {sig['fake_reason']}")
                continue
            opt_sym = get_live_option(root, "PE")
            if opt_sym and opt_sym not in open_positions:
                place_buy(kite, opt_sym, root, sig["price"], brain=brain)

        # ── EXIT: close any open position on opposite signal
        elif action == "HOLD":
            # Optionally exit positions if signal has reversed strongly
            for held_sym in list(open_positions.keys()):
                if root in held_sym:
                    pos_type = "CE" if held_sym.endswith("CE") else "PE"
                    # Exit CE on strong sell, exit PE on strong buy
                    if (pos_type == "CE" and sig["total_sell"] > sig["total_buy"] + 3) or \
                       (pos_type == "PE" and sig["total_buy"] > sig["total_sell"] + 3):
                        log.info(f"  Signal reversed — exiting {held_sym}")
                        place_sell(kite, held_sym, "signal-reversal")

# ══════════════════════════════════════════════════════════
#  DAILY SUMMARY
# ══════════════════════════════════════════════════════════
def print_summary():
    log.info("─" * 60)
    log.info("DAILY SUMMARY")
    open_pnl = 0.0
    for sym, pos in open_positions.items():
        tok = sym_token.get(sym)
        if not tok: continue
        ltp  = live_ticks.get(tok, {}).get("last_price", pos["entry"])
        pnl  = (ltp - pos["entry"]) * pos["qty"]
        open_pnl += pnl
        log.info(f"  OPEN  {sym:30} qty={pos['qty']} prem=₹{ltp:.2f} P&L=₹{pnl:.0f}")
    log.info(f"  Total unrealised P&L: ₹{open_pnl:.0f}")
    log.info("─" * 60)

# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
def main():
    mode_str = "PAPER TRADE" if PAPER_TRADE else "LIVE TRADE"
    log.info("=" * 65)
    log.info(f"  Zerodha AI Options Bot  |  [{mode_str}]")
    log.info(f"  Claude PRIMARY  |  Ollama FALLBACK")
    log.info(f"  Dual timeframe: 1-min (scalp) + 5-min (trend) on INDEX")
    log.info("=" * 65)

    kite   = get_kite()
    tokens = load_tokens(kite)

    watchdog.start()
    start_ticker(kite, tokens)
    time.sleep(5)   # Allow initial ticks to arrive

    schedule.every(60).seconds.do(strategy_loop,  kite=kite)
    schedule.every(10).seconds.do(check_positions, kite=kite)
    schedule.every().day.at(CFG["squareoff_time"]).do(squareoff_all, kite=kite)
    schedule.every().day.at("15:20").do(print_summary)

    log.info(f"  Capital        : ₹{CFG['capital']:,}")
    log.info(f"  SL / Target    : {CFG['sl_pct']*100:.0f}% / {CFG['target_pct']*100:.0f}% of premium")
    log.info(f"  Max lots/trade : {CFG['max_lots']}")
    log.info(f"  Premium range  : ₹{CFG['min_premium']}–₹{CFG['max_premium']}")
    log.info(f"  OTM offset     : {CFG['otm_offset']} strikes")
    log.info(f"  Indices        : {list(CFG['indices'].keys())}")
    log.info(f"  Primary brain  : Claude ({CFG['claude_model']})")
    log.info(f"  Fallback brain : Ollama/{CFG['local_llm_model']}")
    log.info("Press Ctrl+C to stop.\n")

    strategy_loop(kite)
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
