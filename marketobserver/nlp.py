from __future__ import annotations

import re
from dataclasses import dataclass

from .assets import Asset, resolve_asset


@dataclass(frozen=True)
class UserRequest:
    raw_text: str
    intent: str
    language: str
    asset: Asset | None
    timeframe: str | None


def normalize_text(text: str) -> str:
    value = (text or "").strip().lower()
    value = value.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    value = value.replace("ى", "ي").replace("ة", "ه")
    value = re.sub(r"\s+", " ", value)
    return value


def detect_language(text: str) -> str:
    return "ar" if re.search(r"[\u0600-\u06ff]", text or "") else "en"


def _timeframe(text: str) -> str | None:
    lowered = normalize_text(text)
    if re.search(r"(5m|5min|5 دقايق|5 دقيقه|خمس دقايق|خمس دقائق)", lowered):
        return "5m"
    if re.search(r"\b(15m|15min|ربع ساعه|15 دقيقه)\b", lowered):
        return "15m"
    if re.search(r"\b(1h|hour|ساعة|ساعه|ساعي)\b", lowered):
        return "1h"
    if re.search(r"\b(4h|4 ساعات|اربع ساعات)\b", lowered):
        return "4h"
    if re.search(r"\b(1d|daily|day|يومي|اليوم|يوم)\b", lowered):
        return "1d"
    return None


def _intent(text: str) -> str:
    lowered = normalize_text(text)
    if re.search(r"(من انت|من مطورك|من صنعك|مين انت|who are you|your developer|developer|مساعده|help|مرحبا|اهلا|السلام عليكم|شكرا|thanks|thank you|hello|hi)", lowered):
        return "general"
    if re.search(r"(افضل اصل|افضل عمله|افضل سهم|ماذا اشتري|وش اشتري|what should i buy|best asset|best coin|best stock|rank|ترتيب)", lowered):
        return "rank"
    if re.search(r"(\bsmc\b|smart money|smart\-money|price action|liquidity zone|liquidity zones|liquidity pool|liquidity sweep|order block|fair value gap|\bfvg\b|\bbos\b|\bchoch\b|change of character|break of structure|سيوله ذكيه|المال الذكي|اموال ذكيه|مناطق السيوله|منطقة سيوله|منطقة السيوله|صيد السيوله|اوردر بلوك|بلوك الاوامر|فجوه سعريه|فجوات سعريه|كسر الهيكل|كسر البنيه|تغير الشخصيه|تغير الطباع|انفجار سعري|تحليل متعدد الاطر|متعدد الاطر|اطار زمني اعلى|هيكل سعري)", lowered):
        return "smc"
    if re.search(r"(قرار نهائي|القرار النهائي|قرار شراء|قرار بيع|شراء ام بيع|بيع ام شراء|اشتري او ابيع|ابيع او اشتري|انتظر ام ادخل|ادخل ولا انتظر|buy or sell|sell or buy|buy or wait|long or short|\bverdict\b|final decision|strict decision)", lowered):
        # An explicit request for the strict verdict (BUY / SELL / WAIT) routes to
        # the gate chain instead of the looser advisory text.
        return "decision"
    if re.search(r"(خيارات ثنائيه|تداول ثنائي|الخيارات الثنائيه|binary|باينري|ثنائي|كول او بوت|صعود او هبوط)", lowered):
        return "binary"
    if re.search(r"(تقويم|calendar|اجنده اقتصاديه|الأجندة|احداث اليوم|احداث الغد|مفكره اقتصاديه|event calendar|economic calendar)", lowered):
        return "calendar"
    if re.search(r"(دقتك|دقه البوت|ادائك|اداء البوت|احصائيات|احصائياتك|نتائجك|سجل ادائك|win rate|accuracy|performance|stats|how good are you|نتايجك)", lowered):
        return "stats"
    if re.search(r"(خبر|اخبار|news|headline|sentiment|مشاعر السوق|معنويات)", lowered):
        return "news"
    if re.search(r"(سعر|اسعار|بكم|كم قيمه|كم سعر|السعر الحالي|price|quote|كم وصل)", lowered):
        return "price"
    if re.search(r"(نبه|تنبيه|اشعار|راقب|alert|notify|watch)", lowered):
        return "alert"
    if re.search(r"(مخاطر|مخاطره|risk|حجم الصفقه|حجم|position size|وقف الخساره|راس المال|رأس المال)", lowered):
        return "risk"
    if re.search(r"(شارت|رسم|chart|graph)", lowered):
        return "chart"
    if re.search(r"(انصح|تنصح|نصيحه|رايك|مناسب|ادخل|دخول|شراء|اشتر|buy|entry|enter|sell|بيع|اخرج|خروج|exit|ابيع|الوقت المناسب)", lowered):
        return "advice"
    if re.search(r"(ما هو|ماهي|ما هي|اش يعني|يعني ايش|تعريف|كيف يعمل|ما الفرق|الفرق بين|what is|what are|how does|difference between|explain)", lowered):
        return "education"
    if re.search(r"(لماذا|ليش|سبب|why|تحليل|حلل|وضع|اتجاه|analysis|trend)", lowered):
        return "analysis"
    return "unknown"


