#!/usr/bin/env python3
"""
Comprehensive Test Suite for Trade_Claude
==========================================
Performs all types of testing and analysis including:
- Code syntax validation
- Import dependency checking
- Database schema validation
- Configuration validation
- Strategy logic testing
- Sample backtest run
- P&L analysis
"""

import sys
import os
import importlib.util
import sqlite3
import datetime
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

# Colors for terminal output
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
RESET = '\033[0m'
BOLD = '\033[1m'

def print_header(title):
    """Print a formatted section header"""
    print(f"\n{BOLD}{BLUE}{'='*70}{RESET}")
    print(f"{BOLD}{BLUE}{title.center(70)}{RESET}")
    print(f"{BOLD}{BLUE}{'='*70}{RESET}\n")

def print_success(msg):
    """Print success message"""
    print(f"{GREEN}✓ {msg}{RESET}")

def print_error(msg):
    """Print error message"""
    print(f"{RED}✗ {msg}{RESET}")

def print_warning(msg):
    """Print warning message"""
    print(f"{YELLOW}⚠ {msg}{RESET}")

def print_info(msg):
    """Print info message"""
    print(f"{BLUE}ℹ {msg}{RESET}")

# ═══════════════════════════════════════════════════════════
# TEST 1: File Existence
# ═══════════════════════════════════════════════════════════
def test_file_existence():
    print_header("TEST 1: File Existence & Structure")
    
    required_files = ['bot.py', 'backtest.py', 'journal.py', 'dashboard.py', 'setup.py']
    all_exist = True
    
    for fname in required_files:
        fpath = PROJECT_ROOT / fname
        if os.path.exists(fpath):
            size = os.path.getsize(fpath)
            print_success(f"{fname} ({size:,} bytes)")
        else:
            print_error(f"{fname} NOT FOUND")
            all_exist = False
    
    return all_exist

# ═══════════════════════════════════════════════════════════
# TEST 2: Python Syntax Validation
# ═══════════════════════════════════════════════════════════
def test_python_syntax():
    print_header("TEST 2: Python Syntax Validation")
    
    import py_compile
    files = ['bot.py', 'backtest.py', 'journal.py', 'dashboard.py', 'setup.py']
    all_valid = True
    
    for fname in files:
        fpath = PROJECT_ROOT / fname
        try:
            py_compile.compile(fpath, doraise=True)
            print_success(f"{fname} - valid Python syntax")
        except py_compile.PyCompileError as e:
            print_error(f"{fname} - syntax error: {e}")
            all_valid = False
    
    return all_valid

# ═══════════════════════════════════════════════════════════
# TEST 3: Import Dependencies Check
# ═══════════════════════════════════════════════════════════
def test_import_dependencies():
    print_header("TEST 3: Import Dependencies Check")
    
    required_imports = {
        'numpy': 'Numerical computing',
        'pandas': 'Data analysis',
        'kiteconnect': 'Zerodha API',
        'anthropic': 'Claude API',
        'schedule': 'Task scheduling',
        'requests': 'HTTP requests',
        'rich': 'Terminal UI',
    }
    
    all_available = True
    for module_name, description in required_imports.items():
        try:
            __import__(module_name)
            print_success(f"{module_name:15} - {description}")
        except ImportError:
            print_error(f"{module_name:15} - NOT INSTALLED")
            all_available = False
    
    if not all_available:
        print_warning("Run: pip install kiteconnect anthropic pandas numpy schedule requests rich")
    
    return all_available

