from __future__ import annotations

"""Short-expiry (binary-style) verdict engine: CALL / PUT / WAIT.

Binary options are decided by where the price is at expiry, minutes away, so
this engine deliberately works on the 15m context and the 5m execution frame
— not on 4H swing levels. Every gate is arithmetic on closed candles; a
single failed gate produces WAIT with the failing reason, because on these
timeframes noise dominates and most moments have no tradeable edge.

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


def _trigger_candle(candles_5m: list[Candle]) -> Candle:
    return candles_5m[-1]


def decide(asset_key: str, asset_ar: str, asset_en: str, quote: str, decimals: int,
           candles_5m: list[Candle] | None, candles_15m: list[Candle] | None,
           source: str = "unknown", live_price: float | None = None,
           now: datetime | None = None) -> BinaryVerdict:
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

    verdict = "WAIT"
    confidence = "low"
    expiry = None
    decisive_gates = [gate for gate in gates if gate.name in
                      {"fresh-data", "trend-agreement", "rsi-band", "trigger-candle", "no-spike", "price-vs-sma20"}]
    if direction and decisive_gates and all(gate.passed for gate in decisive_gates):
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
    )


def render(verdict: BinaryVerdict, lang: str = "ar") -> str:
    name = verdict.asset_ar if lang == "ar" else verdict.asset_en
    if lang == "ar":
        label = {"CALL": "🟢 صعود CALL", "PUT": "🔴 هبوط PUT", "WAIT": "⏸️ انتظار WAIT"}[verdict.verdict]
        lines = [f"{label} | {name} ({verdict.asset_key})", ""]
        if verdict.verdict != "WAIT":
            lines.append(f"⏱ انتهاء الصلاحية المقترح: {verdict.expiry_minutes} دقيقة")
            lines.append(f"🎯 الثقة: {'مرتفعة' if verdict.confidence == 'high' else 'متوسطة'}")
        else:
            failed = next((gate for gate in verdict.gates if not gate.passed), None)
            lines.append("لا توجد محفزات كافية الآن — الانتظار هو القرار المهني.")
            if failed:
                lines.append(f"⛔ سبب الانتظار: {failed.detail}")
        if verdict.reference_price is not None:
            extra = f" | لحظي {verdict.live_price}" if verdict.live_price else ""
            lines.append(f"💰 السعر المرجعي (إغلاق 5m): {verdict.reference_price} {verdict.quote}{extra}")
        if verdict.rsi_15m is not None:
            lines.append(f"📊 RSI: فريم 15m = {verdict.rsi_15m} | فريم 5m = {verdict.rsi_5m}")
        if verdict.trend_15m:
            lines.append(f"📈 الاتجاه: 15m = {verdict.trend_15m} | 5m = {verdict.trend_5m}")
        lines.append("")
        lines.append("البوابات:")
        for gate in verdict.gates:
            lines.append(f"{'✅' if gate.passed else '❌'} {gate.name}: {gate.detail}")
        for warning in verdict.warnings:
            lines.append(f"⚠️ {warning}")
        lines.append(f"المصدر: {verdict.source}")
        lines.append("⚠️ التداول الثنائي عالي الخطورة وقد تخسر كامل مبلغ الصفقة. هذه قراءة آلية وليست ضمانًا للربح — جرّب على التجريبي أولًا.")
        return "\n".join(lines)
    label = {"CALL": "🟢 CALL (up)", "PUT": "🔴 PUT (down)", "WAIT": "⏸️ WAIT"}[verdict.verdict]
    lines = [f"{label} | {name} ({verdict.asset_key})", ""]
    if verdict.verdict != "WAIT":
        lines.append(f"⏱ Suggested expiry: {verdict.expiry_minutes} minutes")
        lines.append(f"🎯 Confidence: {verdict.confidence}")
    else:
        failed = next((gate for gate in verdict.gates if not gate.passed), None)
        lines.append("No sufficient edge right now — waiting is the professional call.")
        if failed:
            lines.append(f"⛔ Wait reason: {failed.detail}")
    if verdict.reference_price is not None:
        extra = f" | live {verdict.live_price}" if verdict.live_price else ""
        lines.append(f"💰 Reference price (5m close): {verdict.reference_price} {verdict.quote}{extra}")
    if verdict.rsi_15m is not None:
        lines.append(f"📊 RSI: 15m = {verdict.rsi_15m} | 5m = {verdict.rsi_5m}")
    if verdict.trend_15m:
        lines.append(f"📈 Trend: 15m = {verdict.trend_15m} | 5m = {verdict.trend_5m}")
    lines.append("")
    lines.append("Gates:")
    for gate in verdict.gates:
        lines.append(f"{'✅' if gate.passed else '❌'} {gate.name}: {gate.detail}")
    for warning in verdict.warnings:
        lines.append(f"⚠️ {warning}")
    lines.append(f"Source: {verdict.source}")
    lines.append("⚠️ Binary trading is high-risk; you can lose the full stake. This is automated analysis, not a profit guarantee — practice on demo first.")
    return "\n".join(lines)


def to_dict(verdict: BinaryVerdict) -> dict:
    return {
        "asset": verdict.asset_key,
        "verdict": verdict.verdict,
        "confidence": verdict.confidence,
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
