import json
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
