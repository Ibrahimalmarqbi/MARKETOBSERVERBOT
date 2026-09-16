"""Tests for the strict BUY / SELL / WAIT gate chain.

The fixtures are synthetic on purpose — a unit test has to assert exact
geometry (which candle broke which swing, where the block sits, what the volume
multiple was). The engine itself only ever runs on real candles, and the tests
below check every gate in isolation:

* trend: HH+HL allows longs, LH+LL allows shorts, a mixed ladder is ``none``
* volume: RVOL below 1.0 and a break on weak volume both stop the chain
* structure: a BOS/CHoCH that closes with the trend, superseded breaks do not
* candles: an aligned print is required, an opposing print rejects the trade
* entry: price must return to the order block / gap after the confirmation
* risk: reward:risk below 2 is refused
* the mirror image of a long setup must produce the symmetric short
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from marketobserver.decision import (
    StrictDecision,
    confirmation_analysis,
    decide,
    render,
    render_brief,
    decide as run_decision,
    to_dict,
    trend_from_swings,
)
from marketobserver.market_data import Candle
from marketobserver.smc import Swing, analyze_timeframe, drop_forming

WICK = 0.05


# --------------------------------------------------------------------------- #
# fixture builders
# --------------------------------------------------------------------------- #
def series(anchors, length, minutes, start, volumes=None, default_volume=100.0, overrides=None, plateau=True):
    """Price path through ``(index, price)`` anchors.

    Every anchor is held one extra bar so the turn is a real fractal pivot: a
    perfectly straight line never prints one, which would silently change which
    swings the structure detector can see.
    """
    expanded: list[tuple[int, float]] = []
    for index, price in sorted(anchors):
        expanded.append((index, price))
        if plateau and index + 1 < length:
            expanded.append((index + 1, price))
    expanded.sort()
    closes: list[float] = []
    for index in range(length):
        if index <= expanded[0][0]:
            closes.append(expanded[0][1])
            continue
        after = next((item for item in expanded if item[0] >= index), None)
        before = max((item for item in expanded if item[0] <= index), key=lambda item: item[0])
        if after is None or after[0] == before[0]:
            closes.append(before[1])
            continue
        ratio = (index - before[0]) / (after[0] - before[0])
        closes.append(round(before[1] + (after[1] - before[1]) * ratio, 4))
    volumes = volumes or {}
    out: list[Candle] = []
    for index, close in enumerate(closes):
        open_ = closes[index - 1] if index else close
        if close > open_:
            high, low = max(open_, close) + WICK, min(open_, close) - WICK * 0.25
        elif close < open_:
            high, low = max(open_, close) + WICK * 0.25, min(open_, close) - WICK
        else:
            high, low = close + WICK * 0.5, close - WICK * 0.5
        out.append(Candle(start + timedelta(minutes=minutes * index), open_, high, low, close,
                          volumes.get(index, default_volume)))
    for index, (o, h, l, c, v) in (overrides or {}).items():
        out[index] = Candle(start + timedelta(minutes=minutes * index), o, h, l, c, v)
    return out


TAIL_1H = {
    52: (125.40, 125.50, 124.60, 124.80, 130.0),
    53: (124.80, 124.90, 124.20, 124.40, 120.0),
    54: (124.40, 124.50, 123.90, 124.20, 120.0),
    55: (124.20, 124.30, 123.90, 124.10, 110.0),
    56: (124.10, 123.60, 123.30, 123.50, 120.0),
    57: (123.50, 123.60, 122.60, 122.90, 150.0),
    58: (122.90, 123.20, 122.80, 123.10, 130.0),
    59: (123.10, 123.35, 122.70, 123.25, 150.0),
}

TAIL_15M = {
    44: (123.45, 123.50, 123.15, 123.20, 90.0),
    45: (123.20, 123.25, 122.95, 123.00, 90.0),
    46: (123.00, 123.05, 122.70, 122.85, 90.0),
    47: (122.85, 122.95, 122.80, 122.90, 130.0),
    48: (122.90, 123.05, 122.85, 123.00, 130.0),
    49: (123.00, 123.15, 122.95, 123.10, 130.0),
    50: (123.10, 123.25, 123.05, 123.20, 120.0),
    51: (123.20, 123.22, 123.05, 123.10, 90.0),
    52: (123.10, 123.12, 122.85, 122.95, 90.0),
    53: (122.95, 123.30, 122.90, 123.28, 280.0),
    54: (123.28, 123.35, 123.15, 123.20, 110.0),
    55: (123.20, 123.22, 123.00, 123.05, 100.0),
    56: (123.05, 123.10, 122.85, 122.95, 100.0),
    57: (123.05, 123.10, 122.65, 122.95, 120.0),
    58: (122.95, 123.35, 122.90, 123.30, 190.0),
    59: (123.30, 123.42, 123.28, 123.35, 200.0),
}


def build_long_setup():
    """A valid long: 4H HH+HL, a 15m CHoCH on 2.3x volume, price back in the block."""
    now = datetime(2026, 1, 5, 12, 30, tzinfo=timezone.utc)
    four_h = series(
        [(0, 100.0), (6, 100.0), (15, 108.0), (23, 103.0), (33, 120.0), (43, 112.0), (56, 128.5)],
        60, 240, datetime(2025, 12, 26, 12, 0, tzinfo=timezone.utc),
        volumes={50: 130.0, 51: 140.0, 52: 150.0, 53: 150.0, 54: 140.0, 55: 150.0, 56: 170.0,
                 57: 160.0, 58: 170.0, 59: 180.0},
        overrides={
            57: (128.50, 128.55, 126.10, 126.40, 160.0),
            58: (126.40, 126.50, 124.40, 124.60, 170.0),
            59: (124.60, 124.70, 122.60, 123.25, 180.0),
        },
    )
    one_h = series(
        [(0, 126.0), (8, 126.4), (16, 126.1), (22, 125.3), (28, 125.9), (34, 125.5), (40, 126.2), (45, 128.5), (48, 127.6)],
        60, 60, datetime(2026, 1, 3, 0, 0, tzinfo=timezone.utc),
        volumes={44: 150.0, 45: 160.0, 46: 150.0, 47: 130.0, 48: 140.0, 49: 130.0, 50: 130.0, 51: 130.0},
        overrides=dict(TAIL_1H),
    )
    fifteen = series(
        [(0, 128.5), (8, 127.9), (14, 128.2), (20, 126.9), (26, 127.2), (32, 126.0), (38, 125.4), (42, 124.4)],
        60, 15, datetime(2026, 1, 4, 21, 0, tzinfo=timezone.utc),
        overrides=dict(TAIL_15M),
    )
    return now, {"4h": four_h, "1h": one_h, "15m": fifteen}


def mirror(candles: list[Candle], minutes: int) -> list[Candle]:
    """Reflect a series in price (timestamps untouched): the bullish fixture
    becomes the exact bearish mirror, same geometry and same volume, so a long
    setup must produce the symmetric short."""
    pivot = max(candle.high for candle in candles)
    return [Candle(
        timestamp=candle.timestamp,
        open=round(2 * pivot - candle.open, 4), close=round(2 * pivot - candle.close, 4),
        high=round(2 * pivot - candle.low, 4), low=round(2 * pivot - candle.high, 4),
        volume=candle.volume, buy_volume=None,
    ) for candle in candles]


def build_short_setup():
    now, data = build_long_setup()
    return now, {timeframe: mirror(candles, {"4h": 240, "1h": 60, "15m": 15}[timeframe])
                 for timeframe, candles in data.items()}


def run(data, now, **kwargs) -> StrictDecision:
    return run_decision("BTC", "البيتكوين", "Bitcoin", "USD", 2, data, source="synthetic", now=now, **kwargs)


def failed_keys(decision: StrictDecision) -> set[str]:
    return {step.key for step in decision.failed_steps}


# --------------------------------------------------------------------------- #
# trend
# --------------------------------------------------------------------------- #
def test_trend_ladder_rules():
    def swing(kind, price, label):
        return Swing(kind, 0, price, "2026-01-01 00:00 UTC", label, 0)

    bullish = [swing("high", 110, "HH"), swing("low", 104, "HL")]
    bearish = [swing("high", 108, "LH"), swing("low", 99, "LL")]
    mixed = [swing("high", 110, "HH"), swing("low", 99, "LL")]
    assert trend_from_swings(bullish)[0] == "bullish"
    assert trend_from_swings(bearish)[0] == "bearish"
    assert trend_from_swings(mixed)[0] == "none"
    assert trend_from_swings([])[0] == "none"


# --------------------------------------------------------------------------- #
# the happy path and its mirror
# --------------------------------------------------------------------------- #
def test_every_gate_passes_on_a_valid_long():
    now, data = build_long_setup()
    decision = run(data, now)
    assert decision.decision == "buy"
    assert decision.side == "long"
    assert decision.confidence in {"medium", "high"}
    assert decision.trend == "bullish"
    assert decision.volume_status == "strong" and decision.rvol >= 1.0
    assert decision.structure_confirmed and decision.structure_event in {"BOS", "CHoCH"}
    assert decision.candle_signal == "engulfing_bullish"
    assert decision.entry_kind in {"order_block", "fair_value_gap"}
    assert decision.stop < decision.entry_low < decision.entry_high < decision.take_profit
    assert decision.risk_reward >= 2.0
    assert not decision.reasons
    assert all(step.passed for step in decision.steps)
    assert [step.number for step in decision.steps] == [1, 2, 3, 4, 5, 6, 7]


def test_mirrored_setup_produces_the_symmetric_short():
    now, data = build_short_setup()
    decision = run(data, now)
    assert decision.decision == "sell"
    assert decision.side == "short"
    assert decision.trend == "bearish"
    assert decision.structure_confirmed
    assert decision.take_profit < decision.entry_low < decision.entry_high < decision.stop
    assert decision.risk_reward >= 2.0
    assert all(step.passed for step in decision.steps)


# --------------------------------------------------------------------------- #
# gate 1 — trend
# --------------------------------------------------------------------------- #
def test_mixed_structure_is_not_a_trend():
    now, data = build_long_setup()
    # higher high but lower low: neither a bullish nor a bearish ladder
    data["4h"] = series(
        [(0, 100.0), (8, 110.0), (16, 104.0), (24, 109.0), (32, 102.0), (40, 108.0), (48, 111.5), (56, 101.5), (59, 104.0)],
        60, 240, datetime(2025, 12, 26, 12, 0, tzinfo=timezone.utc),
    )
    decision = run(data, now)
    assert decision.decision == "wait"
    assert decision.trend == "none"
    assert failed_keys(decision) == {"trend"}
    assert "Trend (4H)" in decision.reasons[0]
    assert decision.stopped_at == 1 and decision.entry_low == 0.0 and decision.risk_reward == 0.0


def test_missing_4h_data_is_refused():
    now, data = build_long_setup()
    with pytest.raises(ValueError):
        decide("BTC", "البيتكوين", "Bitcoin", "USD", 2, {"15m": data["15m"]}, now=now)


# --------------------------------------------------------------------------- #
# gate 3 — volume overrides everything
# --------------------------------------------------------------------------- #
def test_rvol_below_one_is_a_weak_market():
    now, data = build_long_setup()
    data["15m"][-1] = Candle(data["15m"][-1].timestamp, 123.30, 123.42, 123.28, 123.35, 30.0)
    decision = run(data, now)
    assert decision.decision == "wait"
    assert decision.volume_status == "weak"
    assert "volume" in failed_keys(decision)
    assert any("weak market" in reason for reason in decision.reasons)


def test_break_on_weak_volume_is_an_invalid_break():
    now, data = build_long_setup()
    # the CHoCH candle keeps its body but loses its volume
    timestamp = data["15m"][53].timestamp
    data["15m"][53] = Candle(timestamp, 122.95, 123.30, 122.90, 123.28, 60.0)
    decision = run(data, now)
    assert decision.decision == "wait"
    assert failed_keys(decision) == {"volume"}          # the chain stops on volume
    assert decision.stopped_at == 3
    assert not next(step for step in decision.steps if step.number == 4).reached
    assert next(step for step in decision.steps if step.number == 4).state == "stopped"
    assert any("invalid break" in reason or "fake break" in reason for reason in decision.reasons)
    verbose = render(decision, "en", verbose=True)
    assert "[FAIL] 3." in verbose
    assert "[not reached] 4." in verbose and "[not reached] 7." in verbose
    assert "chain stopped at step 3; gates 4-7 were not reached" in verbose


# --------------------------------------------------------------------------- #
# gate 4 — structure confirmation
# --------------------------------------------------------------------------- #
def test_without_a_closing_break_there_is_no_confirmation():
    now, data = build_long_setup()
    # the "CHoCH" candle becomes a wick-only rejection that closes back below
    timestamp = data["15m"][53].timestamp
    data["15m"][53] = Candle(timestamp, 122.95, 123.30, 122.88, 123.05, 280.0)
    decision = run(data, now)
    assert decision.decision == "wait"
    assert "confirmation" in failed_keys(decision)


# --------------------------------------------------------------------------- #
# gate 5 — candle confirmation
# --------------------------------------------------------------------------- #
def test_opposing_candle_print_rejects_the_trade():
    now, data = build_long_setup()
    # the newest closed candle becomes a shooting star above the same block
    data["15m"][59] = Candle(data["15m"][59].timestamp, 123.28, 123.75, 123.25, 123.36, 200.0)
    decision = run(data, now)
    assert decision.decision == "wait"
    assert failed_keys(decision) == {"candle"}
    assert decision.candle_signal == "shooting_star"
    assert any("opposite signal" in reason for reason in decision.reasons)


# --------------------------------------------------------------------------- #
# gate 6 — entry zone and the return to it
# --------------------------------------------------------------------------- #
def test_price_must_come_back_into_the_zone():
    now, data = build_long_setup()
    # the break is confirmed, then price runs away instead of retracing
    for index, (o, h, l, c) in {54: (123.28, 123.60, 123.25, 123.55), 55: (123.55, 123.90, 123.50, 123.85),
                                56: (123.85, 124.20, 123.80, 124.15), 57: (124.15, 124.50, 124.10, 124.45),
                                58: (124.45, 124.80, 124.40, 124.75), 59: (124.75, 125.10, 124.70, 125.05)}.items():
        data["15m"][index] = Candle(data["15m"][index].timestamp, o, h, l, c, 150.0)
    decision = run(data, now)
    assert decision.decision == "wait"
    assert "entry_zone" in failed_keys(decision)
    assert any("chasing" in reason or "has not returned" in reason for reason in decision.reasons)


# --------------------------------------------------------------------------- #
# gate 7 — risk
# --------------------------------------------------------------------------- #
def test_reward_risk_below_the_floor_is_refused():
    now, data = build_long_setup()
    decision = run(data, now, min_risk_reward=5.0)
    assert decision.decision == "wait"
    assert failed_keys(decision) == {"risk"}
    assert decision.risk_reward >= 2.0  # the setup itself is tradable at 2:1
    assert decision.risk_reward < 5.0
    assert "reward:risk" in decision.reasons[0]


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def test_strict_output_format_for_a_wait(card_capture=None):
    now, data = build_long_setup()
    data["15m"][-1] = Candle(data["15m"][-1].timestamp, 123.30, 123.42, 123.28, 123.35, 30.0)
    decision = run(data, now)
    text = render(decision, "en")
    for line in ("🧠 Smart Analysis | BTC", "Trend (4H): Bullish", "Liquidity Target: ",
                 "Volume:", "- RVOL: ", "- Status: Weak", "Structure:", "- BOS / CHoCH: ",
                 "Candles:", "- Signal: ", "Entry Zone:", "Stop Loss:", "Take Profit:",
                 "Risk/Reward:", "🎯 FINAL DECISION: WAIT", "Confidence: Low"):
        assert line in text, line
    assert "⚠️ WAIT — exact reason:" in text
    assert "weak market" in text


def test_verbose_render_lists_every_gate():
    now, data = build_long_setup()
    decision = run(data, now)
    text = render(decision, "en", verbose=True)
    assert "🎯 FINAL DECISION: BUY" in text
    assert "Gate chain (evaluated in this order" in text
    for number in range(1, 8):
        assert f"[{'PASS' if number <= 7 else 'FAIL'}] {number}." in text
    assert all(f"[PASS] {step.number}." in text for step in decision.steps)


def test_arabic_and_bilingual_render():
    now, data = build_long_setup()
    decision = run(data, now)
    arabic = render(decision, "ar")
    assert "تحليل ذكي" in arabic and "القرار النهائي" in arabic
    both = render(decision, "both")
    assert "🎯 FINAL DECISION: BUY" in both and "القرار النهائي" in both
    assert "BTC" in render_brief(decision, "en")


def test_to_dict_is_json_ready():
    import json

    now, data = build_long_setup()
    decision = run(data, now)
    payload = to_dict(decision)
    assert payload["decision"] == "buy"
    assert payload["side"] == "long"
    assert [step["number"] for step in payload["steps"]] == [1, 2, 3, 4, 5, 6, 7]
    assert json.loads(json.dumps(payload))["risk_reward"] == payload["risk_reward"]


# --------------------------------------------------------------------------- #
# extra coverage: setup-timeframe volume, 1h confirmation, context indicators
# --------------------------------------------------------------------------- #
def test_setup_timeframe_volume_floor_counts_too():
    now, data = build_long_setup()
    # the 15m stays liquid but the 1h dries up: the timeframe the setup has to be
    # confirmed on cannot be dead air, so the volume gate must refuse it
    for index in range(len(data["1h"]) - 6, len(data["1h"])):
        candle = data["1h"][index]
        data["1h"][index] = Candle(candle.timestamp, candle.open, candle.high, candle.low, candle.close, 5.0)
    decision = run(data, now)
    assert decision.decision == "wait"
    assert failed_keys(decision) == {"volume"}
    assert decision.volume_status == "weak"
    assert any("the setup has to be confirmed on" in reason for reason in decision.reasons)


def test_confirmation_may_come_from_the_1h():
    now, data = build_long_setup()
    grind = series([(0, 130.0), (10, 129.2), (18, 129.6), (26, 128.9), (34, 128.6), (42, 128.9)],
                   48, 15, datetime(2026, 1, 4, 20, 0, tzinfo=timezone.utc))
    one_h = series([(0, 132.0), (6, 131.4), (12, 130.6), (18, 129.8), (24, 130.2), (28, 129.4), (34, 128.8)],
                   40, 60, datetime(2026, 1, 3, 4, 0, tzinfo=timezone.utc),
                   overrides={36: (128.90, 129.10, 128.60, 128.85, 90.0),
                              37: (128.85, 129.30, 128.80, 129.20, 110.0),
                              38: (129.20, 130.10, 129.10, 130.00, 260.0),
                              39: (130.00, 130.60, 129.95, 130.40, 220.0)})
    views = {tf: analyze_timeframe(drop_forming(candles, tf, now), tf, 2)
             for tf, candles in {**data, "1h": one_h, "15m": grind}.items()}
    confirmation = confirmation_analysis(views, "long")
    assert confirmation["all"]["15m"]["ok"] is False
    assert confirmation["all"]["1h"]["ok"] is True
    assert confirmation["timeframe"] == "1h"
    assert confirmation["event"].volume_ratio >= 1.10
    assert "CHoCH bullish" in confirmation["detail"]


def test_indicators_are_context_not_gates():
    now, data = build_long_setup()
    decision = run(data, now)
    assert {item.timeframe for item in decision.indicators} == {"4h", "1h", "15m"}
    assert all(0.0 < item.rsi < 100.0 for item in decision.indicators)
    assert all(item.sma20 > 0 and item.sma50 > 0 for item in decision.indicators)
    assert "indicators" not in {step.key for step in decision.steps}
    verbose = render(decision, "en", verbose=True)
    assert "RSI" in verbose and "SMA20" in verbose
    assert to_dict(decision)["indicators"][0]["timeframe"] == "4h"


def test_second_target_is_reported_only_when_it_exists():
    now, data = build_long_setup()
    decision = run(data, now)
    text = render(decision, "en")
    assert decision.take_profit == 127.85
    # the fixture has a further pool behind the first one, so it is reported once
    assert decision.take_profit_two == 128.53
    assert text.count("(second liquidity level)") == 1
    # a plan without a second pool must not print a phantom level or a 0.00 line
    single = replace(decision, take_profit_two=0.0)
    single_text = render(single, "en")
    assert "(second liquidity level)" not in single_text
    assert single_text.count("- 127.85") == 1
    assert "- 0.00" not in single_text.split("Risk/Reward:")[0]


def test_verbose_chain_does_not_claim_a_stop_when_every_gate_passed():
    now, data = build_long_setup()
    decision = run(data, now)
    verbose = render(decision, "en", verbose=True)
    assert "not reached" not in verbose
    assert "chain stopped" not in verbose
    assert decision.stopped_at == 0