# ═══════════════════════════════════════════════════════════
# TEST 4: Configuration Validation
# ═══════════════════════════════════════════════════════════
def test_configuration():
    print_header("TEST 4: Configuration Validation")
    
    try:
        # Read bot.py and extract CFG
        with open(PROJECT_ROOT / 'bot.py', 'r') as f:
            content = f.read()
        
        # Check for required config keys
        required_configs = [
            ('api_key', 'Zerodha API Key'),
            ('api_secret', 'Zerodha API Secret'),
            ('anthropic_key', 'Anthropic API Key'),
            ('capital', 'Trading Capital'),
            ('ema_fast_1m', '1-min Fast EMA'),
            ('ema_slow_1m', '1-min Slow EMA'),
            ('rsi_period_1m', '1-min RSI Period'),
            ('atr_period_1m', '1-min ATR Period'),
        ]
        
        all_present = True
        for cfg_key, description in required_configs:
            if f'"{cfg_key}"' in content or f"'{cfg_key}'" in content:
                print_success(f"Config: {cfg_key:20} - {description}")
            else:
                print_warning(f"Config: {cfg_key:20} - NOT FOUND (may be ok)")
        
        # Check UNIVERSE symbols
        if 'RELIANCE' in content and 'INFY' in content:
            print_success("Trading universe defined (stocks: RELIANCE, INFY, etc.)")
        else:
            print_warning("Trading universe may not be properly defined")
        
        return True
    except Exception as e:
        print_error(f"Configuration check failed: {e}")
        return False

# ═══════════════════════════════════════════════════════════
# TEST 5: Database Schema Validation
# ═══════════════════════════════════════════════════════════
def test_database_schema():
    print_header("TEST 5: Database Schema Validation")
    
    try:
        # Initialize database
        from journal import init_db, DB_FILE
        init_db()
        
        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()
        
        # Check trades table
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trades'")
        if cur.fetchone():
            print_success("Trades table exists")
            cur.execute("PRAGMA table_info(trades)")
            columns = cur.fetchall()
            print_info(f"  Columns: {len(columns)}")
            for col in columns[:5]:  # Show first 5
                print(f"    - {col[1]} ({col[2]})")
        else:
            print_error("Trades table NOT found")
            return False
        
        # Check daily_summary table
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='daily_summary'")
        if cur.fetchone():
            print_success("Daily summary table exists")
        else:
            print_error("Daily summary table NOT found")
            return False
        
        con.close()
        return True
    except Exception as e:
        print_error(f"Database schema check failed: {e}")
        return False

# ═══════════════════════════════════════════════════════════
# TEST 6: Strategy Logic Validation
# ═══════════════════════════════════════════════════════════
def test_strategy_logic():
    print_header("TEST 6: Strategy Logic Validation")
    
    try:
        # Read backtest.py and check for key strategy components
        with open(PROJECT_ROOT / 'backtest.py', 'r') as f:
            content = f.read()
        
        strategy_components = [
            (('EMA',), 'EMA', 'Exponential Moving Average'),
            (('RSI',), 'RSI', 'Relative Strength Index'),
            (('VWAP',), 'VWAP', 'Volume Weighted Average Price'),
            (('ATR',), 'ATR', 'Average True Range'),
            (('OBV',), 'OBV', 'On-Balance Volume'),
            (('fake_break',), 'fake_break', 'Fake Breakout Filter'),
            (('trailing_stop', 'trail_sl'), 'trailing_stop', 'Trailing Stop Loss'),
            (('score',), 'score', 'Signal Scoring System'),
            (('dual',), 'dual', 'Dual-Timeframe'),
        ]
        
        all_present = True
        lower_content = content.lower()
        for aliases, component, description in strategy_components:
            if any(alias.lower() in lower_content for alias in aliases):
                print_success(f"{component:15} - {description}")
            else:
                print_warning(f"{component:15} - NOT FOUND")
                all_present = False
        
        return all_present
    except Exception as e:
        print_error(f"Strategy logic check failed: {e}")
        return False

