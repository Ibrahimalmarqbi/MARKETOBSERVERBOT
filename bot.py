import os
import time
import sqlite3
import threading
import io
import requests
from flask import Flask
import telebot
import google.generativeai as genai

# ضبط نظام الرسم البياني ليعمل بدون واجهة رسومية على الخوادم
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# 1. المفاتيح والتهيئة
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)

if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"Gemini Config Error: {e}")

DB_NAME = "market_pro.db"

# قاموس واسع لربط المصطلحات العربية بأكواد التداول العالمية
ARABIC_ASSETS = {
    "الذهب": "PAXG",
    "ذهب": "PAXG",
    "سهم الذهب": "PAXG",
    "معدن الذهب": "PAXG",
    "xau": "PAXG",
    "الفضة": "XAG",
    "فضة": "XAG",
    "النفط": "USO",
    "نفط": "USO",
    "البيتكوين": "BTC",
    "بيتكوين": "BTC",
    "الإيثريوم": "ETH",
    "إيثريوم": "ETH",
    "اثيريوم": "ETH",
    "سولانا": "SOL",
    "سول": "SOL",
    "تسلا": "TSLA",
    "انفيديا": "NVDA",
    "إنفيديا": "NVDA",
    "ابل": "AAPL",
    "أبل": "AAPL"
}

# 2. إنشاء قاعدة البيانات
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

# 3. محرك جلب الأسعار المباشرة والمؤشرات الفنية
def fetch_klines(symbol, limit=100):
    try:
        clean_symbol = symbol.upper().replace("/", "").replace("-", "").replace("USDT", "")
        url = f"https://min-api.cryptocompare.com/data/v2/histo/hour?fsym={clean_symbol}&tsym=USDT&limit={limit}"
        res = requests.get(url, timeout=6).json()
        
        if 'Data' in res and 'Data' in res['Data'] and 'Data' in res['Data']:
            closes = [float(item['close']) for item in res['Data']['Data']]
            return closes if closes else None
        return None
    except Exception as e:
        print(f"Fetch Error for {symbol}: {e}")
        return None

def calculate_rsi(closes, period=14):
    if not closes or len(closes) < period + 1:
        return 50.0
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
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def get_market_indicators(symbol):
    closes = fetch_klines(symbol)
    if not closes:
        return None, ""
    
    curr_price = closes[-1]
    rsi = calculate_rsi(closes)
    sma50 = round(sum(closes[-50:]) / 50, 2) if len(closes) >= 50 else round(curr_price, 2)
    sma200 = round(sum(closes[-200:]) / 200, 2) if len(closes) >= 200 else round(curr_price, 2)
    
    data_dict = {
        "price": curr_price,
        "rsi": rsi,
        "sma50": sma50,
        "sma200": sma200
    }
    
    formatted_str = f"📊 **البيانات الفنية اللحظية لـ ({symbol.upper()}):**\n• السعر الحالي: **{curr_price}$**\n• مؤشر RSI: **{rsi}**\n• المتوسط SMA(50): **{sma50}$**\n• المتوسط SMA(200): **{sma200}$**"
    return data_dict, formatted_str

# 4. محرك توليد التحليل الفني البرمجي المستقل (في حال تعثر الذكاء الاصطناعي)
def generate_programmatic_analysis(symbol, data):
    if not data:
        return "⚠️ تعذر جلب بيانات السوق الحالية للرمز المطلوب. يرجى التثبت من الرمز وإعادة المحاولة."
        
    price = data['price']
    rsi = data['rsi']
    sma50 = data['sma50']
    
    trend = "صاعد 📈" if price >= sma50 else "هابط/تصحيحي 📉"
    rsi_status = "تشبع شرائي (احتمال تصحيح)" if rsi > 70 else ("تشبع بيعي (فرصة ارتداد)" if rsi < 30 else "محايد ومستقر")
    
    support = round(price * 0.985, 2)
    resistance = round(price * 1.015, 2)
    
    asset_name = "الذهب" if symbol == "PAXG" else symbol.upper()
    
    report = (
        f"🏆 **تقرير التحليل الفني الشامل لـ {asset_name}:**\n\n"
        f"• **الاتجاه العام اللحظي:** {trend}\n"
        f"• **حالة مؤشر القوة النسبية (RSI):** {rsi_status} ({rsi})\n"
        f"• **مستوى الدعم القريب:** {support}$\n"
        f"• **مستوى المقاومة القريب:** {resistance}$\n\n"
        f"💡 **التوصية والرؤية الفنية:**\n"
        f"يتداول {asset_name} حالياً عند مستوى {price}$. يُفضل مراقبة مستوى الدعم عند {support}$ ومستوى المقاومة عند {resistance}$. "
        f"تذكر دائماً الالتزام بإدارة المخاطر وتحديد أمر وقف الخسارة قبل فتح أي صفقة."
    )
    return report

