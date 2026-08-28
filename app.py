from __future__ import annotations

import html
import io
import logging
import re
import threading
import time
from functools import lru_cache
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
from marketobserver.research import MarketResearch, ResearchSnapshot, headline_fingerprint, headline_importance_level
from marketobserver.risk import calculate_position_size
from marketobserver.llm import GroundedLLM

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("marketobserver")

settings = Settings.from_env()
db = Database(settings.database_url)
db.create_all()
market = MarketDataProvider()
research = MarketResearch()
llm = GroundedLLM(settings.llm_api_key, settings.llm_api_base, settings.llm_model)
paper_broker = PaperBroker()
live_broker = LiveBrokerNotConfigured()
last_signal_scan_at = 0.0
last_news_scan_at = 0.0
bot = telebot.TeleBot(settings.telegram_token, threaded=True)
app = Flask(__name__)


AR = {
    "start": "أهلًا بك في MarketObserver Pro. اكتب مثلًا: حلل الذهب، هل أدخل البيتكوين؟ أو احسب مخاطرة رأس المال 10000 بنسبة 1% دخول 4715 وقف 4690. يمكنك أيضًا استخدام /analyze و /alert و /risk و /paperbuy.",
    "data_error": "تعذر الحصول على بيانات سوق موثوقة لهذا الأصل حاليًا. لم يتم إنشاء بيانات بديلة ولن أعرض تحليلًا غير حقيقي. جرّب لاحقًا أو استخدم رمزًا من مزود بيانات آخر.",
}


def unknown_asset_text(lang: str) -> str:
    examples = "BTC, ETH, SOL, XAUUSD, XAGUSD, EURUSD, WTI, BRENT, GASOLINE, NATGAS, AAPL, TSLA, NVDA"
    return ("لم أتعرف على الأصل. اكتب اسمًا أو رمزًا واضحًا، مثل: الذهب، الفضة، برنت، البنزين، BTC، EURUSD، أو AAPL."
            if lang == "ar" else f"I could not identify the asset. Use a clear name or ticker, for example: {examples}.")


def out_of_scope_response(lang: str) -> str:
    if lang == "ar":
        return ("لا أملك إجابة موثوقة لهذا السؤال لأنه خارج نطاقي الحالي. أنا متخصص في تحليل الأسواق "
                "والأصول المدعومة، الأخبار الاقتصادية، المخاطر، الشروحات المالية، والتنبيهات. "
                "إذا أردت تحليلًا فاكتب اسم الأصل بوضوح، مثل: حلل الذهب أو أخبار BTC.")
    return ("I do not have a reliable answer because this question is outside my current scope. "
            "I specialize in supported market assets, economic news, risk, financial explanations, and alerts. "
            "For analysis, include the asset clearly, such as: analyze gold or BTC news.")


def education_response(lang: str, text: str, asset: Asset | None = None) -> str:
    lowered = text.lower()
    if "rsi" in lowered or "مؤشر القوة النسبية" in text or "مؤشر القوه النسبيه" in text:
        return ("RSI يقيس زخم الحركة من 0 إلى 100. ارتفاعه فوق 70 قد يعني تشبعًا شرائيًا، وانخفاضه تحت 30 قد يعني تشبعًا بيعيًا، لكن ذلك ليس إشارة بيع أو شراء منفردة. يجب دمجه مع الاتجاه والدعم والمقاومة وإدارة المخاطر."
                if lang == "ar" else "RSI measures momentum on a 0–100 scale. Above 70 may indicate overbought conditions and below 30 may indicate oversold conditions, but neither is a standalone buy or sell signal. Combine it with trend, support/resistance, and risk management.")
    if "وقف الخساره" in text or "وقف الخسارة" in text or "stop loss" in lowered:
        return ("وقف الخسارة مستوى إلغاء للصفقة؛ يحدد مسبقًا النقطة التي يصبح عندها السيناريو غير صالح. لا تضعه عشوائيًا، واحسب حجم الصفقة بحيث لا تتجاوز الخسارة المحتملة نسبة المخاطرة المسموح بها."
                if lang == "ar" else "A stop loss is a pre-defined invalidation level for a trade. It should be placed based on the market structure, not randomly, and position size should keep the potential loss within your risk limit.")
    if "دعم" in text or "مقاومه" in text or "مقاومة" in text or "support" in lowered or "resistance" in lowered:
        return ("الدعم منطقة قد يظهر عندها طلب، والمقاومة منطقة قد يظهر عندها عرض. هما منطقتان وليستا خطين مضمونين؛ يلزم انتظار تأكيد من السعر والحجم أو الإغلاق قبل اعتبار الكسر حقيقيًا."
                if lang == "ar" else "Support is an area where demand may appear, while resistance is an area where supply may appear. They are zones, not guaranteed lines; wait for price and, when available, volume or close confirmation before treating a breakout as real.")
    if "تضخم" in text or "التضخم" in text or "inflation" in lowered:
        return ("التضخم هو ارتفاع مستمر في المستوى العام للأسعار. تأثيره على الأصل ليس ثابتًا؛ يتأثر بالتوقعات، الفائدة، العملة، والسيولة، لذلك لا يكفي ذكر التضخم وحده لاتخاذ قرار تداول."
                if lang == "ar" else "Inflation is a sustained rise in the general price level. Its effect on an asset is not fixed; expectations, interest rates, currency, and liquidity also matter, so inflation alone is not enough for a trading decision.")
    if asset:
        name = asset.name_ar if lang == "ar" else asset.name_en
        return (f"{name} أصل مالي يمكنني تحليل سعره واتجاهه وأخباره عند طلب ذلك. أما التعريف الأساسي لهذا الأصل فلا يكفي وحده لاتخاذ قرار شراء أو بيع؛ اكتب مثلًا: حلل {name} أو ما أخباره؟"
                if lang == "ar" else f"{name} is a financial asset whose price, trend, and related news I can analyze. A basic description alone is not a buy or sell decision; ask, for example: analyze {name} or show its news.")
    return out_of_scope_response(lang)


