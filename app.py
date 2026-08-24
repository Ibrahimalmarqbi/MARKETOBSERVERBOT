from __future__ import annotations

import io
import logging
import re
import threading
import time
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from flask import Flask, jsonify, request
import telebot
from telebot import types

from marketobserver.assets import ASSETS, Asset, resolve_asset
from marketobserver.analysis import Analysis, analyze
from marketobserver.advisor import Advice, build_advice
from marketobserver.nlp import parse_request
from marketobserver.broker import LiveBrokerNotConfigured, OrderRequest, PaperBroker
from marketobserver.config import Settings
from marketobserver.db import Database
from marketobserver.market_data import DataUnavailable, MarketDataProvider
from marketobserver.risk import calculate_position_size
from marketobserver.llm import GroundedLLM

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("marketobserver")

settings = Settings.from_env()
db = Database(settings.database_url)
db.create_all()
market = MarketDataProvider()
llm = GroundedLLM(settings.llm_api_key, settings.llm_api_base, settings.llm_model)
paper_broker = PaperBroker()
live_broker = LiveBrokerNotConfigured()
bot = telebot.TeleBot(settings.telegram_token, threaded=True)
app = Flask(__name__)


AR = {
    "start": "أهلًا بك في MarketObserver Pro. استخدم /analyze BTC للتحليل، /alert below BTC 60000 للتنبيه، /risk لحساب الحجم النظري، و /paperbuy لتجربة صفقة محاكاة.",
    "data_error": "تعذر الحصول على بيانات سوق موثوقة لهذا الأصل حاليًا. لم يتم إنشاء بيانات بديلة ولن أعرض تحليلًا غير حقيقي.",
    "unknown_asset": "الأصل غير مدعوم. جرّب BTC أو ETH أو SOL أو XAUUSD أو EURUSD أو AAPL أو TSLA أو NVDA.",
}


def user_language(message: types.Message) -> str:
    code = getattr(getattr(message, "from_user", None), "language_code", "") or ""
    return "ar" if code.lower().startswith("ar") else "en"


def remember_user(message: types.Message, asset: Asset | None = None):
    lang = user_language(message)
    return db.upsert_user(
        chat_id=message.chat.id,
        username=getattr(message.from_user, "username", None),
        language=lang,
        last_asset=asset.key if asset else None,
    )


def selected_asset(message: types.Message, supplied: str | None = None) -> Asset | None:
    if supplied is not None:
        asset = resolve_asset(supplied)
        if asset:
            db.set_last_asset(message.chat.id, asset.key)
        return asset
    user = db.get_user(message.chat.id)
    return ASSETS.get(user.last_asset) if user else ASSETS["BTC"]


def get_analysis(asset: Asset) -> tuple[Analysis, list]:
    candles = market.get_candles(asset, settings.default_interval, 200)
    return analyze(candles, asset.price_decimals), candles


def analysis_text(asset: Asset, result: Analysis, lang: str) -> str:
    trend_ar = {"bullish": "صاعد", "bearish": "هابط", "sideways": "جانبي"}[result.trend]
    signal_ar = {"watch_long": "مراقبة شراء محتملة", "watch_short": "مراقبة بيع محتملة", "neutral": "محايد"}[result.signal]
    if lang == "ar":
        return (
            f"📊 **تحليل {asset.name_ar} ({asset.key})**\n\n"
            f"السعر: **{result.price} {asset.quote}**\nRSI: **{result.rsi}**\n"
            f"SMA20: {result.sma20} | SMA50: {result.sma50}\nATR14: {result.atr14}\n"
            f"الدعم التاريخي القريب: {result.support}\nالمقاومة التاريخية القريبة: {result.resistance}\n"
            f"الاتجاه: **{trend_ar}**\nالحالة: **{signal_ar}**\n\n"
            f"وقت آخر شمعة: `{result.candle_time}`\nعدد الشموع: `{result.data_points}`\n"
            f"مصدر البيانات: `{market.last_source(asset.key) or 'unknown'}`\n"
            "هذه قراءة آلية للمؤشرات وليست ضمانًا للربح أو توصية شخصية."
        )
    return (
        f"📊 **{asset.name_en} analysis ({asset.key})**\n\n"
        f"Price: **{result.price} {asset.quote}**\nRSI: **{result.rsi}**\n"
        f"SMA20: {result.sma20} | SMA50: {result.sma50}\nATR14: {result.atr14}\n"
        f"Recent historical support: {result.support}\nRecent historical resistance: {result.resistance}\n"
        f"Trend: **{result.trend}**\nState: **{result.signal}**\n\n"
        f"Last candle: `{result.candle_time}`\nCandles: `{result.data_points}`\n"
        f"Data source: `{market.last_source(asset.key) or 'unknown'}`\n"
        "This is automated indicator analysis, not a guarantee or personalized advice."
    )