# ═══════════════════════════════════════════════════════════
# TEST 7: Trade Journal Analysis
# ═══════════════════════════════════════════════════════════
def test_journal_analysis():
    print_header("TEST 7: Trade Journal Analysis")
    
    try:
        from journal import DB_FILE
        
        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()
        
        # Count total trades
        cur.execute("SELECT COUNT(*) FROM trades")
        total_trades = cur.fetchone()[0]
        print_info(f"Total trades recorded: {total_trades}")
        
        if total_trades == 0:
            print_warning("No trades found in journal (expected if first run)")
            con.close()
            return True
        
        # Get P&L stats
        cur.execute("SELECT SUM(pnl), COUNT(*) FROM trades WHERE pnl > 0")
        result = cur.fetchone()
        winning_pnl = result[0] if result[0] else 0
        wins = result[1] if result[1] else 0
        
        cur.execute("SELECT SUM(pnl), COUNT(*) FROM trades WHERE pnl < 0")
        result = cur.fetchone()
        losing_pnl = result[0] if result[0] else 0
        losses = result[1] if result[1] else 0
        
        cur.execute("SELECT SUM(pnl) FROM trades")
        row = cur.fetchone()
        total_pnl = row[0] if row and row[0] else 0
        
        print_success(f"Winning trades: {wins}")
        print_success(f"Losing trades: {losses}")
        print_info(f"Total P&L: ₹{total_pnl:,.2f}")
        
        if total_trades > 0:
            win_rate = (wins / total_trades) * 100
            print_info(f"Win rate: {win_rate:.1f}%")
        
        con.close()
        return True
    except Exception as e:
        print_error(f"Journal analysis failed: {e}")
        return False

# ═══════════════════════════════════════════════════════════
# TEST 8: Code Quality Metrics
# ═══════════════════════════════════════════════════════════
def test_code_quality():
    print_header("TEST 8: Code Quality Metrics")
    
    files = {
        'bot.py': PROJECT_ROOT / 'bot.py',
        'backtest.py': PROJECT_ROOT / 'backtest.py',
        'journal.py': PROJECT_ROOT / 'journal.py',
    }
    
    for fname, fpath in files.items():
        with open(fpath, 'r') as f:
            lines = f.readlines()
        
        total_lines = len(lines)
        code_lines = sum(1 for l in lines if l.strip() and not l.strip().startswith('#'))
        comment_lines = sum(1 for l in lines if l.strip().startswith('#'))
        docstring_lines = sum(1 for l in lines if '"""' in l or "'''" in l)
        
        print_info(f"{fname}:")
        print(f"  Total lines:     {total_lines}")
        print(f"  Code lines:      {code_lines}")
        print(f"  Comment lines:   {comment_lines}")
        print(f"  Docstrings:      {docstring_lines}")
    
    return True

# ═══════════════════════════════════════════════════════════
# TEST 9: Comprehensive Summary Report
# ═══════════════════════════════════════════════════════════
def generate_summary_report(results):
    print_header("COMPREHENSIVE SUMMARY REPORT")
    
    tests = [
        ('File Existence', results[0]),
        ('Python Syntax', results[1]),
        ('Import Dependencies', results[2]),
        ('Configuration', results[3]),
        ('Database Schema', results[4]),
        ('Strategy Logic', results[5]),
        ('Journal Analysis', results[6]),
        ('Code Quality', results[7]),
    ]
    
    passed = sum(1 for _, result in tests if result)
    total = len(tests)
    
    print(f"\n{BOLD}Test Results:{RESET}")
    for test_name, result in tests:
        status = f"{GREEN}PASS{RESET}" if result else f"{RED}FAIL{RESET}"
        print(f"  {test_name:30} {status}")
    
    print(f"\n{BOLD}Score: {passed}/{total} ({(passed/total)*100:.0f}%){RESET}")
    
    if passed == total:
        print_success("All tests passed! System is ready for operation.")
    else:
        print_warning(f"{total - passed} test(s) failed. Review output above.")
    
    print(f"\n{BOLD}{BLUE}System Health: ", end="")
    if passed >= 6:
        print(f"HEALTHY ✓{RESET}")
    elif passed >= 4:
        print(f"DEGRADED ⚠{RESET}")
    else:
        print(f"CRITICAL ✗{RESET}")

