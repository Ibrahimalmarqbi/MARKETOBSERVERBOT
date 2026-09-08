import os
import time
import sqlite3
import threading
import re
import requests

from flask import Flask, request
import telebot
from telebot import types

# ================== CONFIG ==================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

print("🧠 BOOTING BOT...")
print("TOKEN:", bool(TELEGRAM_TOKEN))
print("WEBHOOK:", WEBHOOK_URL)

if not TELEGRAM_TOKEN or not WEBHOOK_URL:
    raise Exception("Missing TOKEN or WEBHOOK_URL")

# ================== BOT (WEBHOOK ONLY) ==================
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)

# 🚫 IMPORTANT: prevent any polling behavior
bot.remove_webhook()

DB_NAME = "market_pro.db"

# ================== SINGLE INSTANCE GUARD ==================
INSTANCE = True

# ================== ASSETS ==================
ASSETS = {
    "btc": "BTC",
    "eth": "ETH",
    "sol": "SOL",
    "gold": "PAXG",
    "الذهب": "PAXG",
    "بيتكوين": "BTC"
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
        condition TEXT
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
    data = [x[0] for x in c.fetchall()]
    conn.close()
    return data

def add_alert(chat_id, symbol, price, condition):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "INSERT INTO alerts (chat_id, symbol, target_price, condition) VALUES (?, ?, ?, ?)",
        (chat_id, symbol, price, condition)
    )
    conn.commit()
    conn.close()

def get_alerts():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT * FROM alerts")
    data = c.fetchall()
    conn.close()
    return data

def delete_alert(alert_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
    conn.commit()
    conn.close()

# ================== MARKET ==================
def fetch_price(symbol):
    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": f"{symbol}USDT", "interval": "1h", "limit": 1}
        r = requests.get(url, params=params, timeout=5)
        data = r.json()
        return float(data[0][4])
    except:
        return None

# ================== ALERT SYSTEM ==================
def alert_worker():
    print("🚀 ALERT WORKER RUNNING")

    while True:
        try:
            alerts = get_alerts()

            for alert in alerts:
                alert_id, chat_id, symbol, target, cond = alert

                price = fetch_price(symbol)
                if not price:
                    continue

                if cond == "above" and price >= target:
                    try:
                        bot.send_message(chat_id, f"🚨 {symbol} وصل {price}")
                        delete_alert(alert_id)
                    except Exception as e:
                        print("SEND ERROR:", e)

                elif cond == "below" and price <= target:
                    try:
                        bot.send_message(chat_id, f"🚨 {symbol} نزل {price}")
                        delete_alert(alert_id)
                    except Exception as e:
                        print("SEND ERROR:", e)

        except Exception as e:
            print("ALERT LOOP ERROR:", e)

        time.sleep(30)

# ================== HELPERS ==================
def extract_symbol(text):
    for w in re.findall(r'\w+', text.lower()):
        if w in ASSETS:
            return ASSETS[w]
    return "BTC"

# ================== HANDLERS ==================
@bot.message_handler(commands=['start'])
def start(m):
    save_user(m.chat.id)
    bot.reply_to(m, "Bot Ready 🚀")

@bot.message_handler(commands=['price'])
def price_cmd(m):
    sym = extract_symbol(m.text)
    price = fetch_price(sym)
    bot.reply_to(m, f"{sym}: {price}")

@bot.message_handler(commands=['alert'])
def set_alert(m):
    try:
        parts = m.text.split()
        sym = extract_symbol(m.text)
        cond = parts[2]
        target = float(parts[3])

        add_alert(m.chat.id, sym, target, cond)
        bot.reply_to(m, "Alert set ✅")

    except:
        bot.reply_to(m, "Usage: /alert btc above 30000")

# ================== FLASK ==================
app = Flask(__name__)

@app.route("/")
def home():
    return "BOT RUNNING"

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        update = types.Update.de_json(request.data.decode("utf-8"))
        bot.process_new_updates([update])
    except Exception as e:
        print("WEBHOOK ERROR:", e)

    return "OK"

# ================== WEBHOOK SET ==================
def set_webhook():
    try:
        bot.remove_webhook()
        time.sleep(2)
        bot.set_webhook(url=WEBHOOK_URL)
        print("Webhook set ✅")
    except Exception as e:
        print("Webhook error:", e)

# ================== START ==================
def run():
    set_webhook()

    t = threading.Thread(target=alert_worker, daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

if __name__ == "__main__":
    run()