def advice_text(advice: Advice, lang: str) -> str:
    if lang == "ar":
        action = {"watch_long": "مراقبة شراء مشروطة", "watch_short": "مراقبة بيع مشروطة", "wait": "انتظار"}[advice.action]
        lines = [
            f"مساعد القرار: {advice.asset.name_ar} ({advice.asset.key})",
            f"السعر المرجعي: {advice.current_price} {advice.asset.quote}",
            f"الحالة: {action} | الثقة: {advice.confidence}",
            f"السبب: {advice.reason}",
        ]
        if advice.entry_low is not None:
            lines += [
                f"منطقة الدخول النظرية: {advice.entry_low} - {advice.entry_high}",
                f"مستوى إلغاء السيناريو: {advice.invalidation}",
                f"الهدف الأول: {advice.target_one}",
                f"الهدف الثاني: {advice.target_two}",
                f"العائد/المخاطرة التقريبي: 1:{advice.risk_reward}",
            ]
        lines += [
            "الأطر المستخدمة: " + ", ".join(view.timeframe for view in advice.views),
            f"المصدر: {advice.source or 'غير معروف'} | آخر تحديث: {advice.as_of}",
            "هذه سيناريوهات مشروطة وليست ضمانًا أو توصية شخصية. لا تدخل دون تحديد رأس المال والمخاطرة ووقف الخسارة.",
        ]
        return "\\n".join(lines)
    action = {"watch_long": "Conditional long watch", "watch_short": "Conditional short watch", "wait": "Wait"}[advice.action]
    lines = [
        f"Decision assistant: {advice.asset.name_en} ({advice.asset.key})",
        f"Reference price: {advice.current_price} {advice.asset.quote}",
        f"State: {action} | confidence: {advice.confidence}",
        f"Reason: {advice.reason}",
    ]
    if advice.entry_low is not None:
        lines += [
            f"Theoretical entry zone: {advice.entry_low} - {advice.entry_high}",
            f"Scenario invalidation: {advice.invalidation}",
            f"Target one: {advice.target_one}",
            f"Target two: {advice.target_two}",
            f"Approximate reward/risk: 1:{advice.risk_reward}",
        ]
    lines += [
        "Timeframes: " + ", ".join(view.timeframe for view in advice.views),
        f"Source: {advice.source or 'unknown'} | last update: {advice.as_of}",
        "These are conditional scenarios, not a guarantee or personalized advice. Define capital, risk, and a stop before acting.",
    ]
    return "\\n".join(lines)


def chart(asset: Asset, candles: list) -> io.BytesIO:
    fig, ax = plt.subplots(figsize=(9, 4.8))
    fig.patch.set_facecolor("#0b1220")
    ax.set_facecolor("#111827")
    ax.plot([c.timestamp for c in candles[-100:]], [c.close for c in candles[-100:]], color="#22c55e", linewidth=2)
    ax.set_title(f"{asset.name_en} — {settings.default_interval}", color="white")
    ax.tick_params(colors="white", labelsize=8)
    ax.grid(True, alpha=0.2)
    fig.autofmt_xdate()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buffer.seek(0)
    return buffer


def send_localized(chat_id: int, lang: str, ar: str, en: str):
    bot.send_message(chat_id, ar if lang == "ar" else en, parse_mode="Markdown")


@bot.message_handler(commands=["start", "help"])
def start_cmd(message: types.Message):
    remember_user(message)
    bot.reply_to(message, AR["start"] if user_language(message) == "ar" else "Welcome. Use /analyze BTC, /alert below BTC 60000, /risk, and /paperbuy for paper trading.")


@bot.message_handler(commands=["analyze"])
def analyze_cmd(message: types.Message):
    parts = message.text.split(maxsplit=1)
    asset = selected_asset(message, parts[1] if len(parts) == 2 else None)
    remember_user(message, asset)
    if not asset:
        bot.reply_to(message, AR["unknown_asset"])
        return
    try:
        result, _ = get_analysis(asset)
        bot.reply_to(message, analysis_text(asset, result, user_language(message)), parse_mode="Markdown")
    except DataUnavailable:
        bot.reply_to(message, AR["data_error"] if user_language(message) == "ar" else "Reliable market data is unavailable right now. No synthetic data was used.")
    except Exception:
        logger.exception("analysis failed for %s", asset.key)
        bot.reply_to(message, "حدث خطأ مؤقت أثناء التحليل." if user_language(message) == "ar" else "A temporary analysis error occurred.")