def news_text(snapshot: ResearchSnapshot, lang: str) -> str:
    if not snapshot.items:
        return (f"لا توجد عناوين موثوقة متاحة حاليًا لـ {snapshot.asset.name_ar}. لم يتم اختلاق أخبار أو مشاعر سوقية."
                if lang == "ar" else f"No reliable headlines are available for {snapshot.asset.name_en} right now. No news or sentiment was invented.")
    asset_name = snapshot.asset.name_ar if lang == "ar" else snapshot.asset.name_en
    lines = [
        f"📰 <b>أخبار {html.escape(asset_name)} ({snapshot.asset.key})</b>" if lang == "ar" else f"📰 <b>{html.escape(asset_name)} news ({snapshot.asset.key})</b>",
        f"المشاعر التقريبية: إيجابي {snapshot.positive} | سلبي {snapshot.negative} | محايد {snapshot.neutral}" if lang == "ar" else f"Approximate sentiment: positive {snapshot.positive} | negative {snapshot.negative} | neutral {snapshot.neutral}",
        f"المصدر: {html.escape(snapshot.source)} | وقت الجمع: {html.escape(snapshot.as_of)}" if lang == "ar" else f"Source: {html.escape(snapshot.source)} | collected: {html.escape(snapshot.as_of)}",
        "",
    ]
    for index, item in enumerate(snapshot.items[:2], 1):
        headline, source_from_title = _headline_parts(item.title)
        headline = _translated_headline(headline, lang)
        source = source_from_title or snapshot.source
        label = {"positive": "إيجابي", "negative": "سلبي", "neutral": "محايد"}.get(item.sentiment, item.sentiment) if lang == "ar" else item.sentiment
        lines.append(f"<b>{index}. {html.escape(headline[:220])}</b>")
        lines.append(f"[{label}] | {html.escape(source)}")
        if item.link:
            lines.append(f"🔗 <a href=\"{html.escape(item.link, quote=True)}\">فتح المصدر الأصلي</a>" if lang == "ar" else f"🔗 <a href=\"{html.escape(item.link, quote=True)}\">Open original source</a>")
        lines.append("")
    lines.append("تصنيف المشاعر آلي وتقريبي للعناوين، وليس قياسًا شاملاً لمشاعر السوق." if lang == "ar" else "Headline sentiment is automated and approximate, not a complete measure of market sentiment.")
    return "\n".join(lines)


