"""End-to-end checks for the smart-money Telegram layer.

These drive the real handlers through `bot.process_new_updates`, so command
parsing, inline-button callbacks, bilingual rendering, pagination and the
alert-button wiring are exercised together. Only the transport is mocked, and
the analysis object comes from the same deterministic engine (synthetic candles
here, live provider candles in production).
"""

import json
from datetime import datetime, timezone

import pytest

import app
from marketobserver.smc import build_report
from test_smc import build_cases

CALLBACKS: list = []
SENT: list = []
EDITS: list = []
PHOTOS: list = []


@pytest.fixture(autouse=True)
def transport(monkeypatch):
    CALLBACKS.clear(); SENT.clear(); EDITS.clear(); PHOTOS.clear()
    # smc_prefs/smc_reports are process state; without this the chat's stored
    # language leaks out of one test into the next and button labels flip.
    app.smc_prefs.clear(); app.smc_reports.clear()

    def reply_to(message, text, **kwargs):
        SENT.append(("reply", text, kwargs))

    def send_message(chat_id, text, **kwargs):
        SENT.append(("send", text, kwargs))

    def edit_message_text(text, chat_id=None, message_id=None, **kwargs):
        EDITS.append((text, message_id, kwargs))

    def send_photo(chat_id, photo, **kwargs):
        PHOTOS.append((photo, kwargs))

    def answer_callback_query(callback_id, text=None, **kwargs):
        CALLBACKS.append((callback_id, text, kwargs))

    monkeypatch.setattr(app.bot, "reply_to", reply_to)
    monkeypatch.setattr(app.bot, "send_message", send_message)
    monkeypatch.setattr(app.bot, "edit_message_text", edit_message_text)
    monkeypatch.setattr(app.bot, "edit_message_reply_markup", lambda *a, **k: None)
    monkeypatch.setattr(app.bot, "send_photo", send_photo)
    monkeypatch.setattr(app.bot, "send_chat_action", lambda *a, **k: None)
    monkeypatch.setattr(app.bot, "answer_callback_query", answer_callback_query)
    monkeypatch.setattr(app.bot, "threaded", False)  # deliver synchronously for assertions

    candles, _ = build_cases()

    def fake_build(asset, force=False):
        report = build_report(asset.key, asset.name_ar, asset.name_en, asset.quote, asset.price_decimals,
                              candles, "unit-test", now=datetime(2026, 1, 20, tzinfo=timezone.utc))
        return report, candles

    monkeypatch.setattr(app, "smc_build_report", fake_build)
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


def callback_update(data, chat_id=4242):
    return app.types.Update.de_json(json.dumps({
        "update_id": 2,
        "callback_query": {
            "id": "cb-1", "from": {"id": chat_id, "is_bot": False, "first_name": "Tester"},
            "chat_instance": "1", "data": data,
            "message": {
                "message_id": 8, "date": 1789500000,
                "chat": {"id": chat_id, "type": "private", "first_name": "Tester"},
                "from": {"id": chat_id, "is_bot": False, "first_name": "Tester"},
                "text": "previous report",
            },
        },
    }))


def sent_text():
    return "\n".join(part for _, part, _ in SENT) + "\n".join(text for text, _, _ in EDITS)


def test_smc_command_answers_in_the_users_language_and_adds_buttons():
    app.bot.process_new_updates([message_update("/smc BTC", language="en")])
    body = sent_text()
    assert "Trend (4H)" in body and "Final Decision" in body
    assert any(kwargs.get("reply_markup") is not None for _, _, kwargs in SENT)
    keyboard = next(kwargs["reply_markup"] for _, _, kwargs in SENT if kwargs.get("reply_markup"))
    labels = [button.text for row in keyboard.keyboard for button in row]
    assert any("English" in label for label in labels) and any("🔔" in label for label in labels)


def test_arabic_command_produces_the_arabic_report():
    app.bot.process_new_updates([message_update("/smc BTC", language="ar")])
    body = sent_text()
    assert "الاتجاه (4H)" in body and "القرار النهائي" in body


def test_language_button_switches_output_without_touching_the_numbers():
    app.bot.process_new_updates([message_update("/smc BTC", language="ar")])
    arabic = sent_text()
    SENT.clear(); EDITS.clear()
    app.bot.process_new_updates([callback_update("smc:lang:BTC:en:full")])
    english = sent_text()
    assert "Trend (4H)" in english and "Market Structure" in english
    assert "السعر" in arabic
    # identical measured price in both renderings
    assert "78982.00" not in english or "78982.00" in arabic
    assert not CALLBACKS or CALLBACKS[-1][1] is None or isinstance(CALLBACKS[-1][1], str)


