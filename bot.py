# LEGACY STANDALONE SCRIPT — not used by the deployment.
# render.yaml starts `python app.py`, which owns the Telegram token (long polling)
# and the real user table in DATABASE_URL. This file keeps its own throwaway
# `users.db` and sets a webhook, so it cannot see app.py users and the two cannot
# receive updates at the same time. Use `/broadcast` in app.py instead.
import os
import time
import sqlite3
import requests
import re
import websocket
import json
import time

from flask import Flask, request
import telebot
from telebot import types

# ================= CONFIG =================

TOKEN = os.environ.get("TELEGRAM_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

if not TOKEN:
    raise Exception("NO TOKEN")

bot = telebot.TeleBot(TOKEN, threaded=False, skip_pending=True)

DB = "users.db"

# ================= DB =================

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

def add_user(cid):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users VALUES (?)", (cid,))
    conn.commit()
    conn.close()

def get_users():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT chat_id FROM users")
    data = [x[0] for x in c.fetchall()]
    conn.close()
    return data

init_db()

# ================= MARKET =================


_cache = {}  # نخزن آخر سعر مؤقتًا

def get_price(symbol: str) -> float | None:
    symbol = symbol.lower()

    # ========= 1) كاش سريع =========
    if symbol in _cache:
        last_price, last_time = _cache[symbol]

        # إذا السعر حديث أقل من 3 ثواني نرجعه مباشرة
        if time.time() - last_time < 3:
            return last_price

    # ========= 2) WebSocket (السعر الحقيقي) =========
    url = f"wss://stream.binance.com:9443/ws/{symbol}usdt@trade"

    result = {"price": None}

    def on_message(ws, message):
        data = json.loads(message)
        price = float(data["p"])

        result["price"] = price
        _cache[symbol] = (price, time.time())

        ws.close()

    def on_error(ws, error):
        ws.close()

    try:
        ws = websocket.WebSocketApp(
            url,
            on_message=on_message,
            on_error=on_error
        )

        ws.run_forever()

    except:
        pass

    return result["price"]

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
        print("ERROR:", e)
    return "OK"

# ================= RESET =================

def clean_webhook():
    url = f"https://api.telegram.org/bot{TOKEN}"
    requests.get(url + "/deleteWebhook?drop_pending_updates=true")
    time.sleep(1)
    requests.get(url + f"/setWebhook?url={WEBHOOK_URL}")
    print("WEBHOOK CLEAN SET")

# ================= COMMANDS =================

@bot.message_handler(commands=["start"])
def start(m):
    add_user(m.chat.id)
    bot.reply_to(m, "Bot Ready 🚀")

@bot.message_handler(commands=["price"])
def price(m):
    sym = "BTC"
    txt = m.text.lower()

    if "eth" in txt:
        sym = "ETH"
    elif "sol" in txt:
        sym = "SOL"

    p = get_price(sym)

    if not p:
        bot.reply_to(m, "Error fetching price")
        return

    bot.reply_to(m, f"{sym} price: {p}")

# ================= BROADCAST =================

@bot.message_handler(commands=["broadcast"])
def broadcast(m):
    if m.chat.id != ADMIN_ID:
        bot.reply_to(m, "Not allowed")
        return

    text = m.text.replace("/broadcast", "").strip()
    users = get_users()

    if not text:
        bot.reply_to(m, "Usage: /broadcast <text>")
        return

    count = 0
    failed = 0
    for u in users:
        try:
            bot.send_message(u, text)
            count += 1
            time.sleep(0.05)
        except Exception as e:
            failed += 1
            print(f"BROADCAST FAILED chat_id={u} error={e}")

    bot.reply_to(m, f"Sent to {count}/{len(users)} (failed {failed})")

# ================= RUN =================

def run():
    print("STARTING...")

    clean_webhook()

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

if __name__ == "__main__":
    run()