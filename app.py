from __future__ import annotations

import html
import io
import json
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
from telebot.apihelper import ApiTelegramException

from marketobserver.assets import ASSET_CLASS_NAMES, ASSET_CLASS_ORDER, ASSETS, BINARY_POPULAR, Asset, assets_by_class, resolve_asset
from marketobserver.analysis import Analysis, analyze
from marketobserver.advisor import Advice, build_advice
from marketobserver.nlp import parse_request
from marketobserver.broker import LiveBrokerNotConfigured, OrderRequest, PaperBroker
from marketobserver.config import Settings
from marketobserver.db import Database, cooldown_active
from marketobserver.calendar import COUNTRIES, EconomicCalendar, IMPACT_AR, IMPACT_EN
from marketobserver.learning import calibration_for, resolve_due as resolve_journal_due, stats_report
from marketobserver.live import LivePriceProvider, Quote, QuoteUnavailable
from marketobserver.market_data import DataUnavailable, MarketDataProvider
from marketobserver.binary import decide as build_binary_verdict, render as render_binary, to_dict as binary_to_dict
from marketobserver.backtest import HistoryUnavailable, backtest as run_backtest, render as render_backtest, to_dict as backtest_to_dict
from marketobserver.research import MarketResearch, ResearchSnapshot, headline_age_hours, headline_fingerprint, headline_importance_level, is_fresh
from marketobserver.risk import calculate_position_size
from marketobserver.llm import GroundedLLM
from marketobserver.smc import build_report as build_smc_report
from marketobserver.smc_text import LANGS as SMC_LANGS, render as render_smc, render_brief as render_smc_brief, to_dict as smc_to_dict
from marketobserver.decision import (
    decide as build_strict_decision,
    render as render_decision,
    render_brief as render_decision_brief,
    to_dict as decision_to_dict,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("marketobserver")

settings = Settings.from_env()
db = Database(settings.database_url)
db.create_all()
migrated_news_users = db.migrate_news_subscriptions()
if migrated_news_users:
    logger.info("automatically enrolled %s active users in news alerts", migrated_news_users)
market = MarketDataProvider()
live = LivePriceProvider(settings.live_price_ttl_seconds)
research = MarketResearch()
calendar_feed = EconomicCalendar()
llm = GroundedLLM(settings.llm_api_key, settings.llm_api_base, settings.llm_model)
paper_broker = PaperBroker()
live_broker = LiveBrokerNotConfigured()
last_signal_scan_at = 0.0
last_news_scan_at = 0.0
last_calendar_scan_at = 0.0
last_journal_scan_at = 0.0
bot = telebot.TeleBot(settings.telegram_token, threaded=True)
app = Flask(__name__)


AR = {
    "start": "أهلًا بك في MarketObserver Pro. اكتب مثلًا: حلل الذهب، سعر البيتكوين، ثنائي EURUSD، باك تست الذهب، ما أخبار التقويم اليوم؟ أو كم دقتك؟ الأوامر: /price و /assets و /binary و /backtest (اختبار الماضي) و /calendar و /stats و /capital و /analyze و /smc و /decision و /alert و /risk.",
    "data_error": "تعذر الحصول على بيانات سوق موثوقة لهذا الأصل حاليًا. لم يتم إنشاء بيانات بديلة ولن أعرض تحليلًا غير حقيقي. جرّب لاحقًا أو استخدم رمزًا من مزود بيانات آخر.",
    "unknown_command": "لا يوجد أمر بهذا الاسم، لذلك لم يُنفَّذ أي شيء. الأوامر المتاحة: /start /price /assets /binary /backtest /calendar /stats /capital /analyze /smc /decision /chart /risk /alert /alerts /cancel_alert /signals /newsalerts /timezone /paperbuy /papersell /broadcast",
}

EN = {
    "unknown_command": "There is no such command, so nothing was executed. Available commands: /start /price /assets /binary /backtest /calendar /stats /capital /analyze /smc /decision /chart /risk /alert /alerts /cancel_alert /signals /newsalerts /timezone /paperbuy /papersell /broadcast",
}


def unknown_asset_text(lang: str) -> str:
    examples = "BTC, ETH, SOL, BNB, XRP, DOGE, XAUUSD, EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, WTI, AAPL, TSLA"
    return ("لم أتعرف على الأصل. اكتب اسمًا أو رمزًا واضحًا، مثل: الذهب، البيتكوين، EURUSD، الباوند ين، أو DOGE — أو اعرض كل الأصول بـ /assets."
            if lang == "ar" else f"I could not identify the asset. Use a clear name or ticker, for example: {examples}. See /assets for the full catalog.")


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
    if "سيوله" in lowered or "السيولة" in text or "liquidity" in lowered:
        return ("السيولة هي حجم الأوامر المعلّقة عند مستوى سعري. يتحرك السعر غالبًا نحو القمم أو القيعان المتساوية وأطراف الشموع الطويلة لأنها مواضع تجمع أوامر وقف الخسارة؛ كسر سيولة بذيل فقط لا يعتبر كسر هيكل. لا تُتداول السيولة منفردة بل مع الاتجاه وتأكيد الإغلاق والحجم."
                if lang == "ar" else
                "Liquidity is the resting orders stacked at a price area. Price is often drawn to equal highs/lows and long wicks because that is where stop orders cluster; a wick-only run is a sweep, not a structure break. Liquidity is never traded alone: it needs trend, closing confirmation and volume.")
    if "اوردر بلوك" in lowered or "بلوك الاوامر" in lowered or "order block" in lowered:
        return ("أوردر بلوك هو آخر شمعة معاكسة قبل حركة إزاحة كسرت الهيكل؛ تُرسم من قاع/قمة تلك الشمعة وتبقى صالحة حتى إغلاق يخترقها. الأفضل أن تكون غير مختبرة (fresh) وأن تتكون بعد صيد سيولة، مع وقف خلفها مباشرة."
                if lang == "ar" else
                "An order block is the last opposing candle before the displacement leg that broke structure. It is drawn from that candle's range and stays valid until a close passes through it. Prefer a fresh, unmitigated block formed after a liquidity sweep, with the stop just beyond it.")
    if "فجوه سعريه" in lowered or "فجوات سعريه" in lowered or "fair value gap" in lowered or "fvg" in lowered:
        return ("الفجوة السعرية (FVG) هي فراغ ثلاث شموع لا يجد فيه السعر تداولًا متوازنًا؛ تُقاس بنهاية الشمعة الأولى وبداية الثالثة. تُستخدم كمنطقة اهتمام، وكلما قلّ امتلاؤها كانت أنظف؛ الفجوة الممتلئة أكثر من 60% تفقد قيمتها."
                if lang == "ar" else
                "A fair value gap is a three-candle imbalance where trade was one-sided. It is measured between the first candle's extreme and the third candle's opposite extreme. A lightly filled gap is cleaner; once it is filled beyond roughly 60% it loses its value as a point of interest.")
    if "bос" in lowered or "bos" in lowered or "كسر الهيكل" in lowered or "choch" in lowered or "تغير الشخصيه" in lowered:
        return ("BOS هو إغلاق شمعة خارج آخر قمة أو قاع مؤكد في اتجاه الهيكل الحالي، بينما CHoCH هو أول إغلاق عكس الهيكل ويقرأ كإنذار انعكاس لا كدخول. الذيل الذي يخترق المستوى ثم يعود الإغلاق للداخل يسمى صيد سيولة ولا يُحسب كسرًا."
                if lang == "ar" else
                "BOS is a candle closing beyond the last confirmed high or low in the direction of the current structure. CHoCH is the first close against it and is read as an early reversal warning, not an entry. A wick through a level with the close back inside is a liquidity sweep, never a break.")
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


def quote_text(asset: Asset, quote: Quote, lang: str) -> str:
    name = asset.name_ar if lang == "ar" else asset.name_en
    if lang == "ar":
        return (f"💰 {name} ({asset.key}): {quote.price} {asset.quote}\n"
                f"📡 المصدر: {quote.source} | عمر السعر: {quote.age_seconds:.1f} ثانية")
    return (f"💰 {name} ({asset.key}): {quote.price} {asset.quote}\n"
            f"📡 Source: {quote.source} | quote age: {quote.age_seconds:.1f}s")


def assets_text(lang: str, asset_class: str | None = None) -> str:
    classes = [asset_class] if asset_class in ASSET_CLASS_ORDER else list(ASSET_CLASS_ORDER)
    total = sum(len(assets_by_class(cls)) for cls in classes)
    lines = [f"📚 كتالوج الأصول ({total})" if lang == "ar" else f"📚 Asset catalog ({total})"]
    for cls in classes:
        names = ASSET_CLASS_NAMES[cls]
        lines.append("")
        lines.append(f"━━ {names[0] if lang == 'ar' else names[1]} ━━")
        for asset in assets_by_class(cls):
            label = asset.name_ar if lang == "ar" else asset.name_en
            lines.append(f"• {asset.key} — {label}")
    lines.append("")
    lines.append("مثال: /price EURUSD أو /binary BTC أو /smc الذهب" if lang == "ar" else "Examples: /price EURUSD, /binary BTC, /smc gold")
    return "\n".join(lines)


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
    # The smart-money buttons are part of the entry point so the two ways of
    # driving the system (typed commands and taps) are discoverable together.
    language = user_language(message)
    remembered = db.get_user(message.chat.id)
    quick_asset = remembered.last_asset if remembered and remembered.last_asset else "BTC"
    bot.reply_to(message, AR["start"] if language == "ar" else (
        "Welcome. Use /analyze BTC, /smc BTC (multi-timeframe smart-money report with buttons), "
        "/alert below BTC 60000, /risk, and /paperbuy for paper trading."),
        reply_markup=smc_keyboard(quick_asset, language, "full"))


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


def _headline_age_text(item, lang: str) -> str:
    age = headline_age_hours(item)
    if age is None:
        return "الآن" if lang == "ar" else "now"
    if age < 1:
        minutes = max(1, int(age * 60))
        return f"قبل {minutes} د" if lang == "ar" else f"{minutes}m ago"
    if age < 24:
        return f"قبل {age:.0f} س" if lang == "ar" else f"{age:.0f}h ago"
    return f"قبل {age / 24:.0f} يوم" if lang == "ar" else f"{age / 24:.0f}d ago"


def news_alert_text(asset: Asset, items, lang: str, tz_name: str) -> str:
    """Compact auto-alert: one story, impact label, age, source link.

    The engine sends a single top story per cycle (see the news scanner), so
    this format stays short enough to read at a glance; a second item is only
    ever rendered for manual calls, never for automatic bursts.
    """
    asset_name = asset.name_ar if lang == "ar" else asset.name_en
    lines = [
        f"🚨 <b>خبر عالي التأثير</b> | <b>{html.escape(asset_name)} ({asset.key})</b>" if lang == "ar" else f"🚨 <b>High-impact news</b> | <b>{html.escape(asset_name)} ({asset.key})</b>",
    ]
    for index, item in enumerate(items[:2]):
        headline, source_from_title = _headline_parts(item.title)
        headline = _translated_headline(headline, lang)
        source = source_from_title or "Google News RSS"
        level = headline_importance_level(item)
        if lang == "ar":
            level_label = {"high": "عالية", "medium": "متوسطة", "low": "منخفضة"}.get(level, level)
            sentiment_label = {"positive": "إيجابي 📈", "negative": "سلبي 📉", "neutral": "محايد"}.get(item.sentiment, "محايد")
            lines.extend([
                "",
                f"<b>{html.escape(headline[:220])}</b>",
                f"🟠 الأهمية: <b>{level_label}</b> | النبرة: {sentiment_label}",
                f"🕒 {_headline_age_text(item, lang)} | المصدر: {html.escape(source)}",
            ])
        else:
            lines.extend([
                "",
                f"<b>{html.escape(headline[:220])}</b>",
                f"🟠 Importance: <b>{level}</b> | tone: {html.escape(item.sentiment)}",
                f"🕒 {_headline_age_text(item, lang)} | source: {html.escape(source)}",
            ])
        if item.link:
            lines.append(f"🔗 <a href=\"{html.escape(item.link, quote=True)}\">فتح المصدر الأصلي</a>" if lang == "ar" else f"🔗 <a href=\"{html.escape(item.link, quote=True)}\">Open original source</a>")
    lines.append("")
    lines.append("📌 أراقب تأثيره على السعر وسأخبرك إذا تحرك السوق بقوة." if lang == "ar" else "📌 Watching its price impact — I will report back if the market moves strongly.")
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


@bot.message_handler(commands=["price"])
def price_cmd(message: types.Message):
    parts = message.text.split(maxsplit=1)
    asset = selected_asset(message, parts[1] if len(parts) == 2 else None)
    remember_user(message, asset)
    lang = user_language(message)
    if not asset:
        bot.reply_to(message, unknown_asset_text(lang))
        return
    try:
        quote = live.get_quote(asset)
        bot.reply_to(message, quote_text(asset, quote, lang))
    except QuoteUnavailable:
        bot.reply_to(message, AR["data_error"] if lang == "ar" else "No live quote is available for this asset right now.")
    except Exception:
        logger.exception("price failed for %s", asset.key)
        bot.reply_to(message, "تعذر جلب السعر مؤقتًا." if lang == "ar" else "Price lookup failed temporarily.")


@bot.message_handler(commands=["assets"])
def assets_cmd(message: types.Message):
    lang = user_language(message)
    remember_user(message)
    parts = message.text.split(maxsplit=1)
    wanted = (parts[1].strip().lower() if len(parts) == 2 else "")
    aliases = {"crypto": "crypto", "كريبتو": "crypto", "رقمية": "crypto",
               "forex": "forex", "فوركس": "forex", "عملات": "forex",
               "metals": "metals", "معادن": "metals", "ذهب": "metals",
               "commodity": "commodity", "commodities": "commodity", "سلع": "commodity", "نفط": "commodity",
               "index": "index", "indices": "index", "مؤشرات": "index",
               "stock": "stock", "stocks": "stock", "أسهم": "stock", "اسهم": "stock"}
    asset_class = aliases.get(wanted)
    if wanted and not asset_class:
        bot.reply_to(message, "استخدم: /assets [crypto|forex|metals|commodity|index|stock]" if lang == "ar" else "Usage: /assets [crypto|forex|metals|commodity|index|stock]")
        return
    for chunk in split_broadcast_text(assets_text(lang, asset_class)):
        bot.send_message(message.chat.id, chunk)


@bot.message_handler(commands=["capital"])
def capital_cmd(message: types.Message):
    lang = user_language(message)
    remember_user(message)
    parts = (message.text or "").split()
    if len(parts) == 1:
        user = db.get_user(message.chat.id)
        capital = user.capital if user and user.capital else 1000.0
        risk_pct = user.risk_percent if user and user.risk_percent else 1.0
        bot.reply_to(message, f"💰 رأس مالك المسجل: {capital:g}$ | مخاطرة الصفقة: {risk_pct:g}%. غيّرهما بـ: /capital 2000 1" if lang == "ar" else f"💰 Registered capital: ${capital:g} | per-trade risk: {risk_pct:g}%. Change with: /capital 2000 1")
        return
    try:
        numbers = extract_numbers(" ".join(parts[1:]))
        if not numbers or numbers[0] <= 0:
            raise ValueError
        db.set_capital(message.chat.id, float(numbers[0]))
        if len(numbers) >= 2:
            if not 0 < numbers[1] <= 10:
                raise ValueError
            db.set_risk_percent(message.chat.id, float(numbers[1]))
        user = db.get_user(message.chat.id)
        bot.reply_to(message, f"✅ تم: رأس المال {user.capital:g}$ | مخاطرة {user.risk_percent:g}% لكل صفقة." if lang == "ar" else f"✅ Saved: capital ${user.capital:g} | risk {user.risk_percent:g}% per trade.")
    except (ValueError, IndexError):
        bot.reply_to(message, "الاستخدام: /capital رأس_المال [نسبة_المخاطرة] — مثال: /capital 2000 1" if lang == "ar" else "Usage: /capital CAPITAL [RISK_PERCENT] — example: /capital 2000 1")


def calendar_list_text(lang: str, tz_name: str, hours: int = 24) -> str:
    events = [event for event in calendar_feed.upcoming(hours, ("High", "Medium"))]
    if lang == "ar":
        if not events:
            return "📅 لا توجد أحداث عالية/متوسطة التأثير خلال 24 ساعة القادمة."
        lines = ["📅 <b>التقويم الاقتصادي — 24 ساعة</b>", ""]
        for event in events[:15]:
            when = format_timestamp(event.event_time, lang, tz_name)
            lines.append(f"{event.stars} <b>{html.escape(event.title)}</b> | {_country_label(event.country, lang)}")
            exp = f"متوقع {event.forecast} | سابق {event.previous}" if (event.forecast or event.previous) else "بدون توقع رقمي"
            lines.append(f"🕒 {html.escape(when)} | {_impact_label(event.impact, lang)} | {html.escape(exp)}")
            lines.append("")
        lines.append("سأذكرك قبل الأحداث عالية التأثير وأتابع رد الفعل مع خطة.")
        return "\n".join(lines)
    if not events:
        return "📅 No high/medium-impact events in the next 24 hours."
    lines = ["📅 <b>Economic calendar — 24h</b>", ""]
    for event in events[:15]:
        when = format_timestamp(event.event_time, lang, tz_name)
        lines.append(f"{event.stars} <b>{html.escape(event.title)}</b> | {_country_label(event.country, lang)}")
        exp = f"forecast {event.forecast} | previous {event.previous}" if (event.forecast or event.previous) else "no numeric forecast"
        lines.append(f"🕒 {html.escape(when)} | {_impact_label(event.impact, lang)} | {html.escape(exp)}")
        lines.append("")
    lines.append("I will remind you before high-impact events and follow the reaction with a plan.")
    return "\n".join(lines)


@bot.message_handler(commands=["calendar", "cal"])
def calendar_cmd(message: types.Message):
    lang = user_language(message)
    remember_user(message)
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) == 2 else ""
    if arg in {"off", "ايقاف", "إيقاف"}:
        db.set_calendar_enabled(message.chat.id, False)
        bot.reply_to(message, "تم إيقاف تنبيهات التقويم." if lang == "ar" else "Calendar alerts disabled.")
        return
    if arg in {"on", "تشغيل"}:
        db.set_calendar_enabled(message.chat.id, True)
        bot.reply_to(message, "تم تفعيل تنبيهات التقويم." if lang == "ar" else "Calendar alerts enabled.")
        return
    try:
        for chunk in split_broadcast_text(calendar_list_text(lang, user_timezone(message))):
            bot.send_message(message.chat.id, chunk, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        logger.exception("calendar list failed")
        bot.reply_to(message, "تعذر جلب التقويم حاليًا." if lang == "ar" else "Calendar is unavailable right now.")


@bot.message_handler(commands=["stats", "accuracy"])
def stats_cmd(message: types.Message):
    lang = user_language(message)
    remember_user(message)
    try:
        bot.reply_to(message, stats_report(db, lang))
    except Exception:
        logger.exception("stats failed")
        bot.reply_to(message, "تعذر حساب الإحصائيات حاليًا." if lang == "ar" else "Stats are unavailable right now.")


@bot.message_handler(commands=["backtest", "bt"])
def backtest_cmd(message: types.Message):
    lang = user_language(message)
    parts = (message.text or "").split()
    asset_token = parts[1] if len(parts) >= 2 else None
    asset = selected_asset(message, asset_token) if asset_token else selected_asset(message)
    remember_user(message, asset)
    if asset is None:
        bot.reply_to(message, "اذكر الأصل مثل: /backtest EURUSD 30" if lang == "ar" else "Name an asset, for example /backtest EURUSD 30.")
        return
    try:
        numbers = extract_numbers(" ".join(parts[2:])) if len(parts) >= 3 else []
        days = max(7, min(60, int(numbers[0]))) if numbers else 30
    except (ValueError, IndexError):
        days = 30
    bot.reply_to(message, f"🧪 أختبر {asset.key} على ~{days} يومًا... قد يستغرق هذا دقيقة." if lang == "ar" else f"🧪 Backtesting {asset.key} on ~{days} days... this can take a minute.")
    try:
        bot.send_chat_action(message.chat.id, "typing")
        result = run_backtest(asset, days)
        bot.send_message(message.chat.id, render_backtest(result, lang), parse_mode="HTML", disable_web_page_preview=True)
    except HistoryUnavailable:
        bot.reply_to(message, AR["data_error"] if lang == "ar" else "Not enough historical data for this backtest.")
    except Exception:
        logger.exception("backtest failed for %s", asset.key)
        bot.reply_to(message, "تعذر إكمال الباك تست حاليًا." if lang == "ar" else "Backtest could not be completed right now.")


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


# ============================ SMART MONEY (SMC) ============================ #
# Deterministic multi-timeframe detection lives in marketobserver/smc.py.
# Nothing in this layer invents a number: the engine reads real OHLCV only and
# the report text is assembled from its structured output.

SMC_TIMEFRAMES = ("4h", "1h", "15m")
SMC_PRIMARY_ASSETS = ("BTC", "ETH", "XAUUSD", "EURUSD", "GBPUSD", "USDJPY")
SMC_MORE_ASSETS = ("SOL", "PAXG", "XAGUSD", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD", "EURJPY", "GBPJPY",
                   "WTI", "BRENT", "NATGAS", "DXY", "AAPL", "TSLA", "NVDA", "QQQ")
SMC_MODES = ("brief", "full")
# Per-chat UI preference. The process is single-worker by design (Telegram
# polling and the alert loop run in this process), so an in-memory dict is the
# right size of state for button labels; losing it only resets button highlighting.
smc_prefs: dict[int, dict[str, str]] = {}
smc_reports: dict[str, tuple[float, object]] = {}
SMC_REPORT_TTL_SECONDS = 40


def smc_lang_for(message: types.Message, chat_id: int | None = None) -> str:
    """Persisted language choice wins, otherwise detect from the message."""
    if chat_id is None and message is not None:
        chat_id = message.chat.id
    stored = (smc_prefs.get(chat_id or 0) or {}).get("lang")
    if stored in ("ar", "en", "both"):
        return stored
    return user_language(message) if message is not None else "ar"


def smc_build_report(asset: Asset, force: bool = False):
    """Fetch the three timeframes and run the detector once.

    4H and 1H are mandatory; 15m is required for entry timing and its absence is
    reported by the engine instead of being papered over with another timeframe.
    Returns (report, candles_by_timeframe) so the chart can reuse the same real
    candles without a second provider call.
    """
    key = f"{asset.key}:{'1' if force else '0'}"
    cached = smc_reports.get(key)
    if cached and not force and time.time() - cached[0] < SMC_REPORT_TTL_SECONDS:
        return cached[1]
    candles_by_timeframe: dict[str, list] = {}
    for timeframe in SMC_TIMEFRAMES:
        try:
            candles_by_timeframe[timeframe] = market.get_candles(asset, timeframe, 200)
        except DataUnavailable:
            continue
    if "4h" not in candles_by_timeframe or "1h" not in candles_by_timeframe:
        raise DataUnavailable(f"SMC needs 4H and 1H candles for {asset.key}")
    report = build_smc_report(asset.key, asset.name_ar, asset.name_en, asset.quote, asset.price_decimals,
                          candles_by_timeframe, market.last_source(asset.key) or "unknown")
    payload = (report, candles_by_timeframe)
    smc_reports[key] = (time.time(), payload)
    return payload


def smc_keyboard(asset_key: str, lang: str, mode: str) -> types.InlineKeyboardMarkup:
    """Inline buttons: pick asset, switch language, switch detail, refresh,
    chart, zone alerts and the measurement rules. Every button carries the
    current asset/lang/mode so pressing one never loses context."""
    mode = mode if mode in SMC_MODES else "full"
    keyboard = types.InlineKeyboardMarkup(row_width=3)
    names = {"BTC": "₿ BTC", "ETH": "Ξ ETH", "SOL": "◎ SOL", "XAUUSD": "🥇 XAU", "EURUSD": "💶 EUR", "WTI": "🛢 WTI",
             "PAXG": "🥈 PAXG", "XAGUSD": "⚪️ XAG", "GBPUSD": "💷 GBP", "USDJPY": "💴 JPY", "AUDUSD": "🦘 AUD",
             "USDCAD": "🍁 CAD", "USDCHF": "🇨🇭 CHF", "NZDUSD": "🥝 NZD", "EURJPY": "🇪🇺JPY", "GBPJPY": "🇬🇧JPY",
             "BRENT": "🛢 BRENT", "NATGAS": "🔥 GAS", "DXY": "💵 DXY",
             "AAPL": "🍎 AAPL", "TSLA": "🚗 TSLA", "NVDA": "🎮 NVDA", "QQQ": "📈 QQQ"}
    for row in (SMC_PRIMARY_ASSETS[:3], SMC_PRIMARY_ASSETS[3:]):
        keyboard.add(*[types.InlineKeyboardButton(("✅ " if key == asset_key else "") + names.get(key, key),
                                                  callback_data=f"smc:asset:{key}:{lang}:{mode}") for key in row])
    keyboard.add(types.InlineKeyboardButton("أصول أخرى +" if lang == "ar" else "More assets +",
                                           callback_data=f"smc:more:{asset_key}:{lang}:{mode}"))
    keyboard.add(
        types.InlineKeyboardButton("⚡ مختصر" if lang == "ar" else "⚡ Brief", callback_data=f"smc:mode:{asset_key}:{lang}:brief"),
        types.InlineKeyboardButton("📋 تقرير كامل" if lang == "ar" else "📋 Full report", callback_data=f"smc:mode:{asset_key}:{lang}:full"),
        types.InlineKeyboardButton("🔄 إعادة حساب" if lang == "ar" else "🔄 Recompute", callback_data=f"smc:report:{asset_key}:{lang}:{mode}:1"),
    )
    keyboard.add(
        types.InlineKeyboardButton("🇸🇦 عربي", callback_data=f"smc:lang:{asset_key}:ar:{mode}"),
        types.InlineKeyboardButton("🇬🇧 English", callback_data=f"smc:lang:{asset_key}:en:{mode}"),
        types.InlineKeyboardButton("🌐 عربي + English" if lang == "ar" else "🌐 AR + EN", callback_data=f"smc:lang:{asset_key}:both:{mode}"),
    )
    keyboard.add(
        types.InlineKeyboardButton("📊 شارت موسوم" if lang == "ar" else "📊 Annotated chart", callback_data=f"smc:chart:{asset_key}:{lang}:{mode}"),
        types.InlineKeyboardButton("🔔 راقب المنطقة" if lang == "ar" else "🔔 Watch zone", callback_data=f"smc:alert:{asset_key}:{lang}:{mode}"),
    )
    keyboard.add(types.InlineKeyboardButton("🧮 كيف قِستُ (BOS/CHoCH/OB/FVG)" if lang == "ar" else "🧮 How it is measured",
                                            callback_data=f"smc:rules:{asset_key}:{lang}:{mode}"))
    return keyboard


SMC_RULES_AR = (
    "كيف تُكتَب النتائج (كل شيء حسابي، لا LLM):\n"
    "• الهيكل: نقاط ارتكاز Fractal بنافذة شمعتين على كل جانب؛ الوسم لا يُعتمد إلا بعد إقفال الشمعتين التاليتين — لا استخدام لمستقبل البيانات.\n"
    "• BOS: إغلاق جسم شمعة فوق آخر قمة مرتكز (للشراء) أو تحت آخر قاع (للبيع) مع كون الاتجاه على 4H في نفس الجهة.\n"
    "• CHoCH: أول إغلاق معاكس للاتجاه السائد؛ يُستخدم كإنذار مبكر للانعكاس وليس كدخول.\n"
    "• صيد السيولة: ذيل يخترق القمة/القاع ثم يعود الإغلاق للداخل — لا يُعامل ككسر أبدًا.\n"
    "• OB: آخر شمعة معاكسة قبل حركة الإزاحة التي صنعت الكسر، وتبقى صالحة حتى إغلاق خلفها.\n"
    "• FVG: فجوة ثلاث شموع بعرض ≥ 0.30 ATR؛ تُحسب نسبة التعبئة ولا تُقبل منطقة مُلأت ≥ 60%.\n"
    "• الشموع: مطرقة/مقلوبتها/نجمة ساقطة = ذيل ≥ ضعفي الجسم؛ ابتلاعي = جسم يغلق فوق/تحت جسم السابقة مع كبره؛ نجمة صباح/مساء = ثلاث شموع بشروط وسطى صارمة؛ دوجي = جسم ≤ 10% من المدى. كل نمط يُذكر مع موضعه من المنطقة، لا منفردًا.\n"
    "• الحجم: RVOL وارتفاعات ≥ 2.2x ونسبة الشراء العدواني من Binance (taker buy) وشكل OBV.\n"
    "• القرار: كل البوابات (اتجاه 4H + تأكيد 15m + السعر داخل المنطقة + عدم المطاردة + إشارة شمعة + حجم + RR ≥ 1:2) يجب أن تنجح؛ وإلا WAIT."
)
SMC_RULES_EN = (
    "How the numbers are produced (all arithmetic, no LLM):\n"
    "• Structure: fractal pivots, 2 candles on each side; a pivot only counts after those two candles close — no look-ahead.\n"
    "• BOS: a candle *body* closes above the last swing high (long) or below the last swing low (short) while the 4H bias agrees.\n"
    "• CHoCH: first close against the prevailing bias — an early warning, not an entry.\n"
    "• Liquidity sweep: a wick through the level with the close back inside — never counted as a break.\n"
    "• OB: last opposing candle before the displacement leg that broke structure; valid until a close passes it.\n"
    "• FVG: 3-candle gap at least 0.30 ATR wide; gaps filled ≥ 60% are rejected.\n"
    "• Candles: hammer/inverted/shooting star need a wick ≥ 2x body; engulfing must close beyond the prior body; stars use strict three-candle rules; doji body ≤ 10% of range. Each print is reported with its location, never standalone.\n"
    "• Volume: RVOL, spikes ≥ 2.2x, Binance taker buy share, OBV shape.\n"
    "• Decision: every gate (4H trend, 15m confirmation, price in zone, no chasing, candle trigger, volume, RR ≥ 1:2) must pass — otherwise WAIT."
)


def deliver_smc(chat_id: int, text: str, markup=None, edit_message_id: int | None = None) -> None:
    """Send a report, splitting long output; edits in place when it fits."""
    parts = split_broadcast_text(text, TELEGRAM_TEXT_LIMIT) or [text[:TELEGRAM_TEXT_LIMIT]]
    if edit_message_id is not None and len(parts) == 1:
        try:
            bot.edit_message_text(parts[0], chat_id=chat_id, message_id=edit_message_id, reply_markup=markup)
            return
        except ApiTelegramException as exc:
            if "message is not modified" not in str(exc).lower():
                logger.info("smc edit fell back to send: %s", exc)
    for index, part in enumerate(parts):
        bot.send_message(chat_id, part, reply_markup=markup if index == len(parts) - 1 else None)


def smc_respond(target_message, asset: Asset, lang: str, mode: str, force: bool = False, edit_message_id: int | None = None):
    # The keyboard is built before the provider call on purpose: an error reply
    # that carries no buttons would strand the user (no retry, no asset switch),
    # which is exactly what happens when a data source is geo-blocked.
    markup = smc_keyboard(asset.key, lang, mode)
    try:
        report, _ = smc_build_report(asset, force=force)
    except DataUnavailable:
        deliver_smc(target_message.chat.id, "⛔ " + (AR["data_error"] if lang == "ar" else "Reliable market data is unavailable for this asset right now. No synthetic candles were used, so no analysis is produced."), markup, edit_message_id=edit_message_id)
        return None
    except Exception:
        logger.exception("smc analysis failed for %s", asset.key)
        deliver_smc(target_message.chat.id, "⛔ " + ("تعذر إكمال التحليل مؤقتًا. جرّب إعادة الحساب من الزر." if lang == "ar" else "Analysis could not be completed right now. Use Recompute below to retry."), markup, edit_message_id=edit_message_id)
        return None
    text = render_smc_brief(report, lang) if mode == "brief" else render_smc(report, lang)
    deliver_smc(target_message.chat.id, text, markup, edit_message_id)
    return report


def parse_smc_arguments(text: str) -> tuple[str | None, str | None, str | None]:
    """Hybrid command grammar: /smc [ASSET] [ar|en|both] [brief|full|chart].

    Anything unrecognised is treated as the asset name so natural ticker styles
    keep working ("/smc الذهب", "/smc BTC en full").
    """
    asset_token: str | None = None
    lang: str | None = None
    mode: str | None = None
    for token in (text or "").split()[1:]:
        lowered = token.lower().strip("/")
        if lowered in ("ar", "en", "both"):
            lang = lowered
        elif lowered in ("brief", "full", "chart", "مختصر", "كامل", "شارت"):
            mode = {"مختصر": "brief", "كامل": "full", "شارت": "chart"}.get(lowered, lowered)
        elif asset_token is None:
            asset_token = token
    return asset_token, lang, mode


@bot.message_handler(commands=["smc", "smartmoney", "sma", "ict"])
def smc_cmd(message: types.Message):
    supplied, lang_override, mode_override = parse_smc_arguments(message.text or "")
    asset = selected_asset(message, supplied) if supplied else selected_asset(message)
    remember_user(message, asset)
    lang = lang_override or smc_lang_for(message)
    if asset is None:
        bot.reply_to(message, "اذكر الأصل مثل: /smc BTC أو /smc الذهب." if lang == "ar" else "Name an asset, for example /smc BTC or /smc gold.",
                     reply_markup=smc_keyboard("BTC", lang, "full"))
        return
    prefs = smc_prefs.setdefault(message.chat.id, {})
    mode = mode_override or prefs.get("mode", "full")
    if mode == "chart":
        try:
            report, candles = smc_build_report(asset, force=True)
            bot.send_chat_action(message.chat.id, "upload_photo")
            bot.send_photo(message.chat.id, smc_chart(asset, report, candles), caption=render_smc_brief(report, lang),
                           reply_markup=smc_keyboard(asset.key, lang, "full"))
        except DataUnavailable:
            bot.reply_to(message, AR["data_error"] if lang == "ar" else "Reliable market data is unavailable; no chart was invented.",
                         reply_markup=smc_keyboard(asset.key, lang, "full"))
        return
    prefs["mode"] = mode
    if lang_override:
        prefs["lang"] = lang_override
    smc_respond(message, asset, lang, mode, force=True)


SMC_BUTTON_NAMES = {"BTC": "₿ BTC", "ETH": "Ξ ETH", "SOL": "◎ SOL", "PAXG": "🥈 PAXG", "XAUUSD": "🥇 XAU",
                    "XAGUSD": "⚪️ XAG", "EURUSD": "💶 EUR", "GBPUSD": "💷 GBP", "USDJPY": "💴 JPY", "AUDUSD": "🦘 AUD",
                    "USDCAD": "🍁 CAD", "USDCHF": "🇨🇭 CHF", "NZDUSD": "🥝 NZD", "EURJPY": "🇪🇺JPY", "GBPJPY": "🇬🇧JPY",
                    "WTI": "🛢 WTI", "BRENT": "🛢 BRENT", "NATGAS": "🔥 GAS",
                    "DXY": "💵 DXY", "AAPL": "🍎 AAPL", "TSLA": "🚗 TSLA", "NVDA": "🎮 NVDA", "QQQ": "📈 QQQ"}


def parse_smc_callback(data: str) -> tuple[str, str, str, str, bool]:
    """`smc:<action>:<asset>:<lang>:<mode>[:1]` -> (action, asset, lang, mode, force).

    Kept as a pure function so the routing can be unit-tested without Telegram.
    """
    fields = (data or "").split(":")
    action = fields[1] if len(fields) > 1 and fields[1] else "report"
    asset_key = fields[2] if len(fields) > 2 and fields[2] else "BTC"
    lang = fields[3] if len(fields) > 3 and fields[3] in ("ar", "en", "both") else "ar"
    mode = fields[4] if len(fields) > 4 and fields[4] in SMC_MODES else "full"
    return action, asset_key, lang, mode, len(fields) > 5 and fields[5] == "1"


@bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("smc:")))
def smc_callback(call: types.CallbackQuery):
    action, asset_key, lang, mode, force = parse_smc_callback(call.data)
    prefs = smc_prefs.setdefault(call.message.chat.id, {})
    prefs.update({"lang": lang, "mode": mode, "asset": asset_key})
    asset = ASSETS.get(asset_key) or resolve_asset(asset_key)
    try:
        if asset is None:
            bot.answer_callback_query(call.id, "أصل غير معروف" if lang == "ar" else "Unknown asset", show_alert=True)
            return
        if action == "more":
            keyboard = types.InlineKeyboardMarkup(row_width=3)
            for row in (SMC_MORE_ASSETS[:3], SMC_MORE_ASSETS[3:6], SMC_MORE_ASSETS[6:]):
                keyboard.add(*[types.InlineKeyboardButton(SMC_BUTTON_NAMES.get(key, key), callback_data=f"smc:asset:{key}:{lang}:{mode}") for key in row])
            keyboard.add(types.InlineKeyboardButton("↩️ رجوع" if lang == "ar" else "↩️ Back", callback_data=f"smc:asset:{asset_key}:{lang}:{mode}"))
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=keyboard)
            bot.answer_callback_query(call.id)
            return
        if action in ("lang", "mode"):
            smc_respond(call.message, asset, lang, mode)
            bot.answer_callback_query(call.id)
            return
        if action == "asset":
            db.set_last_asset(call.message.chat.id, asset.key)
            smc_respond(call.message, asset, lang, mode)
            bot.answer_callback_query(call.id)
            return
        if action == "rules":
            deliver_smc(call.message.chat.id, SMC_RULES_AR if lang == "ar" else SMC_RULES_EN, smc_keyboard(asset.key, lang, mode))
            bot.answer_callback_query(call.id)
            return
        if action == "chart":
            report, candles = smc_build_report(asset, force=force)
            bot.send_chat_action(call.message.chat.id, "upload_photo")
            bot.send_photo(call.message.chat.id, smc_chart(asset, report, candles), caption=render_smc_brief(report, lang),
                           reply_markup=smc_keyboard(asset.key, lang, mode))
            bot.answer_callback_query(call.id)
            return
        if action == "alert":
            report, _ = smc_build_report(asset)
            plan = report.plan
            created = []
            if plan.zone and plan.stop:
                if plan.side == "long":
                    created.append(db.add_alert(call.message.chat.id, asset.key, round(plan.stop, asset.price_decimals), "below"))
                    created.append(db.add_alert(call.message.chat.id, asset.key, round(plan.target_one, asset.price_decimals), "above"))
                else:
                    created.append(db.add_alert(call.message.chat.id, asset.key, round(plan.stop, asset.price_decimals), "above"))
                    created.append(db.add_alert(call.message.chat.id, asset.key, round(plan.target_one, asset.price_decimals), "below"))
            if plan.zone:
                created.append(db.add_alert(call.message.chat.id, asset.key, round(plan.entry_high if plan.side == "long" else plan.entry_low, asset.price_decimals),
                                             "below" if plan.side == "long" else "above"))
            if not created:
                bot.answer_callback_query(call.id, "لا خطة صالحة لضبط تنبيه — القرار WAIT" if lang == "ar" else "No valid plan to alert on — decision is WAIT", show_alert=True)
                return
            text = (f"🔔 عُلّقت {len(created)} تنبيهات على خطة {asset.name_ar}: "
                    + ", ".join(f"{alert.condition} {alert.target_price}" for alert in created)
                    + " — ألغِ أيًّاها بـ /cancel_alert ID.") if lang == "ar" else (
                f"🔔 {len(created)} alerts attached to the {asset.name_en} plan: "
                + ", ".join(f"{alert.condition} {alert.target_price}" for alert in created)
                + " — cancel any with /cancel_alert ID.")
            deliver_smc(call.message.chat.id, text, smc_keyboard(asset.key, lang, mode))
            bot.answer_callback_query(call.id)
            return
        # default: (re)draw the report
        smc_respond(call.message, asset, lang, mode, force=force)
        bot.answer_callback_query(call.id)
    except DataUnavailable:
        bot.answer_callback_query(call.id, "لا توجد بيانات موثوقة الآن" if lang == "ar" else "No reliable market data right now", show_alert=True)
    except Exception:
        logger.exception("smc callback failed")
        bot.answer_callback_query(call.id, "تعذر تنفيذ الزر" if lang == "ar" else "Button failed", show_alert=True)