# 5. توليد المخطط البياني (Chart)
def generate_chart(symbol):
    closes = fetch_klines(symbol, limit=40)
    if not closes:
        return None
    
    plt.figure(figsize=(8, 4))
    plt.plot(closes, label=f"{symbol.upper()} Trend", color='#00ff88', linewidth=2)
    plt.title(f"Price Chart: {symbol.upper()}", color='white')
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

# 6. التعامل مع قاعدة البيانات والذاكرة
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
    return "\n".join(lessons) if lessons else "الالتزام بقواعد إدارة المخاطر."

def save_auto_lesson(lesson_text):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO lessons (lesson) VALUES (?)", (lesson_text,))
    conn.commit()
    conn.close()

# 7. بناء التوجيهات لمحرك Gemini
def get_system_instruction(symbol_data_str=""):
    lessons = get_learned_lessons()
    return f"""
أنت محلل مالي واقتصادي ومخاطر ذكي وخبير في الأسواق والعملات والمعادن.
تم تطوير هذا النظام بواسطة المهندس إبراهيم المرقبي.

توجيهات الإجابة:
1. أجب بدقة وذكاء وبصيغة خبير مالي حقيقي ومباشر.
2. إذا طلب المستخدم تحليل أصل مالي (مثل الذهب، النفط، البيتكوين)، قدم له تحليلاً فنياً واقتصادياً شاملاً متضمناً الدعم والمقاومة والاتجاه والتوصية المالية.
3. إذا أرسل تحية (مثل: "كيفك"، "مرحبا")، رد بأسلوب ودود ووضح له خدماتك.
4. يمنع كتابة أي كود برمجي للمستخدمين.

بيانات السوق الحالية:
{symbol_data_str}

الدروس السابقة:
{lessons}
"""

def ask_gemini(prompt, symbol_data_raw=None, symbol_data_str=""):
    models = ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.0-flash"]
    sys_inst = get_system_instruction(symbol_data_str)
    
    if GEMINI_API_KEY:
        for model_name in models:
            try:
                try:
                    model = genai.GenerativeModel(model_name=model_name, system_instruction=sys_inst)
                    res = model.generate_content(prompt)
                except Exception:
                    model = genai.GenerativeModel(model_name=model_name)
                    full_prompt = f"{sys_inst}\n\nطلب المستخدم: {prompt}"
                    res = model.generate_content(full_prompt)
                    
                if res and res.text and len(res.text.strip()) > 0:
                    return res.text
            except Exception as e:
                print(f"Gemini Error ({model_name}): {e}")
                continue

    # في حال تعثر الذكاء الاصطناعي: نولد تحليلاً برمجياً حقيقياً ومباشراً للرمز
    if symbol_data_raw:
        return generate_programmatic_analysis(symbol_data_raw.get("symbol", "PAXG"), symbol_data_raw)
    
    return "أهلاً بك! كيف يمكنني مساعدتك اليوم في تحليل العملات والمعادن أو إدارة مخاطر صفقاتك؟"

# 8. محرك التتبع والتعلم التلقائي
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
                
                if (direction == "BUY" and curr_price >= tp) or (direction == "SELL" and curr_price <= tp):
                    c.execute("UPDATE signals SET status = 'SUCCESS' WHERE id = ?", (sig_id,))
                    conn.commit()
                    for u in get_all_users():
                        bot.send_message(u, f"🎯 **صفقة ناجحة!**\nتم تحقيق هدف الربح لـ {symbol} عند سعر {curr_price}$")
                
                elif (direction == "BUY" and curr_price <= sl) or (direction == "SELL" and curr_price >= sl):
                    c.execute("UPDATE signals SET status = 'FAILED' WHERE id = ?", (sig_id,))
                    conn.commit()
                    lesson = ask_gemini(f"فشلت صفقة {symbol} عند سعر {curr_price}$. اكتب قاعدة فنّية لمنع هذا الخطأ مستقبلاً.")
                    save_auto_lesson(f"تنبيه صفقة {symbol}: {lesson}")
                    for u in get_all_users():
                        bot.send_message(u, f"⚠️ **تحديث صفقة {symbol}:**\nوصل السعر لوقف الخسارة. تم تحليل السبب وإضافة درس جديد لشبكة البوت.")
            conn.close()
        except Exception as e:
            print(f"Loop Error: {e}")

# 9. خادم Flask لاستقرار Render
app = Flask(__name__)

@app.route('/')
def home():
    return "MarketObserver Pro Engine Active"

def run_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# 10. معالجة الأوامر والرسائل

@bot.message_handler(commands=['start'])
def start_cmd(message):
    add_user(message.chat.id)
    bot.reply_to(
        message, 
        "أهلاً بك في منصة **MarketObserver Pro** 📈\n"
        "المطور والمصمم: **المهندس إبراهيم المرقبي**\n\n"
        "💡 **كيفية الاستخدام:**\n"
        "• اكتب اسم أي عملة أو سهم أو معدن لتحليله فوراً (مثال: `اريد تحليل الذهب`، `BTC`، `البيتكوين`).\n"
        "• `/chart <الرمز>` : لطلب رسم بياني مباشر (مثال: `/chart BTC` أو `/chart PAXG`).\n"
        "• `/risk <رأس_المال> <نسبة_المخاطرة_%> <سعر_الدخول> <وقف_الخسارة>` : لحساب حجم اللوت المسموح."
    )