@bot.message_handler(commands=["chart"])
def chart_cmd(message: types.Message):
    parts = message.text.split(maxsplit=1)
    asset = selected_asset(message, parts[1] if len(parts) == 2 else None)
    remember_user(message, asset)
    if not asset:
        bot.reply_to(message, AR["unknown_asset"])
        return
    try:
        result, candles = get_analysis(asset)
        bot.send_chat_action(message.chat.id, "upload_photo")
        caption = f"📈 {asset.name_ar if user_language(message) == 'ar' else asset.name_en} | {result.price} {asset.quote}"
        bot.send_photo(message.chat.id, chart(asset, candles), caption=caption)
    except DataUnavailable:
        bot.reply_to(message, AR["data_error"] if user_language(message) == "ar" else "Reliable market data is unavailable right now.")
    except Exception:
        logger.exception("chart failed for %s", asset.key)
        bot.reply_to(message, "تعذر إنشاء الشارت حاليًا." if user_language(message) == "ar" else "Chart generation failed.")


@bot.message_handler(commands=["risk"])
def risk_cmd(message: types.Message):
    lang = user_language(message)
    parts = message.text.split()
    if len(parts) not in {5, 6}:
        bot.reply_to(message, "الاستخدام: /risk رأس_المال نسبة_المخاطرة الدخول الوقف [قيمة_النقطة]" if lang == "ar" else "Usage: /risk CAPITAL RISK_PERCENT ENTRY STOP [POINT_VALUE]")
        return
    try:
        capital, risk_pct, entry, stop = map(float, parts[1:5])
        point_value = float(parts[5]) if len(parts) == 6 else 1.0
        result = calculate_position_size(capital, risk_pct, entry, stop, point_value, settings.max_risk_percent, settings.max_position_notional)
        text = (
            f"🛡️ المخاطرة: {result.risk_amount} USD\nالمسافة: {result.stop_distance}\nالوحدات النظرية: {result.quantity}\nالقيمة الاسمية القصوى: {result.notional} USD"
            if lang == "ar" else
            f"🛡️ Risk amount: {result.risk_amount} USD\nStop distance: {result.stop_distance}\nTheoretical units: {result.quantity}\nCapped notional: {result.notional} USD"
        )
        bot.reply_to(message, text)
    except ValueError as exc:
        logger.info("invalid risk input: %s", exc)
        bot.reply_to(message, "مدخلات المخاطر غير صحيحة أو تتجاوز الحد المسموح." if lang == "ar" else "Invalid risk inputs or risk limit exceeded.")


@bot.message_handler(commands=["alert"])
def alert_cmd(message: types.Message):
    lang = user_language(message)
    parts = message.text.split()
    if len(parts) != 4 or parts[1].lower() not in {"above", "below", "فوق", "تحت"}:
        bot.reply_to(message, "الاستخدام: /alert above|below BTC 60000" if lang == "en" else "الاستخدام: /alert above|below BTC 60000")
        return
    condition = "above" if parts[1].lower() in {"above", "فوق"} else "below"
    asset = resolve_asset(parts[2])
    try:
        target = float(parts[3])
        if not asset or target <= 0:
            raise ValueError
        db.add_alert(message.chat.id, asset.key, target, condition)
        text = f"تم ضبط تنبيه {condition} لـ {asset.name_ar} عند {target}." if lang == "ar" else f"Alert set: {asset.key} {condition} {target}."
        bot.reply_to(message, text)
    except ValueError:
        bot.reply_to(message, "صيغة التنبيه أو السعر غير صحيح." if lang == "ar" else "Invalid alert format or price.")


@bot.message_handler(commands=["alerts"])
def alerts_cmd(message: types.Message):
    lang = user_language(message)
    alerts = db.list_alerts(message.chat.id)
    if not alerts:
        bot.reply_to(message, "لا توجد تنبيهات." if lang == "ar" else "No alerts.")
        return
    lines = [f"#{a.id} {a.asset_key} {a.condition} {a.target_price} — {a.status}" for a in alerts[:20]]
    bot.reply_to(message, ("تنبيهاتك:\n" if lang == "ar" else "Your alerts:\n") + "\n".join(lines))