# --------------------------------------------------------------------------- #
# Strict decision engine (/decision): BUY / SELL / WAIT with a hard gate chain.
# The engine lives in marketobserver/decision.py; this layer only fetches the
# three timeframes, renders the verdict and exposes it over HTTP.
# --------------------------------------------------------------------------- #
DECISION_COMMANDS = ("decision", "verdict", "call")
DECISION_TTL_SECONDS = 40
decision_cache: dict[str, tuple[float, object]] = {}


def decision_lang_for(message: types.Message, chat_id: int | None = None) -> str:
    """Reuse the language the user picked for /smc, then fall back to detection."""
    if chat_id is None and message is not None:
        chat_id = message.chat.id
    stored = (smc_prefs.get(chat_id or 0) or {}).get("lang")
    if stored in SMC_LANGS:
        return stored
    return user_language(message) if message is not None else "en"


def decision_candles(asset: Asset, force: bool = False):
    """Fetch 4H, 1H and 15m once and run the strict gate chain on closed bars."""
    cached = decision_cache.get(asset.key)
    if cached and not force and time.time() - cached[0] < DECISION_TTL_SECONDS:
        return cached[1]
    candles_by_timeframe: dict[str, list] = {}
    for timeframe in SMC_TIMEFRAMES:
        try:
            candles_by_timeframe[timeframe] = market.get_candles(asset, timeframe, 200)
        except DataUnavailable:
            continue
    if "4h" not in candles_by_timeframe:
        raise DataUnavailable(f"the 4H trend gate needs 4H candles for {asset.key}")
    decision = build_strict_decision(asset.key, asset.name_ar, asset.name_en, asset.quote,
                                     asset.price_decimals, candles_by_timeframe,
                                     market.last_source(asset.key) or "unknown")
    payload = (decision, candles_by_timeframe)
    decision_cache[asset.key] = (time.time(), payload)
    return payload


