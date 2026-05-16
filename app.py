"""
Paper Trading Platform — Zerodha AI Bot Mock
=============================================
Flask web app with:
  • Login/Register with hashed passwords (Flask-Login)
  • Per-user portfolios stored in SQLite
  • TradingView Lightweight Charts for real price data
  • Bot signal simulation (same dual-TF logic as bot.py)
  • Place/close paper trades, P&L tracking
  • REST API for live signal polling

Run:
    pip install flask flask-login werkzeug
    python app.py

Then open: http://localhost:5000
"""

import os, json, math, datetime, sqlite3, hashlib, secrets, threading, time
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   session, jsonify, flash, g)
from werkzeug.security import generate_password_hash, check_password_hash
import numpy as np

app = Flask(__name__, template_folder='.')
app.secret_key = secrets.token_hex(32)

DB_FILE   = "papertrading.db"
APP_TITLE = "AlgoDesk"          # your platform brand name
DOMAIN    = "algodesk.local"    # shown in UI (change to your domain)
PASSWORD_HASH_METHOD = "pbkdf2:sha256"

# ── Universe ──────────────────────────────────────────────
UNIVERSE = [
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK",
    "SBIN","BHARTIARTL","ITC","KOTAKBANK","LT",
    "AXISBANK","ASIANPAINT","MARUTI","TITAN","SUNPHARMA",
    "BAJFINANCE","WIPRO","HCLTECH","ULTRACEMCO","NESTLEIND",
]

# Approximate base prices for simulation (updated at startup)
BASE_PRICES = {
    "RELIANCE":2900,"TCS":3900,"HDFCBANK":1700,"INFY":1800,"ICICIBANK":1200,
    "SBIN":800,"BHARTIARTL":1600,"ITC":480,"KOTAKBANK":1900,"LT":3700,
    "AXISBANK":1200,"ASIANPAINT":2800,"MARUTI":12000,"TITAN":3500,"SUNPHARMA":1700,
    "BAJFINANCE":7000,"WIPRO":560,"HCLTECH":1800,"ULTRACEMCO":11000,"NESTLEIND":2500,
}