def rank_assets(lang: str) -> str:
    candidates = [asset for asset in ASSETS.values() if asset.supported][:12]
    ranked: list[tuple[float, Asset, Analysis, ResearchSnapshot]] = []
    for asset in candidates:
        try:
            result, _ = get_analysis(asset)
            snapshot = research.news(asset, limit=5)
            score = 0.0
            score += 2.0 if result.trend == "bullish" else -2.0 if result.trend == "bearish" else 0.0
            score += 1.0 if result.signal == "watch_long" else -1.0 if result.signal == "watch_short" else 0.0
            score += max(-1.0, min(1.0, (snapshot.positive - snapshot.negative) / 3.0))
            if result.rsi is not None and 45 <= result.rsi <= 65:
                score += 0.5
            ranked.append((score, asset, result, snapshot))
        except (DataUnavailable, ValueError, TypeError):
            continue
    ranked.sort(key=lambda row: row[0], reverse=True)
    if not ranked:
        return ("لا أستطيع ترتيب الأصول الآن لأن بيانات السوق الموثوقة غير متاحة. لن أخمّن أو أنشئ ترتيبًا وهميًا."
                if lang == "ar" else "I cannot rank assets because reliable market data is unavailable. I will not guess or fabricate a ranking.")
    if lang == "ar":
        lines = [
            "📊 ترتيب مراقبة أولي للأصول",
            "تم الجمع بين الاتجاه والإشارة وRSI ومشاعر عناوين الأخبار المتاحة. النتيجة ليست احتمالًا إحصائيًا ولا ضمانًا للربح.",
            "",
        ]
        for index, (score, asset, result, snapshot) in enumerate(ranked[:5], 1):
            lines.append(f"{index}. {asset.name_ar} ({asset.key}) | درجة مراقبة: {score:.2f} | الاتجاه: {result.trend} | الأخبار: +{snapshot.positive}/-{snapshot.negative}")
        lines.append("افحص الأصل المختار بتحليل متعدد الأطر وحدد نقطة إلغاء ومخاطرة قبل أي قرار.")
        return "\n".join(lines)
    lines = [
        "📊 Preliminary watch ranking",
        "Combines trend, signal, RSI, and available headline sentiment. This is not a statistical probability or profit guarantee.",
        "",
    ]
    for index, (score, asset, result, snapshot) in enumerate(ranked[:5], 1):
        lines.append(f"{index}. {asset.name_en} ({asset.key}) | watch score: {score:.2f} | trend: {result.trend} | news: +{snapshot.positive}/-{snapshot.negative}")
    lines.append("Inspect the selected asset with multi-timeframe analysis and define invalidation and risk before acting.")
    return "\n".join(lines)


def general_response(lang: str) -> str:
    if lang == "ar":
        return (
            "أنا MarketObserver Pro، مساعد لتحليل الأسواق والبيانات، ومطوّري إبراهيم المرقبي. أستطيع فهم طلبات مثل: «حلل الذهب»، "
            "«هل أدخل البيتكوين؟»، «ما أفضل أصل الآن؟»، «أظهر الأخبار»، و«احسب المخاطرة». كما أستطيع شرح RSI والاتجاه والدعم والمقاومة. "
            "اكتب سؤالك مباشرة، وسأبحث في البيانات المتاحة وأرد باللغة نفسها."
        )
    return (
        "I am MarketObserver Pro, a market-data and analysis assistant developed by Ibrahim Al-Marqbi. "
        "I can understand requests such as 'analyze gold', 'should I enter Bitcoin?', 'what is the best asset now?', "
        "'show the news', and 'calculate risk'. I can also explain RSI, trend, support, and resistance. "
        "Ask directly and I will research the available data and reply in the same language."
    )


def user_language(message: types.Message) -> str:
    text = getattr(message, "text", "") or ""
    if re.search(r"[\u0600-\u06ff]", text):
        return "ar"
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


def extract_numbers(text: str) -> list[float]:
    normalized = (text or "").replace(",", "")
    normalized = normalized.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    return [float(value) for value in re.findall(r"(?<![A-Za-z])[+-]?\d+(?:\.\d+)?", normalized)]


def risk_text(result, lang: str) -> str:
    if lang == "ar":
        return (
            "🛡️ حساب المخاطرة النظري\n"
            f"مبلغ المخاطرة: {result.risk_amount} USD\n"
            f"مسافة وقف الخسارة: {result.stop_distance}\n"
            f"الكمية النظرية: {result.quantity}\n"
            f"القيمة الاسمية: {result.notional} USD\n"
            "هذه نتيجة حسابية وليست توصية بحجم صفقة حقيقية."
        )
    return (
        "🛡️ Theoretical risk calculation\n"
        f"Risk amount: {result.risk_amount} USD\n"
        f"Stop distance: {result.stop_distance}\n"
        f"Theoretical quantity: {result.quantity}\n"
        f"Notional value: {result.notional} USD\n"
        "This is a calculation, not a recommendation to use a real position size."
    )


