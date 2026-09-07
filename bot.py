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
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # https://your-app.onrender.com/webhook

bot = telebot.TeleBot(TELEGRAM_TOKEN)
DB_NAME = "market_pro.db"

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

    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY
    )
    """)

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

# ================== API ==================
def fetch_klines(symbol):
    symbol = symbol.upper()

    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": f"{symbol}USDT", "interval": "1h", "limit": 100}
        res = requests.get(url, params=params, timeout=5).json()

        if isinstance(res, list):
            return [float(c[4]) for c in res]
    except:
        pass

    return None

# ================== INDICATORS ==================
def calculate_rsi(closes, period=14):
    if not closes or len(closes) < period + 1:
        return 50

    gains = []
    losses = []

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

def get_market_indicators(symbol):
    closes = fetch_klines(symbol)

    if not closes:
        return None

    price = closes[-1]
    rsi = calculate_rsi(closes)

    return {
        "symbol": symbol,
        "price": price,
        "rsi": rsi,
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

# ================== ALERTS ==================
def add_alert(chat_id, symbol, price, condition):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    c.execute("""
    INSERT INTO alerts VALUES (NULL, ?, ?, ?, ?, 'active')
    """, (chat_id, symbol, price, condition))

    conn.commit()
    conn.close()

def check_alerts():
    while True:
        try:
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()

            c.execute("SELECT id, chat_id, symbol, target_price, condition FROM alerts WHERE status='active'")
            alerts = c.fetchall()

            for a in alerts:
                id_, chat_id, symbol, target, cond = a

                data = get_market_indicators(symbol)
                if not data:
                    continue

                price = data["price"]

                if (cond == "below" and price <= target) or (cond == "above" and price >= target):
                    bot.send_message(chat_id, f"🔔 {symbol} hit {price}")

                    c.execute("UPDATE alerts SET status='done' WHERE id=?", (id_,))
                    conn.commit()

            conn.close()

        except Exception as e:
            print("check_alerts error:", e)

        time.sleep(30)

# ================== SYMBOL PARSER ==================
def extract_symbol(text):
    for w in re.findall(r'\b\w+\b', text.lower()):
        if w in ASSETS_DICTIONARY:
            return ASSETS_DICTIONARY[w]
    return "BTC"

# ================== TELEGRAM HANDLERS ==================
@bot.message_handler(commands=['start'])
def start(m):
    save_user(m.chat.id)
    bot.reply_to(m, "Bot ready")

@bot.message_handler(commands=['analyze'])
def analyze(m):
    sym = extract_symbol(m.text)
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

@bot.message_handler(commands=['alert'])
def alert_cmd(m):
    sym = extract_symbol(m.text)
    data = get_market_indicators(sym)

    if not data:
        bot.reply_to(m, "❌ no data")
        return

    add_alert(m.chat.id, sym, data["support"], "below")
    bot.reply_to(m, "Alert set")

@bot.message_handler(commands=['chart'])
def chart(m):
    sym = extract_symbol(m.text)
    img = generate_chart(sym)

    if not img:
        bot.reply_to(m, "❌ no chart")
        return

    bot.send_photo(m.chat.id, img)

@bot.message_handler(commands=['broadcast'])
def broadcast_cmd(m):
    ADMIN_ID = 840153842

    if m.from_user.id != ADMIN_ID:
        return

    msg = m.text.replace("/broadcast", "").strip()
    broadcast(msg)
    bot.reply_to(m, "sent")

# ================== BROADCAST ==================
def broadcast(message):
    users = get_all_users()

    for chat_id in users:
        try:
            bot.send_message(chat_id, message)
            time.sleep(0.05)
        except Exception as e:
            print("broadcast error:", e)

# ================== FLASK ==================
app = Flask(__name__)

@app.route("/")
def home():
    return "Running"

@app.route("/webhook", methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")
    update = types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK"

# ================== SET WEBHOOK ==================
def set_bot_webhook():
    if WEBHOOK_URL:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=WEBHOOK_URL)
        print("Webhook set:", WEBHOOK_URL)

# ================== RUN ==================
def run_server():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=check_alerts, daemon=True).start()

    set_bot_webhook()

    print("Bot running via webhook")