def _refers_to_last_asset(text: str) -> bool:
    lowered = normalize_text(text)
    return bool(re.search(r"(حلله|حللها|حلل هذا|هذا الاصل|السعر الحالي|سعره|سعرها|الاتجاه الحالي|اتجاهه|اتجاهها|وضعه|وضعها|اخباره|اخبارها|تحليله|تحليلها|this asset|its price|current price|current trend|analyze it)", lowered))


def _known_education_topic(text: str) -> bool:
    lowered = normalize_text(text)
    return bool(re.search(r"(rsi|سيوله|السيوله|سيولة|اوردر بلوك|بلوك الاوامر|order block|فجوه سعريه|fair value gap|fvg|bos|choch|كسر الهيكل|تغير الشخصيه|هيكل سعري|مناطق السيوله|مؤشر القوه النسبيه|وقف الخساره|وقف الخساره|دعم|مقاومه|تضخم|فائده|فائدة|تداول|استثمار|رافعة|رافعه|هامش|leverage|margin|stop loss|support|resistance|inflation|interest rate|trading|investing)", lowered))


def _is_definition_question(text: str) -> bool:
    lowered = normalize_text(text)
    return bool(re.search(r"(ما هو|ما هي|ماهيه|ماهي|اش يعني|يعني ايش|عرف لي|تعريف|what is|what are|how does|explain|definition)", lowered))


def parse_request(text: str, last_asset_key: str | None = None) -> UserRequest:
    intent = _intent(text)
    if intent == "smc" and resolve_asset(text) is None and _is_definition_question(text):
        # "what is liquidity?" asks for a definition, not for a live report on an
        # asset; keep those in the education path so neither answer is wrong.
        intent = "education"
    asset = resolve_asset(text)
    if intent == "education" and asset is None and not _known_education_topic(text):
        intent = "unknown"
    if intent == "unknown" and asset is not None:
        intent = "analysis"
    if asset is None and last_asset_key and intent in {"advice", "chart", "binary"}:
        asset = resolve_asset(last_asset_key)
    elif asset is None and last_asset_key and intent == "price":
        asset = resolve_asset(last_asset_key)
    elif asset is None and last_asset_key and intent == "news" and _refers_to_last_asset(text):
        asset = resolve_asset(last_asset_key)
    elif asset is None and last_asset_key and intent == "analysis" and _refers_to_last_asset(text):
        asset = resolve_asset(last_asset_key)
    return UserRequest(
        raw_text=text or "",
        intent=intent,
        language=detect_language(text),
        asset=asset,
        timeframe=_timeframe(text),
    )