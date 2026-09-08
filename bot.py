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
print("TOKEN EXISTS:", bool(TELEGRAM_TOKEN))
print("WEBHOOK URL:", WEBHOOK_URL)

if not TELEGRAM_TOKEN or not WEBHOOK_URL:
    raise Exception("❌ Missing TELEGRAM_TOKEN or WEBHOOK_URL")

bot = telebot.TeleBot(
    TELEGRAM_TOKEN,
    threaded=False,
    skip_pending=True
)

DB_NAME = "market_pro.db"

# ================== SINGLE INSTANCE ==================
INSTANCE_FLAG = False

def single_instance_guard():
    global INSTANCE_FLAG
    if INSTANCE_FLAG:
        print("🚨 DUPLICATE INSTANCE - EXIT")
        os._exit(0)
    INSTANCE_FLAG = True

single_instance_guard()

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
    print("✅ DB READY")

init_db()

def save_user(chat_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT chat_id FROM users")
    users = [x[0] for x in c.fetchall()]
    conn.close()
    return users

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

# ================== MARKET DATA ==================
def fetch_binance(symbol):
    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": f"{symbol}USDT", "interval": "1h", "limit": 50}
        r = requests.get(url, params=params, timeout=5)
        if r.status_code == 200:
            data = r.json()
            return [float(c[4]) for c in data]
    except:
        pass
    return None

def fetch_bybit(symbol):
    try:
        url = "https://api.bybit.com/v5/market/kline"
        params = {"category": "spot", "symbol": f"{symbol}USDT", "interval": "60"}
        r = requests.get(url, params=params, timeout=5)
        data = r.json()
        return [float(c[4]) for c in data["result"]["list"]]
    except:
        pass
    return None

def fetch_yahoo(symbol):
    try:
        import yfinance as yf
        hist = yf.Ticker(f"{symbol}-USD").history(period="1d", interval="1h")
        return hist["Close"].tolist()
    except:
        pass
    return None

def fetch_price(symbol):
    for source in [fetch_binance, fetch_bybit, fetch_yahoo]:
        for _ in range(2):
            data = source(symbol)
            if data:
                print(f"✅ {symbol} FROM {source.__name__}")
                return data[-1]
            time.sleep(1)
    print("❌ ALL API FAILED:", symbol)
    return None

# ================== ALERT SYSTEM ==================
def alert_worker():
    print("🚀 ALERT WORKER STARTED")
    while True:
        try:
            alerts = get_alerts()

            for alert in alerts:
                alert_id, chat_id, symbol, target, cond = alert
                price = fetch_price(symbol)

                if not price:
                    continue

                if cond == "above" and price >= target:
                    bot.send_message(chat_id, f"🚨 {symbol} وصل {price}")
                    delete_alert(alert_id)

                elif cond == "below" and price <= target:
                    bot.send_message(chat_id, f"🚨 {symbol} نزل {price}")
                    delete_alert(alert_id)

        except Exception as e:
            print("ALERT ERROR:", e)

        time.sleep(30)

# ================== BOT ==================
def extract_symbol(text):
    for w in re.findall(r'\w+', text.lower()):
        if w in ASSETS:
            return ASSETS[w]
    return "BTC"

@bot.message_handler(commands=['start'])
def start(m):
    save_user(m.chat.id)
    bot.reply_to(m, "Bot Ready")

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
        condition = parts[2]
        target = float(parts[3])

        add_alert(m.chat.id, sym, target, condition)
        bot.reply_to(m, "✅ Alert set")

    except:
        bot.reply_to(m, "Usage: /alert btc above 30000")

# ================== FLASK ==================
app = Flask(__name__)

@app.route("/")
def home():
    return "RUNNING"

@app.route("/webhook", methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")
    update = types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK"

# ================== START ==================
def set_webhook():
    bot.remove_webhook()
    time.sleep(2)
    bot.set_webhook(url=WEBHOOK_URL)

def run():
    set_webhook()

    t = threading.Thread(target=alert_worker)
    t.daemon = True
    t.start()

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

if __name__ == "__main__":
    run()