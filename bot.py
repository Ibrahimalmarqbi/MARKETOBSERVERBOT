import os
import time
import sqlite3
import threading
import io
import re
import requests
import sys

from flask import Flask, request
import telebot
from telebot import types

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# ================== LOCK (NEW IMPORTANT FIX) ==================
LOCK_FILE = "/tmp/marketobserver.lock"

if os.path.exists(LOCK_FILE):
    print("❌ BOT ALREADY RUNNING ELSEWHERE. EXIT.")
    sys.exit()

with open(LOCK_FILE, "w") as f:
    f.write(str(os.getpid()))

print("🔒 LOCK ACQUIRED")

# ================== CONFIG ==================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

print("TOKEN LOADED:", bool(TELEGRAM_TOKEN))
print("WEBHOOK LOADED:", WEBHOOK_URL)

bot = telebot.TeleBot(TELEGRAM_TOKEN)
DB_NAME = "market_pro.db"

# ================== CLEAN EXIT ==================
import atexit

def cleanup():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
            print("🔓 LOCK REMOVED")
    except:
        pass

atexit.register(cleanup)

# ================== ASSETS ==================
ASSETS_DICTIONARY = {
    "الذهب": "PAXG", "gold": "PAXG",
    "btc": "BTC", "بيتكوين": "BTC",
    "eth": "ETH", "ايثريوم": "ETH",
    "sol": "SOL"
}

# ================== DB ==================
def init_db():
    try:
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
        print("DB INIT OK")
    except Exception as e:
        print("DB ERROR:", e)

init_db()

def save_user(chat_id):
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print("SAVE USER ERROR:", e)

def get_all_users():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT chat_id FROM users")
    users = [row[0] for row in c.fetchall()]
    conn.close()
    return users

# ================== API ==================
def fetch_klines(symbol):
    symbol = symbol.upper()
    print("Fetching:", symbol)

    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": f"{symbol}USDT", "interval": "1h", "limit": 100}
        headers = {"User-Agent": "Mozilla/5.0"}

        res = requests.get(url, params=params, headers=headers, timeout=5)

        print("BINANCE STATUS:", res.status_code)

        data = res.json()

        if isinstance(data, list):
            return [float(c[4]) for c in data]

    except Exception as e:
        print("BINANCE ERROR:", e)

    return None

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
                        print("📨 SENDING ALERT:", chat_id)
                        bot.send_message(chat_id, f"🔔 {symbol} hit {price}")

                        c.execute("UPDATE alerts SET status='done' WHERE id=?", (id_,))
                        conn.commit()

                        print("✅ ALERT SENT")

                    except Exception as e:
                        print("SEND ERROR:", e)

            conn.close()

        except Exception as e:
            print("LOOP ERROR:", e)

        time.sleep(30)

# ================== FLASK ==================
app = Flask(__name__)

@app.route("/")
def home():
    return "Running"

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        update = types.Update.de_json(request.data.decode("utf-8"))
        bot.process_new_updates([update])
        print("📩 WEBHOOK RECEIVED")
    except Exception as e:
        print("WEBHOOK ERROR:", e)

    return "OK"

# ================== WEBHOOK SET ==================
def set_bot_webhook():
    try:
        if WEBHOOK_URL:
            bot.remove_webhook()
            time.sleep(1)
            bot.set_webhook(url=WEBHOOK_URL)
            print("WEBHOOK SET:", WEBHOOK_URL)
    except Exception as e:
        print("WEBHOOK ERROR:", e)

# ================== RUN ==================
def run_server():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=check_alerts, daemon=True).start()

    set_bot_webhook()

    print("🚀 BOT RUNNING SAFE MODE (NO DUPLICATES)")