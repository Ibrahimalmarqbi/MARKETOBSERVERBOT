import os
import threading
from flask import Flask
import telebot
from google import genai

# 1. جلب المتغيرات من البيئة
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# 2. تهيئة البوت والذكاء الاصطناعي
bot = telebot.TeleBot(TELEGRAM_TOKEN)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# 3. خادم HTTP وهمي لإرضاء فحص Render
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is active!"

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# 4. دالة الاستعلام من Gemini
def generate_ai_response(prompt_text):
    try:
        response = ai_client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt_text
        )
        return response.text
    except Exception as e:
        print(f"❌ GEMINI ERROR DETAILS: {e}")
        return None

# 5. معالجة الرسائل
@bot.message_handler(func=lambda message: True)
def handle_messages(message):
    user_text = message.text
    
    # إرسال حالة "جاري الكتابة..."
    bot.send_chat_action(message.chat.id, 'typing')
    
    reply = generate_ai_response(user_text)
    
    if reply:
        bot.reply_to(message, reply)
    else:
        bot.reply_to(
            message, 
            "⚠️ تعذر الحصول على رد من Gemini. تحقق من صحة المفتاح GEMINI_API_KEY في Render Logs."
        )

# 6. التشغيل الرئيسي
if __name__ == "__main__":
    # تشغيل السيرفر في خلفية منفصلة
    threading.Thread(target=run_http_server, daemon=True).start()
    
    print("🚀 تم تشغيل البوت بنجاح...")
    
    # skip_pending يمنع تضارب الرسائل القديمة ويقضي على خطأ 409
    bot.infinity_polling(skip_pending=True)
