import os
import time
import sqlite3
import threading
import io
import requests
from flask import Flask
import telebot
import google.generativeai as genai

# ضبط نظام الرسم البياني ليعمل بدون واجهة رسومية (مخصص للخوادم)
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# 1. التهيئة والمفاتيح
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

DB_NAME = "market_pro.db"

# 2. إنشاء وتجهيز قاعدة البيانات
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS signals 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, entry_price REAL, 
                  tp REAL, sl REAL, status TEXT, direction TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS lessons (id INTEGER PRIMARY KEY AUTOINCREMENT, lesson TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS risk_profiles 
                 (chat_id INTEGER PRIMARY KEY, capital REAL, risk_pct REAL)''')
    conn.commit()
    conn.close()

init_db()

# 3. محرك تحليل البيانات الفنية والمؤشرات الرياضية
def fetch_klines(symbol, interval="1h", limit=100):
    try:
        clean_symbol = symbol.upper().replace("/", "").replace("-", "")
        if not clean_symbol.endswith("USDT") and not clean_symbol.endswith("BUSD"):
            clean_symbol += "USDT"
        url = f"https://api.binance.com/api/v3/klines?symbol={clean_symbol}&interval={interval}&limit={limit}"
        res = requests.get(url, timeout=5).json()
        closes = [float(k[4]) for k in res]
        return closes
    except Exception:
        return None

def calculate_rsi(closes, period=14):
    if not closes or len(closes) < period + 1:
        return "N/A"
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(diff if diff > 0 else 0)
        losses.append(abs(diff) if diff < 0 else 0)
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def get_market_indicators(symbol):
    closes = fetch_klines(symbol)
    if not closes:
        return "ملاحظة: البيانات الفنية المباشرة للرمز غير متاحة حالياً."
    
    curr_price = closes[-1]
    rsi = calculate_rsi(closes)
    sma50 = round(sum(closes[-50:]) / 50, 2) if len(closes) >= 50 else "N/A"
    sma200 = round(sum(closes[-200:]) / 200, 2) if len(closes) >= 200 else "N/A"
    
    return f"📊 **بيانات السوق الفنية اللحظية لـ ({symbol.upper()}):**\n- السعر الحالي: {curr_price}\n- مؤشر القوة النسبية RSI: {rsi}\n- المتوسط المتحرك SMA(50): {sma50}\n- المتوسط المتحرك SMA(200): {sma200}"

# 4. توليد المخطط البياني (Chart Generator)
def generate_chart(symbol):
    closes = fetch_klines(symbol, limit=40)
    if not closes:
        return None
    
    plt.figure(figsize=(8, 4))
    plt.plot(closes, label=f"{symbol.upper()} Price", color='#00ff88', linewidth=2)
    plt.title(f"Market Trend: {symbol.upper()}", color='white')
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

# 5. إدارة قاعدة البيانات والذاكرة
def add_user(chat_id):
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

def get_learned_lessons():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT lesson FROM lessons")
    lessons = [f"- {row[0]}" for row in c.fetchall()]
    conn.close()
    return "\n".join(lessons) if lessons else "لا توجد أخطاء مسجلة، النطاق يعمل بالمعايير القياسية."

def save_auto_lesson(lesson_text):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO lessons (lesson) VALUES (?)", (lesson_text,))
    conn.commit()
    conn.close()

# 6. بناء تعليمات النظام لـ Gemini
def get_system_instruction(symbol_data=""):
    lessons = get_learned_lessons()
    return f"""
أنت خبير مالي ومحلل أسواق معتمد ومختص في التداول الفني وإدارة المخاطر.
تصميم وتطوير: المهندس إبراهيم المرقبي.

القواعد والأسس:
1. كن مقتصداً جداً ومباشراً في إجاباتك دون أي مقدمات.
2. نطاقك محدد حصراً بالمال، الأسهم، التداول، والعملات.
3. استند للبيانات الفنية التالية إن وجدت:
{symbol_data}

**الذاكرة الذاتية لتجنب الأخطاء السابقة:**
{lessons}
"""

MODELS_TO_TRY = ["gemini-3.6-flash", "gemini-2.5-flash", "gemini-1.5-flash-latest"]

def ask_gemini(prompt, symbol_data=""):
    for model_name in MODELS_TO_TRY:
        try:
            model = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=get_system_instruction(symbol_data)
            )
            response = model.generate_content(prompt)
            if response and response.text:
                return response.text
        except Exception:
            continue
    return "❌ تعذر تحليل السوق حالياً."

# 7. محرك التقييم وتتبع الصفقات الذاتي
def auto_learning_loop():
    while True:
        time.sleep(300)
        try:
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute("SELECT id, symbol, entry_price, tp, sl, direction FROM signals WHERE status = 'OPEN'")
            open_signals = c.fetchall()
            
            for sig in open_signals:
                sig_id, symbol, entry, tp, sl, direction = sig
                closes = fetch_klines(symbol, limit=1)
                if not closes:
                    continue
                curr_price = closes[-1]
                
                # النجاح
                if (direction == "BUY" and curr_price >= tp) or (direction == "SELL" and curr_price <= tp):
                    c.execute("UPDATE signals SET status = 'SUCCESS' WHERE id = ?", (sig_id,))
                    conn.commit()
                    for u in get_all_users():
                        bot.send_message(u, f"🎯 **صفقة ناجحة!**\nتحقق هدف الربح لـ {symbol} عند {curr_price}")
                
                # التعلم التلقائي عند الخسارة
                elif (direction == "BUY" and curr_price <= sl) or (direction == "SELL" and curr_price >= sl):
                    c.execute("UPDATE signals SET status = 'FAILED' WHERE id = ?", (sig_id,))
                    conn.commit()
                    lesson = ask_gemini(f"فشلت صفقة {symbol} عند {curr_price}. اكتب صياغة محددة جداً كقاعدة لمنع تكرار الخطأ.")
                    save_auto_lesson(f"تنبيه {symbol}: {lesson}")
                    for u in get_all_users():
                        bot.send_message(u, f"⚠️ **تحديث صفقة {symbol}:**\nوصل السعر لوقف الخسارة. استخرج البوت قاعدة جديدة ودونّها في شبكته العصبية لتجنب تكرارها.")
            conn.close()
        except Exception as e:
            print(f"Loop Error: {e}")

# 8. خادم Flask
app = Flask(__name__)

@app.route('/')
def home():
    return "MarketObserver Ultimate Bot Active"

def run_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# 9. الأوامر والرسائل
@bot.message_handler(commands=['start'])
def start_cmd(message):
    add_user(message.chat.id)
    bot.reply_to(
        message, 
        "أهلاً بك في منصة **MarketObserver Pro** 📈\n"
        "المطور: **المهندس إبراهيم المرقبي**\n\n"
        "**الأوامر المتاحة:**\n"
        "• أرسل اسم العملة/السهم للتحليل المباشر (مثل: BTC, ETH, NVDA).\n"
        "• `/chart <رمز>` : لحصول على الرسم البياني المباشر.\n"
        "• `/risk <رأس_المال> <نسبة_المخاطرة_%> <سعر_الدخول> <وقف_الخسارة>` : لحساب حجم اللوت وإدارة المخاطر."
    )

@bot.message_handler(commands=['chart'])
def chart_cmd(message):
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "⚠️ يرجى كتابة الرمز بعد الأمر، مثال: `/chart BTC`", parse_mode="Markdown")
        return
    symbol = args[1]
    bot.send_chat_action(message.chat.id, 'upload_photo')
    chart_img = generate_chart(symbol)
    if chart_img:
        bot.send_photo(message.chat.id, chart_img, caption=f"📈 **المخطط البياني المباشر لـ {symbol.upper()}**")
    else:
        bot.reply_to(message, "❌ تعذر تعقب البيانات البيانية لهذا الرمز.")

@bot.message_handler(commands=['risk'])
def risk_cmd(message):
    try:
        _, capital, risk_pct, entry, sl = message.text.split()
        cap, r_pct, ent, stop = float(capital), float(risk_pct), float(entry), float(sl)
        
        risk_amount = cap * (r_pct / 100.0)
        price_diff = abs(ent - stop)
        if price_diff == 0:
            bot.reply_to(message, "⚠️ سعر الدخول ووقف الخسارة متطابقان!")
            return
            
        pos_size = risk_amount / price_diff
        msg = (
            f"🛡️ **حاسبة إدارة المخاطر:**\n\n"
            f"- المبلغ المخاطر به: **{round(risk_amount, 2)}$** ({r_pct}%)\n"
            f"- حجم الموقف المقترح (Position Size): **{round(pos_size, 4)} وحدة**\n"
            f"💡 *التزم بحجم الموقف لحماية رأس مالك من تقلبات السوق.*"
        )
        bot.reply_to(message, msg, parse_mode="Markdown")
    except Exception:
        bot.reply_to(message, "⚠️ الصيغة خاطئة! استخدم:\n`/risk <Capital> <Risk%> <Entry> <SL>`\nمثال:\n`/risk 1000 2 65000 64000`", parse_mode="Markdown")

@bot.message_handler(content_types=['text'])
def handle_text(message):
    add_user(message.chat.id)
    try:
        bot.send_chat_action(message.chat.id, 'typing')
        symbol = message.text.strip().split()[0]
        indicators = get_market_indicators(symbol)
        
        reply_text = ask_gemini(message.text, symbol_data=indicators)
        
        full_response = f"{indicators}\n\n💡 **التحليل والتوصية:**\n{reply_text}"
        for chunk in [full_response[i:i+4000] for i in range(0, len(full_response), 4000)]:
            bot.reply_to(message, chunk, parse_mode="Markdown")
    except Exception as e:
        print(f"Error: {e}")

# 10. التشغيل
if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=auto_learning_loop, daemon=True).start()
    print("🚀 تم تشغيل البوت الاحترافي الشامل...")
    
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception as e:
            time.sleep(3)
