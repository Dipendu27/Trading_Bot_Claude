#!/usr/bin/env python3
"""
setup.py - First-time setup wizard for the Zerodha AI Bot.

Run once:
    python setup.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"


def save_env(values: dict[str, str]) -> None:
    existing: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" not in line or line.strip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            existing[key.strip()] = value.strip().strip('"').strip("'")

    for key, value in values.items():
        if value:
            existing[key] = value.strip()

    ordered = [
        "KITE_API_KEY",
        "KITE_API_SECRET",
        "ANTHROPIC_API_KEY",
        "TRADE_CAPITAL",
    ]
    keys = ordered + sorted(key for key in existing if key not in ordered)
    ENV_FILE.write_text("\n".join(f"{key}={existing[key]}" for key in keys if existing.get(key)) + "\n")


print("""
╔══════════════════════════════════════════════════╗
║   Zerodha AI Bot - First-time Setup Wizard       ║
╚══════════════════════════════════════════════════╝
""")

print("Step 1: Installing Python packages...")
pkgs = ["kiteconnect", "anthropic", "pandas", "numpy", "schedule", "requests", "rich", "flask"]
subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet"] + pkgs)
print("  Packages installed.\n")

print("""Step 2: API key cost check

  Zerodha Kite API Key/Secret:
    - Personal Kite API tier is free for basic account/order APIs.
    - This bot needs historical candles and live WebSocket ticks, so you need
      Kite Connect: currently listed by Zerodha at INR 500/month.

  Anthropic API Key:
    - Not free for this bot. The key can be created from Anthropic Console,
      but Claude API calls are usage-based token billing.

  Ollama fallback:
    - Free when run locally on your own machine.
    - Ollama cloud has a free tier plus paid Pro/Max plans.

  Python packages installed above are free/open-source packages.
""")

print("Step 3: Enter credentials for local .env\n")
zerodha_key = input("  Zerodha Kite API Key    : ").strip()
zerodha_secret = input("  Zerodha Kite API Secret : ").strip()
anthropic_key = input("  Anthropic API Key       : ").strip()
capital = input("  Capital per day INR (default 50000): ").strip() or "50000"

save_env({
    "KITE_API_KEY": zerodha_key,
    "KITE_API_SECRET": zerodha_secret,
    "ANTHROPIC_API_KEY": anthropic_key,
    "TRADE_CAPITAL": capital,
})

print("\n  Saved credentials to local .env. This file is ignored by Git.\n")

print("Step 4: Local LLM fallback")
want_ollama = input("  Install Ollama for local fallback? [y/N]: ").strip().lower()
if want_ollama == "y":
    print("""
  Download and install Ollama from: https://ollama.com/download
  Then run:  ollama pull mistral
""")
else:
    print("  Skipped. Bot will use Claude API only.\n")

print("""
╔══════════════════════════════════════════════════╗
║  Setup complete.                                ║
║                                                  ║
║  Web terminal:                                   ║
║    python web_dashboard.py                       ║
║                                                  ║
║  Main bot:                                       ║
║    python bot.py                                 ║
║                                                  ║
║  Terminal dashboard:                             ║
║    python dashboard.py                           ║
╚══════════════════════════════════════════════════╝
""")