# ══════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DB_FILE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_db(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    con = sqlite3.connect(DB_FILE)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT UNIQUE NOT NULL,
            email       TEXT UNIQUE NOT NULL,
            password    TEXT NOT NULL,
            capital     REAL DEFAULT 100000,
            created_at  TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS positions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            symbol      TEXT NOT NULL,
            qty         INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            sl          REAL NOT NULL,
            target      REAL NOT NULL,
            entry_time  TEXT DEFAULT (datetime('now','localtime')),
            signal_src  TEXT DEFAULT 'MANUAL',
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            symbol      TEXT NOT NULL,
            qty         INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            exit_price  REAL,
            entry_time  TEXT,
            exit_time   TEXT DEFAULT (datetime('now','localtime')),
            reason      TEXT DEFAULT 'manual',
            pnl         REAL DEFAULT 0,
            signal_src  TEXT DEFAULT 'MANUAL',
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT NOT NULL,
            action      TEXT NOT NULL,
            score_1m    INTEGER,
            score_5m    INTEGER,
            confidence  TEXT,
            price       REAL,
            rsi         REAL,
            fake        INTEGER DEFAULT 0,
            generated_at TEXT DEFAULT (datetime('now','localtime'))
        );
    """)
    con.commit()
    con.close()

# ══════════════════════════════════════════════════════════
#  AUTH HELPERS
# ══════════════════════════════════════════════════════════
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def current_user():
    if 'user_id' not in session:
        return None
    db  = get_db()
    row = db.execute("SELECT * FROM users WHERE id=?", (session['user_id'],)).fetchone()
    return dict(row) if row else None

# ══════════════════════════════════════════════════════════
#  SIGNAL ENGINE  (mirrors bot.py logic, no Kite needed)
# ══════════════════════════════════════════════════════════
def _ema(arr, period):
    """Exponential moving average."""
    result = [arr[0]]
    k = 2 / (period + 1)
    for v in arr[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result

def _rsi(closes, period=7):
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]
    if len(gains) < period:
        return 50.0
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))

def generate_mock_signal(symbol: str) -> dict:
    """
    Generate a signal using synthetic price data (TradingView-style OHLCV simulation).
    In production this would use live Kite WebSocket data.
    """
    np.random.seed(int(time.time() / 60) + hash(symbol) % 1000)   # changes each minute

    base  = BASE_PRICES.get(symbol, 1000)
    trend = np.random.choice([-1, 0, 1], p=[0.3, 0.3, 0.4])       # slight bullish bias
    n     = 60

    # Simulate realistic intraday OHLCV
    prices = [base]
    for _ in range(n - 1):
        chg = trend * np.random.uniform(0, 0.002) + np.random.randn() * 0.003
        prices.append(max(prices[-1] * (1 + chg), 1))

    closes  = prices
    highs   = [p * (1 + abs(np.random.randn() * 0.002)) for p in closes]
    lows    = [p * (1 - abs(np.random.randn() * 0.002)) for p in closes]
    volumes = [float(np.random.randint(50000, 500000)) for _ in range(n)]
    vol_ma  = sum(volumes[-20:]) / 20

    # ── 1-min indicators ──
    ema5  = _ema(closes, 5)
    ema13 = _ema(closes, 13)
    rsi7  = _rsi(closes, 7)

    # ── 5-min (resample every 5 bars) ──
    def resample5(data, fn):
        out = []
        for i in range(4, len(data), 5):
            out.append(fn(data[max(0, i-4):i+1]))
        return out

    c5 = resample5(closes, lambda x: x[-1])
    v5 = resample5(volumes, sum)
    ema9_5  = _ema(c5, 9)  if len(c5) >= 9  else c5
    ema21_5 = _ema(c5, 21) if len(c5) >= 21 else c5
    rsi14_5 = _rsi(c5, 14) if len(c5) >= 14 else 50.0

    # ── VWAP ──
    tp   = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(n)]
    vwap = sum(t * v for t, v in zip(tp, volumes)) / sum(volumes)

    last_close = closes[-1]
    prev_close = closes[-2]
    ago3_close = closes[-4]

    # ── Score 1-min (max 8) ──
    ema_cross_up_1m = ema5[-2] <= ema13[-2] and ema5[-1] > ema13[-1]
    ema_trend_up_1m = ema5[-1] > ema13[-1]
    s1b = sum([
        ema_cross_up_1m or ema_trend_up_1m,
        42 < rsi7 < 70,
        last_close > vwap,
        volumes[-1] > vol_ma * 1.1,
        sum(volumes[-3:]) > sum(volumes[-6:-3]),   # OBV proxy
        last_close > prev_close > ago3_close,
        last_close > vwap * 1.001,
        ema_cross_up_1m,
    ])
    ema_cross_dn_1m = ema5[-2] >= ema13[-2] and ema5[-1] < ema13[-1]
    ema_trend_dn_1m = ema5[-1] < ema13[-1]
    s1s = sum([
        ema_cross_dn_1m or ema_trend_dn_1m,
        30 < rsi7 < 58,
        last_close < vwap,
        volumes[-1] > vol_ma * 1.1,
        sum(volumes[-3:]) < sum(volumes[-6:-3]),
        last_close < prev_close < ago3_close,
        last_close < vwap * 0.999,
        ema_cross_dn_1m,
    ])

    # ── Score 5-min (max 8) ──
    if len(ema9_5) >= 2 and len(ema21_5) >= 2:
        ema_cross_up_5m = ema9_5[-2] <= ema21_5[-2] and ema9_5[-1] > ema21_5[-1]
        ema_trend_up_5m = ema9_5[-1] > ema21_5[-1]
        ema_cross_dn_5m = ema9_5[-2] >= ema21_5[-2] and ema9_5[-1] < ema21_5[-1]
        ema_trend_dn_5m = ema9_5[-1] < ema21_5[-1]
        rsi5 = rsi14_5
        s5b = sum([
            ema_cross_up_5m or ema_trend_up_5m,
            42 < rsi5 < 70,
            c5[-1] > vwap if c5 else False,
            v5[-1] > sum(v5[-5:]) / 5 * 1.1 if len(v5) >= 5 else False,
            c5[-1] > c5[-2] if len(c5) >= 2 else False,
            c5[-1] > c5[-3] if len(c5) >= 3 else False,
            c5[-1] > vwap * 1.001 if c5 else False,
            ema_cross_up_5m,
        ])
        s5s = sum([
            ema_cross_dn_5m or ema_trend_dn_5m,
            30 < rsi5 < 58,
            c5[-1] < vwap if c5 else False,
            v5[-1] > sum(v5[-5:]) / 5 * 1.1 if len(v5) >= 5 else False,
            c5[-1] < c5[-2] if len(c5) >= 2 else False,
            c5[-1] < c5[-3] if len(c5) >= 3 else False,
            c5[-1] < vwap * 0.999 if c5 else False,
            ema_cross_dn_5m,
        ])
    else:
        s5b = s5s = 4  # neutral

    # ── Fake breakout check ──
    body      = abs(last_close - closes[-2])
    rng       = max(highs[-1] - lows[-1], 0.01)
    body_rat  = body / rng
    fake_flags = sum([
        body_rat < 0.3,
        volumes[-1] < vol_ma * 1.1,
        lows[-2] < last_close < highs[-2],
    ])
    fake = fake_flags >= 2

    # ── Decision ──
    if s1b >= 5 and s5b >= 5 and not fake:
        action, confidence = "BUY", "HIGH"
    elif s1s >= 5 and s5s >= 5:
        action, confidence = "SELL", "HIGH"
    elif (s1b >= 4 or s5b >= 4) and not fake:
        action, confidence = "BUY", "MEDIUM"
    elif s1s >= 4 or s5s >= 4:
        action, confidence = "SELL", "MEDIUM"
    else:
        action, confidence = "HOLD", "LOW"

    return {
        "symbol":        symbol,
        "action":        action,
        "confidence":    confidence,
        "price":         float(round(last_close, 2)),
        "score_1m_buy":  int(s1b),
        "score_1m_sell": int(s1s),
        "score_5m_buy":  int(s5b),
        "score_5m_sell": int(s5s),
        "rsi":           float(round(rsi7, 1)),
        "vwap":          float(round(vwap, 2)),
        "fake":          bool(fake),
        "change_pct":    float(round((last_close - base) / base * 100, 2)),
    }

# ══════════════════════════════════════════════════════════
#  BACKGROUND SIGNAL GENERATOR (every 60 sec)
# ══════════════════════════════════════════════════════════
def signal_worker():
    while True:
        try:
            con = sqlite3.connect(DB_FILE)
            for sym in UNIVERSE:
                sig = generate_mock_signal(sym)
                con.execute("""
                    INSERT INTO signals (symbol, action, score_1m, score_5m, confidence, price, rsi, fake)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (sym, sig["action"],
                      sig["score_1m_buy"] + sig["score_1m_sell"],
                      sig["score_5m_buy"] + sig["score_5m_sell"],
                      sig["confidence"], sig["price"], sig["rsi"],
                      1 if sig["fake"] else 0))
            # Keep only last 500 signals
            con.execute("DELETE FROM signals WHERE id NOT IN (SELECT id FROM signals ORDER BY id DESC LIMIT 500)")
            con.commit()
            con.close()
        except Exception as e:
            print(f"Signal worker error: {e}")
        time.sleep(60)

