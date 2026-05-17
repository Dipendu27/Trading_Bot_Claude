# Zerodha AI Trading Bot — Powered by Claude

A fully automated intraday trading bot for NSE Indian markets.
Uses real-time WebSocket tick data, a multi-indicator signal engine, and
Claude AI to validate ambiguous setups before placing orders.

---

## Files

| File | Purpose |
|------|---------|
| `bot.py` | Main bot — run this every morning |
| `journal.py` | SQLite trade journal + P&L tracker |
| `web_dashboard.py` | Browser trading terminal with Kite market data |
| `backtest.py` | Test strategy on historical Kite data |
| `setup.py` | First-time setup wizard |

---

## Quick Start

```bash
# 1. Run the setup wizard (once only)
python setup.py

# 2. Every morning before 9:15 AM
python bot.py

# 3. Optional: browser monitor
python web_dashboard.py

# 4. View today's trades
python journal.py

# 5. Backtest before going live
python backtest.py --symbol RELIANCE --days 30
```

Open the browser terminal at `http://127.0.0.1:5050`. It reads local
`trades.db` and `bot.log` at runtime, renders Kite historical candles with
TradingView Lightweight Charts, and loads live watchlist quotes, market depth,
positions, orders, and equity margins from Zerodha Kite. It does not generate
fallback prices. If Kite credentials or today's `.access_token` are missing,
market panels show a Kite connection error instead of prices. The dashboard can
read credentials from local `.env`, `bot.py`, or `KITE_API_KEY` /
`ZERODHA_API_KEY`, and can read an access token from `.access_token`,
`KITE_ACCESS_TOKEN`, or `ZERODHA_ACCESS_TOKEN`.

The web terminal also has a Kite Connection panel. Save your API key and secret
there, open the Kite login URL, paste the `request_token`, and the dashboard
will save today's access token locally. Runtime databases, `.env`, access
tokens, logs, CSV exports, and generated reports are ignored by Git.

Chart controls include candlestick/bar/line/area modes, dark/light/contrast
themes, volume, EMA 9, SMA 20, SMA 50, VWAP, grid/crosshair toggles, log scale,
auto refresh, reset view, and fullscreen.

---

## Architecture

```
WebSocket (KiteTicker)
    │
    ▼
CandleBuilder  ──→  1-min OHLCV buffer (per symbol)
    │
    ▼
Indicators: EMA(9/21), RSI(14), ATR(14), VWAP, OBV, Volume MA
    │
    ▼
Signal Engine  ──→  score 0–5
  ├─ score ≥ 4  →  BUY/SELL immediately (no AI needed)
  ├─ score = 3  →  AMBIGUOUS → ask Claude API
  └─ score < 3  →  HOLD

Fake Breakout Filter (runs before every BUY):
  • Candle body/range ratio
  • Volume vs 20-period average
  • Close inside prior candle range
  • OBV divergence

ConnectivityWatchdog (background thread, checks every 5s):
  • Internet UP   → use Claude API
  • Internet DOWN → auto-switch to Ollama (local Mistral)

Order Management:
  • MIS (intraday) orders only — no overnight positions
  • Trailing stop-loss (tightens after price moves in your favour)
  • Hard square-off at 3:10 PM regardless of P&L

Trade Journal (SQLite):
  • Every entry/exit logged with P&L and AI decision source
  • Daily summary with win rate, charges, net P&L
```

---

## Strategy Logic

**Entry (BUY) — all from 1-min candles:**
1. EMA(9) crosses above EMA(21)  ← trend signal
2. RSI between 48–72             ← momentum, not overbought
3. Price above VWAP              ← institutional bias
4. Volume > 1.3× 20-period avg  ← real participation
5. OBV trending up               ← accumulation

Score ≥ 4 + no fake breakout → BUY immediately  
Score = 3 → ask Claude → BUY/SELL/HOLD

**Fake Breakout Detection:**
Rejects entries when ≥ 2 of:
- Candle body < 40% of range (wick-heavy = indecision)
- Volume below average (no conviction)
- Price closed back inside previous candle
- OBV diverging from price

**Risk Management:**
- Regime-aware option-premium stop-loss: 25% in trends, 15% in sideways markets
- Higher reward targets: 120% in BULL/BEAR trends, 55% in SIDEWAYS breakouts
- Scaled exit now books only 30% at 1:1 R:R and lets the remaining lots run
- Trailing SL starts after 30% premium move in trends and 15% in sideways markets
- Hard 3:15 PM square-off for all positions
- Max 5 lots per trade, capped by account exposure
- 6% capital at risk per trade on a ₹25,000 account, sized by stop-loss risk and lot size

---

## Claude API Usage

Claude is called **only for AMBIGUOUS signals** (score = 3) to keep API costs low.
Rate-limited to 4 calls/minute.

Claude receives:
- Symbol, price, RSI, VWAP, ATR, scores, fake breakout flags
- Last 10 candles (1-min OHLCV)
- Current open positions count

Returns exactly: `BUY`, `SELL`, or `HOLD`

**Typical API cost:** ₹2–8/day depending on market activity.

---

## Local LLM Fallback (for internet outages)

Install Ollama: https://ollama.com/download

```bash
ollama pull mistral   # ~4GB download, runs on CPU
```

The bot **automatically switches** to Mistral when it detects internet loss,
and switches back to Claude when connectivity is restored.

Mistral handles about 80% of Claude's quality for this use case.

---

## Configuration (in bot.py CFG dict)

| Key | Default | Description |
|-----|---------|-------------|
| `capital` | 25000 | ₹ deployed per day |
| `max_lots` | 5 | Max option lots per trade |
| `risk_per_trade` | 6% | Capital risked per trade |
| `sl_pct` | 15-25% | Regime-aware option-premium stop-loss |
| `target_pct` | 55-120% | Regime-aware option-premium target |
| `scale_exit_ratio` | 30% | Portion booked at first 1:1 target |
| `trail_sl` | True | Enable trailing stop |
| `squareoff_time` | 15:15 | Hard exit time |
| `claude_calls_per_min` | 10 | API rate limit |
| `min_volume_ratio` | 1.1 | Volume spike threshold |
| `fake_break_ratio` | 0.3 | Body/range threshold |

---

## Important Notes

- **Always backtest first:** `python backtest.py --symbol RELIANCE --days 30`
- This bot trades real money. Algo trading carries significant risk.
- Paper trade for at least 1 week before deploying real capital.
- Works best in trending markets; may underperform in choppy/sideways conditions.
- Requires Zerodha MIS (intraday) trading to be enabled on your account.
- Kite's personal API tier is free for basic account/order APIs, but this bot
  needs historical candles and live WebSocket ticks, which require Kite Connect.
  Zerodha currently lists Kite Connect at ₹500/month.
- Get your Anthropic API key at: https://console.anthropic.com

---

## Disclaimer

This software is for educational purposes. The authors are not SEBI-registered
advisors. Past backtest performance does not guarantee future results.
Always trade within your risk tolerance.
