import os
import time
import sqlite3
import threading
import io
import re
import requests
from flask import Flask
import telebot

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# ================== CONFIG ==================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
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

# ================== SAVE USERS ==================
def save_user(chat_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()

# ================== API LAYER ==================
def fetch_klines(symbol):
    symbol = symbol.upper()

    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": f"{symbol}USDT", "interval": "1h", "limit": 100}
        res = requests.get(url, params=params, timeout=5).json()

        if isinstance(res, list) and len(res) > 0:
            return [float(c[4]) for c in res]
    except:
        pass

    try:
        url = "https://min-api.cryptocompare.com/data/v2/histohour"
        params = {"fsym": symbol, "tsym": "USD", "limit": 100}
        res = requests.get(url, params=params, timeout=5).json()

        data = res.get("Data", {}).get("Data", [])
        if data:
            return [float(c["close"]) for c in data]
    except:
        pass

    try:
        cg = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}
        if symbol in cg:
            url = f"https://api.coingecko.com/api/v3/coins/{cg[symbol]}/market_chart"
            params = {"vs_currency": "usd", "days": "2"}

            res = requests.get(url, params=params, timeout=5).json()
            prices = res.get("prices", [])

            if prices:
                return [p[1] for p in prices[-100:]]
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

    if not closes or len(closes) < 20:
        return None

    price = closes[-1]
    rsi = calculate_rsi(closes)
    sma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else price

    return {
        "symbol": symbol,
        "price": price,
        "rsi": rsi,
        "sma50": sma50,
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

# ================== ALERT SYSTEM ==================
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

            c.execute("""
            SELECT id, chat_id, symbol, target_price, condition 
            FROM alerts WHERE status='active'
            """)

            alerts = c.fetchall()

            for a in alerts:
                id_, chat_id, symbol, target, cond = a

                data = get_market_indicators(symbol)
                if not data:
                    continue

                price = data["price"]

                if (cond == "below" and price <= target) or (cond == "above" and price >= target):
                    try:
                        bot.send_message(chat_id, f"🔔 {symbol} hit {price}")
                    except:
                        pass

                    c.execute("UPDATE alerts SET status='done' WHERE id=?", (id_,))
                    conn.commit()

            conn.close()

        except:
            pass

        time.sleep(30)

# ================== TELEGRAM ==================
def extract_symbol(text):
    for w in re.findall(r'\b\w+\b', text.lower()):
        if w in ASSETS_DICTIONARY:
            return ASSETS_DICTIONARY[w]
    return "BTC"

@bot.message_handler(commands=['start'])
def start(m):
    save_user(m.chat.id)
    bot.reply_to(m, "Bot ready")

@bot.message_handler(commands=['analyze'])
def analyze(m):
    sym = extract_symbol(m.text)
    data = get_market_indicators(sym)

    if not data:
        bot.reply_to(m, "❌ لا يوجد بيانات حالياً")
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
        bot.reply_to(m, "❌ لا يوجد بيانات")
        return

    add_alert(m.chat.id, sym, data["support"], "below")
    bot.reply_to(m, "Alert set")

@bot.message_handler(commands=['chart'])
def chart(m):
    sym = extract_symbol(m.text)
    img = generate_chart(sym)

    if not img:
        bot.reply_to(m, "❌ لا يوجد بيانات للشارت")
        return

    bot.send_photo(m.chat.id, img)

@bot.message_handler(commands=['broadcast'])
def broadcast_cmd(m):
    ADMIN_ID = 840153842

if m.from_user.id != ADMIN_ID:
    return
        
    msg = m.text.replace("/broadcast", "").strip()
    broadcast(msg)
    bot.reply_to(m, "تم الإرسال للجميع")



@bot.message_handler(commands=['testusers'])
def test_users(m):
    users = get_all_users()
    bot.reply_to(m, str(users))






# ================== FLASK ==================
app = Flask(__name__)

@app.route("/")
def home():
    return "Running"

def run_server():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
#====================GET ALL THE USERS=====
def get_all_users():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT chat_id FROM users")
    users = [row[0] for row in c.fetchall()]
    conn.close()
    return users
#=================BRODCAST FOR MESSAGES FROM ADMIN=======
def broadcast(message):
    users = get_all_users()

    for chat_id in users:
        try:
            bot.send_message(chat_id, message)
            time.sleep(0.05)  # حماية ضد flood
        except Exception as e:
            print(f"Failed for {chat_id}: {e}")


# ================== RUN ==================
if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=check_alerts, daemon=True).start()

    print("Bot started")

    while True:
        try:
            bot.polling(none_stop=True)
        except:
            time.sleep(3)