# ══════════════════════════════════════════════════════════
#  ROUTES — AUTH
# ══════════════════════════════════════════════════════════
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','')
        db  = get_db()
        row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if row and check_password_hash(row['password'], password):
            session['user_id']  = row['id']
            session['username'] = row['username']
            return redirect(url_for('dashboard'))
        flash('Invalid username or password.', 'error')
    return render_template('login.html', title=APP_TITLE, domain=DOMAIN)

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        email    = request.form.get('email','').strip()
        password = request.form.get('password','')
        confirm  = request.form.get('confirm','')
        capital  = float(request.form.get('capital', 100000))

        if password != confirm:
            flash('Passwords do not match.', 'error')
            return render_template('register.html', title=APP_TITLE, domain=DOMAIN)
        if len(password) < 6:
            flash('Password must be at least 6 characters.', 'error')
            return render_template('register.html', title=APP_TITLE, domain=DOMAIN)

        db = get_db()
        try:
            db.execute("INSERT INTO users (username, email, password, capital) VALUES (?,?,?,?)",
                       (username, email, generate_password_hash(password, method=PASSWORD_HASH_METHOD), capital))
            db.commit()
            flash('Account created! Please log in.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Username or email already exists.', 'error')
    return render_template('register.html', title=APP_TITLE, domain=DOMAIN)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ══════════════════════════════════════════════════════════
