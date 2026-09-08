import os
import time
import sqlite3
import requests
import threading
import re

from flask import Flask, request
import telebot
from telebot import types

import matplotlib
matplotlib.use("Agg")

# ================= CONFIG =================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

if not TELEGRAM_TOKEN:
    raise Exception("NO TELEGRAM TOKEN FOUND")

bot = telebot.TeleBot(
    TELEGRAM_TOKEN,
    threaded=False,
    skip_pending=True
)

DB_NAME = "market.db"

# ================= SINGLE INSTANCE (soft) =================

_instance_lock = True
if not _instance_lock:
    print("DUPLICATE INSTANCE -> EXIT")
    os._exit(0)

# ================= DB =================

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
    c.execute("INSERT OR IGNORE INTO users VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()

def get_users():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT chat_id FROM users")
    data = [x[0] for x in c.fetchall()]
    conn.close()
    return data

# ================= ASSETS =================

ASSETS = {
    "btc": "BTC",
    "bitcoin": "BTC",
    "eth": "ETH",
    "gold": "PAXG",
    "الذهب": "PAXG",
    "sol": "SOL"
}

# ================= MARKET DATA (FIXED) =================

def fetch_klines(symbol):
    symbol = symbol.upper()

    urls = [
        "https://api.binance.com/api/v3/klines",
        "https://api1.binance.com/api/v3/klines",
        "https://api2.binance.com/api/v3/klines",
    ]

    for url in urls:
        try:
            r = requests.get(
                url,
                params={
                    "symbol": f"{symbol}USDT",
                    "interval": "1h",
                    "limit": 100
                },
                timeout=5,
                headers={"User-Agent": "Mozilla/5.0"}
            )

            if r.status_code == 200:
                data = r.json()
                return [float(x[4]) for x in data]

            print("BINANCE FAIL:", r.status_code)

        except Exception as e:
            print("BINANCE ERROR:", e)

    return None

# ================= RSI =================

def rsi(values, period=14):
    if not values or len(values) < period + 1:
        return 50

    gains, losses = [], []

    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def market_data(symbol):
    closes = fetch_klines(symbol)
    if not closes:
        return None

    price = closes[-1]
    return {
        "price": price,
        "rsi": rsi(closes),
        "support": price * 0.99,
        "resistance": price * 1.01
    }

# ================= FLASK =================

app = Flask(__name__)

@app.route("/")
def home():
    return "OK"

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        update = types.Update.de_json(request.data.decode("utf-8"))
        bot.process_new_updates([update])
    except Exception as e:
        print("WEBHOOK ERROR:", e)

    return "OK"

# ================= WEBHOOK =================

def set_webhook():
    bot.remove_webhook()
    time.sleep(2)
    bot.set_webhook(WEBHOOK_URL)
    print("WEBHOOK SET:", WEBHOOK_URL)

# ================= HELPERS =================

def extract_symbol(text):
    for w in re.findall(r'\b\w+\b', text.lower()):
        if w in ASSETS:
            return ASSETS[w]
    return "BTC"

# ================= BROADCAST (IMPORTANT) =================

def is_admin(msg):
    return msg.chat.id == ADMIN_ID

@bot.message_handler(commands=["broadcast"])
def broadcast(m):
    if not is_admin(m):
        bot.reply_to(m, "No permission")
        return

    text = m.text.replace("/broadcast", "").strip()
    users = get_users()

    sent = 0

    for uid in users:
        try:
            bot.send_message(uid, text)
            sent += 1
        except:
            pass

    bot.reply_to(m, f"Sent to {sent} users")

# ================= HANDLERS =================

@bot.message_handler(commands=["start"])
def start(m):
    save_user(m.chat.id)
    bot.reply_to(m, "Bot Ready")

@bot.message_handler(commands=["analyze"])
def analyze(m):
    sym = extract_symbol(m.text)
    data = market_data(sym)

    if not data:
        bot.reply_to(m, "No data")
        return

    bot.reply_to(m,
        f"{sym}\nPrice: {data['price']}\nRSI: {data['rsi']}\nSupport: {data['support']}\nResistance: {data['resistance']}"
    )

# ================= RUN =================

def run():
    print("STARTING...")

    set_webhook()

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

if __name__ == "__main__":
    run()