def natural_risk_response(message: types.Message, lang: str, text: str):
    numbers = extract_numbers(text)
    if len(numbers) < 4:
        bot.reply_to(message, "أرسل رأس المال ونسبة المخاطرة وسعر الدخول ووقف الخسارة، مثل: احسب مخاطرة رأس مال 10000 بنسبة 1% دخول 4715 وقف 4690." if lang == "ar" else "Provide capital, risk percent, entry, and stop, for example: calculate risk for 10000 capital, 1%, entry 4715, stop 4690.")
        return
    capital, risk_pct, entry, stop = numbers[:4]
    point_value = numbers[4] if len(numbers) >= 5 else 1.0
    try:
        result = calculate_position_size(capital, risk_pct, entry, stop, point_value, settings.max_risk_percent, settings.max_position_notional)
        bot.reply_to(message, risk_text(result, lang))
    except ValueError:
        bot.reply_to(message, "قيم المخاطرة غير صحيحة. تأكد من أن رأس المال والأسعار موجبة وأن نسبة المخاطرة ضمن الحد المسموح." if lang == "ar" else "Invalid risk values. Check positive capital/prices and the permitted risk limit.")


def user_timezone(message: types.Message) -> str:
    user = db.get_user(message.chat.id)
    return getattr(user, "tz_name", None) or "Asia/Riyadh"


def format_timestamp(value, lang: str, tz_name: str = "Asia/Riyadh") -> str:
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        try:
            local = value.astimezone(ZoneInfo(tz_name))
        except ZoneInfoNotFoundError:
            local = value.astimezone(ZoneInfo("Asia/Riyadh"))
        zone_label = local.tzname() or tz_name
        return local.strftime("%Y-%m-%d %H:%M") + f" {zone_label}"
    except Exception:
        return str(value).replace("`", "")


def analysis_text(asset: Asset, result: Analysis, lang: str, tz_name: str = "Asia/Riyadh") -> str:
    trend_ar = {"bullish": "صاعد", "bearish": "هابط", "sideways": "جانبي"}[result.trend]
    signal_ar = {"watch_long": "مراقبة شراء محتملة", "watch_short": "مراقبة بيع محتملة", "neutral": "محايد"}[result.signal]
    source = market.last_source(asset.key) or ("غير معروف" if lang == "ar" else "unknown")
    timestamp = format_timestamp(result.candle_time, lang, tz_name)
    if lang == "ar":
        return (
            f"📊 تحليل {asset.name_ar} ({asset.key})\n\n"
            f"السعر: {result.price} {asset.quote}\nRSI: {result.rsi}\n"
            f"SMA20: {result.sma20} | SMA50: {result.sma50}\nATR14: {result.atr14}\n"
            f"الدعم القريب: {result.support}\nالمقاومة القريبة: {result.resistance}\n"
            f"الاتجاه: {trend_ar}\nالحالة: {signal_ar}\n\n"
            f"آخر شمعة: {timestamp}\nعدد الشموع: {result.data_points}\n"
            f"مصدر البيانات: {source}\n"
            "هذه قراءة آلية للمؤشرات وليست ضمانًا للربح أو توصية شخصية."
        )
    return (
        f"📊 {asset.name_en} analysis ({asset.key})\n\n"
        f"Price: {result.price} {asset.quote}\nRSI: {result.rsi}\n"
        f"SMA20: {result.sma20} | SMA50: {result.sma50}\nATR14: {result.atr14}\n"
        f"Nearby support: {result.support}\nNearby resistance: {result.resistance}\n"
        f"Trend: {result.trend}\nState: {result.signal}\n\n"
        f"Last candle: {timestamp}\nCandles: {result.data_points}\n"
        f"Data source: {source}\n"
        "This is automated indicator analysis, not a guarantee or personalized advice."
    )


def translate_advice_reason(reason: str, lang: str) -> str:
    if lang != "ar":
        return reason
    translated = reason
    translated = translated.replace(
        "The higher and execution timeframes align bullishly, while momentum remains below an extreme zone.",
        "الإطار الأعلى وإطار التنفيذ متوافقان باتجاه صاعد، والزخم لم يصل إلى منطقة مبالغة.",
    )
    translated = translated.replace(
        "The higher and execution timeframes align bearishly, while downside momentum is not yet exhausted.",
        "الإطار الأعلى وإطار التنفيذ متوافقان باتجاه هابط، والزخم الهابط لم يصل إلى حالة استنفاد.",
    )
    translated = translated.replace(
        "The timeframes are mixed or momentum does not confirm a clean setup; waiting for confirmation is the safer state.",
        "الأطر الزمنية متباينة أو أن الزخم لا يؤكد فرصة واضحة؛ الانتظار حتى ظهور تأكيد أوضح هو الحالة الأكثر تحفظًا.",
    )
    translated = translated.replace("Unavailable views:", "الأطر غير المتاحة:")
    return translated


