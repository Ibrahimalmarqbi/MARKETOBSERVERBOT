import os
import time
import threading
from flask import Flask
import telebot
import google.generativeai as genai

# 1. جلب المتغيرات
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# تهيئة الذكاء الاصطناعي بالمكتبة المستقرة
genai.configure(api_key=GEMINI_API_KEY)

# قائمة النماذج المتاحة للتجربة التلقائية عند الفشل
MODELS_TO_TRY = ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.0-flash"]

# 2. خادم HTTP لإرضاء Render
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot status: Active"

def run_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# 3. دالة الاستعلام مع نظام التبديل التلقائي
def ask_gemini(prompt):
    last_error = ""
    for model_name in MODELS_TO_TRY:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(prompt)
            if response and response.text:
                return response.text
        except Exception as e:
            last_error = str(e)
            print(f"⚠️ تجربة النموذج {model_name} فشلت: {e}")
            continue
            
    return f"❌ تعذر الاتصال بجميع النماذج. الخطأ: {last_error}"

# 4. معالجة الرسائل
@bot.message_handler(content_types=['text'])
def handle_text(message):
    try:
        bot.send_chat_action(message.chat.id, 'typing')
        reply_text = ask_gemini(message.text)
        
        # تقسيم النصوص الطويلة
        for chunk in [reply_text[i:i+4000] for i in range(0, len(reply_text), 4000)]:
            bot.reply_to(message, chunk)
    except Exception as e:
        print(f"❌ TELEGRAM ERROR: {e}")

# 5. التشغيل المستمر
if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    print("🚀 تم تشغيل البوت بنجاح...")
    
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception as e:
            print(f"⚠️ إعادة اتصال تلقائية: {e}")
            time.sleep(3)
