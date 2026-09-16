"""End-to-end checks for the strict /decision layer (BUY / SELL / WAIT).

The handlers are driven through `bot.process_new_updates`, so command parsing,
the inline buttons, the bilingual render and the HTTP endpoint all run against
the real gate chain. Only the transport and the candle provider are mocked, and
the candles come from the same deterministic fixture the engine tests use.
"""

import json
from datetime import datetime, timezone

import pytest

import app
from marketobserver.decision import decide
from test_decision import build_long_setup

CALLBACKS: list = []
SENT: list = []
EDITS: list = []


@pytest.fixture(autouse=True)
def transport(monkeypatch):
    CALLBACKS.clear(); SENT.clear(); EDITS.clear()
    app.smc_prefs.clear(); app.smc_reports.clear(); app.decision_cache.clear()

    def reply_to(message, text, **kwargs):
        SENT.append(("reply", text, kwargs))

    def send_message(chat_id, text, **kwargs):
        SENT.append(("send", text, kwargs))

    def edit_message_text(text, chat_id=None, message_id=None, **kwargs):
        EDITS.append((text, message_id, kwargs))

    def answer_callback_query(callback_id, text=None, **kwargs):
        CALLBACKS.append((callback_id, text, kwargs))

    monkeypatch.setattr(app.bot, "reply_to", reply_to)
    monkeypatch.setattr(app.bot, "send_message", send_message)
    monkeypatch.setattr(app.bot, "edit_message_text", edit_message_text)
    monkeypatch.setattr(app.bot, "answer_callback_query", answer_callback_query)
    monkeypatch.setattr(app.bot, "send_chat_action", lambda *a, **k: None)
    monkeypatch.setattr(app.bot, "threaded", False)

    now, candles = build_long_setup()

    def fake_candles(asset, force=False):
        if getattr(fake_candles, "calls", 0) == 0 or force:
            fake_candles.calls = getattr(fake_candles, "calls", 0) + 1
            decision = decide(asset.key, asset.name_ar, asset.name_en, asset.quote, asset.price_decimals,
                              candles, "unit-test", now=now)
            fake_candles.payload = (decision, candles)
        return fake_candles.payload

    monkeypatch.setattr(app, "decision_candles", fake_candles)
    yield candles


def message_update(text, chat_id=4242, language="en"):
    return app.types.Update.de_json(json.dumps({
        "update_id": 1,
        "message": {
            "message_id": 7, "date": 1789500000,
            "chat": {"id": chat_id, "type": "private", "first_name": "Tester"},
            "from": {"id": chat_id, "is_bot": False, "first_name": "Tester", "language_code": language},
            "text": text,
        },
    }))


def callback_update(data, chat_id=4242):
    return app.types.Update.de_json(json.dumps({
        "update_id": 2,
        "callback_query": {
            "id": "cb1", "chat_instance": "ci",
            "from": {"id": chat_id, "is_bot": False, "first_name": "Tester", "language_code": "en"},
            "message": {"message_id": 9, "date": 1789500000,
                        "chat": {"id": chat_id, "type": "private", "first_name": "Tester"}, "text": "previous"},
            "data": data,
        },
    }))


def last_text() -> str:
    assert SENT, "nothing was sent"
    return SENT[-1][1]


def test_decision_command_renders_the_strict_block():
    app.bot.process_new_updates([message_update("/decision BTC")])
    text = last_text()
    assert "🧠 Smart Analysis | BTC" in text
    assert "🎯 FINAL DECISION: BUY" in text
    assert "Risk/Reward:" in text and ": 1" in text


def test_decision_command_supports_language_and_gates():
    app.bot.process_new_updates([message_update("/decision BTC ar gates")])
    text = last_text()
    assert "🎯 القرار النهائي: شراء" in text
    assert "سلسلة البوابات" in text
    rendered = [item[1] for item in SENT if item[0] == "send"]
    assert any("القرار" in item for item in rendered) or "القرار النهائي" in text


def test_decision_command_without_asset_uses_the_chat_asset():
    # no asset supplied: the chat's stored asset is used, exactly like /analyze
    app.bot.process_new_updates([message_update("/decision", chat_id=9191)])
    assert "🎯 FINAL DECISION:" in last_text()


def test_natural_language_verdict_request_reaches_the_engine():
    app.bot.process_new_updates([message_update("buy or sell BTC?")])
    assert "🎯 FINAL DECISION: BUY" in last_text()


def test_decision_buttons_switch_language_and_gates():
    app.bot.process_new_updates([message_update("/decision BTC")])
    app.bot.process_new_updates([callback_update("dec:run:BTC:ar:1:0")])
    assert EDITS or SENT
    latest = EDITS[-1][0] if EDITS else last_text()
    assert "🎯 القرار النهائي" in latest


def test_decision_endpoint_returns_json_and_the_rendered_block(monkeypatch):
    client = app.app.test_client()
    response = client.get("/decision/BTC?lang=en&gates=1")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["decision"] == "buy"
    assert payload["side"] == "long"
    assert payload["risk_reward"] >= 2
    assert payload["steps"][0]["number"] == 1
    assert "🎯 FINAL DECISION: BUY" in payload["report"]
    assert "BTC" in payload["summary"]


def test_decision_endpoint_rejects_unknown_asset_and_language():
    client = app.app.test_client()
    assert client.get("/decision/not-an-asset").status_code == 404
    assert client.get("/decision/BTC?lang=fr").status_code == 400
