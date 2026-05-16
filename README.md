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
| `dashboard.py` | Live terminal UI (run in second terminal) |
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

# 3. Optional: live dashboard in another terminal
python dashboard.py

# 4. Optional: browser monitor
python web_dashboard.py

# 5. View today's trades
python journal.py

# 6. Backtest before going live
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
- 1.2% stop-loss from entry
- 2.7% target (2.25:1 reward:risk)
- Trailing SL tightens to 0.84% after price moves up
- Hard 3:10 PM square-off for all positions
- Max 3 concurrent positions
- 1.5% capital at risk per trade (position sized accordingly)

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
| `max_positions` | 3 | Max simultaneous trades |
| `risk_per_trade` | 1.5% | Capital risked per trade |
| `sl_pct` | 1.2% | Stop-loss distance |
| `target_pct` | 2.7% | Target distance |
| `trail_sl` | True | Enable trailing stop |
| `squareoff_time` | 15:10 | Hard exit time |
| `claude_calls_per_min` | 4 | API rate limit |
| `min_volume_ratio` | 1.3 | Volume spike threshold |
| `fake_break_ratio` | 0.4 | Body/range threshold |

---

## Important Notes

- **Always backtest first:** `python backtest.py --symbol RELIANCE --days 30`
- This bot trades real money. Algo trading carries significant risk.
- Paper trade for at least 1 week before deploying real capital.
- Works best in trending markets; may underperform in choppy/sideways conditions.
- Requires Zerodha MIS (intraday) trading to be enabled on your account.
- Kite API requires a ₹2,000/month subscription from Zerodha.
- Get your Anthropic API key at: https://console.anthropic.com

---

## Disclaimer

This software is for educational purposes. The authors are not SEBI-registered
advisors. Past backtest performance does not guarantee future results.
Always trade within your risk tolerance.