def decision_keyboard(asset_key: str, lang: str, verbose: bool = False) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("🔄 إعادة الحساب" if lang == "ar" else "🔄 Recompute",
                                   callback_data=f"dec:run:{asset_key}:{lang}:{1 if verbose else 0}:1"),
        types.InlineKeyboardButton("🧾 البوابات" if lang == "ar" else "🧾 Gate chain",
                                   callback_data=f"dec:run:{asset_key}:{lang}:{0 if verbose else 1}:1"),
    )
    keyboard.add(
        types.InlineKeyboardButton("🇸🇦 عربي", callback_data=f"dec:run:{asset_key}:ar:0:0"),
        types.InlineKeyboardButton("🇬🇧 English", callback_data=f"dec:run:{asset_key}:en:0:0"),
        types.InlineKeyboardButton("🌐 AR + EN", callback_data=f"dec:run:{asset_key}:both:0:0"),
    )
    quick = [key for key in SMC_PRIMARY_ASSETS if key != asset_key][:4]
    keyboard.add(*[types.InlineKeyboardButton(SMC_BUTTON_NAMES.get(key, key),
                                             callback_data=f"dec:run:{key}:{lang}:{1 if verbose else 0}:0")
                   for key in quick])
    return keyboard


def decision_deliver(chat_id: int, text: str, markup=None, edit_message_id: int | None = None) -> None:
    for chunk in split_broadcast_text(text):
        if edit_message_id:
            try:
                bot.edit_message_text(chunk, chat_id, edit_message_id, reply_markup=markup)
                markup, edit_message_id = None, None
                continue
            except Exception as exc:
                logger.info("decision edit fell back to send: %s", exc)
                edit_message_id = None
        bot.send_message(chat_id, chunk, reply_markup=markup)
        markup = None