#  ROUTES — DASHBOARD
# ══════════════════════════════════════════════════════════
@app.route('/dashboard')
@login_required
def dashboard():
    user = current_user()
    db   = get_db()

    # Open positions
    positions = db.execute(
        "SELECT * FROM positions WHERE user_id=? ORDER BY entry_time DESC",
        (user['id'],)).fetchall()

    # Recent trades
    trades = db.execute(
        "SELECT * FROM trades WHERE user_id=? ORDER BY exit_time DESC LIMIT 20",
        (user['id'],)).fetchall()

    # Latest signals
    signals = db.execute("""
        SELECT s.* FROM signals s
        INNER JOIN (SELECT symbol, MAX(id) as mid FROM signals GROUP BY symbol) m
        ON s.symbol=m.symbol AND s.id=m.mid
        ORDER BY s.action DESC, s.score_1m DESC
    """).fetchall()

    # P&L summary
    total_pnl = db.execute(
        "SELECT COALESCE(SUM(pnl),0) as total FROM trades WHERE user_id=?",
        (user['id'],)).fetchone()['total']

    # Get live prices for open positions
    pos_with_pnl = []
    for p in positions:
        sig   = generate_mock_signal(p['symbol'])
        ltp   = sig['price']
        upnl  = (ltp - p['entry_price']) * p['qty']
        pos_with_pnl.append({**dict(p), 'ltp': ltp, 'upnl': round(upnl, 2)})

    return render_template('dashboard.html',
        title=APP_TITLE, domain=DOMAIN,
        user=user,
        positions=pos_with_pnl,
        trades=[dict(t) for t in trades],
        signals=[dict(s) for s in signals],
        total_pnl=round(total_pnl, 2),
        universe=UNIVERSE,
    )

# ══════════════════════════════════════════════════════════
#  ROUTES — CHART PAGE
# ══════════════════════════════════════════════════════════
@app.route('/chart/<symbol>')
@login_required
def chart(symbol):
    if symbol not in UNIVERSE:
        flash(f'Symbol {symbol} not in universe.', 'error')
        return redirect(url_for('dashboard'))
    user = current_user()
    sig  = generate_mock_signal(symbol)
    db   = get_db()
    pos  = db.execute(
        "SELECT * FROM positions WHERE user_id=? AND symbol=?",
        (user['id'], symbol)).fetchone()
    return render_template('chart.html',
        title=APP_TITLE, domain=DOMAIN,
        symbol=symbol, signal=sig,
        position=dict(pos) if pos else None,
        user=user,
    )

# ══════════════════════════════════════════════════════════
#  ROUTES — TRADE ACTIONS
# ══════════════════════════════════════════════════════════
@app.route('/trade/buy', methods=['POST'])
@login_required
def trade_buy():
    user   = current_user()
    symbol = request.form.get('symbol','').upper()
    qty    = int(request.form.get('qty', 1))
    src    = request.form.get('signal_src', 'MANUAL')

    if symbol not in UNIVERSE:
        return jsonify({"error": "Invalid symbol"}), 400

    db  = get_db()
    # Check already open
    existing = db.execute(
        "SELECT id FROM positions WHERE user_id=? AND symbol=?",
        (user['id'], symbol)).fetchone()
    if existing:
        return jsonify({"error": f"Already have open position in {symbol}"}), 400

    sig    = generate_mock_signal(symbol)
    price  = sig['price']
    cost   = price * qty
    sl     = round(price * 0.992, 2)     # 0.8% SL
    target = round(price * 1.020, 2)     # 2.0% target

    if cost > user['capital']:
        return jsonify({"error": "Insufficient capital"}), 400

    db.execute(
        "INSERT INTO positions (user_id, symbol, qty, entry_price, sl, target, signal_src) VALUES (?,?,?,?,?,?,?)",
        (user['id'], symbol, qty, price, sl, target, src))
    db.execute("UPDATE users SET capital=capital-? WHERE id=?", (cost, user['id']))
    db.commit()

    return jsonify({
        "success": True,
        "message": f"Bought {qty} {symbol} @ ₹{price:.2f}",
        "price": price, "sl": sl, "target": target,
    })