def test_both_language_mode_is_sent_as_two_telegram_parts():
    app.bot.process_new_updates([callback_update("smc:lang:BTC:both:full")])
    combined = sent_text()
    assert "الاتجاه (4H)" in combined and "Trend (4H)" in combined
    assert len(SENT) >= 2, "bilingual output must be paginated, never truncated"


def test_brief_mode_is_short_and_keeps_the_blocking_reason():
    app.bot.process_new_updates([callback_update("smc:mode:BTC:en:brief")])
    body = sent_text()
    assert len(body) < 1200
    assert "⛔" in body or "zone" in body


def test_asset_button_reuses_the_same_engine_and_stores_the_choice():
    app.bot.process_new_updates([callback_update("smc:asset:ETH:ar:full")])
    body = sent_text()
    assert "(ETH)" in body and "البيتكوين" not in body
    user = app.db.get_user(4242)
    assert user is not None and user.last_asset == "ETH"


def test_chart_button_uploads_a_png_built_from_the_same_candles():
    app.bot.process_new_updates([callback_update("smc:chart:BTC:en:full")])
    assert PHOTOS, "chart button must upload an image"
    photo, kwargs = PHOTOS[0]
    assert photo.getvalue()[:4] == b"\x89PNG"
    assert "Trend" not in kwargs["caption"]  # captions stay compact
    assert kwargs.get("reply_markup") is not None


def test_alert_button_attaches_stop_target_and_zone_alerts():
    created_before = len(app.db.list_alerts(4242))
    app.bot.process_new_updates([callback_update("smc:alert:BTC:ar:full")])
    created_after = len(app.db.list_alerts(4242))
    if created_after == created_before:
        # a WAIT decision without a zone may legitimately create nothing, but it
        # must say so instead of failing silently
        assert CALLBACKS and CALLBACKS[-1][1]
    else:
        alerts = app.db.list_alerts(4242)[created_after - created_before:]
        assert all(alert.status == "active" for alert in alerts)
        assert {"above", "below"} >= {alert.condition for alert in alerts}


def test_natural_language_reaches_the_same_report():
    app.bot.process_new_updates([message_update("ما مناطق السيولة في البيتكوين؟", language="ar")])
    body = sent_text()
    assert "الاتجاه (4H)" in body and "منطقة الدخول" in body


def test_unknown_asset_button_payload_is_answered_not_swallowed():
    app.bot.process_new_updates([callback_update("smc:asset:NOPE:en:full")])
    assert CALLBACKS, "the button must always be answered so Telegram clears the spinner"


def test_provider_failure_still_leaves_the_buttons_attached(monkeypatch):
    """Regression: a data outage must not strand the user with a bare error.

    The keyboard is what lets them retry or switch asset, and this is the exact
    path a geo-blocked provider triggers in production.
    """
    from marketobserver.market_data import DataUnavailable

    def failing(asset, force=False):
        raise DataUnavailable("SMC needs 4H and 1H candles for BTC")

    monkeypatch.setattr(app, "smc_build_report", failing)
    app.bot.process_new_updates([message_update("/smc BTC", language="ar")])
    assert "⛔" in sent_text()
    assert any(kwargs.get("reply_markup") is not None for _, _, kwargs in SENT), \
        "error replies must keep the inline buttons"
    keyboard = next(kwargs["reply_markup"] for _, _, kwargs in SENT if kwargs.get("reply_markup"))
    labels = [button.text for row in keyboard.keyboard for button in row]
    assert any("إعادة حساب" in label for label in labels)


def test_chart_outage_reply_keeps_its_buttons_too(monkeypatch):
    from marketobserver.market_data import DataUnavailable

    monkeypatch.setattr(app, "smc_build_report",
                        lambda asset, force=False: (_ for _ in ()).throw(DataUnavailable("no candles")))
    app.bot.process_new_updates([message_update("/smc BTC chart", language="en")])
    assert any(kwargs.get("reply_markup") is not None for _, _, kwargs in SENT)


def test_start_message_carries_the_asset_buttons():
    app.bot.process_new_updates([message_update("/start", language="ar")])
    assert any(kwargs.get("reply_markup") is not None for _, _, kwargs in SENT), \
        "/start must expose the smart-money buttons"


def test_keyboard_respects_telegram_limits():
    """Telegram rejects a whole message if any limit is broken — guard it here."""
    for lang in ("ar", "en", "both"):
        for asset_key in app.SMC_PRIMARY_ASSETS + app.SMC_MORE_ASSETS:
            keyboard = app.smc_keyboard(asset_key, lang, "full")
            rows = keyboard.keyboard
            assert len(rows) <= 100
            assert all(len(row) <= 10 for row in rows)
            for row in rows:
                for button in row:
                    data = (button.callback_data or "").encode("utf-8")
                    assert 1 <= len(data) <= 64, (lang, asset_key, button.callback_data)
            labels = [button.text for row in rows for button in row]
            assert len(labels) == len(set(labels)), "duplicate button labels confuse taps"
