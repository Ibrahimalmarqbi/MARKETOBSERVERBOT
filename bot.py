import os
import time
import sqlite3
import threading
import io
import re
import requests

from flask import Flask, request
import telebot
from telebot import types

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# ================== CONFIG ==================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

print("TOKEN LOADED:", bool(TELEGRAM_TOKEN))
print("WEBHOOK LOADED:", WEBHOOK_URL)

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)
DB_NAME = "market_pro.db"

# ================== ASSETS ==================
ASSETS_DICTIONARY = {
    "الذهب": "PAXG", "gold": "PAXG",
    "btc": "BTC", "بيتكوين": "BTC",
    "eth": "ETH", "ايثريوم": "ETH",
    "sol": "SOL"
}

# ================== DB ==================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    c.execute("CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY)")
    c.execute("""
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        symbol TEXT,
        target_price REAL,
        condition TEXT,
        status TEXT
    )
    """)

    conn.commit()
    conn.close()

init_db()

def save_user(chat_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT chat_id FROM users")
    users = [row[0] for row in c.fetchall()]
    conn.close()
    return users

# ================== 3-LAYER MARKET DATA ==================
def fetch_klines(symbol):
    symbol = symbol.upper()
    print("Fetching:", symbol)

    # ========== LAYER 1: BINANCE ==========
    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {
            "symbol": f"{symbol}USDT",
            "interval": "1h",
            "limit": 200
        }
        headers = {"User-Agent": "Mozilla/5.0"}

        res = requests.get(url, params=params, headers=headers, timeout=5)

        print("BINANCE STATUS:", res.status_code)

        if res.status_code == 200:
            data = res.json()
            if isinstance(data, list):
                print("BINANCE OK")
                return [float(c[4]) for c in data]

    except Exception as e:
        print("BINANCE ERROR:", e)

    # ========== LAYER 2: CRYPTOCOMPARE ==========
    try:
        url = "https://min-api.cryptocompare.com/data/v2/histohour"
        params = {
            "fsym": symbol,
            "tsym": "USD",
            "limit": 200
        }

        res = requests.get(url, params=params, timeout=5)
        data = res.json()

        candles = data.get("Data", {}).get("Data", [])

        if candles:
            print("CRYPTOCOMPARE OK")
            return [float(c["close"]) for c in candles]

    except Exception as e:
        print("CRYPTOCOMPARE ERROR:", e)

    # ========== LAYER 3: YFINANCE ==========
    try:
        import yfinance as yf

        ticker = f"{symbol}-USD"
        print("YFINANCE:", ticker)

        df = yf.download(ticker, period="7d", interval="1h", progress=False)

        if df is not None and not df.empty:
            print("YFINANCE OK")
            return df["Close"].tolist()

    except Exception as e:
        print("YFINANCE ERROR:", e)

    print("ALL SOURCES FAILED:", symbol)
    return None

# ================== RSI ==================
def calculate_rsi(closes, period=14):
    if not closes or len(closes) < period + 1:
        return 50

    gains, losses = [], []

    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

# ================== INDICATORS ==================
def get_market_indicators(symbol):
    closes = fetch_klines(symbol)

    if not closes:
        return None

    price = closes[-1]

    return {
        "symbol": symbol,
        "price": price,
        "rsi": calculate_rsi(closes),
        "support": price * 0.988,
        "resistance": price * 1.012
    }

# ================== CHART ==================
def generate_chart(symbol):
    closes = fetch_klines(symbol)

    if not closes:
        return None

    plt.figure(figsize=(8, 4))
    plt.plot(closes[-40:])
    plt.title(symbol)

    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()

    return buf

# ================== ALERT LOOP ==================
def check_alerts():
    while True:
        try:
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()

            c.execute("SELECT id, chat_id, symbol, target_price, condition FROM alerts WHERE status='active'")
            alerts = c.fetchall()

            for id_, chat_id, symbol, target, cond in alerts:
                data = get_market_indicators(symbol)
                if not data:
                    continue

                price = data["price"]

                if (cond == "below" and price <= target) or (cond == "above" and price >= target):
                    try:
                        bot.send_message(chat_id, f"🔔 {symbol} hit {price}")
                        c.execute("UPDATE alerts SET status='done' WHERE id=?", (id_,))
                        conn.commit()
                    except Exception as e:
                        print("SEND ERROR:", e)

            conn.close()

        except Exception as e:
            print("ALERT LOOP ERROR:", e)

        time.sleep(30)

# ================== TELEGRAM ==================
@bot.message_handler(commands=['start'])
def start(m):
    save_user(m.chat.id)
    bot.reply_to(m, "Bot ready")

@bot.message_handler(commands=['analyze'])
def analyze(m):
    sym = "BTC"
    data = get_market_indicators(sym)

    if not data:
        bot.reply_to(m, "❌ no data")
        return

    bot.reply_to(m, f"""
{sym}
Price: {data['price']}
RSI: {data['rsi']}
Support: {data['support']}
Resistance: {data['resistance']}
""")

@bot.message_handler(commands=['chart'])
def chart(m):
    sym = "BTC"
    img = generate_chart(sym)

    if not img:
        bot.reply_to(m, "❌ no chart")
        return

    bot.send_photo(m.chat.id, img)

# ================== FLASK ==================
app = Flask(__name__)

@app.route("/")
def home():
    return "Running"

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        update = types.Update.de_json(request.get_data().decode("utf-8"))
        bot.process_new_updates([update])
    except Exception as e:
        print("WEBHOOK ERROR:", e)

    return "OK"

# ================== WEBHOOK ==================
def set_webhook():
    try:
        if WEBHOOK_URL:
            bot.remove_webhook()
            time.sleep(1)
            bot.set_webhook(url=WEBHOOK_URL)
            print("WEBHOOK SET OK")
    except Exception as e:
        print("WEBHOOK ERROR:", e)

# ================== RUN ==================
def run():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

if __name__ == "__main__":
    threading.Thread(target=run, daemon=True).start()
    threading.Thread(target=check_alerts, daemon=True).start()

    set_webhook()

    print("BOT RUNNING FULL VERSION")