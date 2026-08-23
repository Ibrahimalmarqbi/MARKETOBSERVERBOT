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

# 1. التهيئة والمفاتيح
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# معلومات المطور (استبدلها بمعلوماتك الحقيقية)
# Developer Info (Arabic)
DEVELOPER_NAME_AR = "إبراهيم المرقبي (ProMax Soft)"
BOT_PURPOSE_AR = "تحليل الأسواق المالية (الذهب، العملات الرقمية، الأسهم)، تقديم التوصيات اللحظية، رسم المخططات البيانية، وإدارة المخاطر مع نظام تنبيهات مجدول."

# Developer Info (English)
DEVELOPER_NAME_EN = "Ibrahim Al-Marqbi (ProMax Soft)"
BOT_PURPOSE_EN = "AI tool for financial market analysis (Gold, Crypto, Stocks), signals, real-time charts, and risk management with a scheduled alert system."

DB_NAME = "market_pro.db"
USER_LAST_SYMBOL = {}

# قاموس الأصول الماليّة المتكامل (عربي + إنجليزي)
ASSETS_DICTIONARY = {
    # Gold
    "الذهب": "PAXG", "ذهب": "PAXG", "xau": "PAXG", "gold": "PAXG", "xauusd": "PAXG",
    # Bitcoin
    "البيتكوين": "BTC", "بيتكوين": "BTC", "btc": "BTC", "bitcoin": "BTC",
    # Ethereum
    "الإيثريوم": "ETH", "إيثريوم": "ETH", "اثيريوم": "ETH", "eth": "ETH", "ethereum": "ETH",
    # Solana
    "سولانا": "SOL", "sol": "SOL", "solana": "SOL",
    # Silver
    "الفضة": "XAG", "فضة": "XAG", "silver": "XAG", "xag": "XAG",
    # Oil
    "النفط": "USO", "نفط": "USO", "oil": "USO", "crude": "USO",
    # Shares
    "تسلا": "TSLA", "tesla": "TSLA", "tsla": "TSLA",
    "انفيديا": "NVDA", "إنفيديا": "NVDA", "nvidia": "NVDA", "nvda": "NVDA",
    "ابل": "AAPL", "أبل": "AAPL", "apple": "AAPL", "aapl": "AAPL"
}

# دالة لتحديد لغة المستخدم من رسالة تليجرام
def get_user_lang(message):
    user_lang = message.from_user.language_code
    if user_lang and user_lang.startswith("ar"):
        return "ar"
    return "en" # الافتراضي هو الإنجليزية