def advice_text(advice: Advice, lang: str, tz_name: str = "Asia/Riyadh") -> str:
    if lang == "ar":
        action = {"watch_long": "مراقبة شراء مشروطة", "watch_short": "مراقبة بيع مشروطة", "wait": "انتظار"}[advice.action]
        confidence = {"low": "منخفضة", "medium": "متوسطة", "high": "مرتفعة"}.get(advice.confidence, advice.confidence)
        lines = [
            f"مساعد القرار: {advice.asset.name_ar} ({advice.asset.key})",
            f"السعر المرجعي: {advice.current_price} {advice.asset.quote}",
            f"الحالة: {action} | الثقة: {confidence}",
            f"السبب: {translate_advice_reason(advice.reason, lang)}",
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
            f"المصدر: {advice.source or 'غير معروف'} | آخر تحديث: {format_timestamp(advice.as_of, lang, tz_name)}",
            "هذه سيناريوهات مشروطة وليست ضمانًا أو توصية شخصية. لا تدخل دون تحديد رأس المال والمخاطرة ووقف الخسارة.",
        ]
        return "\n".join(lines)
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
        f"Source: {advice.source or 'unknown'} | last update: {format_timestamp(advice.as_of, lang, tz_name)}",
        "These are conditional scenarios, not a guarantee or personalized advice. Define capital, risk, and a stop before acting.",
    ]
    return "\n".join(lines)


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
    bot.send_message(chat_id, ar if lang == "ar" else en)


@bot.message_handler(commands=["start", "help"])
def start_cmd(message: types.Message):
    remember_user(message)
    bot.reply_to(message, AR["start"] if user_language(message) == "ar" else "Welcome. Use /analyze BTC, /alert below BTC 60000, /risk, and /paperbuy for paper trading.")


def signal_text(rows, lang: str, tz_name: str) -> str:
    now = format_timestamp(datetime.now(timezone.utc), lang, tz_name)
    if lang == "ar":
        lines = ["🔔 إشارات مراقبة تلقائية", f"وقت الفحص: {now}", "هذه إشارات بحثية مشروطة وليست أوامر شراء أو بيع:"]
        for asset, result in rows:
            direction = "مراقبة صعود" if result.signal == "watch_long" else "مراقبة هبوط"
            lines.append(f"{asset.name_ar} ({asset.key}) — {direction} | السعر {result.price} | RSI {result.rsi} | الاتجاه {result.trend}")
        lines.append("تحقق من التحليل متعدد الأطر وحدد وقف الخسارة قبل أي قرار.")
        return "\n".join(lines)
    lines = ["🔔 Automatic watch signals", f"Scan time: {now}", "These are conditional research signals, not buy or sell orders:"]
    for asset, result in rows:
        direction = "upside watch" if result.signal == "watch_long" else "downside watch"
        lines.append(f"{asset.name_en} ({asset.key}) — {direction} | price {result.price} | RSI {result.rsi} | trend {result.trend}")
    lines.append("Verify multi-timeframe analysis and define a stop before acting.")
    return "\n".join(lines)


def scan_signal_candidates(limit: int = 3):
    rows = []
    for asset in list(ASSETS.values())[:12]:
        try:
            result, _ = get_analysis(asset)
            if result.signal in {"watch_long", "watch_short"}:
                strength = (2 if result.trend in {"bullish", "bearish"} else 0) + (1 if result.rsi is not None and 35 < result.rsi < 70 else 0)
                rows.append((strength, asset, result))
        except (DataUnavailable, ValueError, TypeError):
            continue
    rows.sort(key=lambda row: row[0], reverse=True)
    return [(asset, result) for _, asset, result in rows[:limit]]