@app.route('/trade/sell', methods=['POST'])
@login_required
def trade_sell():
    user      = current_user()
    symbol    = request.form.get('symbol','').upper()
    reason    = request.form.get('reason', 'manual')

    db  = get_db()
    pos = db.execute(
        "SELECT * FROM positions WHERE user_id=? AND symbol=?",
        (user['id'], symbol)).fetchone()
    if not pos:
        return jsonify({"error": "No open position"}), 400

    sig        = generate_mock_signal(symbol)
    exit_price = sig['price']
    pnl        = (exit_price - pos['entry_price']) * pos['qty']
    proceeds   = exit_price * pos['qty']

    db.execute("""
        INSERT INTO trades (user_id, symbol, qty, entry_price, exit_price,
                            entry_time, reason, pnl, signal_src)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (user['id'], symbol, pos['qty'], pos['entry_price'], exit_price,
          pos['entry_time'], reason, round(pnl, 2), pos['signal_src']))
    db.execute("DELETE FROM positions WHERE id=?", (pos['id'],))
    db.execute("UPDATE users SET capital=capital+? WHERE id=?", (proceeds, user['id']))
    db.commit()

    return jsonify({
        "success": True,
        "message": f"Sold {pos['qty']} {symbol} @ ₹{exit_price:.2f}  P&L: ₹{pnl:+.0f}",
        "pnl": round(pnl, 2),
    })

# ══════════════════════════════════════════════════════════
#  API — real-time data feeds
# ══════════════════════════════════════════════════════════
@app.route('/api/signals')
@login_required
def api_signals():
    sigs = {sym: generate_mock_signal(sym) for sym in UNIVERSE}
    return jsonify(sigs)

@app.route('/api/signal/<symbol>')
@login_required
def api_signal_symbol(symbol):
    if symbol not in UNIVERSE:
        return jsonify({"error": "Invalid symbol"}), 404
    return jsonify(generate_mock_signal(symbol))

@app.route('/api/candles/<symbol>/<tf>')
@login_required
def api_candles(symbol, tf):
    """Generate synthetic OHLCV candles for TradingView chart."""
    if symbol not in UNIVERSE:
        return jsonify([])
    base   = BASE_PRICES.get(symbol, 1000)
    np.random.seed(hash(symbol) % 9999)
    n      = 200 if tf == '1' else 60
    now    = datetime.datetime.now()
    mins   = int(tf)

    candles = []
    price   = base * np.random.uniform(0.97, 1.03)
    trend   = np.random.choice([-0.5, 0, 0.5])

    for i in range(n, 0, -1):
        ts    = now - datetime.timedelta(minutes=i * mins)
        # Only during market hours
        if not (datetime.time(9,15) <= ts.time() <= datetime.time(15,30)):
            continue
        chg   = trend * 0.001 + np.random.randn() * 0.004
        o     = price
        c     = max(o * (1 + chg), 1)
        h     = max(o, c) * (1 + abs(np.random.randn() * 0.002))
        l     = min(o, c) * (1 - abs(np.random.randn() * 0.002))
        vol   = int(np.random.randint(10000, 200000))
        candles.append({
            "time":   int(ts.timestamp()),
            "open":   round(o, 2),
            "high":   round(h, 2),
            "low":    round(l, 2),
            "close":  round(c, 2),
            "volume": vol,
        })
        price = c

    return jsonify(candles)

@app.route('/api/portfolio')
@login_required
def api_portfolio():
    user = current_user()
    db   = get_db()
    positions = db.execute(
        "SELECT * FROM positions WHERE user_id=?", (user['id'],)).fetchall()

    result = []
    for p in positions:
        sig  = generate_mock_signal(p['symbol'])
        ltp  = sig['price']
        upnl = (ltp - p['entry_price']) * p['qty']
        result.append({
            "symbol":      p['symbol'],
            "qty":         p['qty'],
            "entry_price": p['entry_price'],
            "ltp":         ltp,
            "sl":          p['sl'],
            "target":      p['target'],
            "upnl":        round(upnl, 2),
            "change_pct":  round((ltp - p['entry_price']) / p['entry_price'] * 100, 2),
        })
    return jsonify(result)

# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
if __name__ == '__main__':
    init_db()
    # Start background signal worker
    t = threading.Thread(target=signal_worker, daemon=True)
    t.start()
    print(f"\n{'='*55}")
    print(f"  {APP_TITLE} Paper Trading Platform")
    print(f"  URL     : http://localhost:5000")
    print(f"  Domain  : {DOMAIN}")
    print(f"  Create an account from the Register page before logging in.")
    print(f"{'='*55}\n")
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)