@bot.message_handler(commands=["cancel_alert"])
def cancel_alert_cmd(message: types.Message):
    parts = message.text.split()
    try:
        alert_id = int(parts[1])
        ok = db.cancel_alert(message.chat.id, alert_id)
        bot.reply_to(message, "تم إلغاء التنبيه." if ok else "التنبيه غير موجود أو غير نشط.")
    except (IndexError, ValueError):
        bot.reply_to(message, "الاستخدام: /cancel_alert ID")


@bot.message_handler(commands=["paperbuy", "papersell"])
def paper_order_cmd(message: types.Message):
    lang = user_language(message)
    parts = message.text.split()
    if len(parts) not in {3, 5}:
        bot.reply_to(message, "الاستخدام: /paperbuy BTC 0.01 [STOP TAKE]" if lang == "ar" else "Usage: /paperbuy BTC 0.01 [STOP TAKE]")
        return
    asset = resolve_asset(parts[1])
    try:
        quantity = float(parts[2])
        stop = float(parts[3]) if len(parts) == 5 else None
        take = float(parts[4]) if len(parts) == 5 else None
        if not asset or quantity <= 0:
            raise ValueError
        result, _ = get_analysis(asset)
        order = OrderRequest(asset.key, "buy" if message.text.startswith("/paperbuy") else "sell", quantity, stop_loss=stop, take_profit=take)
        accepted = paper_broker.place_order(order)
        if not accepted.accepted:
            raise ValueError(accepted.message)
        trade = db.add_trade(chat_id=message.chat.id, asset_key=asset.key, side=order.side, quantity=quantity, entry_price=result.price, stop_loss=stop, take_profit=take, status="open", mode="paper", broker_order_id=accepted.order_id, notes="paper order")
        text = f"تم فتح صفقة محاكاة #{trade.id}: {order.side} {quantity} {asset.key} عند {result.price}." if lang == "ar" else f"Paper trade #{trade.id} opened: {order.side} {quantity} {asset.key} at {result.price}."
        bot.reply_to(message, text)
    except (ValueError, DataUnavailable):
        bot.reply_to(message, "تعذر فتح صفقة المحاكاة؛ تحقق من البيانات والمدخلات." if lang == "ar" else "Paper order could not be opened; check data and inputs.")
    except Exception:
        logger.exception("paper order failed")
        bot.reply_to(message, "حدث خطأ أثناء صفقة المحاكاة." if lang == "ar" else "Paper order failed.")


@bot.message_handler(content_types=["text"])
def text_cmd(message: types.Message):
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    user = db.get_user(message.chat.id)
    request = parse_request(text, user.last_asset if user else None)
    asset = request.asset
    remember_user(message, asset)
    lang = request.language
    if asset:
        try:
            if request.intent == "advice":
                advice = build_advice(market, asset, request.timeframe)
                facts = {
                    "asset": advice.asset.key,
                    "price": advice.current_price,
                    "action": advice.action,
                    "confidence": advice.confidence,
                    "reason": advice.reason,
                    "entry_low": advice.entry_low,
                    "entry_high": advice.entry_high,
                    "invalidation": advice.invalidation,
                    "target_one": advice.target_one,
                    "target_two": advice.target_two,
                    "risk_reward": advice.risk_reward,
                    "timeframes": [view.timeframe for view in advice.views],
                    "source": advice.source,
                    "as_of": advice.as_of,
                }
                if llm.enabled:
                    try:
                        bot.reply_to(message, llm.explain(lang, text, facts))
                        return
                    except Exception:
                        logger.exception("grounded LLM explanation failed; using deterministic response")
                bot.reply_to(message, advice_text(advice, lang))
            else:
                result, _ = get_analysis(asset)
                bot.reply_to(message, analysis_text(asset, result, lang))
        except DataUnavailable:
            bot.reply_to(message, AR["data_error"] if lang == "ar" else "Reliable market data is unavailable right now. No synthetic data was used.")
        except Exception:
            logger.exception("natural-language request failed")
            bot.reply_to(message, "تعذر تنفيذ الطلب حاليًا." if lang == "ar" else "The request could not be completed right now.")
    else:
        bot.reply_to(message, "اذكر اسم الأصل مثل الذهب أو BTC أو AAPL، أو اكتب سؤالك مع اسم الأصل." if lang == "ar" else "Mention an asset such as gold, BTC, or AAPL with your question.")