def decision_respond(target_message, asset: Asset, lang: str, verbose: bool = False,
                     force: bool = False, edit_message_id: int | None = None) -> None:
    markup = decision_keyboard(asset.key, lang, verbose)
    try:
        decision, _ = decision_candles(asset, force=force)
    except DataUnavailable:
        decision_deliver(target_message.chat.id,
                         "⛔ " + (AR["data_error"] if lang == "ar" else
                                 "Reliable market data is unavailable for this asset right now, so no verdict "
                                 "is produced. No synthetic candles were used."), markup, edit_message_id)
        return
    except Exception:
        logger.exception("strict decision failed for %s", asset.key)
        decision_deliver(target_message.chat.id,
                         "⛔ " + ("تعذر إكمال القرار الآن. جرّب إعادة الحساب." if lang == "ar" else
                                 "The verdict could not be completed right now. Try Recompute."),
                         markup, edit_message_id)
        return
    decision_deliver(target_message.chat.id, render_decision(decision, lang, verbose=verbose), markup, edit_message_id)


def parse_decision_arguments(text: str) -> tuple[str | None, str | None, bool]:
    """Hybrid grammar: /decision [ASSET] [ar|en|both] [gates]."""
    parts = (text or "").split()
    supplied = lang = None
    verbose = False
    for token in parts[1:]:
        lowered = token.lower()
        if lowered in SMC_LANGS:
            lang = lowered
        elif lowered in {"gates", "verbose", "بوابات", "كامل"}:
            verbose = True
        elif supplied is None and not lowered.startswith("/"):
            supplied = token
    return supplied, lang, verbose


@bot.message_handler(commands=list(DECISION_COMMANDS))
def decision_cmd(message: types.Message):
    supplied, lang_override, verbose = parse_decision_arguments(message.text or "")
    lang = lang_override or decision_lang_for(message)
    asset = resolve_asset(supplied) if supplied else None
    if asset is None:
        asset = selected_asset(message)
    if asset is None:
        bot.reply_to(message,
                     "اذكر الأصل مثل: /decision BTC أو /decision الذهب." if lang == "ar" else
                     "Name an asset, for example /decision BTC or /decision gold.",
                     reply_markup=decision_keyboard("BTC", lang, verbose))
        return
    prefs = smc_prefs.setdefault(message.chat.id, {})
    prefs["lang"] = lang
    decision_respond(message, asset, lang, verbose, force=True)


@bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("dec:")))
def decision_callback(call: types.CallbackQuery):
    parts = (call.data or "").split(":")
    if len(parts) < 6:
        bot.answer_callback_query(call.id)
        return
    _, action, asset_key, lang, verbose, force = parts[:6]
    lang = lang if lang in SMC_LANGS else decision_lang_for(call.message)
    asset = resolve_asset(asset_key)
    if asset is None:
        bot.answer_callback_query(call.id, "أصل غير معروف" if lang == "ar" else "Unknown asset", show_alert=True)
        return
    smc_prefs.setdefault(call.message.chat.id, {})["lang"] = lang
    if action != "run":
        bot.answer_callback_query(call.id)
        return
    decision_respond(call.message, asset, lang, verbose in {"1", "true"}, force=force == "1",
                     edit_message_id=call.message.message_id if call.message else None)
    bot.answer_callback_query(call.id)


# --------------------------------------------------------------------------- #
# Binary engine (/binary): CALL / PUT / WAIT on 15m context + 5m execution.
# Built for short-expiry binary-style trading; the strict gate chain lives in
# marketobserver/binary.py and this layer only fetches candles, attaches the
# live reference price and renders the verdict.
# --------------------------------------------------------------------------- #
BINARY_TIMEFRAMES = ("5m", "15m")
BINARY_TTL_SECONDS = 30
binary_cache: dict[str, tuple[float, object]] = {}


def binary_verdict_for(asset: Asset, force: bool = False):
    cached = binary_cache.get(asset.key)
    if cached and not force and time.time() - cached[0] < BINARY_TTL_SECONDS:
        return cached[1]
    candles: dict[str, list] = {}
    for timeframe in BINARY_TIMEFRAMES:
        try:
            candles[timeframe] = market.get_candles(asset, timeframe, 120)
        except DataUnavailable:
            continue
    if "5m" not in candles or "15m" not in candles:
        raise DataUnavailable(f"binary needs 5m and 15m candles for {asset.key}")
    try:
        live_price = live.get_quote(asset).price
    except QuoteUnavailable:
        live_price = None
    verdict = build_binary_verdict(asset.key, asset.name_ar, asset.name_en, asset.quote,
                                   asset.price_decimals, candles["5m"], candles["15m"],
                                   market.last_source(asset.key) or "unknown", live_price)
    binary_cache[asset.key] = (time.time(), verdict)
    return verdict


