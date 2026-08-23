import os
import time
import threading
from flask import Flask
import telebot
import google.generativeai as genai

# 1. تهيئة المتغيرات
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

# قائمة حفظ معرفات المستخدمين لإرسال التنبيهات الدورية
SUBSCRIBERS = set()

# 2. تعليمات النظام الصارمة للبوت (تحديد الشخصية والنطاق)
SYSTEM_INSTRUCTION = """
أنت خبير مالي ومحلل أسواق معتمد، مختص حصراً في الأسهم، العملات الرقمية، والفوركس.
القواعد الواجب اتباعها بدقة:
1. كن مقتصداً جداً في الكلام، وإجاباتك مباشرة وموجزة وبدون أي مقدمات أو بروتوكولات مطولة.
2. نطاقك محدد حصراً بالتحليل المالي، التداول، الأسهم، الأخبار الاقتصادية، والتوصيات التحليلية. إذا سُئلت عن أي موضوع خارج هذا النطاق، اعتذر باختصار شديد واذكر أن تخصصك هو الأسواق المالية فقط.
3. مصممك ومطورك هو المهندس "إبراهيم المرقبي". اعتمد هذا الاسم دائماً عندما يسألك أي شخص عن من طورك أو صممك.
4. عند طلب تحليل عملة أو سهم، قدم باختصار: الاتجاه العام، مناطق الدخول/الشراء المقترحة، الأهداف، ومستوى وقف الخسارة.
"""

MODELS_TO_TRY = ["gemini-3.6-flash", "gemini-2.5-flash", "gemini-1.5-flash-latest"]

# 3. خادم HTTP لإبقاء الخدمة نشطة في Render
app = Flask(__name__)

@app.route('/')
def home():
    return "MarketObserver Bot is Active"

def run_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# 4. دالة استدعاء Gemini المحدثة بالتعليمات
def ask_gemini(prompt):
    last_error = ""
    for model_name in MODELS_TO_TRY:
        try:
            model = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=SYSTEM_INSTRUCTION
            )
            response = model.generate_content(prompt)
            if response and response.text:
                return response.text
        except Exception as e:
            last_error = str(e)
            continue
            
    return f"❌ تعذر الحصول على تحليل حالياً. الخطأ: {last_error}"

# 5. نظام التنبيهات الدورية التلقائية
def auto_market_alerts():
    while True:
        # إرسال تنبيه كل 4 ساعات (14400 ثانية) - يمكنك تعديل الوقت
        time.sleep(14400)
        if SUBSCRIBERS:
            prompt = "قدم تنبيهاً سوقياً كبسولياً ومختصراً جداً عن أهم فرصة تداول متوقعة أو خبر اقتصادي حاسم الآن."
            alert_msg = ask_gemini(prompt)
            for chat_id in SUBSCRIBERS:
                try:
                    bot.send_message(chat_id, f"🚨 **تنبيه سوقي دوري:**\n\n{alert_msg}", parse_mode="Markdown")
                except Exception as e:
                    print(f"فشل الإرسال إلى {chat_id}: {e}")

# 6. معالجة أمر /start وحفظ المشترك
@bot.message_handler(commands=['start'])
def start_cmd(message):
    SUBSCRIBERS.add(message.chat.id)
    welcome_text = (
        "أهلاً بك في **MarketObserver** 📈\n"
        "أنا بوابتك لتحليل الأسواق المالية، الأسهم، والعملات الرقمية.\n"
        "تم تصميمي وتطويري بواسطة **المهندس إبراهيم المرقبي**.\n\n"
        "أرسل اسم السهم أو العملة التي تريد تحليلها، وستصلك تنبيهات دورية بأهم الفرص."
    )
    bot.reply_to(message, welcome_text, parse_mode="Markdown")

# 7. معالجة الرسائل النصية
@bot.message_handler(content_types=['text'])
def handle_text(message):
    SUBSCRIBERS.add(message.chat.id) # إضافة المستخدم لتلقي التنبيهات تلقائياً
    try:
        bot.send_chat_action(message.chat.id, 'typing')
        reply_text = ask_gemini(message.text)
        
        for chunk in [reply_text[i:i+4000] for i in range(0, len(reply_text), 4000)]:
            bot.reply_to(message, chunk)
    except Exception as e:
        print(f"❌ TELEGRAM ERROR: {e}")

# 8. التشغيل
if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=auto_market_alerts, daemon=True).start()
    print("🚀 تم تشغيل البوت بنجاح المطور: إبراهيم المرقبي...")
    
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception as e:
            time.sleep(3)