def alert_loop():
    while True:
        try:
            active = db.active_alerts()
            by_asset: dict[str, list] = defaultdict(list)
            for alert in active:
                by_asset[alert.asset_key].append(alert)
            prices = {}
            for asset_key in by_asset:
                asset = ASSETS.get(asset_key)
                if not asset:
                    continue
                try:
                    candles = market.get_candles(asset, settings.default_interval, 100)
                    prices[asset_key] = candles[-1].close
                except DataUnavailable:
                    logger.warning("alert data unavailable for %s", asset_key)
            for asset_key, alerts in by_asset.items():
                if asset_key not in prices:
                    continue
                current = prices[asset_key]
                for alert in alerts:
                    hit = (alert.condition == "below" and current <= alert.target_price) or (alert.condition == "above" and current >= alert.target_price)
                    if not hit:
                        continue
                    user = db.get_user(alert.chat_id)
                    lang = user.language if user else "ar"
                    asset = ASSETS[asset_key]
                    try:
                        send_localized(
                            alert.chat_id, lang,
                            f"🔔 تنبيه {asset.name_ar}: وصل السعر إلى {current}، والهدف {alert.target_price}.",
                            f"🔔 {asset.name_en} alert: price reached {current}; target {alert.target_price}.",
                        )
                        db.trigger_alert(alert.id)
                    except Exception:
                        logger.exception("alert delivery failed id=%s", alert.id)
        except Exception:
            logger.exception("alert loop failure")
        time.sleep(settings.alert_poll_seconds)


@app.get("/")
def home():
    return "MarketObserver Pro is running"


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "marketobserver", "stats": db.stats()})


def authorized() -> bool:
    return request.headers.get("X-Admin-Key", "") == settings.admin_api_key


@app.get("/admin/stats")
def admin_stats():
    if not authorized():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(db.stats())


@app.get("/admin/users")
def admin_users():
    if not authorized():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify([
        {
            "chat_id": user.chat_id,
            "username": user.username,
            "language": user.language,
            "role": user.role,
            "is_active": user.is_active,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        }
        for user in db.list_users()
    ])


@app.post("/admin/users/<int:chat_id>/role")
def admin_user_role(chat_id: int):
    if not authorized():
        return jsonify({"error": "unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    try:
        role = str(payload.get("role", "")).lower()
        ok = db.set_user_role(chat_id, role)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"updated": ok, "chat_id": chat_id, "role": role})


@app.post("/admin/users/<int:chat_id>/active")
def admin_user_active(chat_id: int):
    if not authorized():
        return jsonify({"error": "unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    ok = db.set_user_active(chat_id, bool(payload.get("active", True)))
    return jsonify({"updated": ok, "chat_id": chat_id, "active": bool(payload.get("active", True))})


@app.get("/admin")
def admin_page():
    return """<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>MarketObserver Admin</title><style>body{font-family:system-ui;max-width:760px;margin:40px auto;padding:0 20px;background:#0f172a;color:#e2e8f0}input,button{padding:10px;margin:4px;border-radius:6px;border:1px solid #475569}button{cursor:pointer;background:#22c55e;color:#052e16}pre{background:#1e293b;padding:16px;border-radius:8px}</style></head><body><h1>MarketObserver Admin</h1><p>أدخل مفتاح الإدارة لعرض مؤشرات الخدمة. لا تحفظ المفتاح في المتصفح المشترك.</p><input id='key' type='password' placeholder='ADMIN_API_KEY'><button onclick='loadStats()'>Load stats</button><pre id='out'>Waiting...</pre><script>async function loadStats(){const key=document.getElementById('key').value;const r=await fetch('/admin/stats',{headers:{'X-Admin-Key':key}});document.getElementById('out').textContent=await r.text()}</script></body></html>"""


def run_server():
    app.run(host="0.0.0.0", port=settings.port, debug=False, use_reloader=False)


def main():
    threading.Thread(target=run_server, daemon=True, name="health-server").start()
    threading.Thread(target=alert_loop, daemon=True, name="alert-worker").start()
    try:
        bot.remove_webhook()
    except Exception:
        logger.warning("could not remove old webhook", exc_info=True)
    logger.info("MarketObserver Pro started in %s mode", "paper" if settings.paper_trading else "live-disabled")
    while True:
        try:
            bot.polling(non_stop=True, interval=0, timeout=20)
        except Exception:
            logger.exception("telegram polling stopped; retrying")
            time.sleep(5)


if __name__ == "__main__":
    main()