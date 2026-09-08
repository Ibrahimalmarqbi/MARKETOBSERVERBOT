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

print("🧠 BOOTING BOT...")
print("TOKEN EXISTS:", bool(TELEGRAM_TOKEN))
print("WEBHOOK URL:", WEBHOOK_URL)

if not TELEGRAM_TOKEN:
    raise Exception("NO TELEGRAM TOKEN FOUND")

bot = telebot.TeleBot(
    TELEGRAM_TOKEN,
    threaded=False,        # مهم: يمنع threads العشوائية
    skip_pending=True
)

DB_NAME = "market_pro.db"

# ================== DEBUG GUARD ==================
INSTANCE_FLAG = False

def single_instance_guard():
    global INSTANCE_FLAG
    if INSTANCE_FLAG:
        print("🚨 DUPLICATE INSTANCE DETECTED - EXITING")
        os._exit(0)
    INSTANCE_FLAG = True
    print("✅ SINGLE INSTANCE LOCK OK")

single_instance_guard()

# ================== ASSETS ==================
ASSETS_DICTIONARY = {
    "الذهب": "PAXG", "gold": "PAXG",
    "btc": "BTC", "بيتكوين": "BTC",
    "eth": "ETH", "ايثريوم": "ETH",
    "sol": "SOL"
}

# ================== DB ==================
def init_db():
    print("📦 INIT DB...")
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
    print("✅ DB READY")

init_db()

def save_user(chat_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()
    print("👤 USER SAVED:", chat_id)

def get_all_users():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT chat_id FROM users")
    users = [row[0] for row in c.fetchall()]
    conn.close()
    return users

# ================== MARKET DATA ==================
def fetch_klines(symbol):
    symbol = symbol.upper()
    print("📡 FETCH:", symbol)

    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": f"{symbol}USDT", "interval": "1h", "limit": 100}
        headers = {"User-Agent": "Mozilla/5.0"}

        res = requests.get(url, params=params, headers=headers, timeout=5)
        print("BINANCE STATUS:", res.status_code)

        data = res.json()

        if isinstance(data, list):
            print("BINANCE OK DATA")
            return [float(c[4]) for c in data]

        print("BINANCE BAD RESPONSE:", data)

    except Exception as e:
        print("BINANCE ERROR:", e)

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
        print("❌ NO MARKET DATA:", symbol)
        return None

    price = closes[-1]
    rsi = calculate_rsi(closes)

    print("📊 DATA OK:", symbol, price, rsi)

    return {
        "symbol": symbol,
        "price": price,
        "rsi": rsi,
        "support": price * 0.988,
        "resistance": price * 1.012
    }

# ================== FLASK ==================
app = Flask(__name__)

@app.route("/")
def home():
    print("🌐 HOME HIT")
    return "BOT RUNNING"

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        print("📩 WEBHOOK HIT")

        json_str = request.get_data().decode("utf-8")
        update = types.Update.de_json(json_str)

        print("➡️ UPDATE RECEIVED")
        bot.process_new_updates([update])

    except Exception as e:
        print("❌ WEBHOOK ERROR:", e)

    return "OK"

# ================== WEBHOOK SET ==================
def set_webhook():
    try:
        print("🔄 RESET WEBHOOK...")

        bot.remove_webhook()
        time.sleep(2)

        bot.set_webhook(url=WEBHOOK_URL)

        print("✅ WEBHOOK ACTIVE:", WEBHOOK_URL)

    except Exception as e:
        print("❌ WEBHOOK SET ERROR:", e)

# ================== TELEGRAM HANDLERS ==================
@bot.message_handler(commands=['start'])
def start(m):
    print("🚀 START CMD:", m.chat.id)
    save_user(m.chat.id)
    bot.reply_to(m, "Bot Ready")

@bot.message_handler(commands=['analyze'])
def analyze(m):
    print("📊 ANALYZE:", m.text)

    sym = extract_symbol(m.text)
    data = get_market_indicators(sym)

    if not data:
        bot.reply_to(m, "No data")
        return

    bot.reply_to(m,
        f"{sym}\nPrice: {data['price']}\nRSI: {data['rsi']}\nSupport: {data['support']}\nResistance: {data['resistance']}"
    )

def extract_symbol(text):
    for w in re.findall(r'\b\w+\b', text.lower()):
        if w in ASSETS_DICTIONARY:
            return ASSETS_DICTIONARY[w]
    return "BTC"

# ================== SAFE START ==================
def run():
    print("🔥 STARTING SERVER...")

    print("🧪 TEST BOT GET UPDATES (should NOT run polling)")
    print("THREAD MODE DISABLED -> WEBHOOK ONLY")

    set_webhook()

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

if __name__ == "__main__":
    run()