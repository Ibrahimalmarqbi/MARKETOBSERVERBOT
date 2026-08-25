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
    if re.search(r"(من انت|من انت؟|من مطورك|من صنعك|مين انت|who are you|your developer|developer|مساعده|مساعده|help|مرحبا|اهلا|السلام عليكم|شكرا|thanks|thank you|hello|hi)", lowered):
        return "general"
    if re.search(r"(افضل اصل|أفضل اصل|افضل عمله|أفضل عملة|افضل سهم|أفضل سهم|ماذا اشتري|وش اشتري|what should i buy|best asset|best coin|best stock|rank|ترتيب)", lowered):
        return "rank"
    if re.search(r"(خبر|اخبار|أخبار|news|headline|sentiment|مشاعر السوق|معنويات)", lowered):
        return "news"
    if re.search(r"(نبه|تنبيه|اشعار|راقب|alert|notify|watch)", lowered):
        return "alert"
    if re.search(r"(مخاطر|مخاطره|risk|حجم الصفقه|حجم|position size|وقف الخساره|راس المال|رأس المال)", lowered):
        return "risk"
    if re.search(r"(شارت|رسم|chart|graph)", lowered):
        return "chart"
    if re.search(r"(انصح|تنصح|نصيحه|رايك|مناسب|ادخل|دخول|شراء|اشتر|buy|entry|enter|sell|بيع|اخرج|خروج|exit|ابيع|الوقت المناسب)", lowered):
        return "advice"
    if re.search(r"(لماذا|ليش|اشرح|سبب|why|explain|تحليل|حلل|وضع|اتجاه|analysis|trend)", lowered):
        return "analysis"
    return "analysis"


def parse_request(text: str, last_asset_key: str | None = None) -> UserRequest:
    intent = _intent(text)
    asset = resolve_asset(text)
    if asset is None and last_asset_key and intent in {"analysis", "advice", "chart", "news"}:
        asset = resolve_asset(last_asset_key)
    return UserRequest(
        raw_text=text or "",
        intent=intent,
        language=detect_language(text),
        asset=asset,
        timeframe=_timeframe(text),
    )