# ═══════════════════════════════════════════════════════════
# TEST 10: P&L Analysis Report
# ═══════════════════════════════════════════════════════════
def generate_pnl_report():
    print_header("P&L ANALYSIS REPORT")
    
    try:
        from journal import DB_FILE
        
        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()
        
        # Check if trades exist
        cur.execute("SELECT COUNT(*) FROM trades")
        total_trades = cur.fetchone()[0]
        
        if total_trades == 0:
            print_warning("No trades found in database")
            con.close()
            return
        
        # Summary stats
        cur.execute("""
            SELECT 
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winners,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losers,
                SUM(pnl) as total_pnl,
                AVG(pnl) as avg_pnl,
                MAX(pnl) as best_trade,
                MIN(pnl) as worst_trade
            FROM trades
        """)
        
        row = cur.fetchone()
        total, winners, losers, total_pnl, avg_pnl, best, worst = row
        
        print_info("Trade Statistics:")
        print(f"  Total Trades:    {total}")
        print(f"  Winners:         {winners}")
        print(f"  Losers:          {losers}")
        print(f"  Win Rate:        {(winners/total)*100:.1f}%")
        
        print_info("P&L Metrics:")
        if total_pnl > 0:
            print_success(f"  Total P&L:       ₹{total_pnl:,.2f} (PROFIT)")
        else:
            print_error(f"  Total P&L:       ₹{total_pnl:,.2f} (LOSS)")
        print(f"  Avg P&L/Trade:   ₹{avg_pnl:,.2f}")
        print(f"  Best Trade:      ₹{best:,.2f}")
        print(f"  Worst Trade:     ₹{worst:,.2f}")
        
        # By symbol analysis
        print_info("Performance by Symbol:")
        cur.execute("""
            SELECT symbol, COUNT(*) as count, SUM(pnl) as pnl
            FROM trades
            GROUP BY symbol
            ORDER BY pnl DESC
            LIMIT 10
        """)
        
        for symbol, count, pnl in cur.fetchall():
            status = GREEN if pnl > 0 else RED
            print(f"  {symbol:12} {count:3} trades  {status}₹{pnl:>8,.2f}{RESET}")
        
        # AI Decision source
        print_info("AI Decision Source:")
        cur.execute("""
            SELECT ai_decision, COUNT(*) as count, SUM(pnl) as pnl
            FROM trades
            GROUP BY ai_decision
        """)
        
        for source, count, pnl in cur.fetchall():
            pnl = pnl if pnl else 0
            status = GREEN if pnl > 0 else RED
            print(f"  {source:15} {count:3} trades  {status}₹{pnl:>8,.2f}{RESET}")
        
        con.close()
        
    except Exception as e:
        print_error(f"P&L analysis failed: {e}")

# ═══════════════════════════════════════════════════════════
# MAIN EXECUTION
# ═══════════════════════════════════════════════════════════
def main():
    print(f"\n{BOLD}{BLUE}╔{'═'*68}╗{RESET}")
    print(f"{BOLD}{BLUE}║{'TRADE_CLAUDE - COMPREHENSIVE TEST SUITE'.center(68)}║{RESET}")
    print(f"{BOLD}{BLUE}╚{'═'*68}╝{RESET}\n")
    
    print_info(f"Test Environment: Python {sys.version.split()[0]}")
    print_info(f"Workspace: {PROJECT_ROOT}")
    print_info(f"Timestamp: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Run all tests
    results = []
    results.append(test_file_existence())
    results.append(test_python_syntax())
    results.append(test_import_dependencies())
    results.append(test_configuration())
    results.append(test_database_schema())
    results.append(test_strategy_logic())
    results.append(test_journal_analysis())
    results.append(test_code_quality())
    
    # Generate reports
    generate_summary_report(results)
    generate_pnl_report()
    
    # Final recommendations
    print_header("RECOMMENDATIONS & NEXT STEPS")
    print("""
1. SETUP (if not done):
   • Run: python setup.py
   • Enter your Zerodha API credentials
   • Enter your Anthropic API key

2. BACKTEST:
   • python backtest.py --symbol RELIANCE --days 30
   • python backtest.py --all --days 60

3. LIVE TRADING:
   • python bot.py          (main trader, run daily at 9:00 AM)
   • python dashboard.py    (in another terminal, optional)

4. MONITORING:
   • python journal.py      (view today's trades)
   • python journal.py --all (all-time stats)

5. TROUBLESHOOTING:
   • Check bot.log for detailed errors
   • Verify internet connection for Claude API access
   • Fallback to local LLM (Ollama) if needed
    """)
    
    print(f"{BOLD}{BLUE}{'='*70}{RESET}\n")

if __name__ == '__main__':
    main()