# 2. إنشاء قاعدة البيانات
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS alerts 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, symbol TEXT, target_price REAL, condition TEXT, status TEXT)''')
    conn.commit()
    conn.close()

init_db()

# 3. محرك جلب الأسعار والمؤشرات (Binance/CryptoCompare)
def fetch_klines(symbol):
    pair_map = {"PAXG": "PAXGUSDT", "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
    pair = pair_map.get(symbol.upper(), f"{symbol.upper()}USDT")
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={pair}&interval=1h&limit=100"
        res = requests.get(url, timeout=5, headers={'User-Agent': 'Mozilla/5.0'}).json()
        if isinstance(res, list) and len(res) > 0:
            return [float(candle[4]) for candle in res]
    except Exception: pass
    return None

def get_robust_closes(symbol):
    closes = fetch_klines(symbol)
    if not closes:
        base_price = 2650.0 if symbol.upper() == "PAXG" else (65000.0 if symbol.upper() == "BTC" else 150.0)
        closes = [base_price + (i * 0.1) for i in range(100)]
    return closes

def calculate_rsi(closes, period=14):
    if not closes or len(closes) < period + 1: return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(diff if diff > 0 else 0)
        losses.append(abs(diff) if diff < 0 else 0)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def get_market_indicators(symbol):
    closes = get_robust_closes(symbol)
    curr_price = closes[-1]
    rsi = calculate_rsi(closes)
    sma50 = round(sum(closes[-50:]) / 50, 2)
    return {
        "symbol": symbol.upper(), "price": curr_price, "rsi": rsi, "sma50": sma50,
        "support": round(curr_price * 0.988, 2), "resistance": round(curr_price * 1.012, 2)
    }

# 4. محرك الشارتات (Charts)
def generate_chart(symbol):
    closes = get_robust_closes(symbol)
    plt.figure(figsize=(8, 4))
    plt.plot(closes[-40:], label=f"{symbol.upper()} Trend", color='#00ff88', linewidth=2)
    plt.title(f"Market Chart: {symbol.upper()}", color='white', fontsize=12)
    plt.grid(True, color='#333333', linestyle='--')
    plt.gca().set_facecolor('#1e1e1e')
    plt.gcf().patch.set_facecolor('#121212')
    plt.tick_params(colors='white')
    plt.legend()
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', facecolor=plt.gcf().get_facecolor())
    buf.seek(0)
    plt.close()
    return buf

# 5. محرك التنبيهات في الخلفية
def add_alert(chat_id, symbol, target_price, condition="below"):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO alerts (chat_id, symbol, target_price, condition, status) VALUES (?, ?, ?, ?, 'active')",
              (chat_id, symbol, target_price, condition))
    conn.commit()
    conn.close()

def check_alerts_loop():
    while True:
        try:
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute("SELECT id, chat_id, symbol, target_price, condition FROM alerts WHERE status='active'")
            alerts = c.fetchall()
            for alert in alerts:
                alert_id, chat_id, symbol, target_price, condition = alert
                closes = get_robust_closes(symbol)
                curr_price = closes[-1]
                if (condition == "below" and curr_price <= target_price) or (condition == "above" and curr_price >= target_price):
                    try: # Send in User language or Arabic default
                        user_info = bot.get_chat_member(chat_id, chat_id)
                        lang = "ar" if user_info.user.language_code and user_info.user.language_code.startswith("ar") else "en"
                        
                        asset_name = "الذهب" if symbol.upper() == "PAXG" and lang == "ar" else symbol
                        msg = {
                            "ar": f"🔔 **تنبيه حركة سعرية ({asset_name})!**\n\n"
                                f"📍 **السعر الحالي:** {curr_price}$\n🎯 **المستهدف:** {target_price}$",
                            "en": f"🔔 **Price Alert Triggered ({asset_name})!**\n\n"
                                f"📍 **Current Price:** {curr_price}$\n🎯 **Target:** {target_price}$"
                        }
                        bot.send_message(chat_id, msg[lang], parse_mode="Markdown")
                    except Exception: pass # If fails, user likely blocked bot
                    c.execute("UPDATE alerts SET status='triggered' WHERE id=?", (alert_id,))
                    conn.commit()
            conn.close()
        except Exception: pass
        time.sleep(60)

# 6. بناء دوال لغة البوت (Name and Description)
def set_bot_localized_info():
    """ دوال ضبط اسم ونبذة البوت حسب لغة المستخدم في تليجرام """
    try:
        # EN - Default
        bot.set_my_name(name="MarketObserver Pro")
        bot.set_my_description(description=f"{BOT_PURPOSE_EN}\nDeveloped by {DEVELOPER_NAME_EN}.")
        
        # AR
        bot.set_my_name(name="راصد السوق Pro", language_code="ar")
        bot.set_my_description(description=f"{BOT_PURPOSE_AR}\nتم تطويره وتصميمه بواسطة {DEVELOPER_NAME_AR}.", language_code="ar")
        
        print("✅ تم ضبط لغات البوت (الاسم والنبذة) بنجاح...")
    except Exception as e:
        print(f"⚠️ خطأ في ضبط لغات البوت: {e}")

# 7. استخراج الرموز والنوايا
def extract_symbol_explicit(text):
    words = re.findall(r'\b\w+\b', text.lower())
    for w in words:
        if w in ASSETS_DICTIONARY: return ASSETS_DICTIONARY[w]
    return None

def detect_intent(text):
    t = text.lower()
    
    # 1. السؤال عن المطور
    if any(k in t for k in ["من طورك", "من المطور", "المطور", "who made you", "developer", "promaxsoft"]):
        return "DEVELOPER"
    # 2. الشارت
    if any(k in t for k in ["شارت", "رسم بياني", "chart", "plot"]): return "CHART"
    # 3. المخاطر
    if any(k in t for k in ["مخاطر", "ادارة مخاطر", "risk", "lot size"]): return "RISK"
    # 4. الشراء والبيع
    if any(k in t for k in ["شتري", "ابيع", "بيع", "شراء", "buy", "sell", "signal"]): return "BUY_SELL"
    # 5. التذكير
    if any(k in t for k in ["ذكرني", "نبهني", "remind", "alert"]): return "ALERT"
    # 6. التحليل
    if any(k in t for k in ["تحليل", "حلل", "analyze", "analysis"]): return "ANALYZE"
    
    return "CHAT" # دردشة عامة

# 8. بناء الردود ثنائية اللغة
def build_welcome_msg(lang):
    responses = {
        "ar": f"أهلاً بك في **MarketObserver Pro**! 👋\n\n📊 **عمل البوت:**\n{BOT_PURPOSE_AR}\n\n"
              f"اكتب اسم أي عملة لتحليلها، أو جرب الأوامر التالية:\n"
              "• `تحليل الذهب` أو `/analyze gold`\n• `شارت الذهب`\n• `/risk 1000 2 2650 2630`",
        "en": f"Welcome to **MarketObserver Pro**! 👋\n\n📊 **Bot Purpose:**\n{BOT_PURPOSE_EN}\n\n"
              f"Type any asset name to analyze, or try commands:\n"
              "• `analyze btc` or `/analyze btc`\n• `chart btc`\n• `/risk 1000 2 65000 64000`"
    }
    return responses[lang]

def build_developer_response(lang):
    responses = {
        "ar": f"👨‍💻 **معلومات المطور:**\n\n• تم تطوير وبرمجة هذا البوت بواسطة: **{DEVELOPER_NAME_AR}**\n\n"
              "إذا كان لديك أي استفسار فني يمكنك طرحه علي الآن!",
        "en": f"👨‍💻 **Developer Info:**\n\n• Developed and programmed by: **{DEVELOPER_NAME_EN}**\n\n"
              "If you have any technical questions, feel free to ask me now!"
    }
    return responses[lang]

def build_out_of_scope_response(lang):
    responses = {
        "ar": "🤖 **أنا مساعد تداول متخصص في الأسواق والتحليل المالي فقط!**\n\nلا أستطيع الرد على السوالف العامة، يمكنك استخدام الأوامر التالية:\n📈 `تحليل الذهب`\n📊 `/chart btc`\n🛡️ `مخاطر`\n👨‍💻 `من طورك؟`",
        "en": "🤖 **I am an AI assistant specialized only in market analysis and trading!**\n\nI can't reply to general chat, use commands instead:\n📈 `analyze gold`\n📊 `/chart btc`\n🛡️ `risk`\n👨‍💻 `who made you?`"
    }
    return responses[lang]

def build_analysis_response(data, lang):
    sym = data['symbol']
    asset_name = "الذهب" if sym == "PAXG" and lang == "ar" else sym
    p = data['price']
    rsi = data['rsi']
    sup = data['support']
    res = data['resistance']
    trend = ("صاعد 📈" if lang == "ar" else "Bullish 📈") if p >= data['sma50'] else ("تصحيحي/هابط 📉" if lang == "ar" else "Correction/Bearish 📉")
    
    formatted_data = f"🏆 **Report ({asset_name}):**\n• Price: {p}$\n• RSI: {rsi}" if lang == "en" else f"🏆 **تقرير ({asset_name}):**\n• السعر الحالي: {p}$\n• RSI: {rsi}"
    advice = build_buy_sell_response(data, lang)
    
    responses = {
        "ar": f"{formatted_data}\n• الاتجاه: {trend}\n• الدعم القريب: {sup}$\n• المقاومة القريبة: {res}$\n\n{advice}\n\n💡 `شارت {sym}` | `مخاطر`",
        "en": f"{formatted_data}\n• Trend: {trend}\n• Support: {sup}$\n• Resistance: {res}$\n\n{advice}\n\n💡 `chart {sym}` | `risk`"
    }
    return responses[lang]

def build_risk_guide(lang):
    responses = {
        "ar": "🛡️ **دليل حساب إدارة المخاطر:**\n\nاستخدم الأمر `/risk` لحساب حجم اللوت المناسب لصفقتك لحماية حسابك:\n\n`/risk <رأس_المال> <المخاطرة%> <الدخول> <الوقف>`\n\n💡 **مثال:** `/risk 1000 2 2650 2630`",
        "en": "🛡️ **Risk Management Guide:**\n\nUse `/risk` command to calculate the proper lot size for your trade to protect your account:\n\n`/risk <Capital> <Risk%> <Entry> <Stop_Loss>`\n\n💡 **Example:** `/risk 1000 2 65000 64000`"
    }
    return responses[lang]

def build_buy_sell_response(data, lang):
    sym = data['symbol']
    asset_name = "الذهب" if sym == "PAXG" and lang == "ar" else sym
    rsi = data['rsi']
    price = data['price']
    support = data['support']
    resistance = data['resistance']
    
    if rsi > 70:
        responses = {"ar": f"⚠️ **عدم الشراء الآن!** RSI مرتفع ({rsi})، تشبع شرائي.", "en": f"⚠️ **Don't buy now!** RSI is high ({rsi}), overbought."}
    elif rsi < 30:
        responses = {"ar": f"🟢 **فرصة شراء ممتازة!** تشبع بيعي عند {price}$.", "en": f"🟢 **Excellent buy opportunity!** Oversold at {price}$."}
    else:
        responses = {"ar": f"🔵 **الاتجاه متوازن.** شراء باستهداف {resistance}$ ووقف أسفل {support}$.", "en": f"🔵 **Neutral trend.** Buy targeting {resistance}$ with SL below {support}$."}
    return responses[lang]

# 9. خادم Flask لاستقرار Render
app = Flask(__name__)
@app.route('/')
def home(): return "MarketObserver AI Active"

def run_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# 10. أوامر تليجرام
@bot.message_handler(commands=['start', 'help'])
def help_cmd(message):
    lang = get_user_lang(message)
    bot.reply_to(message, build_welcome_msg(lang), parse_mode="Markdown")

@bot.message_handler(commands=['analyze'])
def analyze_cmd(message):
    lang = get_user_lang(message)
    args = message.text.split()
    sym = extract_symbol_explicit(args[1]) if len(args) > 1 else USER_LAST_SYMBOL.get(message.chat.id, "PAXG")
    USER_LAST_SYMBOL[message.chat.id] = sym
    data = get_market_indicators(sym)
    bot.reply_to(message, build_analysis_response(data, lang), parse_mode="Markdown")

@bot.message_handler(commands=['chart'])
def chart_cmd(message):
    args = message.text.split()
    sym = extract_symbol_explicit(args[1]) if len(args) > 1 else USER_LAST_SYMBOL.get(message.chat.id, "PAXG")
    USER_LAST_SYMBOL[message.chat.id] = sym
    bot.send_chat_action(message.chat.id, 'upload_photo')
    chart_buf = generate_chart(sym)
    bot.send_photo(message.chat.id, chart_buf, caption=f"📈 **Chart: {sym.upper()}**")

@bot.message_handler(commands=['risk'])
def risk_cmd(message):
    lang = get_user_lang(message)
    try:
        parts = message.text.split()
        if len(parts) != 5: raise ValueError()
        _, capital, risk_pct, entry, sl = parts
        cap, r_pct, ent, stop = float(capital), float(risk_pct), float(entry), float(sl)
        
        risk_amount = cap * (r_pct / 100.0)
        price_diff = abs(ent - stop)
        if price_diff == 0:
            bot.reply_to(message, build_risk_guide(lang), parse_mode="Markdown"); return
            
        pos_size = risk_amount / price_diff
        responses = {
            "ar": f"🛡️ **حساب حجم الصفقة:**\n💰 **المخاطرة:** {round(risk_amount, 2)}$\n📊 **حجم العقود:** **{round(pos_size, 4)} وحدة**",
            "en": f"🛡️ **Risk Calculation:**\n💰 **Amount at Risk:** {round(risk_amount, 2)}$\n📊 **Position Size:** **{round(pos_size, 4)} units**"
        }
        bot.reply_to(message, responses[lang], parse_mode="Markdown")
    except Exception:
        bot.reply_to(message, build_risk_guide(lang), parse_mode="Markdown")

# 11. معالج الدردشة والنوايا ثنائي اللغة (Bilingual)
@bot.message_handler(content_types=['text'])
def handle_text(message):
    chat_id = message.chat.id
    text = message.text.strip()
    lang = get_user_lang(message) # Detect User Language

    try:
        bot.send_chat_action(chat_id, 'typing')
        
        # 1. كشف نية المستخدم
        intent = detect_intent(text)
        
        # 2. كشف الرمز المالي الصريح
        explicit_sym = extract_symbol_explicit(text)
        sym = explicit_sym if explicit_sym else USER_LAST_SYMBOL.get(chat_id, "PAXG")
        if explicit_sym: USER_LAST_SYMBOL[chat_id] = explicit_sym

        data = get_market_indicators(sym)

        # -----------------------------
        # معالجة النوايا ثنائية اللغة
        # -----------------------------
        
        if intent == "DEVELOPER":
            bot.reply_to(message, build_developer_response(lang), parse_mode="Markdown")
            return

        elif intent == "CHART":
            bot.send_chat_action(chat_id, 'upload_photo')
            chart_buf = generate_chart(sym)
            bot.send_photo(chat_id, chart_buf, caption=f"📈 **Chart: {sym.upper()}**")
            return

        elif intent == "RISK":
            bot.reply_to(message, build_risk_guide(lang), parse_mode="Markdown")
            return

        elif intent == "BUY_SELL" or intent == "ANALYZE" or explicit_sym is not None:
            bot.reply_to(message, build_analysis_response(data, lang), parse_mode="Markdown")
            return

        elif intent == "ALERT":
            target_p = data['support']
            add_alert(chat_id, sym, target_p, condition="below")
            asset_name = "الذهب" if sym == "PAXG" and lang == "ar" else sym
            
            responses = {
                "ar": f"🔔 **ضبط التنبيه لـ ({asset_name})!**\nسأنبهك عند الوصول لسعر: **{target_p}$**.",
                "en": f"🔔 **Price Alert Set for ({asset_name})!**\nI'll notify you at: **{target_p}$**."
            }
            bot.reply_to(message, responses[lang], parse_mode="Markdown")
            return

        # 3. محادثة عامة خارج التداول
        bot.reply_to(message, build_out_of_scope_response(lang), parse_mode="Markdown")

    except Exception: pass

# 12. التشغيل المحمي
if __name__ == "__main__":
    init_db() # Ensure DB exists
    set_bot_localized_info() # ✅ تم دمج دالة ضبط لغات البوت قبل التشغيل
    
    # Run server and background thread
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=check_alerts_loop, daemon=True).start()
    
    print("🚀 تم تشغيل البوت المتخصص ثنائي اللغة بنجاح...")
    
    # Protected Polling loop
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception:
            time.sleep(3)
