"""Backtest engine: aggregation, walk-forward honesty, metrics, wiring."""

import json
from datetime import datetime, timedelta, timezone

import pytest

import app
from marketobserver.assets import ASSETS
from marketobserver.backtest import (
    HistoryUnavailable, backtest, render, run, to_15m, to_dict,
)
from marketobserver.market_data import Candle


BASE = datetime(2026, 6, 1, tzinfo=timezone.utc)


def staircase(start: float, step: float, count: int = 300, gap_at: int | None = None) -> list[Candle]:
    """Realistic trend with pullbacks; optional weekend-style gap."""
    out, price = [], start
    pattern = [step, -step, step]
    for index in range(count):
        move = pattern[index % 3]
        price += move
        moment = BASE + timedelta(minutes=5 * index)
        if gap_at is not None and index >= gap_at:
            moment += timedelta(hours=48)  # market reopens two days later
        out.append(Candle(moment, price - move, price + abs(move) * 0.2,
                          price - abs(move) * 0.2, price, 1000.0))
    return out


def test_15m_aggregation_is_exact():
    bars = staircase(1.08, 0.0002, 6)
    agg = to_15m(bars)
    assert len(agg) == 2
    first = agg[0]
    assert first.open == bars[0].open
    assert first.close == bars[2].close
    assert first.high == max(bar.high for bar in bars[:3])
    assert first.low == min(bar.low for bar in bars[:3])
    assert first.timestamp == BASE.replace(minute=0, second=0, microsecond=0)


def test_gaps_produce_no_phantom_bars():
    bars = staircase(1.08, 0.0002, 12, gap_at=6)
    agg = to_15m(bars)
    assert len(agg) == 4  # two 15m bars per side, nothing interpolated
    assert agg[2].timestamp - agg[1].timestamp > timedelta(hours=24)


def wave(count: int = 400) -> list[Candle]:
    """Uptrend with real pullbacks so 5m AND 15m RSI stay inside the bands."""
    import math
    out, price = [], 1.08
    for index in range(count):
        move = 0.00004 + 0.0003 * math.sin(2 * math.pi * index / 36)
        price += move
        moment = BASE + timedelta(minutes=5 * index)
        out.append(Candle(moment, price - move, price + abs(move) * 0.2,
                          price - abs(move) * 0.2, price, 1000.0))
    return out


def test_walk_forward_finds_calls_and_counts_honestly():
    result = run(wave(), ASSETS["EURUSD"])
    assert len(result.trades) > 10
    assert all(trade.verdict == "CALL" for trade in result.trades)
    assert result.wins + result.losses + result.flats == len(result.trades)
    assert result.win_rate > 0.5
    assert result.breakeven_rate == pytest.approx(1 / 1.8)
    assert result.expectancy == pytest.approx(result.net_units / result.decided)
    assert result.monthly and result.data_from and result.data_to


def test_no_lookahead_future_data_changes_nothing():
    full = run(wave(400), ASSETS["EURUSD"])
    prefix = run(wave(250), ASSETS["EURUSD"])
    assert len(prefix.trades) < len(full.trades)
    for early, late in zip(prefix.trades, full.trades):
        assert (early.time, early.verdict, early.ref_price, early.outcome) == \
               (late.time, late.verdict, late.ref_price, late.outcome)


def test_losing_market_reports_honest_loss():
    # Downtrend slices still run; reversing verdicts is impossible, so a broken
    # strategy must be able to print a negative number.
    result = run(wave(), ASSETS["EURUSD"], payout=0.5)
    assert result.breakeven_rate == pytest.approx(1 / 1.5)
    assert set(to_dict(result)) >= {"win_rate", "expectancy", "net_units", "monthly",
                                    "breakeven_rate", "max_losing_streak"}
    text = render(result, "ar")
    assert "باك تست" in text and "التوقع/صفقة" in text
    assert "الماضي ليس ضمانًا" in text
    assert "profit" in render(result, "en").lower() or "win rate" in render(result, "en").lower()


def test_history_shortage_raises_clearly(monkeypatch):
    import marketobserver.backtest as bt

    class FakeResponse:
        status_code = 200

        def json(self):
            return []

    class FakeSession:
        def get(self, *args, **kwargs):
            return FakeResponse()

    with pytest.raises(HistoryUnavailable):
        bt._fetch_binance_5m(ASSETS["BTC"], 30, FakeSession())


def test_binance_pagination_collects_pages(monkeypatch):
    import marketobserver.backtest as bt

    base_ms = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
    page1 = [[base_ms + i * 300000, "1.08", "1.081", "1.079", "1.080", "10", 0, 0, 0, "5", 0, 0]
             for i in range(1000)]
    page2 = [[base_ms + (1000 + i) * 300000, "1.08", "1.081", "1.079", "1.080", "10", 0, 0, 0, "5", 0, 0]
             for i in range(10)]

    class FakeResponse:
        def __init__(self, rows):
            self.rows, self.status_code = rows, 200

        def json(self):
            return self.rows

    calls = []

    class FakeSession:
        def get(self, url, params=None, timeout=None):
            calls.append(params.get("startTime"))
            return FakeResponse(page1 if len(calls) == 1 else page2)

    candles, source = bt._fetch_binance_5m(ASSETS["BTC"], 30, FakeSession())
    assert len(candles) == 1010
    assert source == "binance:BTCUSDT"
    assert candles == sorted(candles, key=lambda item: item.timestamp)


def test_endpoint_and_nl_intent(monkeypatch):
    from marketobserver.nlp import parse_request
    assert parse_request("باك تست EURUSD 30").intent == "backtest"
    assert parse_request("backtest BTC").intent == "backtest"

    result = run(wave(), ASSETS["EURUSD"])
    monkeypatch.setattr(app, "run_backtest", lambda asset, days=30, payout=0.8, strict=True: result)
    client = app.app.test_client()
    response = client.get("/backtest/EURUSD?days=30&lang=en")
    assert response.status_code == 200
    assert response.json["asset"] == "EURUSD"
    assert "report" in response.json
    assert client.get("/backtest/EURUSD?payout=5").status_code == 400
    assert client.get("/backtest/not-an-asset").status_code == 404


SENT: list = []


@pytest.fixture()
def telegram(monkeypatch):
    SENT.clear()
    monkeypatch.setattr(app.bot, "reply_to", lambda message, text, **kwargs: SENT.append(text))
    monkeypatch.setattr(app.bot, "send_message", lambda chat_id, text, **kwargs: SENT.append(text))
    monkeypatch.setattr(app.bot, "send_chat_action", lambda *a, **k: None)
    monkeypatch.setattr(app.bot, "threaded", False)
    yield


def message_update(text, chat_id=4242, language="ar"):
    return app.types.Update.de_json(json.dumps({
        "update_id": 1,
        "message": {
            "message_id": 7, "date": 1789500000,
            "chat": {"id": chat_id, "type": "private", "first_name": "Tester"},
            "from": {"id": chat_id, "is_bot": False, "first_name": "Tester", "language_code": language},
            "text": text,
        },
    }))


def test_backtest_command_end_to_end(telegram, monkeypatch):
    result = run(wave(), ASSETS["EURUSD"])
    monkeypatch.setattr(app, "run_backtest", lambda asset, days=30, payout=0.8, strict=True: result)
    app.bot.process_new_updates([message_update("/backtest EURUSD 30")])
    assert len(SENT) == 2  # progress note + report
    assert "باك تست" in SENT[-1] and "EURUSD" in SENT[-1]
