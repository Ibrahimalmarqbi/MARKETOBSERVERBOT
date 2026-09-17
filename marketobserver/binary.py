from __future__ import annotations

"""Short-expiry (binary-style) verdict engine: CALL / PUT / WAIT.

Binary options are decided by where the price is at expiry, minutes away, so
this engine deliberately works on the 15m context and the 5m execution frame
— not on 4H swing levels. Every gate is arithmetic on closed candles; the
engine never uses an LLM for the decision itself, because the same candles
must always produce the same verdict (that is what makes the walk-forward
backtest and the accuracy journal meaningful).

Scoring model
-------------
The 8 gates are tiered instead of all equal:

* VETO gates (safety): data-5m, data-15m, fresh-data, trend-agreement,
  rsi-band, no-spike. A veto failure means WAIT no matter how many points
  the rest collected — you cannot "buy" stale data, a sideways market, an
  overbought snap-back or a news spike with extra points.
* SCORED gates (quality): trigger-candle, price-vs-sma20. These sharpen the
  conviction but do not block by themselves in ``scored`` mode.

Entry modes
-----------
* ``strict`` (default): all 8 gates must pass (the original rule).
* ``scored``: enter when every veto gate passes; the score is then 6-8/8 and
  maps to MODERATE / STRONG / VERY STRONG. ``BINARY_ENTRY_MODE=scored``
  switches the live engine, and the backtest measures the same mode so the
  two can be compared on real history.

The engine never promises a win rate. It only answers: "is momentum aligned
enough on 15m+5m with a confirming candle to justify the next 10-15 minutes,
or is waiting the professional choice?"
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .analysis import Analysis, analyze
from .market_data import Candle

MIN_CANDLES = 50
# RSI bands keep entries out of overbought/oversold extremes where a binary
# expiry is most likely to snap back.
RSI_CALL = (50.0, 72.0)
RSI_PUT = (28.0, 50.0)
# A trigger candle whose body is less than this fraction of its range is a
# doji-like indecision print, not confirmation.
MIN_BODY_FRACTION = 0.25
# A trigger candle wider than 3x ATR usually means a news spike; binary
# pricing during spikes is at its worst, so the engine stands aside.
MAX_RANGE_ATR_MULTIPLE = 3.0
# Suggested expiry: 2-3 execution candles.
EXPIRY_MINUTES = 15
# A 5m bar older than this means the market is closed (stocks overnight,
# forex on weekends) or the feed is stale — deciding on it would be fiction.
MAX_BAR_AGE = {"hours": 4}

# Safety gates: any one failing forces WAIT, score irrelevant.
VETO_GATES = ("data-5m", "data-15m", "fresh-data", "trend-agreement", "rsi-band", "no-spike")
# Quality gates: each adds conviction to the score.
SCORED_GATES = ("trigger-candle", "price-vs-sma20")
ALL_GATES = VETO_GATES + SCORED_GATES
TOTAL_SCORE = len(ALL_GATES)
# Gates that must all pass in strict mode (the original all-or-nothing rule).
DECISIVE_GATES = ("fresh-data", "trend-agreement", "rsi-band",
                  "trigger-candle", "no-spike", "price-vs-sma20")

STRENGTHS = {TOTAL_SCORE: "VERY STRONG", 7: "STRONG", 6: "MODERATE"}


def strength_for(score: int, verdict: str) -> str:
    """Grade the verdict by its score. WAIT is always WEAK context."""
    if verdict == "WAIT":
        return "WEAK"
    return STRENGTHS.get(score, "WEAK")


@dataclass(frozen=True)
class Gate:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class BinaryVerdict:
    asset_key: str
    asset_ar: str
    asset_en: str
    quote: str
    decimals: int
    verdict: str  # CALL | PUT | WAIT
    confidence: str  # high | medium | low
    expiry_minutes: int | None
    reference_price: float | None
    live_price: float | None
    rsi_15m: float | None
    rsi_5m: float | None
    trend_15m: str | None
    trend_5m: str | None
    gates: tuple[Gate, ...] = ()
    source: str = "unknown"
    as_of: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)
    score: int = 0  # gates passed, out of TOTAL_SCORE
    entry_mode: str = "strict"  # strict | scored


def _trigger_candle(candles_5m: list[Candle]) -> Candle:
    return candles_5m[-1]


def decide(asset_key: str, asset_ar: str, asset_en: str, quote: str, decimals: int,
           candles_5m: list[Candle] | None, candles_15m: list[Candle] | None,
           source: str = "unknown", live_price: float | None = None,
           now: datetime | None = None, strict: bool = True) -> BinaryVerdict:
    gates: list[Gate] = []
    warnings: list[str] = []
    moment = now or datetime.now(timezone.utc)
    as_of = moment.isoformat()

    if not candles_5m or len(candles_5m) < MIN_CANDLES:
        gates.append(Gate("data-5m", False, f"needs {MIN_CANDLES} closed 5m candles, got {len(candles_5m or [])}"))
    else:
        gates.append(Gate("data-5m", True, f"{len(candles_5m)} closed 5m candles"))
    if not candles_15m or len(candles_15m) < MIN_CANDLES:
        gates.append(Gate("data-15m", False, f"needs {MIN_CANDLES} closed 15m candles, got {len(candles_15m or [])}"))
    else:
        gates.append(Gate("data-15m", True, f"{len(candles_15m)} closed 15m candles"))

    if candles_5m and len(candles_5m) >= MIN_CANDLES:
        newest = candles_5m[-1].timestamp
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        age_minutes = (moment - newest).total_seconds() / 60
        fresh = age_minutes <= MAX_BAR_AGE["hours"] * 60
        gates.append(Gate("fresh-data", fresh,
                          f"newest 5m bar is {age_minutes:.0f} min old" + ("" if fresh else " — market closed or stale feed")))
    else:
        gates.append(Gate("fresh-data", False, "no 5m bars to date"))

    view_5m: Analysis | None = None
    view_15m: Analysis | None = None
    if all(gate.passed for gate in gates):
        try:
            view_5m = analyze(candles_5m, decimals)  # type: ignore[arg-type]
            view_15m = analyze(candles_15m, decimals)  # type: ignore[arg-type]
        except ValueError as exc:
            gates.append(Gate("indicators", False, str(exc)))

    reference = round(view_5m.price, decimals) if view_5m else None
    if live_price is not None and reference:
        drift = abs(live_price - reference) / max(reference, 1e-12)
        if drift > 0.002:
            warnings.append(f"live price moved {drift * 100:.2f}% since the last 5m close")

    direction: str | None = None
    if view_5m and view_15m:
        trends = (view_15m.trend, view_5m.trend)
        if trends[0] == trends[1] and trends[0] in {"bullish", "bearish"}:
            direction = "CALL" if trends[0] == "bullish" else "PUT"
            gates.append(Gate("trend-agreement", True, f"15m={trends[0]} 5m={trends[1]}"))
        else:
            gates.append(Gate("trend-agreement", False, f"15m={trends[0]} 5m={trends[1]} — frames disagree"))

        if direction == "CALL":
            in_band = RSI_CALL[0] <= view_15m.rsi <= RSI_CALL[1] and RSI_CALL[0] <= view_5m.rsi <= RSI_CALL[1] + 3
            gates.append(Gate("rsi-band", in_band, f"15m RSI {view_15m.rsi}, 5m RSI {view_5m.rsi} (CALL band {RSI_CALL[0]}-{RSI_CALL[1]})"))
        elif direction == "PUT":
            in_band = RSI_PUT[0] <= view_15m.rsi <= RSI_PUT[1] and RSI_PUT[0] - 3 <= view_5m.rsi <= RSI_PUT[1]
            gates.append(Gate("rsi-band", in_band, f"15m RSI {view_15m.rsi}, 5m RSI {view_5m.rsi} (PUT band {RSI_PUT[0]}-{RSI_PUT[1]})"))
        else:
            in_band = False
            gates.append(Gate("rsi-band", False, "no direction to band — trend gate failed first"))

        if direction and in_band and candles_5m:
            trigger = _trigger_candle(candles_5m)
            body = abs(trigger.close - trigger.open)
            candle_range = trigger.high - trigger.low
            body_fraction = (body / candle_range) if candle_range > 0 else 0.0
            aligned = (trigger.close > trigger.open) if direction == "CALL" else (trigger.close < trigger.open)
            decisive = body_fraction >= MIN_BODY_FRACTION
            gates.append(Gate("trigger-candle", bool(aligned and decisive),
                              f"last closed 5m {'up' if trigger.close > trigger.open else 'down' if trigger.close < trigger.open else 'flat'}, "
                              f"body {body_fraction * 100:.0f}% of range (min {MIN_BODY_FRACTION * 100:.0f}%)"))
            atr = max(view_5m.atr14, 1e-12)
            spike = candle_range > atr * MAX_RANGE_ATR_MULTIPLE
            gates.append(Gate("no-spike", not spike,
                              f"trigger range {candle_range:g} vs ATR {view_5m.atr14:g}" + (" — spike, stand aside" if spike else "")))
            sma_ok = (trigger.close > view_5m.sma20) if direction == "CALL" else (trigger.close < view_5m.sma20)
            gates.append(Gate("price-vs-sma20", bool(sma_ok),
                              f"close {trigger.close:g} vs 5m SMA20 {view_5m.sma20:g}"))
        else:
            gates.append(Gate("trigger-candle", False, "skipped — earlier gate failed"))
            gates.append(Gate("no-spike", False, "skipped — earlier gate failed"))
            gates.append(Gate("price-vs-sma20", False, "skipped — earlier gate failed"))

    score = sum(1 for gate in gates if gate.passed)
    verdict = "WAIT"
    confidence = "low"
    expiry = None
    entry_mode = "strict" if strict else "scored"
    if direction:
        if strict:
            decisive = [gate for gate in gates if gate.name in DECISIVE_GATES]
            enter = bool(decisive) and all(gate.passed for gate in decisive)
        else:
            vetoes = [gate for gate in gates if gate.name in VETO_GATES]
            enter = bool(vetoes) and all(gate.passed for gate in vetoes)
        if enter:
            verdict = direction
            expiry = EXPIRY_MINUTES
            rsi_mid = abs((view_5m.rsi if view_5m else 50) - 50)
            confidence = "high" if rsi_mid >= 8 else "medium"

    return BinaryVerdict(
        asset_key=asset_key, asset_ar=asset_ar, asset_en=asset_en, quote=quote,
        decimals=decimals, verdict=verdict, confidence=confidence,
        expiry_minutes=expiry, reference_price=reference, live_price=live_price,
        rsi_15m=view_15m.rsi if view_15m else None,
        rsi_5m=view_5m.rsi if view_5m else None,
        trend_15m=view_15m.trend if view_15m else None,
        trend_5m=view_5m.trend if view_5m else None,
        gates=tuple(gates), source=source, as_of=as_of, warnings=tuple(warnings),
        score=score, entry_mode=entry_mode,
    )


# --------------------------------------------------------------------------- #
# Human-readable report (the "scoring" display: X/8, strength, ✅/❌, reason).
# Pure text over the computed verdict — no LLM involved.
# --------------------------------------------------------------------------- #

_TREND_AR = {"bullish": "صاعد", "bearish": "هابط", "sideways": "عرضي"}
_TREND_EN = {"bullish": "up", "bearish": "down", "sideways": "sideways"}

_WAIT_REASON_AR = {
    "data-5m": "بيانات 5m غير كافية (أقل من 50 شمعة)",
    "data-15m": "بيانات 15m غير كافية (أقل من 50 شمعة)",
    "fresh-data": "البيانات قديمة — السوق مغلق أو المزود متعطل",
    "trend-agreement": "الاتجاه غير متفق بين الفريمين (sideways = لا دخول)",
    "rsi-band": "RSI خارج النطاق الآمن (تشبع → احتمال ارتداد)",
    "trigger-candle": "لا توجد شمعة تأكيد بجسم حاسم",
    "no-spike": "شمعة سبايك (خبر متفجر) — الدخول وقتها أسوأ صفقة",
    "price-vs-sma20": "السعر في الجهة الخاطئة من SMA20",
    "indicators": "تعذر حساب المؤشرات",
}
_WAIT_REASON_EN = {
    "data-5m": "not enough 5m data (fewer than 50 candles)",
    "data-15m": "not enough 15m data (fewer than 50 candles)",
    "fresh-data": "stale data — market closed or feed down",
    "trend-agreement": "frames disagree (sideways = no entry)",
    "rsi-band": "RSI outside the safe band (overbought → snap-back risk)",
    "trigger-candle": "no decisive trigger candle",
    "no-spike": "news-spike candle — entering now is the worst trade",
    "price-vs-sma20": "price on the wrong side of SMA20",
    "indicators": "could not compute indicators",
}


def _trend_line(trend_15m: str | None, trend_5m: str | None, lang: str) -> str:
    table = _TREND_AR if lang == "ar" else _TREND_EN
    if not trend_15m and not trend_5m:
        return "غير محدد — بيانات ناقصة" if lang == "ar" else "unknown — missing data"
    if trend_15m == trend_5m:
        extra = " — متفق على الفريمين (15m+5m)" if lang == "ar" else " — frames agree (15m+5m)"
        return f"{table.get(trend_15m, trend_15m)}{extra}"
    if lang == "ar":
        return f"غير متفق: 15m {table.get(trend_15m, trend_15m)} | 5m {table.get(trend_5m, trend_5m)}"
    return f"disagree: 15m {table.get(trend_15m, trend_15m)} | 5m {table.get(trend_5m, trend_5m)}"


def _wait_reason(gates: tuple[Gate, ...], lang: str) -> str:
    table = _WAIT_REASON_AR if lang == "ar" else _WAIT_REASON_EN
    order = {name: index for index, name in enumerate(ALL_GATES)}
    failed = [gate for gate in gates if not gate.passed]
    if not failed:
        return "انتظار احترازي" if lang == "ar" else "cautious wait"
    failed.sort(key=lambda gate: order.get(gate.name, 99))
    return table.get(failed[0].name, failed[0].detail)


def _entry_reason(verdict: str, gates: tuple[Gate, ...], lang: str) -> str:
    passed = {gate.name for gate in gates if gate.passed}
    up = verdict == "CALL"
    if lang == "ar":
        parts = [
            f"اتجاه {'صاعد' if up else 'هابط'} متفق على الفريمين",
            "RSI ضمن النطاق الآمن",
            "شمعة تأكيد حاسمة" if "trigger-candle" in passed else "بلا شمعة تأكيد (تأكيد أضعف)",
            ("السعر فوق SMA20" if up else "السعر تحت SMA20") if "price-vs-sma20" in passed
            else "لكن السعر بعيد عن SMA20",
        ]
    else:
        parts = [
            f"trending {'up' if up else 'down'} on both frames",
            "RSI inside the safe band",
            "decisive trigger candle" if "trigger-candle" in passed else "no decisive trigger (weaker confirmation)",
            ("price above SMA20" if up else "price below SMA20") if "price-vs-sma20" in passed
            else "but price is away from SMA20",
        ]
    return " + ".join(parts)


def render(verdict: BinaryVerdict, lang: str = "ar") -> str:
    name = verdict.asset_ar if lang == "ar" else verdict.asset_en
    strength = strength_for(verdict.score, verdict.verdict)
    passed = [gate for gate in verdict.gates if gate.passed]
    failed = [gate for gate in verdict.gates if not gate.passed]

    if lang == "ar":
        label = {"CALL": "🟢 CALL", "PUT": "🔴 PUT", "WAIT": "⏸️ WAIT"}[verdict.verdict]
        lines = [f"📊 {name} ({verdict.asset_key}) Analysis", ""]
        lines.append(f"🎯 القرار: {label}")
        lines.append(f"📈 الاتجاه: {_trend_line(verdict.trend_15m, verdict.trend_5m, 'ar')}")
        lines.append(f"⭐ القوة: {strength}")
        lines.append(f"📊 النقاط: {verdict.score} / {TOTAL_SCORE}")
        if verdict.verdict != "WAIT":
            lines.append(f"⏱️ المدة: {verdict.expiry_minutes} دقيقة")
        lines.append("")
        lines.append("✅ الشروط الناجحة:")
        lines += [f"- {gate.name}: {gate.detail}" for gate in passed] or ["- (لا شيء)"]
        if failed:
            lines.append("❌ الشروط الفاشلة:")
            lines += [f"- {gate.name}: {gate.detail}" for gate in failed]
        lines.append("")
        reason = _wait_reason(verdict.gates, "ar") if verdict.verdict == "WAIT" else _entry_reason(verdict.verdict, verdict.gates, "ar")
        lines.append(f"🧠 السبب: {reason}")
        if verdict.reference_price is not None:
            extra = f" | لحظي {verdict.live_price}" if verdict.live_price else ""
            lines.append(f"💰 السعر المرجعي (إغلاق 5m): {verdict.reference_price} {verdict.quote}{extra}")
        if verdict.rsi_15m is not None:
            lines.append(f"📊 RSI: 15m = {verdict.rsi_15m} | 5m = {verdict.rsi_5m}")
        lines.append("")
        lines.append(f"المصدر: {verdict.source} | الوضع: {verdict.entry_mode}")
        for warning in verdict.warnings:
            lines.append(f"⚠️ {warning}")
        lines.append("⚠️ التداول الثنائي عالي الخطورة وقد تخسر كامل مبلغ الصفقة. هذه قراءة آلية وليست ضمانًا للربح — جرّب على التجريبي أولًا.")
        return "\n".join(lines)

    label = {"CALL": "🟢 CALL (up)", "PUT": "🔴 PUT (down)", "WAIT": "⏸️ WAIT"}[verdict.verdict]
    lines = [f"📊 {name} ({verdict.asset_key}) Analysis", ""]
    lines.append(f"🎯 Verdict: {label}")
    lines.append(f"📈 Trend: {_trend_line(verdict.trend_15m, verdict.trend_5m, 'en')}")
    lines.append(f"⭐ Strength: {strength}")
    lines.append(f"📊 Score: {verdict.score} / {TOTAL_SCORE}")
    if verdict.verdict != "WAIT":
        lines.append(f"⏱️ Suggested expiry: {verdict.expiry_minutes} minutes")
    lines.append("")
    lines.append("✅ Passed gates:")
    lines += [f"- {gate.name}: {gate.detail}" for gate in passed] or ["- (none)"]
    if failed:
        lines.append("❌ Failed gates:")
        lines += [f"- {gate.name}: {gate.detail}" for gate in failed]
    lines.append("")
    reason = _wait_reason(verdict.gates, "en") if verdict.verdict == "WAIT" else _entry_reason(verdict.verdict, verdict.gates, "en")
    lines.append(f"🧠 Why: {reason}")
    if verdict.reference_price is not None:
        extra = f" | live {verdict.live_price}" if verdict.live_price else ""
        lines.append(f"💰 Reference price (5m close): {verdict.reference_price} {verdict.quote}{extra}")
    if verdict.rsi_15m is not None:
        lines.append(f"📊 RSI: 15m = {verdict.rsi_15m} | 5m = {verdict.rsi_5m}")
    lines.append("")
    lines.append(f"Source: {verdict.source} | mode: {verdict.entry_mode}")
    for warning in verdict.warnings:
        lines.append(f"⚠️ {warning}")
    lines.append("⚠️ Binary trading is high-risk; you can lose the full stake. This is automated analysis, not a profit guarantee — practice on demo first.")
    return "\n".join(lines)


def to_dict(verdict: BinaryVerdict) -> dict:
    return {
        "asset": verdict.asset_key,
        "verdict": verdict.verdict,
        "confidence": verdict.confidence,
        "strength": strength_for(verdict.score, verdict.verdict),
        "score": verdict.score,
        "score_total": TOTAL_SCORE,
        "entry_mode": verdict.entry_mode,
        "expiry_minutes": verdict.expiry_minutes,
        "reference_price": verdict.reference_price,
        "live_price": verdict.live_price,
        "rsi_15m": verdict.rsi_15m,
        "rsi_5m": verdict.rsi_5m,
        "trend_15m": verdict.trend_15m,
        "trend_5m": verdict.trend_5m,
        "gates": [{"name": gate.name, "passed": gate.passed, "detail": gate.detail} for gate in verdict.gates],
        "warnings": list(verdict.warnings),
        "source": verdict.source,
        "as_of": verdict.as_of,
    }
