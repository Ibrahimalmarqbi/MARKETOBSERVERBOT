"""Deterministic Smart-Money / Price-Action detection engine.

Nothing in this module uses a language model, randomness or synthetic prices.
Every finding is computed from the supplied OHLCV candles and carries the candle
index plus its UTC timestamp, so any statement in a report can be audited
against the exchange feed.

Detectors
---------
* Fractal swing pivots with explicit confirmation latency (``swings``).
* Market structure: HH / HL / LH / LL / EQH / EQL and the resulting bias
  (``classify_structure``).
* BOS and CHoCH from *closing* breaks only, plus wick-only stop runs reported
  separately as sweeps (``structure_events``).
* Fair value gaps with mitigation tracking (``fair_value_gaps``).
* Order blocks anchored to the displacement leg that produced the break
  (``order_blocks``), with freshness, mitigation and invalidation.
* Support/resistance clusters, equal-high/low liquidity pools, previous day and
  previous week extremes (``levels``).
* Candlestick patterns qualified by where they printed: hammer, inverted
  hammer, shooting star, doji family, bullish/bearish engulfing, morning and
  evening star, piercing line, dark cloud cover, inside/outside bars
  (``patterns``).
* Volume behaviour: relative volume, spikes, aggressive buy share (when the
  provider returns taker buy volume), OBV slope and break-candle validation
  (``volume_stats``).

``build_report`` then runs the multi-timeframe gate list (4H trend, 1H setup,
15m trigger). A plan is promoted to WATCH LONG / WATCH SHORT only when every
gate passes, including a minimum reward:risk on the first target; otherwise the
decision is WAIT and the failing gates are listed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import mean

from .market_data import Candle

SWING_LOOKBACK = 2
MIN_CANDLES = {"15m": 40, "30m": 40, "1h": 40, "4h": 60, "1d": 40}
TIMEFRAME_ORDER = {"4h": 0, "1d": 0, "1h": 1, "15m": 2, "30m": 2}
DOJI_BODY_RATIO = 0.10
HAMMER_WICK_MULTIPLE = 2.0
ENGULF_MIN_BODY_ATR = 0.5
FVG_MIN_SIZE_ATR = 0.30
FVG_MITIGATED_PCT = 60.0
SWING_TOLERANCE_PCT = 0.06
LEVEL_TOLERANCE_PCT = 0.18


# --------------------------------------------------------------------------- #
# data structures
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Swing:
    kind: str  # "high" | "low"
    index: int
    price: float
    time: str
    label: str = ""  # HH / LH / EQH / HL / LL / EQL
    confirmed_at_index: int = 0

    @property
    def is_high(self) -> bool:
        return self.kind == "high"


@dataclass(frozen=True)
class StructuralEvent:
    kind: str  # "bos" | "choch" | "sweep"
    direction: str  # "bullish" | "bearish"
    index: int
    time: str
    level: float
    broken_index: int
    wick_only: bool
    volume_ratio: float = 0.0

    @property
    def label(self) -> str:
        return "SWEEP" if self.kind == "sweep" else ("BOS" if self.kind == "bos" else "CHoCH")


@dataclass(frozen=True)
class Gap:
    kind: str  # "bullish" | "bearish"
    index: int
    time: str
    bottom: float
    top: float
    filled_pct: float
    mitigated: bool
    displacement: float

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def size(self) -> float:
        return self.top - self.bottom


@dataclass(frozen=True)
class OrderBlock:
    kind: str  # "bullish" | "bearish"
    index: int
    time: str
    bottom: float
    top: float
    fresh: bool
    mitigated: bool
    invalid: bool
    swept_liquidity: bool
    displacement: float
    volume_ratio: float
    caused_by: str = ""  # "bos" | "choch"

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0


@dataclass(frozen=True)
class Level:
    kind: str
    bottom: float
    top: float
    touches: int
    last_touch_index: int
    strength: float
    liquidity: bool = False

    @property
    def price(self) -> float:
        return (self.top + self.bottom) / 2.0


@dataclass(frozen=True)
class Pattern:
    name: str
    direction: str  # bullish | bearish | neutral
    index: int
    time: str
    body: float
    range: float
    context: str = ""  # at_demand | at_supply | at_discount | at_premium | mid_range
    note: str = ""


@dataclass(frozen=True)
class VolumeStats:
    last: float
    average20: float
    relative: float
    trend: str  # expanding | contracting | flat
    trend_change_pct: float
    spikes: tuple[tuple[int, str, float], ...]
    buy_pct: float | None
    obv_slope: float
    note: str = ""


@dataclass(frozen=True)
class Zone:
    kind: str  # order_block | fair_value_gap | level
    bottom: float
    top: float
    index: int
    time: str
    fresh: bool
    detail: str


@dataclass(frozen=True)
class TimeframeSMC:
    timeframe: str
    candles: int
    price: float
    atr: float
    swings: tuple[Swing, ...]
    events: tuple[StructuralEvent, ...]
    gaps: tuple[Gap, ...]
    blocks: tuple[OrderBlock, ...]
    levels: tuple[Level, ...]
    patterns: tuple[Pattern, ...]
    volume: VolumeStats
    range_high: float
    range_low: float
    structure: str
    bias: str
    as_of: str

    def swings_of(self, is_high: bool) -> list[Swing]:
        return [swing for swing in self.swings if swing.is_high is is_high]


@dataclass(frozen=True)
class Gate:
    key: str
    passed: bool
    detail: str
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TradePlan:
    side: str  # long | short | none
    zone: Zone | None
    stop: float
    target_one: float
    target_two: float
    risk: float
    reward_one: float
    risk_reward: float
    confidence: str
    gates: tuple[Gate, ...]
    decision: str  # watch_long | watch_short | wait
    stop_rule: str = ""  # "below" | "above"
    stop_timeframe: str = ""  # whose close invalidates the plan

    @property
    def entry_low(self) -> float:
        return self.zone.bottom if self.zone else 0.0

    @property
    def entry_high(self) -> float:
        return self.zone.top if self.zone else 0.0


@dataclass(frozen=True)
class SmartMoneyReport:
    asset_key: str
    asset_name_ar: str
    asset_name_en: str
    quote: str
    decimals: int
    price: float
    source: str
    as_of: str
    views: tuple[TimeframeSMC, ...]
    trend: str
    structure_text: str
    price_position: str
    position_pct: float
    premium_discount: str
    plan: TradePlan
    flags: tuple[str, ...] = ()
    liquidity_above: tuple = ()
    liquidity_below: tuple = ()
    extra_spikes: tuple = ()
    live_price: float = 0.0
    live_note: str = ""
    watch_condition: dict = field(default_factory=dict)

    def view(self, timeframe: str) -> TimeframeSMC | None:
        for item in self.views:
            if item.timeframe == timeframe:
                return item
        return None

    @property
    def highest(self) -> TimeframeSMC:
        return self.views[0]

    @property
    def failing_gates(self) -> tuple[Gate, ...]:
        return tuple(gate for gate in self.plan.gates if not gate.passed)

    @property
    def passed_gates(self) -> tuple[Gate, ...]:
        return tuple(gate for gate in self.plan.gates if gate.passed)


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #
def stamp(candle: Candle) -> str:
    value = candle.timestamp
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _shape(candle: Candle) -> tuple[float, float, float, float, float]:
    body = abs(candle.close - candle.open)
    span = candle.high - candle.low
    upper = candle.high - max(candle.open, candle.close)
    lower = min(candle.open, candle.close) - candle.low
    return body, span, upper, lower, (1.0 if candle.close >= candle.open else -1.0)


def atr_at(candles: list[Candle], period: int = 14) -> float:
    """Wilder ATR, used to derive every tolerance in this module."""
    if len(candles) < 2:
        return 0.0
    ranges = []
    for index, candle in enumerate(candles):
        previous = candles[index - 1].close if index else candle.close
        ranges.append(max(candle.high - candle.low, abs(candle.high - previous), abs(candle.low - previous)))
    if len(ranges) <= period:
        return round(sum(ranges) / len(ranges), 8)
    value = sum(ranges[1:period + 1]) / period
    for item in ranges[period + 1:]:
        value = ((value * (period - 1)) + item) / period
    return round(value, 8)


def _mean_volume(candles: list[Candle]) -> float:
    volumes = [candle.volume for candle in candles]
    if not volumes:
        return 0.0
    return mean(volumes[-20:]) if len(volumes) >= 20 else mean(volumes)


# --------------------------------------------------------------------------- #
# swings + structure
# --------------------------------------------------------------------------- #
def swings(candles: list[Candle], lookback: int = SWING_LOOKBACK) -> list[Swing]:
    """Fractal pivots: candle *i* is a swing high when its high is >= the highs
    of the `lookback` candles on both sides (strict on the right so a flat top is
    not counted twice). A pivot only becomes usable after `lookback` candles
    closed after it, recorded in `confirmed_at_index`, so the engine never
    pretends a level was known before it actually was."""
    if len(candles) < lookback * 2 + 1:
        return []
    found: list[Swing] = []
    for index in range(lookback, len(candles) - lookback):
        left = candles[index - lookback:index]
        right = candles[index + 1:index + 1 + lookback]
        high, low = candles[index].high, candles[index].low
        if high >= max(candle.high for candle in left) and high > max(candle.high for candle in right):
            found.append(Swing("high", index, round(high, 8), stamp(candles[index]), "", index + lookback))
        elif low <= min(candle.low for candle in left) and low < min(candle.low for candle in right):
            found.append(Swing("low", index, round(low, 8), stamp(candles[index]), "", index + lookback))
    return label_swings(found)


def label_swings(found: list[Swing], tolerance_pct: float = SWING_TOLERANCE_PCT) -> list[Swing]:
    """Compare each pivot with the previous pivot on the same side."""
    last_high: Swing | None = None
    last_low: Swing | None = None
    result: list[Swing] = []
    for swing in found:
        label = ""
        reference = last_high if swing.is_high else last_low
        if reference is not None and reference.price > 0:
            diff = (swing.price - reference.price) / reference.price * 100
            if abs(diff) <= tolerance_pct:
                label = "EQH" if swing.is_high else "EQL"
            elif swing.is_high:
                label = "HH" if diff > 0 else "LH"
            else:
                label = "HL" if diff > 0 else "LL"
        if swing.is_high:
            if label != "EQH":
                last_high = swing
        elif label != "EQL":
            last_low = swing
        result.append(Swing(swing.kind, swing.index, swing.price, swing.time, label, swing.confirmed_at_index))
    return result


def classify_structure(found: list[Swing]) -> tuple[str, str]:
    """Return (bias, description). Bullish requires both a higher high and a
    higher low among the latest pivots with no fresh lower low; bearish is the
    mirror. Anything else is not a trend and must not be traded."""
    highs = [swing for swing in found if swing.is_high][-3:]
    lows = [swing for swing in found if not swing.is_high][-3:]
    if not highs or not lows:
        return "unknown", "not enough confirmed pivots to define structure"
    high_labels = [swing.label for swing in highs]
    low_labels = [swing.label for swing in lows]
    bull = ("HH" in high_labels or "EQH" in high_labels) and "HL" in low_labels and "LL" not in low_labels[-2:]
    bear = ("LL" in low_labels or "EQL" in low_labels) and "LH" in high_labels and "HH" not in high_labels[-2:]
    if bull and not bear:
        bias = "bullish"
    elif bear and not bull:
        bias = "bearish"
    else:
        bias = "sideways"
    ordered = sorted(highs + lows, key=lambda swing: swing.index)
    sequence = " -> ".join(f"{swing.label or ('H' if swing.is_high else 'L')}@{swing.price:g}" for swing in ordered[-4:])
    description = (f"{sequence}; last swing high {highs[-1].price:g} ({highs[-1].label or 'n/a'}), "
                   f"last swing low {lows[-1].price:g} ({lows[-1].label or 'n/a'})")
    return bias, description


def structure_events(candles: list[Candle], found: list[Swing], atr: float) -> list[StructuralEvent]:
    """BOS / CHoCH / sweep detection.

    Fixed rules, no discretion:
    * a break counts only when a *closed* candle body closes beyond the active
      swing point; a wick through it with the body back inside is a liquidity
      sweep (stop run), never a break;
    * the first close-break against the prevailing bias is a CHoCH, a break with
      the bias is a BOS continuation;
    * only pivots confirmed before the current candle are eligible, which
      reproduces what was knowable in real time;
    * after a break that side's reference is released, so the same level cannot
      be reported as broken over and over.
    """
    if not found:
        return []
    average_volume = _mean_volume(candles)
    events: list[StructuralEvent] = []
    bias = "neutral"
    active_high: Swing | None = None
    active_low: Swing | None = None
    queue: list[Swing] = []
    cursor = 0
    tolerance = max(atr * 0.05, 1e-9)
    for index, candle in enumerate(candles):
        while cursor < len(found) and found[cursor].index <= index - SWING_LOOKBACK:
            queue.append(found[cursor])
            cursor += 1
        if queue:
            for swing in queue:
                if swing.is_high:
                    active_high = swing
                else:
                    active_low = swing
            queue = []
        if active_high is None or active_low is None:
            continue
        ratio = round(candle.volume / average_volume, 3) if average_volume else 0.0
        if candle.close > active_high.price:
            kind = "choch" if bias == "bearish" else "bos"
            events.append(StructuralEvent(kind, "bullish", index, stamp(candle), round(active_high.price, 8), active_high.index, False, ratio))
            bias, active_high = "bullish", None
        elif candle.close < active_low.price:
            kind = "choch" if bias == "bullish" else "bos"
            events.append(StructuralEvent(kind, "bearish", index, stamp(candle), round(active_low.price, 8), active_low.index, False, ratio))
            bias, active_low = "bearish", None
        elif candle.high > active_high.price + tolerance and candle.close <= active_high.price:
            events.append(StructuralEvent("sweep", "bearish", index, stamp(candle), round(active_high.price, 8), active_high.index, True, ratio))
            active_high = None
        elif candle.low < active_low.price - tolerance and candle.close >= active_low.price:
            events.append(StructuralEvent("sweep", "bullish", index, stamp(candle), round(active_low.price, 8), active_low.index, True, ratio))
            active_low = None
    return events


# --------------------------------------------------------------------------- #
# imbalance + order blocks
# --------------------------------------------------------------------------- #
def fair_value_gaps(candles: list[Candle], atr: float, min_size_atr: float = FVG_MIN_SIZE_ATR, limit: int = 10) -> list[Gap]:
    """Three-candle imbalance. Bullish gap: low[i] - high[i-2]. Bearish gap:
    low[i-2] - high[i]. Only gaps at least `min_size_atr` ATR wide count, so
    micro-gaps inside noise are ignored. Filled percentage measures how far a
    later candle traded back through the gap; >= 60% counts as mitigated."""
    threshold = max(atr * min_size_atr, 1e-9)
    average_volume = _mean_volume(candles)
    gaps: list[Gap] = []
    for index in range(2, len(candles)):
        first, third = candles[index - 2], candles[index]
        bull_size = third.low - first.high
        bear_size = first.low - third.high
        if bull_size >= threshold and bull_size >= bear_size:
            kind, bottom, top = "bullish", first.high, third.low
        elif bear_size >= threshold:
            kind, bottom, top = "bearish", third.high, first.low
        else:
            continue
        span = max(top - bottom, 1e-9)
        filled = 0.0
        for later in candles[index + 1:]:
            if kind == "bullish":
                if later.low <= bottom:
                    filled = 100.0
                    break
                filled = max(filled, (top - later.low) / span * 100)
            else:
                if later.high >= top:
                    filled = 100.0
                    break
                filled = max(filled, (later.high - bottom) / span * 100)
        displacement = abs(third.close - first.open) / atr if atr else 0.0
        volume_ratio = third.volume / average_volume if average_volume else 1.0
        gaps.append(Gap(kind, index, stamp(third), round(bottom, 8), round(top, 8), round(min(max(filled, 0.0), 100.0), 1),
                        filled >= FVG_MITIGATED_PCT, round(displacement * min(volume_ratio, 3.0), 2)))
    usable = [gap for gap in gaps if not gap.mitigated] or gaps
    return sorted(usable, key=lambda gap: gap.index, reverse=True)[:limit]


def order_blocks(candles: list[Candle], events: list[StructuralEvent], atr: float, limit: int = 6) -> list[OrderBlock]:
    """For each body break, the last opposing candle before the displacement leg
    is the order block. A bullish block is the final down candle underneath the
    break upward (its low to high, extended to the break candle's high so the
    origin of the move is included). It stays valid until price closes through
    it and is only a fresh point of interest while nothing traded back into it."""
    average_volume = _mean_volume(candles)
    break_events = [event for event in events if event.kind in {"bos", "choch"}]
    blocks: list[OrderBlock] = []
    for event in break_events:
        bullish = event.direction == "bullish"
        origin = None
        for index in range(event.index, max(event.index - 14, -1), -1):
            candle = candles[index]
            opposing = (candle.close < candle.open) if bullish else (candle.close > candle.open)
            if opposing:
                origin = index
                break
        if origin is None:
            continue
        anchor = candles[origin]
        top = max(anchor.high, event.level) if bullish else anchor.high
        bottom = anchor.low if bullish else min(anchor.low, event.level)
        displacement = sum(abs(candles[index].close - candles[index].open) for index in range(origin + 1, event.index + 1))
        mitigated = invalid = False
        for later in candles[origin + 1:]:
            if bullish:
                invalid = invalid or later.close < bottom
                mitigated = mitigated or later.low <= top
            else:
                invalid = invalid or later.close > top
                mitigated = mitigated or later.high >= bottom
        swept = any(other.index <= origin and other.kind == "sweep" and other.direction == event.direction for other in events)
        blocks.append(OrderBlock(
            "bullish" if bullish else "bearish", origin, stamp(anchor), round(bottom, 8), round(top, 8),
            not mitigated and not invalid, mitigated, invalid, swept,
            round(displacement / atr, 2) if atr else 0.0,
            round(anchor.volume / average_volume, 2) if average_volume else 0.0,
            event.kind,
        ))
    usable = [block for block in blocks if not block.invalid]
    return sorted(usable, key=lambda block: block.index, reverse=True)[:limit]


# --------------------------------------------------------------------------- #
# levels + liquidity
# --------------------------------------------------------------------------- #
def levels(candles: list[Candle], found: list[Swing], atr: float, tolerance_pct: float = LEVEL_TOLERANCE_PCT) -> list[Level]:
    """Cluster pivots into zones, then add the resting-liquidity references
    traders actually leave orders at: equal highs/lows and previous day/week
    extremes."""
    if not candles:
        return []
    price = candles[-1].close
    tolerance = max(price * tolerance_pct / 100.0, atr * 0.4, 1e-9)
    clusters: list[dict] = []
    for swing in sorted(found, key=lambda item: item.price):
        home = None
        for cluster in clusters:
            if abs(swing.price - cluster["total"] / cluster["count"]) <= tolerance:
                home = cluster
                break
        if home is None:
            home = {"total": 0.0, "count": 0, "index": 0, "labels": set()}
            clusters.append(home)
        home["total"] += swing.price
        home["count"] += 1
        home["index"] = max(home["index"], swing.index)
        home["labels"].add(swing.label)
    result: list[Level] = []
    for cluster in clusters:
        average = cluster["total"] / cluster["count"]
        half = max(tolerance * 0.5, atr * 0.2 if atr else 0.0, average * 0.0004)
        labels = cluster["labels"]
        if "EQH" in labels:
            kind = "equal_highs"
        elif "EQL" in labels:
            kind = "equal_lows"
        else:
            kind = "resistance" if average >= price else "support"
        recency = 1.0 - (len(candles) - 1 - cluster["index"]) / max(len(candles), 1)
        strength = round(cluster["count"] + (2.0 if kind.startswith("equal") else 0.0) + recency * 2.0, 2)
        result.append(Level(kind, round(average - half, 8), round(average + half, 8), cluster["count"], cluster["index"], strength, True))
    by_day: dict[tuple[int, int], list[Candle]] = {}
    for candle in candles:
        value = candle.timestamp if candle.timestamp.tzinfo else candle.timestamp.replace(tzinfo=timezone.utc)
        by_day.setdefault((value.year, value.timetuple().tm_yday), []).append(candle)
    ordered_days = sorted(by_day)
    if len(ordered_days) >= 2:
        previous = by_day[ordered_days[-2]]
        high = round(max(candle.high for candle in previous), 8)
        low = round(min(candle.low for candle in previous), 8)
        result.append(Level("prev_day_high", high, high, 1, len(candles) - 1, 2.6, True))
        result.append(Level("prev_day_low", low, low, 1, len(candles) - 1, 2.6, True))
    if len(ordered_days) >= 4:
        week = [candle for day in ordered_days[-5:] for candle in by_day[day]]
        high = round(max(candle.high for candle in week), 8)
        low = round(min(candle.low for candle in week), 8)
        result.append(Level("week_high", high, high, 1, len(candles) - 1, 3.1, True))
        result.append(Level("week_low", low, low, 1, len(candles) - 1, 3.1, True))
    deduped: list[Level] = []
    for level in sorted(result, key=lambda item: item.strength, reverse=True):
        if any(abs(level.price - other.price) <= tolerance for other in deduped):
            continue
        deduped.append(level)
    return sorted(deduped, key=lambda item: abs(item.price - price))


def liquidity_pools(view: TimeframeSMC) -> list[Level]:
    return [level for level in view.levels if level.kind in {"equal_highs", "equal_lows", "prev_day_high", "prev_day_low", "week_high", "week_low"}]


# --------------------------------------------------------------------------- #
# candlestick patterns
# --------------------------------------------------------------------------- #
def patterns(candles: list[Candle], atr: float, lookback: int = 24) -> list[Pattern]:
    """Strict OHLC definitions evaluated on closed candles. A pattern is only
    reported when the candle is meaningful relative to ATR, otherwise it is
    noise; ``analyze_timeframe`` attaches where it printed."""
    out: list[Pattern] = []
    start = max(2, len(candles) - lookback)
    for index in range(start, len(candles)):
        candle, previous = candles[index], candles[index - 1]
        body, span, upper, lower, sign = _shape(candle)
        pbody, pspan, _, _, psign = _shape(previous)
        if span <= 0 or pspan <= 0:
            continue
        notable = (span >= atr * 0.55) if atr else True
        big_body = (body >= atr * ENGULF_MIN_BODY_ATR) if atr else True
        found: tuple[str, str, str] | None = None
        # a near-zero body is a doji family print and wins over the wick rules,
        # otherwise a dragonfly would be renamed "hammer" for the same geometry
        if body <= span * DOJI_BODY_RATIO:
            if lower >= span * 0.6:
                found = ("dragonfly_doji", "bullish", "indecision that defended the low")
            elif upper >= span * 0.6:
                found = ("gravestone_doji", "bearish", "indecision that rejected the high")
            else:
                found = ("doji", "neutral", "open and close almost equal: balance between offer and demand")
        # single-candle rejections
        elif notable and lower >= body * HAMMER_WICK_MULTIPLE and upper <= max(body, span * 0.25):
            found = ("hammer", "bullish", "long lower wick: supply was tested and absorbed inside the same candle")
        elif notable and upper >= body * HAMMER_WICK_MULTIPLE and lower <= max(body, span * 0.25):
            if psign < 0:
                found = ("inverted_hammer", "bullish", "long upper wick after a decline: supply absorbed by buyers")
            else:
                found = ("shooting_star", "bearish", "upper rejection after an advance: buyers could not hold the high")
        # three-candle reversals; needs a real third candle back (no wrap-around)
        if found is None and index >= 2:
            first, second = candles[index - 2], candles[index - 1]
            fbody, _, _, _, fsign = _shape(first)
            sbody, _, _, _, _ = _shape(second)
            if (atr and fbody >= atr * 0.6 and fsign < 0 and sbody <= fbody * 0.45 and sign > 0 and body >= fbody * 0.55
                    and candle.close >= (first.open + first.close) / 2 and second.low < first.close):
                found = ("morning_star", "bullish", "selloff, stall, reclaim above the midpoint of the first candle")
            elif (atr and fbody >= atr * 0.6 and fsign > 0 and sbody <= fbody * 0.45 and sign < 0 and body >= fbody * 0.55
                  and candle.close <= (first.open + first.close) / 2 and second.high > first.close):
                found = ("evening_star", "bearish", "rally, stall, failure below the midpoint of the first candle")
        # two-candle reversals
        if found is None and psign < 0 and sign > 0 and candle.close >= previous.open and candle.open <= previous.close and body > pbody and big_body:
            found = ("engulfing_bullish", "bullish", "body closed over the previous bearish body: liquidity transferred to buyers")
        elif found is None and psign > 0 and sign < 0 and candle.close <= previous.open and candle.open >= previous.close and body > pbody and big_body:
            found = ("engulfing_bearish", "bearish", "body closed under the previous bullish body: liquidity transferred to sellers")
        elif (found is None and psign < 0 and sign > 0 and pbody >= (atr * 0.6 if atr else pbody) and big_body
              and previous.close < previous.open and candle.open < previous.low and (previous.open + previous.close) / 2 < candle.close < previous.open):
            found = ("piercing_line", "bullish", "closes above the midpoint of the bearish candle but under its open")
        elif (found is None and psign > 0 and sign < 0 and pbody >= (atr * 0.6 if atr else pbody) and big_body
              and candle.open > previous.high and (previous.open + previous.close) / 2 > candle.close > previous.open):
            found = ("dark_cloud_cover", "bearish", "closes below the midpoint of the bullish candle but above its open")
        # relative size structure
        if found is None and candle.high <= previous.high and candle.low >= previous.low and span < pspan:
            found = ("inside_bar", "neutral", "compression: resting liquidity on both sides of the range")
        elif found is None and candle.high > previous.high and candle.low < previous.low and span > pspan:
            found = ("outside_bar", "neutral", "two-way stop run: the next close decides the direction")
        if found:
            name, direction, note = found
            out.append(Pattern(name, direction, index, stamp(candle), round(body, 8), round(span, 8), "", note))
    return out


# --------------------------------------------------------------------------- #
# volume
# --------------------------------------------------------------------------- #
def volume_stats(candles: list[Candle], lookback: int = 40) -> VolumeStats:
    """Relative volume, expansion/contraction, spike detection, aggressive buy
    share (taker buy base volume when the provider supplies it) and an OBV
    slope. Descriptive only: no volume number is ever invented."""
    volumes = [candle.volume for candle in candles]
    if not volumes:
        return VolumeStats(0.0, 0.0, 1.0, "flat", 0.0, (), None, 0.0, "no volume data returned by the provider")
    base = mean(volumes[-21:-1]) if len(volumes) >= 21 else mean(volumes)
    relative = round(volumes[-1] / base, 2) if base else 1.0
    window = volumes[-lookback:] if len(volumes) >= lookback else volumes
    half = max(len(window) // 2, 1)
    recent, earlier = mean(window[-half:]), mean(window[:half]) if len(window) >= 2 else mean(window)
    change = (recent - earlier) / earlier * 100 if earlier else 0.0
    trend = "expanding" if change > 12 else "contracting" if change < -12 else "flat"
    spikes: list[tuple[int, str, float]] = []
    for index in range(max(1, len(candles) - 20), len(candles)):
        reference = volumes[max(0, index - 20):index]
        if not reference:
            continue
        ratio = volumes[index] / mean(reference)
        if ratio >= 2.2:
            spikes.append((index, stamp(candles[index]), round(ratio, 2)))
    buy_pct: float | None = None
    tail = candles[-len(window):]
    buys = [getattr(candle, "buy_volume", None) for candle in tail]
    if buys and all(value is not None for value in buys):
        total = sum(candle.volume for candle in tail)
        if total > 0:
            buy_pct = round(sum(buys) / total * 100, 1)
    obv = 0.0
    series: list[float] = []
    for index in range(1, len(candles)):
        direction = 1 if candles[index].close > candles[index - 1].close else -1 if candles[index].close < candles[index - 1].close else 0
        obv += direction * volumes[index]
        series.append(obv)
    slope = 0.0
    if len(series) >= 10:
        head = series[:len(series) // 2]
        tail_obv = series[len(series) // 2:]
        # scaled by the volume traded in the window so the number stays
        # interpretable instead of exploding when the OBV base is near zero
        denominator = sum(volumes[-len(series):]) or 1.0
        slope = round((mean(tail_obv) - mean(head)) / denominator * 100, 3)
    note = ""
    if buy_pct is not None:
        side = "aggressive buying dominates" if buy_pct >= 53 else "aggressive selling dominates" if buy_pct <= 47 else "taker flow balanced"
        note = f"aggressive buys {buy_pct}% of taker volume: {side}"
    return VolumeStats(round(volumes[-1], 6), round(base, 6), relative, trend, round(change, 1), tuple(spikes[-3:]), buy_pct, slope, note)


# --------------------------------------------------------------------------- #
# timeframe analysis
# --------------------------------------------------------------------------- #
INTERVAL_MINUTES = {"15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def drop_forming(candles: list[Candle], timeframe: str, now: datetime | None = None) -> list[Candle]:
    """Remove the in-progress candle. Exchanges return the current, still-open bar
    as the last row; a pattern or a break measured on it is not a fact yet. All
    structure, pattern and volume reads therefore use closed bars, and the live
    price is reported separately."""
    minutes = INTERVAL_MINUTES.get(timeframe)
    items = list(candles)
    if not minutes or not items:
        return items
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    while items:
        opened = items[-1].timestamp
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        if opened + timedelta(minutes=minutes) <= now:
            break
        items.pop()
    return items


def analyze_timeframe(candles: list[Candle], timeframe: str, decimals: int = 2) -> TimeframeSMC:
    candles = list(candles)
    required = MIN_CANDLES.get(timeframe, 50)
    if len(candles) < required:
        raise ValueError(f"{timeframe}: at least {required} candles are required, got {len(candles)}")
    atr = atr_at(candles)
    pivots = swings(candles)
    bias, description = classify_structure(pivots)
    events = structure_events(candles, pivots, atr)
    gaps = fair_value_gaps(candles, atr)
    blocks = order_blocks(candles, events, atr)
    zones = levels(candles, pivots, atr)
    prints = patterns(candles, atr)
    price = candles[-1].close
    range_high = max(candle.high for candle in candles)
    range_low = min(candle.low for candle in candles)
    contextual: list[Pattern] = []
    for print_ in prints:
        value = candles[print_.index].close
        band = max(atr * 1.2, value * 0.0015)
        at_demand = any(level.kind in {"support", "equal_lows", "prev_day_low", "week_low"} and abs(level.price - value) <= band for level in zones)
        at_supply = any(level.kind in {"resistance", "equal_highs", "prev_day_high", "week_high"} and abs(level.price - value) <= band for level in zones)
        position = (value - range_low) / (range_high - range_low) if range_high > range_low else 0.5
        context = ("at_demand" if at_demand and not at_supply else "at_supply" if at_supply and not at_demand
                   else "at_discount" if position <= 0.35 else "at_premium" if position >= 0.65 else "mid_range")
        contextual.append(Pattern(print_.name, print_.direction, print_.index, print_.time, print_.body, print_.range, context, print_.note))
    return TimeframeSMC(
        timeframe, len(candles), round(price, decimals), round(atr, decimals), tuple(pivots[-16:]), tuple(events[-10:]),
        gaps, blocks, tuple(zones), tuple(contextual[-8:]), volume_stats(candles), round(range_high, decimals),
        round(range_low, decimals), description, bias, stamp(candles[-1]),
    )


# --------------------------------------------------------------------------- #
# points of interest
# --------------------------------------------------------------------------- #
def best_zone(view: TimeframeSMC | None, price: float, side: str, decimals: int) -> Zone | None:
    """Choose the point of interest for the intended side. The preference order is
    deliberately SMC-correct: fresh unmitigated order block, then an unmitigated
    fair value gap, then a clustered level. Zones already left behind the current
    price are skipped because they cannot be entered."""
    if view is None or side not in {"long", "short"}:
        return None
    bullish = side == "long"
    options: list[tuple[int, float, Zone]] = []

    def gap_to_entry(bottom: float, top: float) -> float:
        anchor = top if bullish else bottom
        return abs(price - anchor)

    for block in view.blocks:
        wanted = "bullish" if bullish else "bearish"
        if block.kind != wanted:
            continue
        if bullish and block.top < price * 0.985:
            continue
        if not bullish and block.bottom > price * 1.015:
            continue
        rank = 0 if block.fresh else 2
        rank -= 1 if block.swept_liquidity else 0
        detail = (f"order block {block.bottom:.{decimals}f}-{block.top:.{decimals}f} @ {block.time} | displacement {block.displacement} ATR "
                  f"| volume {block.volume_ratio}x | {'fresh' if block.fresh else 'mitigated'}"
                  + (" | formed after a liquidity sweep" if block.swept_liquidity else "") + f" | after {block.caused_by.upper()}")
        options.append((rank, gap_to_entry(block.bottom, block.top),
                        Zone("order_block", round(block.bottom, decimals), round(block.top, decimals), block.index, block.time, block.fresh, detail)))
    for gap in view.gaps:
        wanted = "bullish" if bullish else "bearish"
        if gap.kind != wanted or gap.mitigated:
            continue
        if bullish and gap.top < price * 0.99:
            continue
        if not bullish and gap.bottom > price * 1.01:
            continue
        detail = f"fair value gap {gap.bottom:.{decimals}f}-{gap.top:.{decimals}f} @ {gap.time} | filled {gap.filled_pct}% | displacement {gap.displacement}"
        options.append((1, gap_to_entry(gap.bottom, gap.top),
                        Zone("fair_value_gap", round(gap.bottom, decimals), round(gap.top, decimals), gap.index, gap.time, gap.filled_pct < 25, detail)))
    for level in view.levels:
        if bullish and level.kind in {"support", "equal_lows", "prev_day_low"} and level.price <= price:
            detail = f"{level.kind} cluster {level.bottom:.{decimals}f}-{level.top:.{decimals}f} | {level.touches} touch(es)"
        elif not bullish and level.kind in {"resistance", "equal_highs", "prev_day_high"} and level.price >= price:
            detail = f"{level.kind} cluster {level.bottom}-{level.top} | {level.touches} touch(es)"
        else:
            continue
        options.append((3, gap_to_entry(level.bottom, level.top),
                        Zone("level", round(level.bottom, decimals), round(level.top, decimals), level.last_touch_index, "", False, detail)))
    if not options:
        return None
    options.sort(key=lambda item: (item[0], item[1]))
    return options[0][2]


# --------------------------------------------------------------------------- #
# multi-timeframe report
# --------------------------------------------------------------------------- #
def build_report(
    asset_key: str,
    asset_name_ar: str,
    asset_name_en: str,
    quote: str,
    decimals: int,
    candles_by_timeframe: dict[str, list[Candle]],
    source: str = "",
    min_risk_reward: float = 2.0,
    now: datetime | None = None,
) -> SmartMoneyReport:
    views: list[TimeframeSMC] = []
    live_price = 0.0
    live_note = ""
    for timeframe in ("4h", "1d", "1h", "30m", "15m"):
        candles = candles_by_timeframe.get(timeframe)
        if not candles:
            continue
        closed = drop_forming(candles, timeframe, now)
        if len(closed) < len(candles):
            live_price = candles[-1].close
            live_note = f"the last {timeframe} bar ({stamp(candles[-1])}) is still open and was excluded from structure, patterns and volume"
        if not closed:
            continue
        try:
            views.append(analyze_timeframe(closed, timeframe, decimals))
        except ValueError:
            continue
    if not views:
        raise ValueError("no timeframe could be analysed from the supplied data")
    views.sort(key=lambda view: (TIMEFRAME_ORDER.get(view.timeframe, 5), -len(view.timeframe)))
    highest = views[0]
    setup = next((view for view in views if view.timeframe == "1h"), None)
    trigger = next((view for view in views if view.timeframe in {"15m", "30m"}), None) or setup or highest
    price = highest.price
    range_high, range_low = highest.range_high, highest.range_low
    position = (price - range_low) / (range_high - range_low) if range_high > range_low else 0.5
    premium_discount = "premium" if position >= 0.6 else "discount" if position <= 0.4 else "equilibrium"
    trend = highest.bias
    trend_note = ""
    if trend == "sideways" and highest.events:
        last_highest = highest.events[-1]
        if last_highest.kind == "choch" and last_highest.index >= highest.candles - 12:
            # The character of the 4H changed but the ladder has not been rebuilt
            # yet. Treated as an *early* trend: tradeable direction is allowed,
            # confidence stays capped and every other gate must still pass.
            trend = f"{last_highest.direction}_early"
            trend_note = f"early: fresh 4H CHoCH {last_highest.direction} at {last_highest.time}"
    side = {"bullish": "long", "bearish": "short", "bullish_early": "long", "bearish_early": "short"}.get(trend, "none")
    gates: list[Gate] = []

    gates.append(Gate("htf_trend", side != "none", f"4H structure bias: {trend} | {highest.structure}" + (f" | {trend_note}" if trend_note else ""),
                      {"trend": trend, "timeframe": highest.timeframe, "note": trend_note,
                       "sequence": [(swing.label, swing.price) for swing in sorted(highest.swings, key=lambda item: item.index)[-4:]]}))

    directional_events = list(trigger.events) if side != "none" else []
    wanted = "bullish" if side in ("long", "bullish_early") else "bearish"
    aligned = [event for event in directional_events if event.kind in {"bos", "choch"} and event.direction == wanted]
    opposed = [event for event in directional_events if event.kind in {"bos", "choch"} and event.direction != wanted]
    sweeps = [event for event in directional_events if event.kind == "sweep" and event.direction == wanted]
    latest_aligned = aligned[-1] if aligned else None
    stale_opposed = bool(opposed) and (latest_aligned is None or opposed[-1].index > latest_aligned.index)
    confirmation = bool(latest_aligned) and not stale_opposed and side != "none"
    confirmation_detail = (
        f"{trigger.timeframe} {latest_aligned.label} {latest_aligned.direction} at {latest_aligned.time} breaking {latest_aligned.level}"
        if latest_aligned else f"no aligned BOS/CHoCH close on {trigger.timeframe}")
    if stale_opposed:
        confirmation_detail += f" | superseded by an opposing {opposed[-1].label}"
    gates.append(Gate("confirmation", confirmation, confirmation_detail,
                      {"timeframe": trigger.timeframe, "kind": latest_aligned.kind if latest_aligned else "none",
                       "direction": latest_aligned.direction if latest_aligned else "none",
                       "time": latest_aligned.time if latest_aligned else "",
                       "level": latest_aligned.level if latest_aligned else 0.0,
                       "wick_only": bool(latest_aligned and latest_aligned.wick_only), "stale": stale_opposed}))
    if sweeps:
        gates.append(Gate("liquidity_sweep", True, f"{trigger.timeframe} swept {sweeps[-1].level} at {sweeps[-1].time} (wick only) before the break",
                          {"timeframe": trigger.timeframe, "level": sweeps[-1].level, "time": sweeps[-1].time}))

    zone_view = setup or highest
    zone = best_zone(zone_view, price, side, decimals) if side != "none" else None
    tolerance = max(zone_view.atr * 1.5, price * 0.003)
    anchor = (zone.top if side == "long" else zone.bottom) if zone else price
    distance = abs(price - anchor)
    at_zone = zone is not None and distance <= tolerance
    gates.append(Gate("price_at_zone", at_zone,
                      f"distance to {zone.kind if zone else 'zone'} = {round(distance, decimals)} vs tolerance {round(tolerance, decimals)}",
                      {"kind": zone.kind if zone else "none", "distance": round(distance, decimals),
                       "tolerance": round(tolerance, decimals), "bottom": zone.bottom if zone else 0.0, "top": zone.top if zone else 0.0}))

    if zone is None:
        no_chase, chase_detail = False, "no valid zone to enter, so there is nothing to time yet"
    else:
        no_chase = (price <= zone.top + tolerance * 0.5) if side == "long" else (price >= zone.bottom - tolerance * 0.5)
        chase_detail = ("price has not left the zone" if no_chase
                        else "price already left the zone: entering now is chasing")
    gates.append(Gate("not_early", no_chase, chase_detail,
                      {"price": price, "side": side, "zone_bottom": zone.bottom if zone else 0.0, "zone_top": zone.top if zone else 0.0}))

    wanted_direction = "bullish" if side == "long" else "bearish"
    rejection = [print_ for print_ in trigger.patterns
                 if print_.direction == wanted_direction and print_.name not in {"inside_bar", "outside_bar", "doji"}]
    gates.append(Gate("candle_confirm", bool(rejection),
                      f"{len(rejection)} aligned reversal print(s) on {trigger.timeframe}"
                      + (f": {rejection[-1].name} at {rejection[-1].time} ({rejection[-1].context})" if rejection else ""),
                      {"count": len(rejection), "name": rejection[-1].name if rejection else "",
                       "time": rejection[-1].time if rejection else "", "context": rejection[-1].context if rejection else "",
                       "timeframe": trigger.timeframe}))

    break_ratio = latest_aligned.volume_ratio if latest_aligned else 0.0
    volume_ok = trigger.volume.relative >= 1.1 or break_ratio >= 1.25
    gates.append(Gate("volume", volume_ok,
                      f"RVOL {trigger.volume.relative:.2f}x, break candle {break_ratio:.2f}x, volume {trigger.volume.trend} ({trigger.volume.trend_change_pct:+.0f}%)",
                      {"relative": trigger.volume.relative, "break": break_ratio, "trend": trigger.volume.trend,
                       "change": trigger.volume.trend_change_pct, "buy_pct": trigger.volume.buy_pct, "timeframe": trigger.timeframe}))

    risk = reward = ratio = 0.0
    stop = target_one = target_two = 0.0
    no_target_note = ""
    if side != "none" and zone is not None:
        buffer_ = max(highest.atr * 0.2, price * 0.0012)
        pivots = (setup or highest).swings
        if side == "long":
            entry = zone.top
            # the pivot that actually invalidates the idea is the last confirmed
            # swing low under the entry, not the extreme of the whole window
            guards = [swing.price for swing in pivots if not swing.is_high and swing.price < entry]
            structure_low = min([swing.price for swing in pivots if not swing.is_high] or [range_low])
            anchor_low = max(guards) if guards else min(structure_low, zone.bottom)
            stop = round(anchor_low - buffer_, decimals)
            risk = entry - stop
            # targets are measured from the entry, not from the current price, so a
            # zone that sits above the market can never produce a target below it
            above = [level.price for level in highest.levels if level.price > entry]
            if above:
                target_one = round(min(above), decimals)
            elif range_high > entry:
                target_one = round(range_high, decimals)
            else:
                target_one = 0.0
                no_target_note = "no 4H liquidity sits beyond the entry, so there is no honest first target"
            target_two = round(max([*above, range_high, target_one]), decimals) if target_one else 0.0
        else:
            entry = zone.bottom
            guards = [swing.price for swing in pivots if swing.is_high and swing.price > entry]
            structure_high = max([swing.price for swing in pivots if swing.is_high] or [range_high])
            anchor_high = min(guards) if guards else max(structure_high, zone.top)
            stop = round(anchor_high + buffer_, decimals)
            risk = stop - entry
            below = [level.price for level in highest.levels if level.price < entry]
            if below:
                target_one = round(max(below), decimals)
            elif range_low < entry:
                target_one = round(range_low, decimals)
            else:
                target_one = 0.0
                no_target_note = "no 4H liquidity sits beyond the entry, so there is no honest first target"
            target_two = round(min([*below, range_low, target_one]), decimals) if target_one else 0.0
        reward = abs(target_one - entry) if target_one else 0.0
        ratio = round(reward / risk, 2) if (risk > 0 and reward > 0) else 0.0
        rr_detail = (f"reward:risk to the first target = {ratio}:1 (minimum {min_risk_reward}:1)" if reward
                     else no_target_note or "no measurable target beyond the entry")
        gates.append(Gate("risk_reward", ratio >= min_risk_reward, rr_detail,
                          {"ratio": ratio, "minimum": min_risk_reward, "no_target": not bool(reward)}))
        all_passed = all(gate.passed for gate in gates)
        extra = (1 if len(aligned) >= 2 else 0) + (1 if sweeps else 0) + (1 if len(rejection) >= 2 else 0)
        confidence = "high" if all_passed and extra >= 2 else "medium" if all_passed else "low"
        decision = ("watch_long" if side == "long" else "watch_short") if all_passed else "wait"
        plan = TradePlan(side, zone, stop, target_one, target_two, round(risk, decimals), round(reward, decimals), ratio,
                         confidence, tuple(gates), decision, "below" if side == "long" else "above", trigger.timeframe)
    else:
        plan = TradePlan(side, zone, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, "low", tuple(gates), "wait", "", trigger.timeframe)

    flags: list[str] = []
    if not any(timeframe in candles_by_timeframe for timeframe in ("15m", "30m")):
        flags.append("no_entry_timeframe")
    if zone is None and side != "none":
        flags.append("no_zone_in_front")
    pools = liquidity_pools(highest)
    above_pools = sorted((pool for pool in pools if pool.price > price), key=lambda item: item.price)
    below_pools = sorted((pool for pool in pools if pool.price < price), key=lambda item: -item.price)
    liquidity_above = (above_pools[0].kind, round(above_pools[0].price, decimals),
                       round((above_pools[0].price - price) / price * 100, 2)) if above_pools else ()
    liquidity_below = (below_pools[0].kind, round(below_pools[0].price, decimals),
                       round((price - below_pools[0].price) / price * 100, 2)) if below_pools else ()
    trigger_timeframe = trigger.timeframe
    extra_spikes = tuple(
        (view.timeframe, when, ratio)
        for view in views if view.timeframe != trigger_timeframe for _, when, ratio in view.volume.spikes
    )

    watch = {}
    if side != "none":
        # what would flip this WAIT into a plan, expressed only with measured
        # levels so the report never invents a "signal" of its own
        reclaim = ([swing.price for swing in trigger.swings if swing.is_high and swing.price > price] if side == "long"
                   else [swing.price for swing in trigger.swings if swing.is_high and swing.price < price])
        watch = {
            "side": side,
            "reclaim_level": round(max(reclaim) if side == "long" and reclaim else (min(reclaim) if reclaim else 0.0), decimals),
            "zone_kind": plan.zone.kind if plan.zone else "none",
            "zone_low": plan.entry_low,
            "zone_high": plan.entry_high,
            "invalidation": plan.stop,
            "timeframe": trigger.timeframe,
            "missing": tuple(gate.key for gate in gates if not gate.passed),
        }

    return SmartMoneyReport(
        asset_key, asset_name_ar, asset_name_en, quote, decimals, round(price, decimals), source, highest.as_of,
        tuple(views), trend, highest.structure, f"{position * 100:.1f}% of the 4H range {range_low}-{range_high}",
        round(position * 100, 1), premium_discount, plan, tuple(flags), liquidity_above, liquidity_below,
        extra_spikes, round(live_price, decimals) if live_price else 0.0, live_note, watch,
    )