@bot.message_handler(commands=['chart'])
def chart_cmd(message):
    args = message.text.split()
    if len(args) < 2:
        guide_msg = (
            "📈 **دليل استخدام أمر الرسم البياني (`/chart`):**\n\n"
            "✏️ **طريقة الاستخدام:** اكتب الأمر متبوعاً برمز العملة.\n"
            "💡 **أمثلة:**\n"
            "• `/chart BTC` — رسم بياني البيتكوين\n"
            "• `/chart PAXG` — رسم بياني الذهب\n"
            "• `/chart ETH` — رسم بياني الإيثريوم"
        )
        bot.reply_to(message, guide_msg, parse_mode="Markdown")
        return
        
    symbol = args[1]
    bot.send_chat_action(message.chat.id, 'upload_photo')
    chart_img = generate_chart(symbol)
    if chart_img:
        bot.send_photo(message.chat.id, chart_img, caption=f"📈 **المخطط البياني المباشر لـ {symbol.upper()}**")
    else:
        bot.reply_to(message, f"❌ **تعذر تعقب الرمز `{symbol.upper()}`**\nيرجى التأكد من كتابة رمز العملة بالشكل الصحيح (مثال: BTC, ETH, PAXG).", parse_mode="Markdown")

@bot.message_handler(commands=['risk'])
def risk_cmd(message):
    try:
        parts = message.text.split()
        if len(parts) != 5:
            raise ValueError("Invalid parameters")
            
        _, capital, risk_pct, entry, sl = parts
        cap, r_pct, ent, stop = float(capital), float(risk_pct), float(entry), float(sl)
        
        risk_amount = cap * (r_pct / 100.0)
        price_diff = abs(ent - stop)
        if price_diff == 0:
            bot.reply_to(message, "⚠️ **خطأ:** سعر الدخول ووقف الخسارة متطابقان!")
            return
            
        pos_size = risk_amount / price_diff
        msg = (
            f"🛡️ **نتيجة حساب إدارة المخاطر:**\n\n"
            f"💰 **رأس المال:** {cap}$\n"
            f"📉 **المبلغ المخاطر به:** {round(risk_amount, 2)}$ (بنسبة {r_pct}%)\n"
            f"📊 **حجم الموقف المقترح (Position Size):** **{round(pos_size, 4)} وحدة**\n\n"
            f"💡 *التزم بهذا الحجم لحماية حسابك من تقلبات السوق.*"
        )
        bot.reply_to(message, msg, parse_mode="Markdown")
        
    except Exception:
        guide_msg = (
            "📖 **دليل استخدام حاسبة إدارة المخاطر (`/risk`):**\n\n"
            "✏️ **الصيغة المطلوبة:**\n"
            "`/risk <رأس_المال> <نسبة_المخاطرة_%> <سعر_الدخول> <وقف_الخسارة>`\n\n"
            "💡 **مثال عملي:**\n"
            "`/risk 1000 2 65000 64000`"
        )
        bot.reply_to(message, guide_msg, parse_mode="Markdown")

@bot.message_handler(content_types=['text'])
def handle_text(message):
    add_user(message.chat.id)
    try:
        bot.send_chat_action(message.chat.id, 'typing')
        text = message.text.strip()
        
        target_symbol = ""
        
        # 1. مطابقة الكلمات العربية لمعرفة الأصل المالي المكتوب
        for ar_word, symbol in ARABIC_ASSETS.items():
            if ar_word in text.lower():
                target_symbol = symbol
                break
                
        # 2. المطابقة للرموز الإنجليزية المباشرة
        if not target_symbol:
            words = text.split()
            if len(words) == 1 and words[0].isalpha() and words[0].isascii() and 2 <= len(words[0]) <= 6:
                target_symbol = words[0].upper()

        data_raw, indicators_str = None, ""
        if target_symbol:
            data_raw, indicators_str = get_market_indicators(target_symbol)
            if data_raw:
                data_raw["symbol"] = target_symbol

        # توليد الإجابة
        reply_text = ask_gemini(text, symbol_data_raw=data_raw, symbol_data_str=indicators_str)
        
        if indicators_str and "🏆 **تقرير التحليل الفني" not in reply_text:
            full_response = f"{indicators_str}\n\n💡 **التحليل والتوصية:**\n{reply_text}"
        else:
            full_response = reply_text
            
        for chunk in [full_response[i:i+4000] for i in range(0, len(full_response), 4000)]:
            bot.reply_to(message, chunk, parse_mode="Markdown")
            
    except Exception as e:
        print(f"Handler Error: {e}")
        bot.reply_to(message, "أهلاً بك! كيف يمكنني مساعدتك اليوم في تحليل الأسواق وإدارة المخاطر؟")

# 11. التشغيل
if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=auto_learning_loop, daemon=True).start()
    print("🚀 تم تشغيل البوت المطور بنجاح...")
    
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception as e:
            time.sleep(3)
