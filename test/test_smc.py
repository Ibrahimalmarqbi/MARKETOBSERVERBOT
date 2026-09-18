"""Unit tests for the deterministic smart-money detectors.

These fixtures are *synthetic* on purpose: a test must assert exact geometry, and
the engine is only ever fed real candles at runtime (Binance first, then Kraken,
then Yahoo). Every test checks arithmetic that the report depends on: pivot
labelling, BOS vs CHoCH vs sweep, gap mitigation, order-block validity, pattern
definitions, volume reading, gate outcomes and bilingual rendering.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from marketobserver.market_data import Candle
from marketobserver.smc import (
    SmartMoneyReport, analyze_timeframe, atr_at, build_report, classify_structure, drop_forming,
    fair_value_gaps, label_swings, levels, order_blocks, patterns, structure_events, swings, volume_stats,
)
from marketobserver.smc_text import render, render_brief, to_dict
from marketobserver.nlp import parse_request

START = datetime(2026, 1, 5, tzinfo=timezone.utc)


def candle(open_: float, high: float, low: float, close: float, index: int, volume: float = 100.0,
           buy: float | None = None, minutes: int = 240) -> Candle:
    return Candle(START + timedelta(minutes=minutes * index), open_, high, low, close, volume, buy)


def path(closes: list[float], wick: float = 0.02, volumes=None, minutes: int = 240) -> list[Candle]:
    """Monotone candles between the given closes.

    The wick is added only on the direction the candle travelled, so a local peak
    candle is strictly higher than its neighbours instead of tying with the flat
    open of the next bar — otherwise a fractal detector cannot see the pivot.
    """
    candles: list[Candle] = []
    for index, close in enumerate(closes):
        open_ = closes[index - 1] if index else close
        high = max(open_, close) + (wick if close > open_ else 0.0)
        low = min(open_, close) - (wick if close < open_ else 0.0)
        volume = volumes[index] if volumes else 100.0
        candles.append(candle(open_, high, low, close, index, volume, minutes=minutes))
    return candles


def zigzag(points: list[tuple[int, float]], length: int, minutes: int = 240) -> list[Candle]:
    """Linear price path through (index, price) anchors — used to build clean
    higher-high/higher-low or lower-low ladders."""
    anchors = sorted(points)
    closes: list[float] = []
    for index in range(length):
        if index < anchors[0][0]:
            closes.append(round(anchors[0][1], 4))
            continue
        before = max((anchor for anchor in anchors if anchor[0] <= index), key=lambda item: item[0])
        after = min((anchor for anchor in anchors if anchor[0] > index), default=None)
        if after is None:
            closes.append(round(before[1], 4))
            continue
        span = after[0] - before[0]
        ratio = (index - before[0]) / span
        closes.append(round(before[1] + (after[1] - before[1]) * ratio, 4))
    return path(closes, minutes=minutes)


# --------------------------------------------------------------------------- #
# pivots + structure
# --------------------------------------------------------------------------- #
def test_swings_are_only_confirmed_after_the_lookback_window():
    series = zigzag([(0, 100), (5, 110), (10, 102), (15, 115), (20, 106), (25, 120), (29, 112)], 30)
    pivots = swings(series)
    highs = [pivot for pivot in pivots if pivot.is_high]
    assert [pivot.index for pivot in highs] == [5, 15, 25]
    assert all(pivot.confirmed_at_index == pivot.index + 2 for pivot in highs)


def test_higher_high_higher_low_ladder_reads_bullish():
    series = zigzag([(0, 100), (6, 110), (12, 104), (18, 118), (24, 112), (30, 126)], 36)
    labelled = swings(series)
    assert {pivot.label for pivot in labelled} >= {"HH", "HL"}
    bias, description = classify_structure(labelled)
    assert bias == "bullish"
    assert "HH" in description or "HL" in description


def test_lower_high_lower_low_ladder_reads_bearish():
    series = zigzag([(0, 130), (6, 120), (12, 126), (18, 112), (24, 118), (30, 104)], 36)
    bias, _ = classify_structure(swings(series))
    assert bias == "bearish"


def test_equal_highs_are_labelled_as_a_liquidity_pool():
    series = zigzag([(0, 100), (6, 120.00), (12, 110), (18, 120.02), (24, 112), (30, 118), (35, 113)], 36)
    labelled = swings(series)
    assert any(pivot.label == "EQH" for pivot in labelled)
    zones = levels(series, labelled, atr_at(series))
    assert any(zone.kind == "equal_highs" for zone in zones)


# --------------------------------------------------------------------------- #
# BOS / CHoCH / sweep
# --------------------------------------------------------------------------- #
def test_first_break_against_the_bias_is_choch_and_the_next_is_bos():
    # 132 -> 120 -> 126 -> 112 -> 118 -> 108 -> 130: two bearish breaks first,
    # then the reclaim of 118.02 must be read as a change of character
    series = zigzag([(2, 132), (8, 120), (14, 126), (20, 112), (26, 118), (30, 108), (36, 130)], 40)
    pivots = swings(series)
    events = structure_events(series, pivots, atr_at(series))
    assert [event.kind for event in events[:2]] == ["bos", "bos"]
    assert all(event.direction == "bearish" for event in events[:2])
    assert events[-1].kind == "choch" and events[-1].direction == "bullish"
    assert events[-1].level == pytest.approx(118.02, abs=0.5)

    # after the reclaim, a fresh higher high is a continuation BOS, not a CHoCH
    extended = zigzag([(2, 132), (8, 120), (14, 126), (20, 112), (26, 118), (30, 108), (36, 130),
                       (40, 124), (44, 134)], 46)
    later = [event for event in structure_events(extended, swings(extended), atr_at(extended)) if event.index > 36]
    assert later and later[-1].kind == "bos" and later[-1].direction == "bullish"


def test_wick_through_a_level_is_a_sweep_never_a_break():
    series = [candle(100.0, 101.0, 99.0, 100.5, 0), candle(100.5, 102.0, 100.4, 101.5, 1),
              candle(101.5, 103.0, 101.4, 102.5, 2), candle(102.5, 105.0, 102.4, 104.5, 3),
              candle(104.5, 104.6, 102.0, 102.5, 4), candle(102.5, 102.6, 100.0, 100.5, 5),
              candle(100.5, 102.0, 100.4, 101.5, 6), candle(101.5, 103.0, 101.4, 102.5, 7),
              # wick above the 105 swing high, body closes back under it
              candle(102.5, 106.0, 102.0, 103.0, 8)]
    pivots = swings(series)
    assert [pivot.price for pivot in pivots if pivot.is_high][0] == pytest.approx(105.0)
    events = structure_events(series, pivots, atr_at(series))
    assert events and events[-1].kind == "sweep"
    assert events[-1].wick_only is True
    assert events[-1].level == pytest.approx(105.0)
    assert all(event.kind != "bos" or event.index != 8 for event in events)


# --------------------------------------------------------------------------- #
# patterns
# --------------------------------------------------------------------------- #
def test_hammer_needs_a_lower_wick_twice_the_body():
    series = [candle(100.0, 100.4, 99.6, 100.2, 0), candle(100.2, 100.6, 99.8, 100.0, 1),
              candle(100.4, 101.7, 96.8, 101.6, 2)]
    found = patterns(series, atr=1.0, lookback=3)
    assert found[-1].name == "hammer" and found[-1].direction == "bullish"


def test_inverted_hammer_requires_a_bearish_prior_candle():
    series = [candle(100.0, 100.4, 99.6, 100.2, 0), candle(100.8, 100.9, 99.8, 100.0, 1),
              candle(100.0, 104.2, 99.9, 100.9, 2)]
    names = [item.name for item in patterns(series, atr=1.0, lookback=3)]
    assert "inverted_hammer" in names and names[-1] == "inverted_hammer"


def test_shooting_star_is_the_same_shape_after_a_rally():
    series = [candle(99.0, 99.4, 98.6, 99.2, 0), candle(99.2, 100.0, 99.1, 99.9, 1),
              candle(100.0, 104.2, 99.9, 100.9, 2)]
    names = [item.name for item in patterns(series, atr=1.0, lookback=3)]
    assert names[-1] == "shooting_star"


def test_bullish_and_bearish_engulfing_need_a_bigger_body():
    bullish = [candle(101.0, 102.0, 100.6, 101.6, 0), candle(102.4, 102.5, 99.9, 100.0, 1),
               candle(99.6, 103.1, 99.5, 103.0, 2)]
    assert [item.name for item in patterns(bullish, atr=1.0, lookback=3)][-1] == "engulfing_bullish"

    bearish = [candle(99.0, 99.6, 98.6, 99.4, 0), candle(99.6, 102.4, 99.5, 102.0, 1),
               candle(102.4, 102.7, 99.3, 99.4, 2)]
    assert [item.name for item in patterns(bearish, atr=1.0, lookback=3)][-1] == "engulfing_bearish"


def test_doji_needs_a_tiny_body_relative_to_range():
    series = [candle(100, 100.4, 99.6, 100.0, 0), candle(100.0, 100.3, 99.7, 100.02, 1),
              candle(100.0, 103.0, 97.0, 100.05, 2)]
    found = patterns(series, atr=3.0, lookback=3)
    assert found[-1].name == "doji" and found[-1].direction == "neutral"


def test_morning_and_evening_star_three_candle_rules():
    morning = [candle(104.5, 105.0, 104.0, 104.2, 0), candle(104.0, 104.1, 100.3, 100.4, 1),
               candle(100.2, 100.3, 99.8, 100.4, 2), candle(100.8, 104.2, 100.7, 104.0, 3)]
    assert "morning_star" in [item.name for item in patterns(morning, atr=1.0, lookback=4)]

    evening = [candle(99.0, 99.6, 98.6, 99.4, 0), candle(99.6, 103.4, 99.5, 103.2, 1),
               candle(103.3, 103.9, 103.2, 103.6, 2), candle(103.8, 103.9, 99.8, 100.0, 3)]
    assert "evening_star" in [item.name for item in patterns(evening, atr=1.0, lookback=4)]


def test_inside_and_outside_bar_compression_markers():
    inside = [candle(99.0, 100.0, 98.0, 99.5, 0), candle(99.5, 102.0, 98.0, 101.5, 1),
              candle(101.2, 101.6, 100.4, 101.0, 2)]
    assert [item.name for item in patterns(inside, atr=1.0, lookback=3)][-1] == "inside_bar"

    outside = [candle(99.0, 100.0, 98.0, 99.5, 0), candle(99.5, 101.0, 99.0, 100.5, 1),
               candle(100.2, 102.5, 98.0, 98.6, 2)]
    assert [item.name for item in patterns(outside, atr=1.0, lookback=3)][-1] == "outside_bar"


def test_patterns_ignore_dust_candles_below_the_atr_floor():
    tiny = [candle(100.0, 100.02, 99.99, 100.01, 0), candle(100.01, 100.03, 99.98, 100.02, 1),
            candle(100.02, 100.05, 100.01, 100.03, 2)]
    # a doji needs a wide range to be meaningful, so dust candles are skipped
    assert [item.name for item in patterns(tiny, atr=3.0, lookback=3)] == []


# --------------------------------------------------------------------------- #
# gaps + order blocks
# --------------------------------------------------------------------------- #
def test_bullish_fair_value_gap_and_mitigation():
    # candle 2 opens far above candle 0's high, leaving a gap that stays open
    series = [candle(100, 101, 99, 100.5, 0), candle(100.5, 103, 100.4, 102.6, 1),
              candle(102.7, 104, 102.2, 103.6, 2)] + [candle(103.6, 104.2, 103.2, 104.0, i) for i in range(3, 8)]
    gaps = fair_value_gaps(series, atr=1.0)
    assert gaps and gaps[0].kind == "bullish"
    assert gaps[0].bottom == pytest.approx(101.0) and gaps[0].top == pytest.approx(102.2)
    assert gaps[0].mitigated is False

    filled = series[:3] + [candle(103.6, 103.8, 99.0, 99.5, 3)]
    gaps = fair_value_gaps(filled, atr=1.0)
    assert gaps[0].mitigated is True


def test_order_block_is_the_last_opposing_candle_before_the_break():
    series = zigzag([(2, 132), (8, 120), (14, 126), (20, 112), (26, 118), (30, 108), (36, 130)], 40)
    events = structure_events(series, swings(series), atr_at(series))
    blocks = order_blocks(series, events, atr_at(series))
    assert blocks
    block = next(item for item in blocks if item.kind == "bullish")
    assert block.caused_by in {"bos", "choch"}
    assert block.index < 33  # sits under the leg that broke structure
    assert block.bottom == pytest.approx(min(series[block.index].low, 130.0), abs=1.5)
    assert block.invalid is False


def test_order_block_is_dropped_once_price_closes_through_it():
    series = zigzag([(2, 132), (8, 120), (14, 126), (20, 112), (26, 118), (30, 108), (36, 130)], 40)
    events = structure_events(series, swings(series), atr_at(series))
    block = next(item for item in order_blocks(series, events, atr_at(series)) if item.kind == "bullish")
    breached = series + [candle(block.bottom + 0.4, block.bottom + 0.5, block.bottom - 6.0, block.bottom - 5.5, len(series))]
    after = order_blocks(breached, structure_events(breached, swings(breached), atr_at(breached)), atr_at(breached))
    assert all(item.index != block.index or item.invalid for item in after)


# --------------------------------------------------------------------------- #
# volume
# --------------------------------------------------------------------------- #
def test_volume_reads_relative_volume_spikes_and_taker_delta():
    volumes = [100.0] * 19 + [400.0]
    closes = [100 + index * 0.5 for index in range(20)]
    buys = [value * 0.7 for value in volumes]
    series = [Candle(START + timedelta(hours=index), close - 0.2, close + 0.2, close - 0.3, close, volume, buy)
              for index, (close, volume, buy) in enumerate(zip(closes, volumes, buys))]
    stats = volume_stats(series)
    assert stats.relative > 2
    assert stats.spikes and stats.spikes[-1][2] > 2
    assert stats.buy_pct == pytest.approx(70.0, abs=1.0)
    assert stats.trend in {"expanding", "flat"}


def test_volume_stats_stay_silent_when_the_provider_has_no_delta():
    series = path([100 + index for index in range(10)])
    stats = volume_stats(series)
    assert stats.buy_pct is None
    assert stats.note == ""


# --------------------------------------------------------------------------- #
# forming candles
# --------------------------------------------------------------------------- #
def test_forming_candle_is_excluded_from_the_reads():
    series = [candle(100, 101, 99, 100.5, index) for index in range(10)]
    now = series[-1].timestamp + timedelta(minutes=120)  # 4h bar still open
    assert drop_forming(series, "4h", now) == series[:-1]
    assert drop_forming(series, "4h", series[-1].timestamp + timedelta(minutes=241)) == series


# --------------------------------------------------------------------------- #
# report + gates
# --------------------------------------------------------------------------- #
def build_cases() -> tuple[dict, dict]:
    bull_4h = zigzag([(0, 100), (10, 110), (20, 106), (30, 118), (40, 114), (50, 126), (60, 122), (66, 132)], 70)
    bull_1h = zigzag([(0, 124), (8, 128), (16, 126), (24, 130), (32, 128), (40, 133), (48, 131), (55, 135)], 60)
    bull_15m = zigzag([(0, 133), (6, 134.5), (12, 133.8), (20, 135.2), (28, 134.4), (36, 136.0)], 44)
    flat = [candle(100 + (index % 3) * 0.05, 100.2 + (index % 3) * 0.05, 99.9 + (index % 3) * 0.05,
                   100.1 + (index % 3) * 0.05, index) for index in range(70)]
    return ({"4h": bull_4h, "1h": bull_1h, "15m": bull_15m}, {"4h": flat})


def make_report(candles: dict) -> SmartMoneyReport:
    return build_report("TEST", "اختبار", "Test", "USD", 2, candles, "unit-test")


def test_report_reports_a_side_only_when_4h_structure_qualifies():
    bullish, flat = build_cases()
    report = make_report(bullish)
    assert report.trend in {"bullish", "bullish_early"}
    assert report.plan.side in {"long", "none"}
    flat_report = make_report(flat)
    assert flat_report.trend == "sideways"
    assert flat_report.plan.side == "none"
    assert flat_report.plan.decision == "wait"
    assert flat_report.plan.zone is None


def test_wait_is_enforced_when_a_gate_fails_and_reasons_are_listed():
    bullish, _ = build_cases()
    report = make_report(bullish)
    failed = [gate for gate in report.plan.gates if not gate.passed]
    if report.plan.decision == "wait":
        assert failed, "a WAIT decision must name the gates that failed"
    else:
        assert not failed
        assert report.plan.risk_reward >= 2.0


def test_stop_sits_outside_structure_and_targets_use_real_levels():
    bullish, _ = build_cases()
    report = make_report(bullish)
    plan = report.plan
    if plan.zone is None:
        pytest.skip("no zone formed in this fixture")
    view = report.highest
    if plan.side == "long":
        assert plan.stop < plan.entry_low
        if plan.target_one:
            assert plan.target_one > plan.entry_high
    else:
        assert plan.stop > plan.entry_high
        if plan.target_one:
            assert plan.target_one < plan.entry_low
    if plan.target_one:
        assert any(abs(plan.target_one - level.price) < max(view.atr, 1e-9) * 3 for level in view.levels)
    else:
        # no honest target beyond the entry must block the plan, never fake one
        assert plan.risk_reward == 0.0 and plan.decision == "wait"


def test_analysis_refuses_thin_data_instead_of_guessing():
    with pytest.raises(ValueError):
        analyze_timeframe(path([100 + index * 0.1 for index in range(20)]), "4h")


def test_render_is_bilingual_and_numbers_are_shared():
    bullish, _ = build_cases()
    report = make_report(bullish)
    english = render(report, "en")
    arabic = render(report, "ar")
    both = render(report, "both")
    assert "Trend (4H)" in english and "Confirmation (BOS / CHoCH / none)" in english
    assert "Final Decision" in english and "Risk/Reward" in english
    assert "الاتجاه (4H)" in arabic and "القرار النهائي" in arabic
    assert "التأكيد (BOS / CHoCH / لا شيء)" in arabic
    assert both.startswith(arabic) and english.splitlines()[0] in both
    assert f"{report.price:.2f}" in english and f"{report.price:.2f}" in arabic
    assert "WAIT" in both or "WATCH" in both


def test_brief_and_dict_expose_the_same_facts():
    bullish, _ = build_cases()
    report = make_report(bullish)
    brief = render_brief(report, "ar")
    assert "🧠" in brief and report.as_of in brief
    payload = to_dict(report)
    assert payload["asset"] == "TEST" and payload["source"] == "unit-test"
    assert payload["decision"] in {"wait", "watch_long", "watch_short"}
    assert {frame["timeframe"] for frame in payload["timeframes"]} == {"4h", "1h", "15m"}
    assert len(payload["gates"]) >= 6
    assert all(set(gate) == {"key", "passed", "detail"} for gate in payload["gates"])
    # nothing in the payload is a bare promise: either a plan or an explicit null
    if payload["zone"] is None:
        assert payload["stop"] is None and payload["risk_reward"] is None


def test_labels_and_zone_types_are_translated_in_both_languages():
    bullish, _ = build_cases()
    report = make_report(bullish)
    english = render(report, "en")
    if report.plan.zone is not None:
        assert {"order block", "fair value gap (FVG)", "clustered level"} & set(
            token for token in english.replace("|", " ").split()) or "Entry Zone" in english


# --------------------------------------------------------------------------- #
# routing (hybrid commands)
# --------------------------------------------------------------------------- #
def test_smart_money_phrases_route_to_the_engine_in_both_languages():
    for text in ("smart money BTC", "SMC on gold", "what is the BTC liquidity zone", "price action ETH 4h",
                 "ما مناطق السيولة في البيتكوين", "حلل الذهب بسيولة ذكية", "اوردر بلوك BTC", "فين فجوة سعرية للإيثريوم"):
        request = parse_request(text)
        assert request.intent == "smc", text
    assert parse_request("smart money on BTC").asset.key == "BTC"
    assert parse_request("ما مناطق السيولة في البيتكوين").asset.key == "BTC"
    assert parse_request("price action ETH 4h").timeframe == "4h"


def test_other_intents_are_untouched_by_the_new_route():
    assert parse_request("حلل الذهب").intent in {"analysis", "advice"}
    assert parse_request("احسب مخاطرة رأس المال 10000 بنسبة 1% دخول 4715 وقف 4690").intent == "risk"
    assert parse_request("ما أخبار البيتكوين").intent == "news"


def test_button_payload_and_command_arguments_parse():
    from app import parse_smc_arguments, parse_smc_callback

    assert parse_smc_callback("smc:asset:XAUUSD:en:brief") == ("asset", "XAUUSD", "en", "brief", False)
    assert parse_smc_callback("smc:report:BTC:ar:full:1") == ("report", "BTC", "ar", "full", True)
    assert parse_smc_callback("nonsense")[0] == "report"
    assert parse_smc_callback("smc:lang:BTC:zz:full") == ("lang", "BTC", "ar", "full", False)
    assert parse_smc_arguments("/smc BTC en brief") == ("BTC", "en", "brief")
    assert parse_smc_arguments("/smc الذهب كامل") == ("الذهب", None, "full")
    assert parse_smc_arguments("/smc") == (None, None, None)


def test_keyboard_carries_state_and_stays_inside_telegram_limits():
    from app import smc_keyboard

    keyboard = smc_keyboard("BTC", "ar", "full")
    rows = keyboard.keyboard
    assert len(rows) >= 6
    for row in rows:
        for button in row:
            assert len(button.callback_data.encode("utf-8")) <= 64
            # smc: for smart-money actions, bin: for global settings entry point
            assert button.callback_data.startswith(("smc:", "bin:", "set:"))
    assert any("🔔" in button.text for row in rows for button in row)
    assert any("🌐" in button.text for row in rows for button in row)
    assert any("⚙️" in button.text for row in rows for button in row)


def test_http_endpoint_serves_the_same_analysis(tmp_path, monkeypatch):
    """The JSON route returns the identical deterministic payload, and fails
    loudly instead of inventing data when a provider has nothing."""
    import app as application

    candles, _ = build_cases()
    report = build_report("BTC", "البيتكوين", "Bitcoin", "USD", 2, candles, "unit-test")
    payload = (report, candles)

    def fake_builder(asset, force=False):
        if asset.key != "BTC":
            raise application.DataUnavailable("no candles for this asset")
        return payload

    monkeypatch.setattr(application, "smc_build_report", fake_builder)
    client = application.app.test_client()

    english = client.get("/smc/BTC?lang=en")
    assert english.status_code == 200
    body = english.get_json()
    assert body["asset"] == "BTC" and body["decision"] in {"wait", "watch_long", "watch_short"}
    assert "Trend (4H)" in body["report"]
    assert {frame["timeframe"] for frame in body["timeframes"]} == {"4h", "1h", "15m"}
    assert body["name_ar"] == "البيتكوين" and body["name_en"] == "Bitcoin"  # bilingual by default

    arabic = client.get("/smc/BTC?lang=ar&mode=brief")
    assert arabic.status_code == 200
    assert "اتجاه 4H" in arabic.get_json()["summary"]
    assert "report" not in arabic.get_json()

    assert client.get("/smc/NOTANASSET?lang=en").status_code == 503
    assert client.get("/smc/BTC?lang=de").status_code == 400


def bull_hourly() -> list[Candle]:
    return zigzag([(0, 124), (8, 128), (16, 126), (24, 130), (32, 128), (40, 133), (48, 131), (55, 135)], 60)


def bull_quarter() -> list[Candle]:
    return zigzag([(0, 133), (6, 134.5), (12, 133.8), (20, 135.2), (28, 134.4), (36, 136.0)], 44)
