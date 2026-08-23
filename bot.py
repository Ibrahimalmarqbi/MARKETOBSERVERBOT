import os
import time
import sqlite3
import threading
import requests
from flask import Flask
import telebot
import google.generativeai as genai

# 1. تهيئة المتغيرات الأساسية
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

DB_NAME = "market_memory.db"

# 2. إنشاء وتجهيز قاعدة البيانات لتسجيل المشتركين والصفقات والدروس
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # جدول المشتركين
    c.execute('''CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY)''')
    # جدول الصفقات المفتوحة للمتابعة التلقائية
    c.execute('''CREATE TABLE IF NOT EXISTS signals 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, entry_price REAL, 
                  tp REAL, sl REAL, status TEXT, direction TEXT)''')
    # جدول الدروس المستفادة تلقائياً من الأخطاء
    c.execute('''CREATE TABLE IF NOT EXISTS lessons (id INTEGER PRIMARY KEY AUTOINCREMENT, lesson TEXT)''')
    conn.commit()
    conn.close()

init_db()

# 3. وظائف التعامل مع قاعدة البيانات
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
    return "\n".join(lessons) if lessons else "لا توجد أخطاء مسجلة، المحرك يعمل بالمعايير القياسية."

def save_auto_lesson(lesson_text):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO lessons (lesson) VALUES (?)", (lesson_text,))
    conn.commit()
    conn.close()

# 4. جلب سعر العملة اللحظي تلقائياً (مثال: Binance API للعملات الرقمية)
def get_crypto_price(symbol):
    try:
        clean_symbol = symbol.upper().replace("/", "").replace("-", "")
        if not clean_symbol.endswith("USDT"):
            clean_symbol += "USDT"
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={clean_symbol}"
        res = requests.get(url, timeout=5).json()
        return float(res['price'])
    except Exception:
        return None

# 5. بناء تعليمات النظام مع الذاكرة العصبية التراكمية
def get_system_instruction():
    lessons = get_learned_lessons()
    return f"""
أنت خبير مالي ومحلل أسواق معتمد، تعتمد على التحيليل الفني والموجي.
تم تصميمك وتطويرك بواسطة المهندس "إبراهيم المرقبي".

قواعد العمل:
1. كن مقتصداً ومباشراً جداً في كلامك دون مقدمات.
2. نطاقك محدد حصراً بالتحليل المالي وتوصيات التداول لجميع المستخدمين.
3. عند تقديم توصية، اذكر دائماً: (الرمز Symbol، سعر الدخول Entry، الهدف TP، وقف الخسارة SL).

**الذاكرة الذاتية (أخطاء سابقة تعلمتها تلقائياً وتتجنبها الآن):**
{lessons}
"""

MODELS_TO_TRY = ["gemini-3.6-flash", "gemini-2.5-flash", "gemini-1.5-flash-latest"]

def ask_gemini(prompt):
    for model_name in MODELS_TO_TRY:
        try:
            model = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=get_system_instruction()
            )
            response = model.generate_content(prompt)
            if response and response.text:
                return response.text
        except Exception:
            continue
    return "❌ تعذر تحليل السوق حالياً."

# 6. المحرك الخلفي للتقييم والتعلم التلقائي (Auto-Evaluator Loop)
def auto_learning_loop():
    while True:
        time.sleep(300) # فحص الصفقات والسوق كل 5 دقائق تلقائياً
        try:
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute("SELECT id, symbol, entry_price, tp, sl, direction FROM signals WHERE status = 'OPEN'")
            open_signals = c.fetchall()
            
            for sig in open_signals:
                sig_id, symbol, entry, tp, sl, direction = sig
                current_price = get_crypto_price(symbol)
                
                if not current_price:
                    continue
                
                # حالة ضرب هدف الربح
                if (direction == "BUY" and current_price >= tp) or (direction == "SELL" and current_price <= tp):
                    c.execute("UPDATE signals SET status = 'SUCCESS' WHERE id = ?", (sig_id,))
                    conn.commit()
                    # إرسال إشعار لكافة المشتركين بنجاح الصفقة
                    for u in get_all_users():
                        bot.send_message(u, f"🎯 **صفقة ناجحة!**\nتم تحقيق هدف الربح لـ {symbol} عند سعر {current_price}")
                
                # حالة ضرب وقف الخسارة (هنا يتم التعلم التلقائي)
                elif (direction == "BUY" and current_price <= sl) or (direction == "SELL" and current_price >= sl):
                    c.execute("UPDATE signals SET status = 'FAILED' WHERE id = ?", (sig_id,))
                    conn.commit()
                    
                    # استدعاء الذكاء الاصطناعي لاستخراج الدرس التلقائي
                    analysis_prompt = f"الصفقة على {symbol} ضربت وقف الخسارة عند {current_price}. استخرج دقيقة واحدة فقط كقاعدة فنّية لتجنب هذا الخطأ مستقبلاً."
                    new_lesson = ask_gemini(analysis_prompt)
                    save_auto_lesson(f"صفقة {symbol}: {new_lesson}")
                    
                    # إشعارات المستخدمين
                    for u in get_all_users():
                        bot.send_message(u, f"⚠️ **تحديث صفقة {symbol}:**\nضربت نقطة وقف الخسارة. تم تحليل السبب وإضافة درس جديد لشبكة البوت الذاتية لتجنبه مستقبلاً.")
            
            conn.close()
        except Exception as e:
            print(f"خطأ في محرك التعلم: {e}")

# 7. سيرفر Flask لإبقاء الخدمة تعمل على Render
app = Flask(__name__)

@app.route('/')
def home():
    return "Autonomous Learning Market Bot Active"

def run_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# 8. معالجة رسائل تلغرام
@bot.message_handler(commands=['start'])
def start_cmd(message):
    add_user(message.chat.id)
    bot.reply_to(
        message, 
        "أهلاً بك! أنا محرك التحليل والتعلم المالي الذاتي المفتوح للجميع 📈\n"
        "تم تطويري بواسطة **المهندس إبراهيم المرقبي**.\n\n"
        "أقوم بتحليل الصفقات، تتبعها حياً، والتعلم تلقائياً من تقلبات السوق لتطوير دقة التوصيات."
    )

@bot.message_handler(content_types=['text'])
def handle_text(message):
    add_user(message.chat.id)
    try:
        bot.send_chat_action(message.chat.id, 'typing')
        reply_text = ask_gemini(message.text)
        
        for chunk in [reply_text[i:i+4000] for i in range(0, len(reply_text), 4000)]:
            bot.reply_to(message, chunk)
    except Exception as e:
        print(f"❌ ERROR: {e}")

# 9. تشغيل كل الخيوط التزامنية
if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=auto_learning_loop, daemon=True).start()
    print("🚀 تم تشغيل البوت ذاتي التعلم بنجاح...")
    
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception as e:
            time.sleep(3)