@bot.message_handler(commands=["signals"])
def signals_cmd(message: types.Message):
    lang = user_language(message)
    remember_user(message)
    parts = message.text.split()
    if len(parts) == 1:
        user = db.get_user(message.chat.id)
        enabled = bool(user and user.signals_enabled)
        bot.reply_to(message, (f"المراقبة التلقائية: {'مفعلة' if enabled else 'متوقفة'}. استخدم /signals on أو /signals off." if lang == "ar" else f"Automatic signal monitoring: {'on' if enabled else 'off'}. Use /signals on or /signals off."))
        return
    value = parts[1].lower()
    if value not in {"on", "off", "تشغيل", "ايقاف", "إيقاف"}:
        bot.reply_to(message, "الاستخدام: /signals on أو /signals off" if lang == "ar" else "Usage: /signals on or /signals off")
        return
    enabled = value in {"on", "تشغيل"}
    db.set_signals_enabled(message.chat.id, enabled)
    bot.reply_to(message, "تم تفعيل المراقبة التلقائية." if enabled and lang == "ar" else "تم إيقاف المراقبة التلقائية." if lang == "ar" else "Automatic signal monitoring enabled." if enabled else "Automatic signal monitoring disabled.")


def _headline_parts(title: str) -> tuple[str, str | None]:
    """Split a common RSS title suffix such as ' - Reuters' from the headline."""
    cleaned = re.sub(r"\s+", " ", (title or "").strip())
    if " - " in cleaned:
        headline, source = cleaned.rsplit(" - ", 1)
        if 2 <= len(source) <= 48:
            return headline.strip(), source.strip()
    return cleaned, None


def _news_time(published: str | None, lang: str, tz_name: str) -> str:
    if not published:
        return "غير معروف" if lang == "ar" else "unknown"
    try:
        return format_timestamp(parsedate_to_datetime(published), lang, tz_name)
    except (TypeError, ValueError, OverflowError):
        return published


@lru_cache(maxsize=256)
def _translated_headline(headline: str, lang: str) -> str:
    if lang != "ar" or re.search(r"[\u0600-\u06ff]", headline or ""):
        return headline
    try:
        translated = llm.translate_headline(headline, "ar")
        return translated[:220] if translated else headline
    except Exception:
        logger.warning("headline translation unavailable; preserving original title")
        return headline


def _news_guidance(sentiment: str, lang: str) -> tuple[str, str]:
    if lang == "ar":
        before = "تجنب الدخول أثناء أول حركة؛ خفّض الرافعة وانتظر هدوء السبريد."
        after = "بعد الخبر: انتظر إغلاق شمعة تأكيد، ثم قيّم الاتجاه والدعم والمقاومة؛ الخبر وحده ليس إشارة دخول."
    else:
        before = "Avoid entering during the first move; reduce leverage and wait for spreads to normalize."
        after = "After the news: wait for a confirming candle, then reassess trend and levels; the headline alone is not an entry signal."
    return before, after


def news_alert_text(asset: Asset, items, lang: str, tz_name: str) -> str:
    now = format_timestamp(datetime.now(timezone.utc), lang, tz_name)
    asset_name = asset.name_ar if lang == "ar" else asset.name_en
    lines = [
        f"🚨 <b>خبر مهم</b> | <b>{html.escape(asset_name)} ({asset.key})</b>" if lang == "ar" else f"🚨 <b>Important news</b> | <b>{html.escape(asset_name)} ({asset.key})</b>",
        f"🕒 وقت الإرسال: {html.escape(now)}" if lang == "ar" else f"🕒 Alert time: {html.escape(now)}",
        "",
    ]
    for index, item in enumerate(items[:2]):
        headline, source_from_title = _headline_parts(item.title)
        headline = _translated_headline(headline, lang)
        source = source_from_title or "Google News RSS"
        level = headline_importance_level(item)
        if lang == "ar":
            level_label = {"high": "عالية", "medium": "متوسطة", "low": "منخفضة"}.get(level, level)
            sentiment_label = {"positive": "إيجابي", "negative": "سلبي", "neutral": "محايد"}.get(item.sentiment, "محايد")
            impact = "قد يرفع التقلب والسيولة على المدى القصير؛ لا يعني اتجاهًا مضمونًا."
            before, after = _news_guidance(item.sentiment, lang)
            lines.extend([
                f"<b>{index + 1}. {html.escape(headline[:220])}</b>",
                f"🟠 الأهمية: <b>{level_label}</b> | النبرة: {sentiment_label}",
                f"🕒 الخبر: {html.escape(_news_time(item.published, lang, tz_name))} | المصدر: {html.escape(source)}",
                f"📌 الأثر المحتمل: {impact}",
                f"🛡 قبل الخبر: {before}",
                f"✅ بعد الخبر: {after}",
            ])
        else:
            impact = "May increase short-term volatility and liquidity; it does not guarantee direction."
            before, after = _news_guidance(item.sentiment, lang)
            lines.extend([
                f"<b>{index + 1}. {html.escape(headline[:220])}</b>",
                f"🟠 Importance: <b>{level}</b> | tone: {html.escape(item.sentiment)}",
                f"🕒 Published: {html.escape(_news_time(item.published, lang, tz_name))} | source: {html.escape(source)}",
                f"📌 Potential impact: {impact}",
                f"🛡 Before news: {before}",
                f"✅ After news: {after}",
            ])
        if item.link:
            lines.append(f"🔗 <a href=\"{html.escape(item.link, quote=True)}\">فتح المصدر الأصلي</a>" if lang == "ar" else f"🔗 <a href=\"{html.escape(item.link, quote=True)}\">Open original source</a>")
        lines.append("")
    lines.append("ℹ️ تنبيه بحثي: تحقق من الخبر الأصلي، ولا تعتبره أمرًا مباشرًا بالتداول." if lang == "ar" else "ℹ️ Research alert: verify the original article; this is not a direct trading order.")
    return "\n".join(lines)


