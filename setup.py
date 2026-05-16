#!/usr/bin/env python3
"""
setup.py — First-time setup wizard for the Zerodha AI Bot
Run once:  python setup.py
"""

import os, subprocess, sys

print("""
╔══════════════════════════════════════════════════╗
║   Zerodha AI Bot — First-time Setup Wizard       ║
╚══════════════════════════════════════════════════╝
""")

# ── 1. Install dependencies ───────────────────────
print("Step 1: Installing Python packages…")
pkgs = ["kiteconnect", "anthropic", "pandas", "numpy", "schedule", "requests", "rich"]
subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet"] + pkgs)
print("  ✅ Packages installed.\n")

# ── 2. Collect credentials ────────────────────────
print("Step 2: Enter your credentials (stored only in bot.py locally)\n")

zerodha_key    = input("  Zerodha API Key    : ").strip()
zerodha_secret = input("  Zerodha API Secret : ").strip()
anthropic_key  = input("  Anthropic API Key  : ").strip()
capital        = input("  Capital per day ₹  (default 25000): ").strip() or "25000"

# ── 3. Patch bot.py ───────────────────────────────
bot_path = os.path.join(os.path.dirname(__file__), "bot.py")
with open(bot_path) as f:
    src = f.read()

src = src.replace("YOUR_ZERODHA_API_KEY",    zerodha_key)
src = src.replace("YOUR_ZERODHA_API_SECRET", zerodha_secret)
src = src.replace("YOUR_ANTHROPIC_API_KEY",  anthropic_key)
src = src.replace('"capital":           25000', f'"capital":           {capital}')

with open(bot_path, "w") as f:
    f.write(src)

print("\n  ✅ bot.py configured.\n")

# ── 4. Optional Ollama ────────────────────────────
print("Step 3: Local LLM fallback (for internet outages)")
want_ollama = input("  Install Ollama for local fallback? [y/N]: ").strip().lower()
if want_ollama == "y":
    print("""
  ➡  Download and install Ollama from: https://ollama.com/download
  ➡  Then run:  ollama pull mistral
  ➡  The bot will auto-switch to Mistral when your internet drops.
""")
else:
    print("  Skipped. Bot will use Claude API only (may pause on internet drops).\n")

# ── 5. Done ───────────────────────────────────────
print("""
╔══════════════════════════════════════════════════╗
║  Setup complete!  Here's how to run the bot:     ║
║                                                  ║
║  Every morning before 9:15 AM:                   ║
║    python bot.py                                 ║
║                                                  ║
║  In a second terminal (optional live view):      ║
║    python dashboard.py                           ║
║                                                  ║
║  Backtest a symbol first (recommended):          ║
║    python backtest.py --symbol RELIANCE --days 30║
║                                                  ║
║  View today's trade journal:                     ║
║    python journal.py                             ║
╚══════════════════════════════════════════════════╝
""")