def binary_keyboard(asset_key: str, lang: str) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=3)
    for index in range(0, len(BINARY_POPULAR), 3):
        row = BINARY_POPULAR[index:index + 3]
        keyboard.add(*[types.InlineKeyboardButton(
            ("✅ " if key == asset_key else "") + SMC_BUTTON_NAMES.get(key, key),
            callback_data=f"bin:run:{key}:{lang}:0") for key in row])
    keyboard.add(
        types.InlineKeyboardButton("🔄 تحديث" if lang == "ar" else "🔄 Refresh",
                                   callback_data=f"bin:run:{asset_key}:{lang}:1"),
        types.InlineKeyboardButton("🇸🇦 عربي" if lang == "ar" else "🇸🇦 AR",
                                   callback_data=f"bin:run:{asset_key}:ar:0"),
        types.InlineKeyboardButton("🇬🇧 EN", callback_data=f"bin:run:{asset_key}:en:0"),
    )
    return keyboard


def binary_respond(target_message, asset: Asset, lang: str, force: bool = False,
                   edit_message_id: int | None = None) -> None:
    markup = binary_keyboard(asset.key, lang)
    try:
        verdict = binary_verdict_for(asset, force=force)
    except DataUnavailable:
        text = "⛔ " + (AR["data_error"] if lang == "ar" else
                        "Reliable 5m/15m market data is unavailable for this asset right now, so no binary verdict is produced.")
        deliver_smc(target_message.chat.id, text, markup, edit_message_id=edit_message_id)
        return
    except Exception:
        logger.exception("binary verdict failed for %s", asset.key)
        deliver_smc(target_message.chat.id,
                      "⛔ " + ("تعذر إكمال القرار الثنائي الآن. جرّب التحديث." if lang == "ar" else
                              "The binary verdict could not be completed. Try Refresh."),
                      markup, edit_message_id=edit_message_id)
        return
    text = render_binary(verdict, lang)
    if verdict.verdict in {"CALL", "PUT"}:
        # Learning loop: journal every directional call and check it later.
        ref = verdict.live_price or verdict.reference_price
        if ref:
            try:
                db.journal_add("binary", asset.key, verdict.verdict, ref,
                               verdict.expiry_minutes or 15, chat_id=target_message.chat.id,
                               note=f"conf:{verdict.confidence}")
            except Exception:
                logger.exception("binary journal failed")
        calibration = calibration_for(db, asset.key, verdict.verdict)
        if calibration.downgrade:
            text += "\n" + (calibration.note_ar if lang == "ar" else calibration.note_en)
    deliver_smc(target_message.chat.id, text, markup, edit_message_id)


@bot.message_handler(commands=["binary", "bin"])
def binary_cmd(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    lang = user_language(message)
    asset = selected_asset(message, parts[1] if len(parts) == 2 else None)
    remember_user(message, asset)
    if asset is None:
        bot.reply_to(message,
                     "اذكر الأصل مثل: /binary EURUSD أو /binary الذهب." if lang == "ar" else
                     "Name an asset, for example /binary EURUSD or /binary gold.",
                     reply_markup=binary_keyboard("EURUSD", lang))
        return
    prefs = smc_prefs.setdefault(message.chat.id, {})
    prefs["lang"] = lang
    binary_respond(message, asset, lang, force=True)


@bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("bin:")))
def binary_callback(call: types.CallbackQuery):
    parts = (call.data or "").split(":")
    if len(parts) < 5:
        bot.answer_callback_query(call.id)
        return
    _, _, asset_key, lang, force = parts[:5]
    lang = lang if lang in ("ar", "en") else "ar"
    asset = ASSETS.get(asset_key) or resolve_asset(asset_key)
    if asset is None:
        bot.answer_callback_query(call.id, "أصل غير معروف" if lang == "ar" else "Unknown asset", show_alert=True)
        return
    smc_prefs.setdefault(call.message.chat.id, {})["lang"] = lang
    db.set_last_asset(call.message.chat.id, asset.key)
    binary_respond(call.message, asset, lang, force=force == "1",
                   edit_message_id=call.message.message_id if call.message else None)
    bot.answer_callback_query(call.id)


