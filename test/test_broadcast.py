import io
import json
from types import SimpleNamespace
from dataclasses import replace

import pytest
from sqlalchemy import delete
from telebot.apihelper import ApiTelegramException

import app
from marketobserver.db import User


ADMIN_CHAT = 999


class Chat:
    def __init__(self, chat_id):
        self.id = chat_id


class FromUser:
    def __init__(self, language_code):
        self.language_code = language_code
        self.username = None


class Message:
    def __init__(self, text, chat_id=ADMIN_CHAT, language_code="ar"):
        self.text = text
        self.chat = Chat(chat_id)
        self.from_user = FromUser(language_code)


@pytest.fixture(autouse=True)
def clean_users():
    with app.db.session() as s:
        s.execute(delete(User))
    yield
    with app.db.session() as s:
        s.execute(delete(User))


def telegram_update(text, chat_id=ADMIN_CHAT, update_id=1):
    return app.types.Update.de_json(json.dumps({
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1757500000,
            "chat": {"id": chat_id, "type": "private", "first_name": "Tester"},
            "from": {"id": chat_id, "is_bot": False, "first_name": "Tester"},
            "text": text,
        },
    }))


def blocked_error():
    return ApiTelegramException(
        "sendMessage", "result", {"error_code": 403, "description": "Forbidden: bot was blocked by the user"}
    )


