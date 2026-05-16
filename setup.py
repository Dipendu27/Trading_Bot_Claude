#!/usr/bin/env python3
"""
Trade_Claude — First-time Setup Wizard
Run once before anything else:
    python setup.py
"""

import os, subprocess, sys

print("""
╔══════════════════════════════════════════════════════╗
║         Trade_Claude — First-time Setup Wizard       ║
║         Zerodha AI Options Bot (NFO)                 ║
╚══════════════════════════════════════════════════════╝
""")

# ── Step 1: Install dependencies ─────────────────────
print("Step 1 — Installing Python packages…")
pkgs = ["kiteconnect", "anthropic", "pandas", "numpy",
        "schedule", "requests"]
subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet"] + pkgs)
print("  ✅ Packages installed.\n")

# ── Step 2: Collect credentials ──────────────────────
print("Step 2 — Enter your credentials (written into bot.py locally)\n")
zerodha_key    = input("  Zerodha API Key      : ").strip()
zerodha_secret = input("  Zerodha API Secret   : ").strip()
anthropic_key  = input("  Anthropic API Key    : ").strip()
capital_str    = input("  Capital per day ₹    (default 50000): ").strip() or "50000"

# ── Step 3: Patch bot.py ──────────────────────────────
bot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")
if not os.path.exists(bot_path):
    print(f"  ❌ bot.py not found at {bot_path}. Make sure setup.py is in the same folder.")
    sys.exit(1)

src = open(bot_path).read()
src = src.replace("YOUR_ZERODHA_API_KEY",    zerodha_key)
src = src.replace("YOUR_ZERODHA_API_SECRET", zerodha_secret)
src = src.replace("YOUR_ANTHROPIC_API_KEY",  anthropic_key)
src = src.replace('"capital":           50000', f'"capital":           {capital_str}')
open(bot_path, "w").write(src)
print("\n  ✅ bot.py updated with your credentials.\n")

# ── Step 4: Optional Ollama ──────────────────────────
print("Step 3 — Local LLM fallback (Ollama/Mistral) for internet outages")
want = input("  Set up Ollama? [y/N]: ").strip().lower()
if want == "y":
    print("""
  1. Download Ollama from: https://ollama.com/download
  2. Run: ollama pull mistral
  The bot will auto-switch to Mistral if internet drops,
  and switch back to Claude when it returns.
""")
else:
    print("  Skipped — bot will use Claude only.\n")

# ── Step 5: Paper trade reminder ─────────────────────
print("""
  ⚠  PAPER_TRADE = True is set in bot.py by default.
     No real orders will be placed until you change that line to False.
     Always run paper mode for at least 1 week before going live.
""")

# ── Done ─────────────────────────────────────────────
print("""
╔══════════════════════════════════════════════════════╗
║  Setup complete!  Quick reference:                   ║
║                                                      ║
║  Backtest first (paper, no real data needed):        ║
║    python backtest.py --index "NIFTY 50" --days 30   ║
║                                                      ║
║  Run the bot (PAPER_TRADE=True by default):          ║
║    python bot.py                                     ║
║                                                      ║
║  Browser dashboard:                                  ║
║    python web_dashboard.py                           ║
║                                                      ║
║  View today's trades:                                ║
║    python journal.py                                 ║
║                                                      ║
║  All-time P&L:                                       ║
║    python journal.py --all                           ║
╚══════════════════════════════════════════════════════╝
""")