def smc_chart(asset: Asset, report, candles_by_timeframe: dict):
    """Annotated 4H chart drawn from the same real candles the engine used."""
    candles = candles_by_timeframe.get("4h") or []
    view = report.view("4h")
    figure, axis = plt.subplots(figsize=(10, 6))
    figure.patch.set_facecolor("#0b1220")
    axis.set_facecolor("#111827")
    window = candles[-110:]
    for offset, candle in enumerate(window):
        up = candle.close >= candle.open
        color = "#22c55e" if up else "#ef4444"
        axis.plot([offset, offset], [candle.low, candle.high], color=color, linewidth=0.8, zorder=2)
        axis.add_patch(plt.Rectangle((offset - 0.32, min(candle.open, candle.close)), 0.64, max(abs(candle.close - candle.open), 1e-9),
                                     color=color, alpha=0.95, zorder=3))
    if view:
        for level in sorted(view.levels, key=lambda item: item.strength, reverse=True)[:5]:
            axis.axhline(level.price, color="#38bdf8", alpha=0.45, linewidth=1.0)
            axis.text(len(window) + 0.5, level.price, f"{level.kind} {level.price:g}", color="#38bdf8", fontsize=7, va="center")
        for event in view.events[-6:]:
            position = event.index - (len(candles) - len(window))
            if 0 <= position < len(window):
                axis.annotate(event.label, (position, event.level), color="#fbbf24", fontsize=8, ha="center",
                              va="bottom" if event.direction == "bullish" else "top")
        plan = report.plan
        # plan labels go inside the plot on the left; level labels live on the
        # right edge, so the two never overlap on a narrow chart
        inside = dict(ha="left", va="bottom", fontsize=7,
                      bbox=dict(facecolor="#0b1220", edgecolor="none", alpha=0.6, pad=1.2))
        if plan.zone:
            axis.axhspan(plan.entry_low, plan.entry_high, color="#22d3ee", alpha=0.18, zorder=1)
            axis.text(0, plan.entry_high, f"ENTRY {plan.entry_low:g}-{plan.entry_high:g} ({plan.zone.kind})",
                      color="#22d3ee", **inside)
        if plan.stop:
            axis.axhline(plan.stop, color="#f43f5e", linestyle="--", linewidth=1.2)
            axis.text(0, plan.stop, f"SL {plan.stop:g}", color="#f43f5e", **inside)
        if plan.target_one:
            axis.axhline(plan.target_one, color="#84cc16", linestyle="--", linewidth=1.2)
            axis.text(0, plan.target_one + view.atr * 0.25, f"TP1 {plan.target_one:g}", color="#84cc16", **inside)
        if plan.risk_reward:
            axis.text(0.01, 0.03, f"decision: {plan.decision.replace('_', ' ')} | RR 1:{plan.risk_reward:.2f} | {plan.confidence} confidence",
                      transform=axis.transAxes, color="#e2e8f0", fontsize=8)
        else:
            axis.text(0.01, 0.03, f"decision: {plan.decision.replace('_', ' ')} (no measurable target beyond the entry)",
                      transform=axis.transAxes, color="#e2e8f0", fontsize=8)
    shown = f"{report.price}" + (f" (live {report.live_price})" if report.live_price else "")
    axis.set_title(f"{asset.name_en} 4H — {shown} | closed candles only", color="white", fontsize=11)
    axis.set_xticks(list(range(0, len(window), max(len(window) // 8, 1))))
    axis.set_xticklabels([window[index].timestamp.strftime("%m-%d %H:%M") for index in range(0, len(window), max(len(window) // 8, 1))], color="white", fontsize=7)
    axis.tick_params(colors="white", labelsize=8)
    axis.grid(True, alpha=0.15)
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", bbox_inches="tight", facecolor=figure.get_facecolor())
    plt.close(figure)
    buffer.seek(0)
    return buffer


TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024
# Telegram allows roughly 30 messages per second bot-wide; stay clearly under it.
BROADCAST_SEND_DELAY_SECONDS = 0.035


def split_broadcast_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split an announcement into Telegram-sized chunks without losing content."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk]


def is_admin_chat(chat_id: int) -> bool:
    """Admins are configured by chat id in ADMIN_CHAT_IDS or hold role=admin."""
    if chat_id in settings.admin_chat_ids:
        return True
    user = db.get_user(chat_id)
    return bool(user and user.role == "admin")


def blocked_by_user(exc: Exception) -> bool:
    """True when Telegram says this chat can never receive messages again."""
    if isinstance(exc, ApiTelegramException) and exc.error_code == 403:
        return True
    lowered = str(exc).lower()
    return "bot was blocked" in lowered or "user is deactivated" in lowered or "chat not found" in lowered


def broadcast_text(text: str, chat_ids: list[int] | None = None, delay: float = BROADCAST_SEND_DELAY_SECONDS) -> dict[str, int]:
    """Deliver one announcement to every active user and report real outcomes."""
    chunks = split_broadcast_text(text)
    if not chunks:
        return {"users": 0, "sent": 0, "failed": 0, "deactivated": 0}
    if chat_ids is None:
        chat_ids = [user.chat_id for user in db.broadcast_users()]
    sent = failed = deactivated = 0
    for chat_id in chat_ids:
        try:
            for chunk in chunks:
                bot.send_message(chat_id, chunk, disable_web_page_preview=True)
            sent += 1
        except Exception as exc:
            failed += 1
            logger.warning("broadcast delivery failed chat_id=%s error=%s", chat_id, exc)
            if blocked_by_user(exc):
                db.set_user_active(chat_id, False)
                deactivated += 1
        if delay:
            time.sleep(delay)
    logger.info("broadcast finished: delivered %s of %s, failed %s, deactivated %s", sent, len(chat_ids), failed, deactivated)
    return {"users": len(chat_ids), "sent": sent, "failed": failed, "deactivated": deactivated}


def broadcast_photo(photo, caption: str, chat_ids: list[int] | None = None, delay: float = BROADCAST_SEND_DELAY_SECONDS) -> dict[str, int]:
    """Send a file_id, URL or file-like photo, caching its first successful file_id."""
    caption = caption or ""
    photo_caption = caption[:TELEGRAM_CAPTION_LIMIT]
    overflow = caption[TELEGRAM_CAPTION_LIMIT:]
    # Slice without stripping/reordering so the full caption survives the split.
    chunks = [overflow[i:i + TELEGRAM_TEXT_LIMIT] for i in range(0, len(overflow), TELEGRAM_TEXT_LIMIT)]
    if chat_ids is None:
        chat_ids = [user.chat_id for user in db.broadcast_users()]
    file_id = photo if isinstance(photo, str) and not photo.lower().startswith(("http://", "https://")) else None
    upload_position = photo.tell() if hasattr(photo, "seekable") and photo.seekable() else None
    sent = failed = deactivated = 0
    for chat_id in chat_ids:
        try:
            if file_id is None and upload_position is not None:
                photo.seek(upload_position)
            message = bot.send_photo(chat_id, file_id if file_id is not None else photo, caption=photo_caption)
            if file_id is None:
                # Cache before sending overflow: a text failure must not trigger a re-upload.
                file_id = message.photo[-1].file_id
            for chunk in chunks:
                if delay:
                    time.sleep(delay)
                bot.send_message(chat_id, chunk, disable_web_page_preview=True)
            sent += 1
        except Exception as exc:
            failed += 1
            logger.warning("photo broadcast delivery failed chat_id=%s error=%s", chat_id, exc)
            if blocked_by_user(exc):
                db.set_user_active(chat_id, False)
                deactivated += 1
        if delay:
            time.sleep(delay)
    logger.info("photo broadcast finished: delivered %s of %s, failed %s, deactivated %s", sent, len(chat_ids), failed, deactivated)
    return {"users": len(chat_ids), "sent": sent, "failed": failed, "deactivated": deactivated}


@bot.message_handler(commands=["broadcast"])
def broadcast_cmd(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    text = parts[1].strip() if len(parts) > 1 else ""
    run_broadcast_command(message, text)


@bot.message_handler(content_types=["photo"])
def broadcast_photo_cmd(message: types.Message):
    # pyTelegramBotAPI's commands filter only matches text, never photo captions.
    parts = (message.caption or "").split(maxsplit=1)
    if not parts or parts[0].split("@", 1)[0] not in {"/broadcast", "/broadcast_photo"}:
        return
    caption = parts[1].strip() if len(parts) > 1 else ""
    run_broadcast_command(message, caption, photo=message.photo[-1].file_id)


def run_broadcast_command(message: types.Message, text: str, photo=None):
    lang = user_language(message)
    remember_user(message)
    if not is_admin_chat(message.chat.id):
        bot.reply_to(message, "هذا الأمر مخصص للمشرفين فقط." if lang == "ar" else "This command is restricted to admins.")
        return
    if not text and photo is None:
        bot.reply_to(message, "الاستخدام: /broadcast نص الرسالة المراد إرسالها للجميع." if lang == "ar" else "Usage: /broadcast <message text>.")
        return
    pending = db.count_active_users()
    if not pending:
        bot.reply_to(message, "لا يوجد مستخدمون نشطون مسجلون للإرسال." if lang == "ar" else "There are no active users to send to.")
        return
    bot.reply_to(message, f"جارٍ الإرسال إلى {pending} مستخدم نشط..." if lang == "ar" else f"Sending to {pending} active users...")
    result = broadcast_photo(photo, text) if photo is not None else broadcast_text(text)
    if lang == "ar":
        summary = (f"وصلت الرسالة إلى {result['sent']} من {result['users']}. فشل الإرسال: {result['failed']}. "
                   f"تم تعطيل {result['deactivated']} مستخدم حظر البوت.")
    else:
        summary = (f"Delivered to {result['sent']} of {result['users']}. Failed: {result['failed']}. "
                   f"Deactivated {result['deactivated']} blocked users.")
    bot.reply_to(message, summary)


@bot.message_handler(content_types=["text"])
def text_cmd(message: types.Message):
    text = (message.text or "").strip()
    if text.startswith("/"):
        # Never swallow an unknown command silently; say it was not handled.
        lang = user_language(message)
        bot.reply_to(message, AR["unknown_command"] if lang == "ar" else EN["unknown_command"])
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
    if request.intent == "smc":
        # Natural language ("smart money on gold", "مناطق السيولة في البيتكوين") reaches
        # the same deterministic engine as /smc, keeping the chat's stored language
        # preference when the message itself has no clear language marker.
        prefs = smc_prefs.setdefault(message.chat.id, {})
        smc_lang = lang if lang in SMC_LANGS else prefs.get("lang", "ar")
        prefs["lang"] = smc_lang
        if asset is None:
            bot.reply_to(message,
                         "اذكر الأصل مع الطلب، مثل: smc الذهب أو ما مناطق السيولة في BTC؟" if lang == "ar" else
                         "Name the asset with the request, for example: smc gold or what is the BTC liquidity?",
                         reply_markup=smc_keyboard("BTC", smc_lang, prefs.get("mode", "full")))
            return
        smc_respond(message, asset, smc_lang, prefs.get("mode", "full"))
        return
    if request.intent == "decision":
        # "قرار البيتكوين", "buy or sell gold", "verdict BTC" -> the strict engine
        prefs = smc_prefs.setdefault(message.chat.id, {})
        decision_lang = lang if lang in SMC_LANGS else prefs.get("lang", "en")
        prefs["lang"] = decision_lang
        if asset is None:
            bot.reply_to(message,
                         "اذكر الأصل مع طلب القرار، مثل: قرار الذهب شراء أم بيع؟" if lang == "ar" else
                         "Name the asset with the request, for example: buy or sell BTC?",
                         reply_markup=decision_keyboard("BTC", decision_lang))
            return
        decision_respond(message, asset, decision_lang)
        return
    if request.intent == "price":
        # "سعر الذهب؟", "BTC price" -> instant live quote, never a stale close.
        if asset is None:
            bot.reply_to(message,
                         "اذكر الأصل مع السعر، مثل: سعر الذهب أو BTC price." if lang == "ar" else
                         "Name the asset with the price request, for example: gold price or BTC price.")
            return
        try:
            bot.reply_to(message, quote_text(asset, live.get_quote(asset), lang))
        except QuoteUnavailable:
            bot.reply_to(message, AR["data_error"] if lang == "ar" else "No live quote is available for this asset right now.")
        except Exception:
            logger.exception("live price failed")
            bot.reply_to(message, "تعذر جلب السعر حاليًا." if lang == "ar" else "Price lookup failed right now.")
        return
    if request.intent == "binary":
        # "ثنائي EURUSD", "binary gold" -> the short-expiry CALL/PUT engine.
        prefs = smc_prefs.setdefault(message.chat.id, {})
        binary_lang = lang if lang in ("ar", "en") else prefs.get("lang", "ar")
        prefs["lang"] = binary_lang
        if asset is None:
            bot.reply_to(message,
                         "اذكر الأصل مع طلب الثنائي، مثل: ثنائي EURUSD أو binary BTC." if lang == "ar" else
                         "Name the asset with the request, for example: binary EURUSD or ثنائي الذهب.",
                         reply_markup=binary_keyboard("EURUSD", binary_lang))
            return
        binary_respond(message, asset, binary_lang)
        return
    if request.intent == "backtest":
        if asset is None:
            bot.reply_to(message,
                         "اذكر الأصل مع الباك تست، مثل: باك تست EURUSD أو backtest BTC." if lang == "ar" else
                         "Name the asset with the backtest, for example: backtest EURUSD.")
            return
        try:
            numbers = extract_numbers(text)
            days = max(7, min(60, int(numbers[0]))) if numbers else 30
        except (ValueError, IndexError):
            days = 30
        bot.reply_to(message, f"🧪 أختبر {asset.key} على ~{days} يومًا... قد يستغرق هذا دقيقة." if lang == "ar" else f"🧪 Backtesting {asset.key} on ~{days} days... this can take a minute.")
        try:
            result = run_backtest(asset, days)
            bot.send_message(message.chat.id, render_backtest(result, lang), parse_mode="HTML", disable_web_page_preview=True)
        except HistoryUnavailable:
            bot.reply_to(message, AR["data_error"] if lang == "ar" else "Not enough historical data for this backtest.")
        except Exception:
            logger.exception("backtest NL failed")
            bot.reply_to(message, "تعذر إكمال الباك تست حاليًا." if lang == "ar" else "Backtest could not be completed right now.")
        return
    if request.intent == "calendar":
        try:
            for chunk in split_broadcast_text(calendar_list_text(lang, user_timezone(message))):
                bot.send_message(message.chat.id, chunk, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            logger.exception("calendar NL failed")
            bot.reply_to(message, "تعذر جلب التقويم حاليًا." if lang == "ar" else "Calendar is unavailable right now.")
        return
    if request.intent == "stats":
        try:
            bot.reply_to(message, stats_report(db, lang))
        except Exception:
            logger.exception("stats NL failed")
            bot.reply_to(message, "تعذر حساب الإحصائيات حاليًا." if lang == "ar" else "Stats are unavailable right now.")
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
                    "deterministic_indicators_only": True,
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


# --------------------------------------------------------------------------- #
# Economic calendar engine + decision journal resolver.
#
# High-impact events flow through three exactly-once stages (tracked in the
# calendar_state table, so restarts can neither duplicate nor skip a stage):
#   1. pre-brief ~30 min before (expectations + affected assets + caution)
#   2. release ping at event time (live price snapshot for later measuring)
#   3. follow-up ~30 min after (measured market reaction + verdict + plan)
# Events sharing a country and a 5-minute window are grouped into ONE message
# so correlated releases (e.g. the three CAD CPI prints) never spam.
# --------------------------------------------------------------------------- #
NEWS_IMPACT_THRESHOLD = {
    "crypto": 0.008, "forex": 0.002, "metals": 0.003,
    "commodity": 0.005, "index": 0.004, "stock": 0.008, "custom": 0.005,
}
CALENDAR_FOLLOWUP_MINUTES = 30
CALENDAR_PRE_MINUTES = 30


def live_price_for(asset_key: str) -> float | None:
    asset = ASSETS.get(asset_key)
    if not asset:
        return None
    try:
        return live.get_quote(asset).price
    except QuoteUnavailable:
        pass
    try:
        return market.get_candles(asset, settings.default_interval, 100)[-1].close
    except DataUnavailable:
        return None


def _country_label(country: str, lang: str) -> str:
    name, flag = COUNTRIES.get(country, (country, "🏳️"))
    return f"{name} {flag}" if lang == "ar" else f"{country} {flag}"


def _impact_label(impact: str, lang: str) -> str:
    names = IMPACT_AR if lang == "ar" else IMPACT_EN
    return names.get(impact, impact)


def _group_calendar_events(events) -> list[list]:
    """Group correlated releases (same country, same 5-minute bucket).

    Groups come out in time order so messages always read chronologically.
    """
    buckets: dict[tuple[str, int], list] = {}
    for event in sorted(events, key=lambda item: item.event_time):
        bucket = int(event.event_time.timestamp() // 300)
        buckets.setdefault((event.country, bucket), []).append(event)
    return sorted(buckets.values(), key=lambda group: group[0].event_time)


def calendar_pre_text(group, lang: str, tz_name: str) -> str:
    first = group[0]
    titles = " + ".join(event.title for event in group[:3])
    when = format_timestamp(first.event_time, lang, tz_name)
    assets = sorted({key for event in group for key in event.assets})[:4]
    if lang == "ar":
        lines = [
            f"⏳ <b>بعد قليل: {html.escape(titles)}</b>",
            f"{_country_label(first.country, lang)} | {first.stars} {_impact_label(first.impact, lang)}",
            f"🕒 الموعد: {html.escape(when)}",
        ]
        for event in group[:3]:
            if event.forecast or event.previous:
                lines.append(f"🔮 {html.escape(event.title)}: متوقع {html.escape(event.forecast or '—')} | سابق {html.escape(event.previous or '—')}")
        if assets:
            lines.append(f"🎯 الأصول المتأثرة: {', '.join(assets)}")
        lines.append("💡 قلل المخاطرة قبل الخبر: صغّر اللوت أو ضيّق الوقف — سأرسل التحليل لحظة الصدور وبعده.")
        return "\n".join(lines)
    lines = [
        f"⏳ <b>Coming up: {html.escape(titles)}</b>",
        f"{_country_label(first.country, lang)} | {first.stars} {_impact_label(first.impact, lang)}",
        f"🕒 At: {html.escape(when)}",
    ]
    for event in group[:3]:
        if event.forecast or event.previous:
            lines.append(f"🔮 {html.escape(event.title)}: forecast {html.escape(event.forecast or '—')} | previous {html.escape(event.previous or '—')}")
    if assets:
        lines.append(f"🎯 Watch: {', '.join(assets)}")
    lines.append("💡 Reduce risk into the release: smaller size or tighter stop — I will report at release and after.")
    return "\n".join(lines)


def calendar_release_text(group, prices: dict[str, float], lang: str) -> str:
    first = group[0]
    titles = " + ".join(event.title for event in group[:3])
    if lang == "ar":
        lines = [
            f"{first.stars} <b>صدر الآن: {html.escape(titles)}</b>",
            f"{_country_label(first.country, lang)} | {_impact_label(first.impact, lang)}",
            "",
            "💰 أسعار لحظة الصدور (مرجع القياس):",
        ]
    else:
        lines = [
            f"{first.stars} <b>Released: {html.escape(titles)}</b>",
            f"{_country_label(first.country, lang)} | {_impact_label(first.impact, lang)}",
            "",
            "💰 Release-time prices (reaction baseline):",
        ]
    for key in sorted(prices):
        lines.append(f"• {key}: {prices[key]:g}")
    lines.append("")
    lines.append("⚠️ أول دقائق = تذبذب عنيف وسبريد واسع؛ لا تطارد الحركة. سأرسل تحليل رد الفعل بعد 30 دقيقة مع خطة واضحة." if lang == "ar" else "⚠️ First minutes = violent chop and wide spreads; do not chase. I will send the reaction analysis with a clear plan in 30 minutes.")
    return "\n".join(lines)


def event_trade_plan(asset: Asset, direction: str, capital: float, risk_pct: float, lang: str) -> str | None:
    """Theoretical entry/SL/TP + lot size from 15m ATR. None when unmeasurable."""
    try:
        candles = market.get_candles(asset, "15m", 120)
        result = analyze(candles, asset.price_decimals)
    except (DataUnavailable, ValueError):
        return None
    atr = max(result.atr14, result.price * 0.0005)
    entry = result.price
    sl_dist = atr * 1.5
    risk_amount = max(capital, 0) * max(risk_pct, 0) / 100
    if risk_amount <= 0 or sl_dist <= 0:
        return None
    if direction == "BUY":
        stop, tp1, tp2 = entry - sl_dist, entry + sl_dist * 2, entry + sl_dist * 3
    else:
        stop, tp1, tp2 = entry + sl_dist, entry - sl_dist * 2, entry - sl_dist * 3
    units = risk_amount / sl_dist
    if asset.asset_class == "metals" and asset.key in {"XAUUSD", "XAGUSD"}:
        size_text = f"{units / 100:.2f} لوت (1 لوت = 100 أونصة)" if lang == "ar" else f"{units / 100:.2f} lots (1 lot = 100 oz)"
    elif asset.asset_class == "forex":
        size_text = f"≈ {units / 100000:.2f} لوت (تقريبي)" if lang == "ar" else f"≈ {units / 100000:.2f} lots (approx)"
    elif asset.asset_class == "crypto":
        size_text = f"{units:.4f} {asset.key}" if lang == "ar" else f"{units:.4f} {asset.key}"
    else:
        size_text = f"{units:.2f} وحدة" if lang == "ar" else f"{units:.2f} units"
    if lang == "ar":
        return (
            f"📋 <b>خطة نظرية ({'شراء' if direction == 'BUY' else 'بيع'} {asset.key})</b>\n"
            f"🎯 دخول: {entry:g} | 🛑 وقف: {stop:g} | ✅ هدف1: {tp1:g} | هدف2: {tp2:g}\n"
            f"💰 رأس مالك: {capital:g}$ | مخاطرة {risk_pct:g}% = {risk_amount:.2f}$\n"
            f"📦 الحجم المقترح: {size_text}\n"
            f"⚠️ خطة حسابية للتجربة أولًا — ليست توصية مالية."
        )
    return (
        f"📋 <b>Theoretical plan ({direction} {asset.key})</b>\n"
        f"🎯 Entry: {entry:g} | 🛑 SL: {stop:g} | ✅ TP1: {tp1:g} | TP2: {tp2:g}\n"
        f"💰 Capital: ${capital:g} | risk {risk_pct:g}% = ${risk_amount:.2f}\n"
        f"📦 Suggested size: {size_text}\n"
        f"⚠️ A calculated draft for demo first — not financial advice."
    )


def calendar_followup_text(group, moves: list[tuple[str, float, float]], verdict_line: str,
                           plan_text: str | None, lang: str) -> str:
    first = group[0]
    titles = " + ".join(event.title for event in group[:3])
    if lang == "ar":
        lines = [
            f"📊 <b>رد فعل السوق: {html.escape(titles)}</b>",
            f"{_country_label(first.country, lang)} | بعد {CALENDAR_FOLLOWUP_MINUTES} دقيقة من الصدور",
            "",
        ]
    else:
        lines = [
            f"📊 <b>Market reaction: {html.escape(titles)}</b>",
            f"{_country_label(first.country, lang)} | {CALENDAR_FOLLOWUP_MINUTES} min after release",
            "",
        ]
    for key, ref, current in moves:
        asset = ASSETS.get(key)
        decimals = asset.price_decimals if asset else 4
        pct = (current - ref) / ref * 100 if ref else 0.0
        arrow = "🟢" if pct > 0.02 else "🔴" if pct < -0.02 else "⚪"
        lines.append(f"{arrow} {key}: {ref:g} ← {current:g} ({pct:+.2f}%)")
    lines += ["", verdict_line]
    if plan_text:
        lines += ["", plan_text]
    return "\n".join(lines)


def scan_calendar(now_utc: datetime) -> None:
    """Run the three event stages. Safe to call every minute."""
    try:
        pre_due = calendar_feed.due_for_pre(CALENDAR_PRE_MINUTES)
        release_due = calendar_feed.due_for_release()
        followup_due = calendar_feed.due_for_followup(CALENDAR_FOLLOWUP_MINUTES)
    except Exception:
        logger.exception("calendar scan failed")
        return
    users = db.calendar_users()
    if not users:
        return

    for group in _group_calendar_events(pre_due):
        states = [(event, db.ensure_calendar_state(event.key, event.title, event.country,
                                                   event.impact, event.event_time, event.forecast, event.previous))
                  for event in group]
        if all(state.pre_sent for _, state in states):
            continue
        for user in users:
            try:
                bot.send_message(user.chat_id, calendar_pre_text(group, user.language, user.tz_name),
                                 parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                logger.exception("calendar pre-brief failed chat_id=%s", user.chat_id)
        for _, state in states:
            db.mark_calendar_stage(state.event_key, "pre_sent")

    for group in _group_calendar_events(release_due):
        states = [(event, db.ensure_calendar_state(event.key, event.title, event.country,
                                                   event.impact, event.event_time, event.forecast, event.previous))
                  for event in group]
        if all(state.release_sent for _, state in states):
            continue
        assets = sorted({key for event in group for key in event.assets})[:4]
        prices = {key: price for key in assets if (price := live_price_for(key)) is not None}
        for user in users:
            try:
                bot.send_message(user.chat_id, calendar_release_text(group, prices, user.language),
                                 parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                logger.exception("calendar release failed chat_id=%s", user.chat_id)
        for _, state in states:
            db.mark_calendar_stage(state.event_key, "release_sent", json.dumps(prices))

    for group in _group_calendar_events(followup_due):
        states = [(event, db.ensure_calendar_state(event.key, event.title, event.country,
                                                   event.impact, event.event_time, event.forecast, event.previous))
                  for event in group]
        if all(state.followup_sent for _, state in states):
            continue
        # Skip ancient backlog (bot was down for hours): measuring a reaction
        # days late would be fiction presented as analysis.
        age_hours = (now_utc - group[0].event_time).total_seconds() / 3600
        if age_hours > 3:
            for _, state in states:
                db.mark_calendar_stage(state.event_key, "followup_sent")
            continue
        try:
            refs = json.loads(states[0][1].ref_prices or "{}")
        except (ValueError, TypeError):
            refs = {}
        if not refs:
            continue  # release snapshot missing; wait for nothing, mark nothing
        moves: list[tuple[str, float, float]] = []
        for key, ref in refs.items():
            current = live_price_for(key)
            if current:
                moves.append((key, float(ref), current))
        if not moves:
            continue
        # Direction comes from the short-expiry engine on the most-moved asset.
        main_key = max(moves, key=lambda row: abs((row[2] - row[1]) / row[1] if row[1] else 0.0))[0]
        main_asset = ASSETS.get(main_key)
        plan_direction = None
        try:
            verdict = binary_verdict_for(main_asset) if main_asset else None
        except DataUnavailable:
            verdict = None
        if verdict is not None and verdict.verdict == "CALL":
            plan_direction = "BUY"
        elif verdict is not None and verdict.verdict == "PUT":
            plan_direction = "SELL"

        def _verdict_line(lang: str) -> str:
            if verdict is None or main_asset is None:
                return "⚖️ Live verdict unavailable — watch only." if lang == "en" else "⚖️ تعذر حساب قرار لحظي — راقب فقط."
            if verdict.verdict == "WAIT":
                return f"⚖️ Verdict on {main_key}: ⏸️ WAIT — no entry." if lang == "en" else f"⚖️ القرار على {main_key}: ⏸️ انتظار — لا دخول الآن."
            if lang == "en":
                arrow = "🟢 BUY" if verdict.verdict == "CALL" else "🔴 SELL"
                return f"⚖️ Verdict on {main_key}: {arrow} ({verdict.confidence} confidence)."
            arrow = "🟢 شراء" if verdict.verdict == "CALL" else "🔴 بيع"
            return f"⚖️ القرار على {main_key}: {arrow} (ثقة {verdict.confidence})."

        for user in users:
            lang = user.language
            line = _verdict_line(lang)
            plan = event_trade_plan(main_asset, plan_direction, user.capital or 1000.0,
                                    user.risk_percent or 1.0, lang) if (main_asset and plan_direction) else None
            try:
                bot.send_message(user.chat_id, calendar_followup_text(group, moves, line, plan, lang),
                                 parse_mode="HTML", disable_web_page_preview=True)
                if main_asset and plan_direction:
                    db.journal_add("event", main_asset.key, plan_direction, moves[0][2] if moves else 0,
                                   60, chat_id=user.chat_id, note=f"calendar:{group[0].key[:8]}")
            except Exception:
                logger.exception("calendar followup failed chat_id=%s", user.chat_id)
        for _, state in states:
            db.mark_calendar_stage(state.event_key, "followup_sent")


def news_impact_text(asset: Asset, headline: str, move_pct: float, lang: str) -> str:
    name = asset.name_ar if lang == "ar" else asset.name_en
    if lang == "ar":
        direction = "صاعد 📈" if move_pct > 0 else "هابط 📉"
        return (
            f"📡 <b>تأثير الخبر على {html.escape(name)}</b>\n"
            f"📰 {html.escape(headline[:180])}\n"
            f"📊 الحركة منذ الخبر: <b>{move_pct:+.2f}%</b> — الاتجاه قصير المدى {direction}\n"
            f"💡 هذه قراءة لرد الفعل الفعلي؛ إن أردت خطة دخول اطلب /binary {asset.key}."
        )
    direction = "up 📈" if move_pct > 0 else "down 📉"
    return (
        f"📡 <b>News impact on {html.escape(name)}</b>\n"
        f"📰 {html.escape(headline[:180])}\n"
        f"📊 Move since the news: <b>{move_pct:+.2f}%</b> — short-term direction {direction}\n"
        f"💡 This reads the actual reaction; ask /binary {asset.key} for an entry plan."
    )


def resolve_journal_and_notify() -> None:
    """Resolve due journal rows; loudly report big news impacts only."""
    try:
        due = db.journal_due(limit=50)
    except Exception:
        logger.exception("journal fetch failed")
        return
    for entry in due:
        asset = ASSETS.get(entry.asset_key)
        exit_price = live_price_for(entry.asset_key) if asset else None
        if exit_price is None:
            continue  # keep pending; a missing quote must not forge an outcome
        from marketobserver.learning import classify as classify_outcome
        outcome = classify_outcome(entry.ref_price, exit_price, entry.verdict)
        if entry.kind == "news" and entry.chat_id and asset:
            move_pct = (exit_price - entry.ref_price) / entry.ref_price * 100 if entry.ref_price else 0.0
            threshold = NEWS_IMPACT_THRESHOLD.get(asset.asset_class, 0.005) * 100
            if abs(move_pct) >= threshold:
                user = db.get_user(entry.chat_id)
                lang = user.language if user else "ar"
                try:
                    bot.send_message(entry.chat_id, news_impact_text(asset, entry.note or "", move_pct, lang),
                                     parse_mode="HTML", disable_web_page_preview=True)
                except Exception:
                    logger.exception("news impact delivery failed chat_id=%s", entry.chat_id)
        db.journal_resolve(entry.id, outcome, exit_price)


def alert_loop():
    global last_signal_scan_at, last_news_scan_at, last_calendar_scan_at, last_journal_scan_at
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
                # Alerts trigger on the live quote (seconds old), not on the
                # last closed candle; the candle close is only a fallback so a
                # brief quote outage never freezes every pending alert.
                try:
                    prices[asset_key] = live.get_quote(asset).price
                    continue
                except QuoteUnavailable:
                    logger.info("live quote unavailable for %s; falling back to candle close", asset_key)
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
                        if cooldown_active(user.signal_cooldown_until, current_utc):
                            continue
                        try:
                            bot.send_message(user.chat_id, signal_text(rows, user.language, user.tz_name))
                            db.set_signal_cooldown(user.chat_id, current_utc + timedelta(hours=1))
                        except Exception:
                            logger.exception("signal delivery failed chat_id=%s", user.chat_id)
                last_signal_scan_at = now
            if now - last_news_scan_at >= settings.news_scan_seconds:
                # One top story per user per cycle: high impact + fresh + never
                # sent to THIS user. Seen-tracking is per chat, so an idle user
                # misses nothing that another user received.
                users = db.news_users()
                snapshots = {}
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
                    if cooldown_active(user.news_cooldown_until, current_news_time):
                        continue
                    scope = (user.news_assets or "ALL").strip()
                    requested = list(ASSETS.keys())[:12] if scope.upper() == "ALL" else [key.strip() for key in scope.split(",") if key.strip()]
                    candidates: list[tuple] = []
                    for asset_key in requested:
                        snapshot = snapshots.get(asset_key)
                        if not snapshot or not snapshot.items:
                            continue
                        for item in snapshot.items:
                            if headline_importance_level(item) != "high":
                                continue
                            if not is_fresh(item, 6.0, current_news_time):
                                continue
                            if db.news_was_seen(headline_fingerprint(item), chat_id=user.chat_id):
                                continue
                            age = headline_age_hours(item, current_news_time)
                            candidates.append((age if age is not None else 1e9, snapshot.asset, item))
                    if not candidates:
                        continue
                    candidates.sort(key=lambda row: row[0])
                    _, top_asset, top_item = candidates[0]
                    try:
                        bot.send_message(user.chat_id, news_alert_text(top_asset, [top_item], user.language, user.tz_name), parse_mode="HTML", disable_web_page_preview=True)
                        db.mark_news_seen(headline_fingerprint(top_item), chat_id=user.chat_id)
                        db.set_news_cooldown(user.chat_id, current_news_time + timedelta(minutes=30))
                        # Watch the impact: directional news is journaled so the
                        # resolver can report the real market reaction later.
                        if top_item.sentiment in {"positive", "negative"}:
                            ref = live_price_for(top_asset.key)
                            if ref:
                                db.journal_add("news", top_asset.key,
                                               "CALL" if top_item.sentiment == "positive" else "PUT",
                                               ref, 45, chat_id=user.chat_id,
                                               note=_headline_parts(top_item.title)[0][:180])
                    except Exception:
                        logger.exception("news delivery failed chat_id=%s", user.chat_id)
                last_news_scan_at = now
            if now - last_calendar_scan_at >= settings.calendar_scan_seconds:
                scan_calendar(datetime.now(timezone.utc))
                last_calendar_scan_at = now
            if now - last_journal_scan_at >= 120:
                resolve_journal_and_notify()
                last_journal_scan_at = now
        except Exception:
            logger.exception("alert loop failure")
        time.sleep(settings.alert_poll_seconds)


@app.get("/")
def home():
    return "MarketObserver Pro is running"


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "marketobserver", "stats": db.stats()})


@app.get("/smc/<asset_key>")
def smc_endpoint(asset_key: str):
    """One multi-timeframe analysis as JSON from the same deterministic engine the
    bot uses, so a dashboard or script can consume it without Telegram.

    Query params: lang (ar|en|both), mode=brief for the short summary only,
    refresh=1 to bypass the short cache.
    """
    asset = resolve_asset(asset_key)
    if asset is None:
        return jsonify({"error": "unknown asset"}), 404
    lang = request.args.get("lang", "en")
    if lang not in SMC_LANGS:
        return jsonify({"error": "lang must be ar, en or both"}), 400
    try:
        report, _ = smc_build_report(asset, force=request.args.get("refresh") == "1")
    except DataUnavailable:
        return jsonify({"error": "no reliable market data for this asset", "asset": asset.key}), 503
    payload = smc_to_dict(report)
    if request.args.get("mode") == "brief":
        payload["summary"] = render_smc_brief(report, lang)
    else:
        payload["report"] = render_smc(report, lang)
    return jsonify(payload)


@app.get("/decision/<asset_key>")
def decision_endpoint(asset_key: str):
    """The strict BUY / SELL / WAIT verdict as JSON plus its rendered block.

    Query params: lang (ar|en|both), gates=1 for the full gate chain,
    refresh=1 to bypass the short cache.
    """
    asset = resolve_asset(asset_key)
    if asset is None:
        return jsonify({"error": "unknown asset"}), 404
    lang = request.args.get("lang", "en")
    if lang not in SMC_LANGS:
        return jsonify({"error": "lang must be ar, en or both"}), 400
    verbose = request.args.get("gates") == "1"
    try:
        decision, _ = decision_candles(asset, force=request.args.get("refresh") == "1")
    except DataUnavailable:
        return jsonify({"error": "no reliable market data for this asset", "asset": asset.key}), 503
    payload = decision_to_dict(decision)
    payload["summary"] = render_decision_brief(decision, "ar" if lang == "ar" else "en")
    payload["report"] = render_decision(decision, lang, verbose=verbose)
    return jsonify(payload)


@app.get("/price/<asset_key>")
def price_endpoint(asset_key: str):
    """Instant live quote as JSON: price, venue source and quote age."""
    asset = resolve_asset(asset_key)
    if asset is None:
        return jsonify({"error": "unknown asset"}), 404
    try:
        quote = live.get_quote(asset)
    except QuoteUnavailable:
        return jsonify({"error": "no live quote for this asset", "asset": asset.key}), 503
    return jsonify({
        "asset": asset.key,
        "name_ar": asset.name_ar,
        "name_en": asset.name_en,
        "price": quote.price,
        "quote": asset.quote,
        "source": quote.source,
        "age_seconds": quote.age_seconds,
        "as_of": quote.as_of.isoformat(),
    })


@app.get("/assets")
def assets_endpoint():
    """Full asset catalog grouped by class, from the same source the bot uses."""
    wanted = (request.args.get("class") or "").strip().lower()
    classes = [wanted] if wanted in ASSET_CLASS_ORDER else list(ASSET_CLASS_ORDER)
    return jsonify({
        "count": sum(len(assets_by_class(cls)) for cls in classes),
        "classes": {
            cls: [{"key": asset.key, "name_ar": asset.name_ar, "name_en": asset.name_en,
                   "quote": asset.quote, "decimals": asset.price_decimals}
                  for asset in assets_by_class(cls)]
            for cls in classes
        },
    })


@app.get("/binary/<asset_key>")
def binary_endpoint(asset_key: str):
    """Short-expiry CALL / PUT / WAIT verdict as JSON plus its rendered block.

    Query params: lang (ar|en), refresh=1 to bypass the short cache.
    """
    asset = resolve_asset(asset_key)
    if asset is None:
        return jsonify({"error": "unknown asset"}), 404
    lang = request.args.get("lang", "ar")
    if lang not in ("ar", "en"):
        return jsonify({"error": "lang must be ar or en"}), 400
    try:
        verdict = binary_verdict_for(asset, force=request.args.get("refresh") == "1")
    except DataUnavailable:
        return jsonify({"error": "no reliable 5m/15m data for this asset", "asset": asset.key}), 503
    payload = binary_to_dict(verdict)
    payload["report"] = render_binary(verdict, lang)
    return jsonify(payload)


@app.get("/calendar")
def calendar_endpoint():
    """Upcoming high/medium-impact events. Query: hours (default 24)."""
    try:
        hours = max(1, min(168, int(request.args.get("hours", "24"))))
    except ValueError:
        return jsonify({"error": "hours must be an integer"}), 400
    try:
        events = calendar_feed.upcoming(hours, ("High", "Medium"))
    except Exception:
        return jsonify({"error": "calendar unavailable"}), 503
    return jsonify({
        "count": len(events),
        "events": [{
            "title": event.title, "country": event.country, "impact": event.impact,
            "time": event.event_time.isoformat(), "forecast": event.forecast,
            "previous": event.previous, "assets": list(event.assets),
        } for event in events],
    })


@app.get("/stats")
def stats_endpoint():
    """Decision-journal accuracy. Query: days (default 30), kind, lang."""
    try:
        days = max(1, min(365, int(request.args.get("days", "30"))))
    except ValueError:
        return jsonify({"error": "days must be an integer"}), 400
    kind = request.args.get("kind") or None
    if kind and kind not in {"binary", "event", "news"}:
        return jsonify({"error": "kind must be binary, event or news"}), 400
    lang = request.args.get("lang", "ar")
    if lang not in {"ar", "en"}:
        return jsonify({"error": "lang must be ar or en"}), 400
    summary = db.journal_stats(days=days, kind=kind)
    decided = summary["wins"] + summary["losses"]
    return jsonify({
        "days": days, "kind": kind or "all",
        "trades": summary["trades"], "wins": summary["wins"],
        "losses": summary["losses"], "flats": summary["flats"],
        "win_rate": round(summary["wins"] / decided, 3) if decided else 0.0,
        "breakdown": [
            {"asset": asset, "verdict": verdict, "trades": count, "wins": wins}
            for asset, verdict, count, wins in db.journal_breakdown(days=days, kind=kind)
        ],
        "report": stats_report(db, lang, days),
    })


@app.get("/backtest/<asset_key>")
def backtest_endpoint(asset_key: str):
    """Walk-forward binary backtest. Query: days (7-60), payout, lang."""
    asset = resolve_asset(asset_key)
    if asset is None:
        return jsonify({"error": "unknown asset"}), 404
    try:
        days = max(7, min(60, int(request.args.get("days", "30"))))
        payout = float(request.args.get("payout", "0.8"))
        if not 0 < payout <= 1:
            raise ValueError
    except ValueError:
        return jsonify({"error": "days must be 7-60 and payout within (0, 1]"}), 400
    lang = request.args.get("lang", "ar")
    if lang not in {"ar", "en"}:
        return jsonify({"error": "lang must be ar or en"}), 400
    try:
        result = run_backtest(asset, days, payout)
    except HistoryUnavailable:
        return jsonify({"error": "not enough historical data", "asset": asset.key}), 503
    payload = backtest_to_dict(result)
    payload["report"] = render_backtest(result, lang)
    return jsonify(payload)


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


@app.post("/admin/broadcast")
def admin_broadcast():
    if not authorized():
        return jsonify({"error": "unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "payload must be a JSON object"}), 400
    text = str(payload.get("text", "")).strip()
    photo_url = payload.get("photo_url")
    photo_file_id = payload.get("photo_file_id")
    for field in ("photo_url", "photo_file_id"):
        if field in payload and (not isinstance(payload[field], str) or not payload[field].strip()):
            return jsonify({"error": f"{field} must be a non-empty string"}), 400
    if photo_url and photo_file_id:
        return jsonify({"error": "provide only one of photo_url or photo_file_id"}), 400
    photo = photo_file_id or photo_url
    caption = payload.get("caption", text)
    if photo is not None and not isinstance(caption, str):
        return jsonify({"error": "caption must be a string"}), 400
    if not text and photo is None:
        return jsonify({"error": "text, photo_url or photo_file_id is required"}), 400
    chat_ids = payload.get("chat_ids")
    if chat_ids is not None:
        if not isinstance(chat_ids, list) or not all(isinstance(value, int) for value in chat_ids):
            return jsonify({"error": "chat_ids must be a list of integers"}), 400
    if photo is not None:
        return jsonify(broadcast_photo(photo.strip(), caption, chat_ids))
    return jsonify(broadcast_text(text, chat_ids))


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
    try:
        bot.set_my_commands([
            types.BotCommand("start", "البداية والأزرار / start and buttons"),
            types.BotCommand("price", "سعر لحظي / live price"),
            types.BotCommand("assets", "كتالوج الأصول / asset catalog"),
            types.BotCommand("binary", "تداول ثنائي CALL/PUT / binary verdict"),
            types.BotCommand("backtest", "اختبار الاستراتيجية على الماضي / backtest"),
            types.BotCommand("calendar", "التقويم الاقتصادي / economic calendar"),
            types.BotCommand("stats", "دقة البوت / bot accuracy"),
            types.BotCommand("capital", "رأس المال والمخاطرة / capital and risk"),
            types.BotCommand("decision", "قرار صارم شراء/بيع/انتظار / strict verdict"),
            types.BotCommand("smc", "تحليل سيولة ذكية 4H/1H/15m / smart-money report"),
            types.BotCommand("analyze", "تحليل المؤشرات / indicator analysis"),
            types.BotCommand("chart", "شارت من بيانات حقيقية / real-data chart"),
            types.BotCommand("risk", "حساب المخاطرة / position sizing"),
            types.BotCommand("alert", "تنبيه سعري / price alert"),
            types.BotCommand("alerts", "قائمة التنبيهات / list alerts"),
            types.BotCommand("cancel_alert", "إلغاء تنبيه / cancel alert"),
            types.BotCommand("signals", "إشارات السوق / market signals"),
            types.BotCommand("newsalerts", "تنبيهات الأخبار / news alerts"),
            types.BotCommand("timezone", "المنطقة الزمنية / timezone"),
            types.BotCommand("paperbuy", "شراء ورقي / paper buy"),
            types.BotCommand("papersell", "بيع ورقي / paper sell"),
        ])
    except Exception:
        logger.warning("could not register the Telegram command menu", exc_info=True)
    logger.info("MarketObserver Pro started in %s mode", "paper" if settings.paper_trading else "live-disabled")
    while True:
        try:
            bot.polling(non_stop=True, interval=0, timeout=20)
        except Exception:
            logger.exception("telegram polling stopped; retrying")
            time.sleep(5)


if __name__ == "__main__":
    main()