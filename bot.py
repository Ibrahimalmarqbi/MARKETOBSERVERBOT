from http.server import BaseHTTPRequestHandler, HTTPServer
import os
import threading
import time
from google import genai
from google.genai import types
import pandas as pd
import schedule
import telebot
import yfinance as yf

# --- خادم وهمي لإرضاء Render في الخطة المجانية ---
class SimpleServer(BaseHTTPRequestHandler):

  def do_GET(self):
    self.send_response(200)
    self.end_headers()
    self.wfile.write(b"Bot is active!")


def run_http_server():
  port = int(os.environ.get("PORT", 8080))
  server = HTTPServer(("0.0.0.0", port), SimpleServer)
  server.serve_forever()


# --- جلب البيانات الحساسة من متغيرات البيئة ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = """
أنت خبير ومحلل مالي ومستشار أسواق عالمية (Forex، ذهب، بيتكوين).
تتحدث بأسلوب احترافي، سلس، وشخصية خبير محترف ولطيف.
"""

FOREX_PAIRS = {
    "EURUSD=X": "EUR/USD (يورو / دولار)",
    "GBPUSD=X": "GBP/USD (باوند / دولار)",
    "USDJPY=X": "USD/JPY (دولار / ين)",
    "GC=F": "XAU/USD (الذهب)",
    "BTC-USD": "BTC/USD (البيتكوين)",
}


def generate_ai_response(prompt):
  available_models = ["gemini-2.0-flash", "gemini-1.5-flash"]
  for model_name in available_models:
    try:
      response = client.models.generate_content(
          model=model_name,
          contents=prompt,
          config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
      )
      return response.text
    except Exception:
      continue
  return "عذراً، تعذر الاتصال بنماذج الذكاء الاصطناعي حالياً."


def calculate_rsi(series, period=14):
  delta = series.diff()
  gain = (delta.where(delta > 0, 0)).fillna(0)
  loss = (-delta.where(delta < 0, 0)).fillna(0)
  avg_gain = gain.rolling(window=period).mean()
  avg_loss = loss.rolling(window=period).mean()
  rs = avg_gain / avg_loss
  return 100 - (100 / (1 + rs))


def fetch_market_data():
  summary_data = []
  for symbol, name in FOREX_PAIRS.items():
    try:
      ticker = yf.Ticker(symbol)
      df = ticker.history(period="1mo", interval="1d", timeout=5)
      if df is not None and len(df) >= 15:
        df["RSI"] = calculate_rsi(df["Close"])
        last_close = round(float(df["Close"].iloc[-1]), 4)
        last_rsi = round(float(df["RSI"].iloc[-1]), 1)
        summary_data.append(
            f"• {name}: السعر الحالي {last_close} | مؤشر RSI: {last_rsi}"
        )
    except Exception:
      pass
  return (
      "\n".join(summary_data)
      if summary_data
      else "تعذر جلب البيانات اللحظية."
  )


def generate_expert_report():
  raw_data = fetch_market_data()
  prompt = f"بيانات السوق الحالية:\n{raw_data}\n\nاكتب تقرير تحليلي مالي شامل كخبير تداول بأسلوب جذاب."
  return generate_ai_response(prompt)


@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
  res = generate_ai_response(
      "رحب بالعميل كخبير مالي ووضح له الأمر /analyze."
  )
  bot.reply_to(message, res)


@bot.message_handler(commands=["analyze"])
def handle_analyze(message):
  waiting_msg = bot.reply_to(message, "📈 جاري تحضير التقرير...")
  try:
    report = generate_expert_report()
    bot.edit_message_text(
        report, chat_id=message.chat.id, message_id=waiting_msg.message_id
    )
  except Exception:
    bot.send_message(message.chat.id, "حدث خطأ أثناء إعداد التقرير.")


@bot.message_handler(func=lambda msg: True)
def handle_chat(message):
  res = generate_ai_response(
      f"المستخدم يقول: '{message.text}'. رد عليه كخبير مالي."
  )
  bot.reply_to(message, res)


def run_scheduler():
  if CHAT_ID:
    schedule.every(2).hours.do(
        lambda: bot.send_message(CHAT_ID, generate_expert_report())
    )
    while True:
      schedule.run_pending()
      time.sleep(1)


if name == "main":
  threading.Thread(target=run_http_server, daemon=True).start()
  threading.Thread(target=run_scheduler, daemon=True).start()
  print("🚀 البوت يعمل بنجاح!")
  bot.infinity_polling()