@bot.message_handler(commands=["newsalerts"])
def newsalerts_cmd(message: types.Message):
    lang = user_language(message)
    remember_user(message)
    parts = message.text.split(maxsplit=1)
    if len(parts) == 1:
        user = db.get_user(message.chat.id)
        enabled = bool(user is None or not user.news_preference_set or user.news_enabled)
        scope = (user.news_assets if user else "ALL")
        bot.reply_to(message, f"تنبيهات الأخبار: {'مفعلة' if enabled else 'متوقفة'} | النطاق: {scope}. استخدم /newsalerts on أو /newsalerts all أو /newsalerts off." if lang == "ar" else f"News alerts: {'on' if enabled else 'off'} | scope: {scope}. Use /newsalerts on, /newsalerts all, or /newsalerts off.")
        return
    value = parts[1].strip().lower()
    if value in {"off", "ايقاف", "إيقاف"}:
        db.set_news_enabled(message.chat.id, False)
        bot.reply_to(message, "تم إيقاف تنبيهات الأخبار." if lang == "ar" else "News alerts disabled.")
        return
    if value in {"all", "الكل", "كل"}:
        scope = ",".join(list(ASSETS.keys())[:12])
    elif value in {"on", "تشغيل"}:
        scope = (db.get_user(message.chat.id).last_asset if db.get_user(message.chat.id) else "BTC")
    else:
        resolved = resolve_asset(value)
        if not resolved:
            bot.reply_to(message, "اكتب /newsalerts on أو all أو اسم أصل مثل BTC أو الذهب." if lang == "ar" else "Use /newsalerts on, all, or an asset such as BTC or gold.")
            return
        scope = resolved.key
    db.set_news_enabled(message.chat.id, True, scope)
    bot.reply_to(message, f"تم تفعيل تنبيهات الأخبار للنطاق: {scope}." if lang == "ar" else f"News alerts enabled for: {scope}.")


@bot.message_handler(commands=["timezone"])
def timezone_cmd(message: types.Message):
    lang = user_language(message)
    remember_user(message)
    parts = message.text.split(maxsplit=1)
    if len(parts) == 1:
        current = user_timezone(message)
        bot.reply_to(message, f"منطقتك الزمنية الحالية: {current}. غيّرها مثلًا: /timezone Asia/Riyadh" if lang == "ar" else f"Your current timezone is {current}. Change it with /timezone Asia/Riyadh")
        return
    tz_name = parts[1].strip()
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        bot.reply_to(message, "المنطقة الزمنية غير صحيحة. استخدم اسمًا مثل Asia/Riyadh أو Asia/Dubai أو Europe/London." if lang == "ar" else "Unknown timezone. Use a name such as Asia/Riyadh, Asia/Dubai, or Europe/London.")
        return
    db.set_timezone(message.chat.id, tz_name)
    bot.reply_to(message, f"تم ضبط التوقيت على {tz_name}." if lang == "ar" else f"Timezone set to {tz_name}.")


