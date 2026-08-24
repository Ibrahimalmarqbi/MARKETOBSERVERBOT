from datetime import datetime, timezone

import pytest

from marketobserver.analysis import rsi_wilder, analyze
from marketobserver.assets import resolve_asset
from marketobserver.db import Database
from marketobserver.market_data import Candle
from marketobserver.risk import calculate_position_size
from marketobserver.nlp import parse_request


def candle(value: float, index: int) -> Candle:
    return Candle(datetime(2026, 1, index + 1, tzinfo=timezone.utc), value, value + 1, value - 1, value, 100)


def test_asset_aliases_are_normalized():
    assert resolve_asset("تحليل الذهب").key == "XAUUSD"
    assert resolve_asset("EUR/USD").key == "EURUSD"
    assert resolve_asset("nvidia").key == "NVDA"
    assert resolve_asset("not-an-asset") is None


def test_rsi_constant_series_is_neutral():
    assert rsi_wilder([100.0] * 30) == 50.0


def test_analysis_requires_enough_data():
    with pytest.raises(ValueError):
        analyze([candle(100 + i, i) for i in range(20)])


def test_risk_is_capped_and_validated():
    result = calculate_position_size(10000, 1, 100, 90, max_notional=500)
    assert result.risk_amount == 100
    assert result.notional == 500
    with pytest.raises(ValueError):
        calculate_position_size(10000, 3, 100, 90, max_risk_percent=2)


def test_natural_language_request_is_multilingual():
    request = parse_request("متى الوقت المناسب للدخول في الذهب؟")
    assert request.language == "ar"
    assert request.intent == "advice"
    assert request.asset.key == "XAUUSD"


def test_english_natural_language_request():
    request = parse_request("Should I buy BTC now?")
    assert request.language == "en"
    assert request.intent == "advice"
    assert request.asset.key == "BTC"


def test_alerts_are_scoped_to_user(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'test.db'}")
    db.create_all()
    db.upsert_user(1, "u1", "ar")
    db.upsert_user(2, "u2", "en")
    alert = db.add_alert(1, "BTC", 60000, "below")
    assert len(db.list_alerts(1)) == 1
    assert db.cancel_alert(2, alert.id) is False
    assert db.cancel_alert(1, alert.id) is True