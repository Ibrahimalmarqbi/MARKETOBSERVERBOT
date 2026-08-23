import os
import time
import threading
from flask import Flask
import telebot
from google import genai

# 1. جلب المتغيرات البيئية
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# 2. خادم HTTP لمنع الخمول وإرضاء Render
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot status: Active"

def run_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# 3. دالة استدعاء Gemini مع النموذج الحديث معالجة الأخطاء
def ask_gemini(prompt):
    try:
        response = ai_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        if response and hasattr(response, 'text') and response.text:
            return response.text
        return "⚠️ لم يتمكن الذكاء الاصطناعي من صياغة رد (قد يكون النص مخالفاً لسياسات المحتوى)."
    except Exception as e:
        print(f"❌ GEMINI API ERROR: {e}")
        return f"❌ حدث خطأ أثناء الاتصال بـ Gemini: {e}"

# 4. معالجة الرسائل النصية
@bot.message_handler(content_types=['text'])
def handle_text(message):
    try:
        bot.send_chat_action(message.chat.id, 'typing')
        reply_text = ask_gemini(message.text)
        
        # تقسيم الرد تلقائياً إذا تجاوز الحد الأقصى للتليجرام (4000 حرف)
        for chunk in [reply_text[i:i+4000] for i in range(0, len(reply_text), 4000)]:
            bot.reply_to(message, chunk)
    except Exception as e:
        print(f"❌ TELEGRAM ERROR: {e}")

# 5. تشغيل البوت بحلقة مستمرة مقاومة للسقوط
if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    print("🚀 البوت يعمل الآن بنجاح مع نموذج gemini-2.0-flash...")
    
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception as e:
            print(f"⚠️ حدث انقطاع في الشبكة، يتم إعادة الاتصال خلال 3 ثوانٍ: {e}")
            time.sleep(3)
