"""Pro upgrade: catalog, live quotes, binary engine, NLP and HTTP endpoints.

Live venues are never hit here — HTTP is stubbed at the session level so the
fallback chain, TTL cache and parsers are exercised deterministically.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import app
from marketobserver.assets import ASSETS, assets_by_class, resolve_asset
from marketobserver.binary import decide, render, to_dict
from marketobserver.live import LivePriceProvider, QuoteUnavailable
from marketobserver.market_data import Candle
from marketobserver.nlp import parse_request


# ---------------- asset catalog ----------------

def test_catalog_covers_crypto_and_binary_forex():
    assert len(assets_by_class("crypto")) >= 20
    assert len(assets_by_class("forex")) >= 25
    for key in ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF",
                "NZDUSD", "EURJPY", "GBPJPY", "BTC", "DOGE", "XAUUSD"):
        assert key in ASSETS


def test_every_asset_resolves_by_key_and_has_unique_symbols():
    seen = set()
    for key, asset in ASSETS.items():
        assert resolve_asset(key).key == key
        assert resolve_asset(key.lower()).key == key
        if asset.binance:
            assert asset.binance not in seen
            seen.add(asset.binance)


def test_forex_and_crypto_aliases():
    assert resolve_asset("الباوند ين").key == "GBPJPY"
    assert resolve_asset("الدولار ين").key == "USDJPY"
    assert resolve_asset("استرالي دولار").key == "AUDUSD"
    assert resolve_asset("EUR/USD").key == "EURUSD"
    assert resolve_asset("gbp/jpy").key == "GBPJPY"
    assert resolve_asset("دوجكوين").key == "DOGE"
    assert resolve_asset("ريبل").key == "XRP"
    assert resolve_asset("تشين لينك").key == "LINK"


def test_short_tickers_do_not_misfire_inside_words():
    assert resolve_asset("what is stop loss?") is None
    assert resolve_asset("press the button") is None
    assert resolve_asset("not-an-asset") is None
    # ...but still resolve as standalone tokens.
    assert resolve_asset("buy OP now").key == "OP"
    assert resolve_asset("TON price").key == "TON"


# ---------------- live quotes ----------------

class FakeResponse:
    def __init__(self, payload=None, text="", status_code=200):
        self._payload = payload
        self.text = text
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_binance_quote_parses_and_caches(monkeypatch):
    provider = LivePriceProvider(ttl_seconds=60)
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(url)
        return FakeResponse({"symbol": "BTCUSDT", "price": "97432.50"})

    monkeypatch.setattr(provider.session, "get", fake_get)
    first = provider.get_quote(ASSETS["BTC"])
    second = provider.get_quote(ASSETS["BTC"])
    assert first.price == 97432.50
    assert first.source == "binance:BTCUSDT"
    assert second.age_seconds >= 0
    assert len(calls) == 1  # second read served from the TTL cache


def test_crypto_falls_back_to_kraken_when_binance_is_blocked(monkeypatch):
    provider = LivePriceProvider(ttl_seconds=60)

    def fake_get(url, params=None, timeout=None):
        if "binance" in url:
            return FakeResponse(status_code=451)  # geo-blocked host
        return FakeResponse({"result": {"XETHZUSD": {"c": ["3512.10", "1"]}}})

    monkeypatch.setattr(provider.session, "get", fake_get)
    quote = provider.get_quote(ASSETS["ETH"])
    assert quote.price == 3512.10
    assert quote.source == "kraken:ETHUSD"


def test_stooq_csv_parses_and_na_falls_through(monkeypatch):
    provider = LivePriceProvider(ttl_seconds=60)

    def fake_get(url, params=None, timeout=None):
        assert params["s"] == "eurusd"
        return FakeResponse(text="Symbol,Date,Time,Open,High,Low,Close,Volume\nEURUSD,2026-09-16,20:00:00,1.0851,1.0862,1.0849,1.0859,1000\n")

    monkeypatch.setattr(provider.session, "get", fake_get)
    quote = provider.get_quote(ASSETS["EURUSD"])
    assert quote.price == 1.0859
    assert quote.source == "stooq:eurusd"


def test_stooq_na_uses_yahoo_instead_of_failing(monkeypatch):
    provider = LivePriceProvider(ttl_seconds=60)

    def fake_get(url, params=None, timeout=None):
        return FakeResponse(text="Symbol,Date,Time,Open,High,Low,Close,Volume\nEURUSD,N/A,N/A,N/A,N/A,N/A,N/A,N/A\n")

    class FakeTicker:
        fast_info = {"last_price": 1.0840}

        def history(self, **kwargs):
            raise AssertionError("fast_info should win")

    monkeypatch.setattr(provider.session, "get", fake_get)
    monkeypatch.setattr("marketobserver.live.yf.Ticker", lambda symbol: FakeTicker())
    quote = provider.get_quote(ASSETS["EURUSD"])
    assert quote.price == 1.084
    assert quote.source.startswith("yahoo:")


def test_quote_unavailable_when_every_venue_fails(monkeypatch):
    provider = LivePriceProvider(ttl_seconds=60)

    def fake_get(url, params=None, timeout=None):
        raise RuntimeError("network down")

    class FakeTicker:
        fast_info = {}

        def history(self, **kwargs):
            import pandas as pd
            return pd.DataFrame()

    monkeypatch.setattr(provider.session, "get", fake_get)
    monkeypatch.setattr("marketobserver.live.yf.Ticker", lambda symbol: FakeTicker())
    with pytest.raises(QuoteUnavailable):
        provider.get_quote(ASSETS["EURUSD"])


# ---------------- binary engine ----------------

def trend_candles(start: float, step: float, count: int = 60) -> list[Candle]:
    # Realistic staircase trend: two steps with the trend, one pullback.
    # A monotonic series would peg RSI at 0/100, which the engine correctly
    # rejects as overbought/oversold; this pattern settles RSI near 67/33.
    base = datetime.now(timezone.utc) - timedelta(minutes=5 * count + 10)
    pattern = [step, -step, step]
    candles = []
    price = start
    for index in range(count):
        move = pattern[index % 3]
        price += move
        candles.append(Candle(base + timedelta(minutes=5 * index),
                              price - move, price + abs(move) * 0.2,
                              price - abs(move) * 0.2, price, 1000.0))
    return candles


def test_binary_call_on_aligned_uptrend():
    up_5m = trend_candles(1.0800, 0.0002)
    up_15m = trend_candles(1.0780, 0.0004)
    verdict = decide("EURUSD", "اليورو/الدولار", "EUR/USD", "USD", 5, up_5m, up_15m, "unit-test")
    assert verdict.verdict == "CALL"
    assert verdict.expiry_minutes == 15
    assert all(gate.passed for gate in verdict.gates)
    text = render(verdict, "ar")
    assert "CALL" in text and "15" in text


def test_binary_put_on_aligned_downtrend():
    down_5m = trend_candles(1.0900, -0.0002)
    down_15m = trend_candles(1.0920, -0.0004)
    verdict = decide("EURUSD", "اليورو/الدولار", "EUR/USD", "USD", 5, down_5m, down_15m, "unit-test")
    assert verdict.verdict == "PUT"
    assert "PUT" in render(verdict, "en")


def test_binary_waits_when_frames_disagree():
    up_5m = trend_candles(1.0800, 0.0002)
    down_15m = trend_candles(1.0920, -0.0004)
    verdict = decide("EURUSD", "اليورو/الدولار", "EUR/USD", "USD", 5, up_5m, down_15m, "unit-test")
    assert verdict.verdict == "WAIT"
    assert any(gate.name == "trend-agreement" and not gate.passed for gate in verdict.gates)


def test_binary_waits_without_enough_data():
    verdict = decide("EURUSD", "اليورو/الدولار", "EUR/USD", "USD", 5, trend_candles(1.08, 0.0001, 10), None, "unit-test")
    assert verdict.verdict == "WAIT"
    assert verdict.expiry_minutes is None
    payload = to_dict(verdict)
    assert payload["verdict"] == "WAIT" and payload["gates"]


def test_binary_waits_on_stale_bars_like_a_closed_market():
    up_5m = trend_candles(1.0800, 0.0002)
    up_15m = trend_candles(1.0780, 0.0004)
    # A weekend later the same perfect trend must NOT produce a verdict.
    verdict = decide("EURUSD", "اليورو/الدولار", "EUR/USD", "USD", 5, up_5m, up_15m, "unit-test",
                     now=datetime.now(timezone.utc) + timedelta(days=2))
    assert verdict.verdict == "WAIT"
    assert any(gate.name == "fresh-data" and not gate.passed for gate in verdict.gates)


# ---------------- NLP ----------------

def test_price_and_binary_intents_are_multilingual():
    request = parse_request("سعر الذهب كم؟")
    assert request.intent == "price" and request.asset.key == "XAUUSD"
    request = parse_request("BTC price")
    assert request.intent == "price" and request.asset.key == "BTC"
    request = parse_request("ثنائي EURUSD")
    assert request.intent == "binary" and request.asset.key == "EURUSD"
    request = parse_request("binary gold")
    assert request.intent == "binary" and request.asset.key == "XAUUSD"


def test_existing_intents_are_unaffected():
    request = parse_request("Should I buy BTC now?")
    assert request.intent == "advice" and request.asset.key == "BTC"
    request = parse_request("متى الوقت المناسب للدخول في الذهب؟")
    assert request.intent == "advice" and request.asset.key == "XAUUSD"


# ---------------- HTTP endpoints ----------------

def test_assets_endpoint_lists_the_catalog():
    client = app.app.test_client()
    response = client.get("/assets")
    assert response.status_code == 200
    payload = response.json
    assert payload["count"] >= 60
    assert "EURUSD" in [row["key"] for row in payload["classes"]["forex"]]
    forex_only = client.get("/assets?class=forex")
    assert set(forex_only.json["classes"]) == {"forex"}


def test_price_endpoint_uses_the_live_quote(monkeypatch):
    from marketobserver.live import Quote
    quote = Quote("EURUSD", 1.0859, "stooq:eurusd", datetime.now(timezone.utc), 0.4)
    monkeypatch.setattr(app.live, "get_quote", lambda asset: quote)
    client = app.app.test_client()
    response = client.get("/price/EURUSD")
    assert response.status_code == 200
    assert response.json["price"] == 1.0859
    assert response.json["source"] == "stooq:eurusd"
    assert client.get("/price/not-an-asset").status_code == 404


def test_binary_endpoint_renders_both_languages(monkeypatch):
    verdict = decide("EURUSD", "اليورو/الدولار", "EUR/USD", "USD", 5,
                     trend_candles(1.0800, 0.0002), trend_candles(1.0780, 0.0004), "unit-test")
    monkeypatch.setattr(app, "binary_verdict_for", lambda asset, force=False: verdict)
    client = app.app.test_client()
    response = client.get("/binary/EURUSD?lang=en")
    assert response.status_code == 200
    assert response.json["verdict"] == "CALL"
    assert "CALL" in response.json["report"]
    assert client.get("/binary/EURUSD?lang=xx").status_code == 400


# ---------------- Telegram commands ----------------

SENT: list = []


@pytest.fixture()
def telegram(monkeypatch):
    SENT.clear()
    app.smc_prefs.clear()
    app.binary_cache.clear()
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


def test_price_command_replies_with_source_and_age(telegram, monkeypatch):
    from marketobserver.live import Quote
    monkeypatch.setattr(app.live, "get_quote",
                        lambda asset: Quote(asset.key, 1.0859, "stooq:eurusd", datetime.now(timezone.utc), 0.2))
    app.bot.process_new_updates([message_update("/price EURUSD")])
    assert SENT and "1.0859" in SENT[-1] and "stooq" in SENT[-1]


def test_binary_command_and_natural_language(telegram, monkeypatch):
    verdict = decide("EURUSD", "اليورو/الدولار", "EUR/USD", "USD", 5,
                     trend_candles(1.0800, 0.0002), trend_candles(1.0780, 0.0004), "unit-test")
    monkeypatch.setattr(app, "binary_verdict_for", lambda asset, force=False: verdict)
    app.bot.process_new_updates([message_update("/binary EURUSD")])
    assert SENT and "CALL" in SENT[-1]
    SENT.clear()
    app.bot.process_new_updates([message_update("ثنائي EURUSD")])
    assert SENT and "CALL" in SENT[-1]


def test_assets_command_lists_groups(telegram):
    app.bot.process_new_updates([message_update("/assets forex")])
    assert SENT and "EURUSD" in SENT[-1] and "GBPJPY" in SENT[-1]


def test_binary_keyboard_respects_telegram_limits():
    for lang in ("ar", "en"):
        keyboard = app.binary_keyboard("EURUSD", lang)
        assert len(keyboard.keyboard) <= 100
        for row in keyboard.keyboard:
            assert len(row) <= 10
            for button in row:
                assert 1 <= len((button.callback_data or "").encode("utf-8")) <= 64