def test_split_broadcast_text_keeps_every_chunk_within_telegram_limit():
    text = "\n".join(f"line {index}" for index in range(900))
    chunks = app.split_broadcast_text(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= app.TELEGRAM_TEXT_LIMIT for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")
    assert app.split_broadcast_text("   ") == []


def test_split_broadcast_text_cuts_a_single_long_line():
    chunks = app.split_broadcast_text("x" * 9000)
    assert all(len(chunk) <= app.TELEGRAM_TEXT_LIMIT for chunk in chunks)
    assert "".join(chunks) == "x" * 9000


def test_broadcast_text_reaches_every_active_user(monkeypatch):
    app.db.upsert_user(111, "u111", "ar")
    app.db.upsert_user(222, "u222", "ar")
    app.db.upsert_user(333, "u333", "ar")
    app.db.set_user_active(333, False)

    delivered = []
    monkeypatch.setattr(app.bot, "send_message", lambda chat_id, text, **kwargs: delivered.append((chat_id, text)))

    result = app.broadcast_text("hello everyone", delay=0)
    assert result == {"users": 2, "sent": 2, "failed": 0, "deactivated": 0}
    assert sorted(delivered) == [(111, "hello everyone"), (222, "hello everyone")]


def test_broadcast_text_deactivates_users_who_blocked_the_bot(monkeypatch):
    app.db.upsert_user(111, "u111", "ar")
    app.db.upsert_user(222, "u222", "ar")

    def fake_send(chat_id, text, **kwargs):
        if chat_id == 222:
            raise blocked_error()
        return True

    monkeypatch.setattr(app.bot, "send_message", fake_send)

    result = app.broadcast_text("hello", delay=0)
    assert result == {"users": 2, "sent": 1, "failed": 1, "deactivated": 1}
    assert app.db.get_user(111).is_active is True
    assert app.db.get_user(222).is_active is False


def test_broadcast_command_sends_through_the_real_update_pipeline(monkeypatch):
    app.db.upsert_user(111, "u111", "ar")
    app.db.upsert_user(222, "u222", "ar")
    monkeypatch.setattr(app, "settings", replace(app.settings, admin_chat_ids=(ADMIN_CHAT,)))

    delivered = []
    replies = []
    monkeypatch.setattr(app.bot, "send_message", lambda chat_id, text, **kwargs: delivered.append((chat_id, text)))
    monkeypatch.setattr(app.bot, "reply_to", lambda message, text, **kwargs: replies.append(text))
    monkeypatch.setattr(app.bot, "threaded", False)

    app.bot.process_new_updates([telegram_update("/broadcast إعلان مهم للجميع")])

    # The admin is registered as a user too, so they receive their own announcement.
    assert sorted(delivered) == [(111, "إعلان مهم للجميع"), (222, "إعلان مهم للجميع"), (ADMIN_CHAT, "إعلان مهم للجميع")]
    assert any("3" in reply for reply in replies)


def test_broadcast_command_rejects_non_admin(monkeypatch):
    app.db.upsert_user(111, "u111", "ar")
    delivered = []
    replies = []
    monkeypatch.setattr(app.bot, "send_message", lambda chat_id, text, **kwargs: delivered.append((chat_id, text)))
    monkeypatch.setattr(app.bot, "reply_to", lambda message, text, **kwargs: replies.append(text))

    app.broadcast_cmd(Message("/broadcast spam", chat_id=555, language_code="en"))

    assert delivered == []
    assert replies == ["This command is restricted to admins."]


def test_broadcast_command_allows_users_with_admin_role(monkeypatch):
    app.db.upsert_user(111, "u111", "ar")
    app.db.upsert_user(777, "boss", "ar")
    app.db.set_user_role(777, "admin")

    delivered = []
    monkeypatch.setattr(app.bot, "send_message", lambda chat_id, text, **kwargs: delivered.append((chat_id, text)))
    monkeypatch.setattr(app.bot, "reply_to", lambda message, text, **kwargs: None)

    app.broadcast_cmd(Message("/broadcast تحديث النظام", chat_id=777))

    assert sorted(delivered) == [(111, "تحديث النظام"), (777, "تحديث النظام")]


def test_broadcast_command_requires_a_message_body(monkeypatch):
    replies = []
    delivered = []
    monkeypatch.setattr(app, "settings", replace(app.settings, admin_chat_ids=(ADMIN_CHAT,)))
    monkeypatch.setattr(app.bot, "send_message", lambda chat_id, text, **kwargs: delivered.append((chat_id, text)))
    monkeypatch.setattr(app.bot, "reply_to", lambda message, text, **kwargs: replies.append(text))

    app.broadcast_cmd(Message("/broadcast"))

    assert delivered == []
    assert replies and "الاستخدام" in replies[0]


def test_unknown_command_is_answered_not_ignored(monkeypatch):
    replies = []
    monkeypatch.setattr(app.bot, "reply_to", lambda message, text, **kwargs: replies.append(text))

    app.text_cmd(Message("/nonexistent", chat_id=555))

    assert replies and "/broadcast" in replies[0]


def test_admin_broadcast_endpoint_requires_key_and_delivers(monkeypatch):
    app.db.upsert_user(111, "u111", "ar")
    app.db.upsert_user(222, "u222", "ar")
    delivered = []
    monkeypatch.setattr(app.bot, "send_message", lambda chat_id, text, **kwargs: delivered.append((chat_id, text)))

    client = app.app.test_client()
    assert client.post("/admin/broadcast", json={"text": "hi"}).status_code == 401
    assert client.post("/admin/broadcast", json={}, headers={"X-Admin-Key": "test-admin-key"}).status_code == 400

    response = client.post("/admin/broadcast", json={"text": "hi"}, headers={"X-Admin-Key": "test-admin-key"})
    assert response.status_code == 200
    assert response.json == {"users": 2, "sent": 2, "failed": 0, "deactivated": 0}
    assert sorted(delivered) == [(111, "hi"), (222, "hi")]


def test_admin_broadcast_endpoint_accepts_explicit_chat_ids(monkeypatch):
    app.db.upsert_user(111, "u111", "ar")
    app.db.upsert_user(222, "u222", "ar")
    delivered = []
    monkeypatch.setattr(app.bot, "send_message", lambda chat_id, text, **kwargs: delivered.append((chat_id, text)))

    response = app.app.test_client().post(
        "/admin/broadcast",
        json={"text": "targeted", "chat_ids": [222]},
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert response.json == {"users": 1, "sent": 1, "failed": 0, "deactivated": 0}
    assert delivered == [(222, "targeted")]


def telegram_photo_update(caption, chat_id=ADMIN_CHAT):
    payload = {
        "message_id": 1,
        "date": 1757500000,
        "chat": {"id": chat_id, "type": "private", "first_name": "Tester"},
        "from": {"id": chat_id, "is_bot": False, "first_name": "Tester", "language_code": "en"},
        "photo": [
            {"file_id": "small-photo", "file_unique_id": "small", "width": 90, "height": 90},
            {"file_id": "large-photo", "file_unique_id": "large", "width": 800, "height": 800},
        ],
    }
    if caption is not None:
        payload["caption"] = caption
    return app.types.Update.de_json(json.dumps({"update_id": 1, "message": payload}))


@pytest.fixture
def photo_delivery(monkeypatch):
    events, replies = [], []

    def send_photo(chat_id, photo, caption=None, **kwargs):
        events.append(("photo", chat_id, photo, caption))
        return SimpleNamespace(photo=[SimpleNamespace(file_id="cached-photo")])

    monkeypatch.setattr(app.bot, "send_photo", send_photo)
    monkeypatch.setattr(app.bot, "send_message", lambda chat_id, text, **kwargs: events.append(("text", chat_id, text)))
    monkeypatch.setattr(app.bot, "reply_to", lambda message, text, **kwargs: replies.append(text))
    monkeypatch.setattr(app.bot, "threaded", False)
    monkeypatch.setattr(app.time, "sleep", lambda delay: None)
    monkeypatch.setattr(app, "settings", replace(app.settings, admin_chat_ids=(ADMIN_CHAT,)))
    return events, replies


@pytest.mark.parametrize("command", ["/broadcast", "/broadcast_photo", "/broadcast@MarketObserverBot", "/broadcast_photo@MarketObserverBot"])
@pytest.mark.parametrize("admin_source", ["settings", "role"])
def test_photo_command_through_update_pipeline(command, admin_source, monkeypatch, photo_delivery):
    events, replies = photo_delivery
    app.db.upsert_user(111, "active", "ar")
    app.db.upsert_user(222, "inactive", "ar")
    app.db.set_user_active(222, False)
    if admin_source == "role":
        monkeypatch.setattr(app, "settings", replace(app.settings, admin_chat_ids=()))
        app.db.upsert_user(ADMIN_CHAT, "admin", "en")
        app.db.set_user_role(ADMIN_CHAT, "admin")

    app.bot.process_new_updates([telegram_photo_update(f"{command} إعلان بصورة")])

    assert events == [
        ("photo", 111, "large-photo", "إعلان بصورة"),
        ("photo", ADMIN_CHAT, "large-photo", "إعلان بصورة"),
    ]
    assert "Delivered to 2 of 2" in replies[-1]


@pytest.mark.parametrize("command", ["/broadcast", "/broadcast_photo"])
def test_photo_command_rejects_non_admin_through_update_pipeline(command, photo_delivery):
    events, replies = photo_delivery
    app.db.upsert_user(111, "active", "ar")
    app.bot.process_new_updates([telegram_photo_update(f"{command} spam", chat_id=555)])
    assert events == []
    assert replies == ["This command is restricted to admins."]


@pytest.mark.parametrize("caption", [None, "", "ordinary photo", "Look /broadcast hi", "/broadcasting hi", "/broadcast_photo_extra hi"])
def test_unrelated_photos_are_not_broadcast(caption, photo_delivery):
    events, replies = photo_delivery
    app.db.upsert_user(111, "active", "ar")
    app.bot.process_new_updates([telegram_photo_update(caption)])
    assert events == []
    assert replies == []


@pytest.mark.parametrize("caption", ["", "ص" * 1024, "ص" * 1025, "ص" * 1024 + "first\n" + "x" * 9000 + "\nlast"])
def test_photo_caption_limit_and_overflow_order_through_pipeline(caption, photo_delivery):
    events, _ = photo_delivery
    app.db.upsert_user(111, "active", "ar")
    app.bot.process_new_updates([telegram_photo_update("/broadcast_photo " + caption)])
    for chat_id in (111, ADMIN_CHAT):
        deliveries = [event for event in events if event[1] == chat_id]
        assert deliveries[0] == ("photo", chat_id, "large-photo", caption[:1024])
        assert all(event[0] == "text" and len(event[2]) <= 4096 for event in deliveries[1:])
        assert deliveries[0][3] + "".join(event[2] for event in deliveries[1:]) == caption
        assert len(deliveries) == 1 + (max(0, len(caption) - 1024) + 4095) // 4096


@pytest.mark.parametrize("failure_stage", ["photo", "text"])
def test_photo_broadcast_403_through_update_pipeline(failure_stage, monkeypatch, photo_delivery):
    events, replies = photo_delivery
    for chat_id in (111, 222):
        app.db.upsert_user(chat_id, None, "en")
    method = "send_photo" if failure_stage == "photo" else "send_message"
    original = getattr(app.bot, method)

    def fail_blocked(chat_id, *args, **kwargs):
        if chat_id == 111:
            raise blocked_error()
        return original(chat_id, *args, **kwargs)

    monkeypatch.setattr(app.bot, method, fail_blocked)
    app.bot.process_new_updates([telegram_photo_update("/broadcast " + "x" * 1025)])
    assert app.db.get_user(111).is_active is False
    assert app.db.get_user(222).is_active is True
    assert ("photo", 222, "large-photo", "x" * 1024) in events
    assert "Delivered to 2 of 3. Failed: 1. Deactivated 1" in replies[-1]


@pytest.mark.parametrize("source", ["url", "upload"])
def test_photo_broadcast_caches_first_successful_file_id(source, monkeypatch, photo_delivery):
    photo = "https://example.com/photo.jpg" if source == "url" else io.BytesIO(b"photo bytes")
    attempts = []

    def send_photo(chat_id, value, **kwargs):
        attempts.append(value)
        if source == "upload" and not isinstance(value, str):
            assert value.read() == b"photo bytes"
        if chat_id == 111:
            raise blocked_error()
        return SimpleNamespace(photo=[SimpleNamespace(file_id="small-cache"), SimpleNamespace(file_id="large-cache")])

    monkeypatch.setattr(app.bot, "send_photo", send_photo)
    for chat_id in (111, 222, 333, 444):
        app.db.upsert_user(chat_id, None, "en")
    result = app.broadcast_photo(photo, "hi", delay=0)
    assert attempts == [photo, photo, "large-cache", "large-cache"]
    assert result == {"users": 4, "sent": 3, "failed": 1, "deactivated": 1}


def test_photo_cache_survives_overflow_failure(monkeypatch, photo_delivery):
    events, _ = photo_delivery

    def fail_text(chat_id, text, **kwargs):
        if chat_id == 111:
            raise RuntimeError("temporary network failure")

    monkeypatch.setattr(app.bot, "send_message", fail_text)
    app.db.upsert_user(111, None, "en")
    result = app.broadcast_photo("https://example.com/photo.jpg", "x" * 1025, [111, 222], delay=0)
    assert [event[2] for event in events] == ["https://example.com/photo.jpg", "cached-photo"]
    assert result == {"users": 2, "sent": 1, "failed": 1, "deactivated": 0}
    assert app.db.get_user(111).is_active is True


@pytest.mark.parametrize("chat_ids", [None, []])
def test_photo_broadcast_without_recipients(chat_ids, photo_delivery):
    events, _ = photo_delivery
    if chat_ids == []:
        app.db.upsert_user(111, None, "en")
    assert app.broadcast_photo("photo-id", "", chat_ids, delay=0) == {"users": 0, "sent": 0, "failed": 0, "deactivated": 0}
    assert events == []


def test_photo_broadcast_delays_each_message(monkeypatch, photo_delivery):
    sleeps = []
    monkeypatch.setattr(app.time, "sleep", sleeps.append)
    app.broadcast_photo("photo-id", "x" * (1024 + 4096 + 1), [111, 222], delay=0.1)
    assert sleeps == [0.1] * 6


@pytest.mark.parametrize("field,value", [("photo_url", "https://example.com/photo.jpg"), ("photo_file_id", "existing-photo")])
@pytest.mark.parametrize("caption_field", ["text", "caption"])
def test_admin_broadcast_photo_endpoint(field, value, caption_field, photo_delivery):
    events, _ = photo_delivery
    for chat_id in (111, 222, 333):
        app.db.upsert_user(chat_id, None, "en")
    app.db.set_user_active(333, False)
    payload = {field: value, caption_field: "x" * 1025}
    client = app.app.test_client()
    assert client.post("/admin/broadcast", json=payload).status_code == 401
    assert events == []
    response = client.post("/admin/broadcast", json=payload, headers={"X-Admin-Key": "test-admin-key"})
    assert response.status_code == 200
    assert response.json == {"users": 2, "sent": 2, "failed": 0, "deactivated": 0}
    assert events == [
        ("photo", 111, value, "x" * 1024), ("text", 111, "x"),
        ("photo", 222, "cached-photo" if field == "photo_url" else value, "x" * 1024), ("text", 222, "x"),
    ]


def test_admin_broadcast_photo_without_caption_and_explicit_chat_ids(photo_delivery):
    events, _ = photo_delivery
    for chat_id in (111, 222):
        app.db.upsert_user(chat_id, None, "en")
    response = app.app.test_client().post(
        "/admin/broadcast", json={"photo_file_id": "photo-id", "chat_ids": [222]},
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert response.json == {"users": 1, "sent": 1, "failed": 0, "deactivated": 0}
    assert events == [("photo", 222, "photo-id", "")]


@pytest.mark.parametrize("payload", [
    {"photo_url": 123}, {"photo_file_id": " "}, {"photo_url": None},
    {"photo_url": "https://example.com/photo.jpg", "photo_file_id": "photo-id"},
    {"photo_file_id": "photo-id", "caption": 123},
    {"photo_file_id": "photo-id", "chat_ids": "111"},
    {"photo_file_id": "photo-id", "chat_ids": ["111"]},
    ["not a JSON object"],
])
def test_admin_broadcast_photo_rejects_invalid_payload(payload, photo_delivery):
    events, _ = photo_delivery
    response = app.app.test_client().post(
        "/admin/broadcast", json=payload, headers={"X-Admin-Key": "test-admin-key"},
    )
    assert response.status_code == 400
    assert events == []
