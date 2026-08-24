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


def detect_language(text: str) -> str:
    return "ar" if re.search(r"[\u0600-\u06ff]", text or "") else "en"


def _timeframe(text: str) -> str | None:
    lowered = (text or "").lower()
    if re.search(r"\b(15m|15min|ربع ساعة|15 دقيق)\b", lowered):
        return "15m"
    if re.search(r"\b(1h|hour|ساعة|ساعه)\b", lowered):
        return "1h"
    if re.search(r"\b(4h|4 ساعات|اربع ساعات)\b", lowered):
        return "4h"
    if re.search(r"\b(1d|daily|day|يومي|اليوم)\b", lowered):
        return "1d"
    return None


def _intent(text: str) -> str:
    lowered = (text or "").lower()
    if re.search(r"(نبه|تنبيه|alert|notify|راقب)", lowered):
        return "alert"
    if re.search(r"(مخاطر|مخاطرة|risk|حجم الصفقة|position size|وقف الخسارة)", lowered):
        return "risk"
    if re.search(r"(شارت|رسم|chart|graph)", lowered):
        return "chart"
    if re.search(r"(ادخل|أدخل|دخول|شراء|اشتر|buy|entry|enter|sell|بيع|اخرج|خروج|exit|أبيع)", lowered):
        return "advice"
    if re.search(r"(لماذا|ليش|اشرح|سبب|why|explain|تحليل|حلل|وضع|اتجاه|analysis|trend)", lowered):
        return "analysis"
    return "analysis"


def parse_request(text: str, last_asset_key: str | None = None) -> UserRequest:
    asset = resolve_asset(text)
    if asset is None and last_asset_key:
        asset = resolve_asset(last_asset_key)
    return UserRequest(
        raw_text=text or "",
        intent=_intent(text),
        language=detect_language(text),
        asset=asset,
        timeframe=_timeframe(text),
    )