@bot.message_handler(commands=["analyze"])
def analyze_cmd(message: types.Message):
    parts = message.text.split(maxsplit=1)
    asset = selected_asset(message, parts[1] if len(parts) == 2 else None)
    remember_user(message, asset)
    if not asset:
        bot.reply_to(message, unknown_asset_text(user_language(message)))
        return
    try:
        result, _ = get_analysis(asset)
        bot.reply_to(message, analysis_text(asset, result, user_language(message), user_timezone(message)))
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
        bot.reply_to(message, unknown_asset_text(user_language(message)))
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
    if request.intent == "general":
        bot.reply_to(message, general_response(lang))
        return
    if request.intent == "education":
        bot.reply_to(message, education_response(lang, text, asset))
        return
    if request.intent == "unknown":
        bot.reply_to(message, out_of_scope_response(lang) if not asset else education_response(lang, text, asset))
        return
    if request.intent == "rank":
        bot.reply_to(message, rank_assets(lang))
        return
    if request.intent == "risk":
        natural_risk_response(message, lang, text)
        return
    if request.intent == "news" and asset:
        try:
            bot.reply_to(message, news_text(research.news(asset), lang), parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            logger.exception("news research failed")
            bot.reply_to(message, "تعذر جلب الأخبار حاليًا." if lang == "ar" else "News research is temporarily unavailable.")
        return
    if request.intent == "news" and not asset:
        bot.reply_to(message, "اذكر الأصل المطلوب مع الأخبار، مثل: أخبار الذهب أو أخبار BTC. لم أستخدم أصلًا افتراضيًا." if lang == "ar" else "Name the asset with the news request, for example: gold news or BTC news. No default asset was used.")
        return
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
                bot.reply_to(message, advice_text(advice, lang, user_timezone(message)))
            else:
                result, _ = get_analysis(asset)
                bot.reply_to(message, analysis_text(asset, result, lang, user_timezone(message)))
        except DataUnavailable:
            bot.reply_to(message, AR["data_error"] if lang == "ar" else "Reliable market data is unavailable right now. No synthetic data was used.")
        except Exception:
            logger.exception("natural-language request failed")
            bot.reply_to(message, "تعذر تنفيذ الطلب حاليًا." if lang == "ar" else "The request could not be completed right now.")
    else:
        bot.reply_to(message, "اذكر اسم الأصل مثل الذهب أو BTC أو AAPL، أو اكتب سؤالك مع اسم الأصل." if lang == "ar" else "Mention an asset such as gold, BTC, or AAPL with your question.")


def alert_loop():
    global last_signal_scan_at, last_news_scan_at
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
            now = time.time()
            if now - last_signal_scan_at >= settings.signal_scan_seconds:
                rows = scan_signal_candidates()
                if rows:
                    current_utc = datetime.now(timezone.utc)
                    for user in db.signal_users():
                        if user.signal_cooldown_until and user.signal_cooldown_until > current_utc:
                            continue
                        try:
                            bot.send_message(user.chat_id, signal_text(rows, user.language, user.tz_name))
                            db.set_signal_cooldown(user.chat_id, current_utc + timedelta(hours=1))
                        except Exception:
                            logger.exception("signal delivery failed chat_id=%s", user.chat_id)
                last_signal_scan_at = now
            if now - last_news_scan_at >= settings.news_scan_seconds:
                users = db.news_users()
                snapshots = {}
                pending_fingerprints = set()
                for user in users:
                    scope = (user.news_assets or "ALL").strip()
                    requested = list(ASSETS.keys())[:12] if scope.upper() == "ALL" else [key.strip() for key in scope.split(",") if key.strip()]
                    for asset_key in requested:
                        asset = ASSETS.get(asset_key)
                        if asset and asset.key not in snapshots:
                            try:
                                snapshots[asset.key] = research.important_news(asset, limit=5)
                            except Exception:
                                logger.exception("news scan failed asset=%s", asset_key)
                current_news_time = datetime.now(timezone.utc)
                for user in users:
                    if user.news_cooldown_until and user.news_cooldown_until > current_news_time:
                        continue
                    scope = (user.news_assets or "ALL").strip()
                    requested = list(ASSETS.keys())[:12] if scope.upper() == "ALL" else [key.strip() for key in scope.split(",") if key.strip()]
                    for asset_key in requested:
                        snapshot = snapshots.get(asset_key)
                        if not snapshot or not snapshot.items:
                            continue
                        fresh = []
                        for item in snapshot.items:
                            fingerprint = headline_fingerprint(item)
                            if not db.news_was_seen(fingerprint):
                                fresh.append(item)
                                pending_fingerprints.add(fingerprint)
                        if fresh:
                            for item in fresh[:2]:
                                try:
                                    bot.send_message(user.chat_id, news_alert_text(snapshot.asset, [item], user.language, user.tz_name), parse_mode="HTML", disable_web_page_preview=True)
                                    db.set_news_cooldown(user.chat_id, datetime.now(timezone.utc) + timedelta(minutes=30))
                                except Exception:
                                    logger.exception("news delivery failed chat_id=%s", user.chat_id)
                for fingerprint in pending_fingerprints:
                    db.mark_news_seen(fingerprint)
                last_news_scan_at = now
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