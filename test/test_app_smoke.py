import os

os.environ.setdefault("TELEGRAM_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("DATABASE_URL", "sqlite:///test-smoke.db")

from app import app


def test_invalid_explicit_asset_is_not_reused():
    from app import selected_asset
    class Chat:
        id = 901
    class Message:
        chat = Chat()
    assert selected_asset(Message(), "not-supported") is None


def test_health_and_admin_auth():
    client = app.test_client()
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json["ok"] is True
    assert client.get("/admin/stats").status_code == 401
    assert client.get("/admin/stats", headers={"X-Admin-Key": "test-admin-key"}).status_code == 200