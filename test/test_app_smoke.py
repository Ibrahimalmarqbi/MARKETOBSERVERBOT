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


def test_news_alert_is_compact_and_uses_short_link_label():
    from app import news_alert_text
    from marketobserver.assets import ASSETS
    from marketobserver.research import NewsItem

    items = [
        NewsItem("What next as bitcoin tops $80,000, solana jumps 8% - CoinDesk", "https://news.google.com/rss/articles/very-long-id", "Tue, 25 Aug 2026 05:25:00 GMT", "positive"),
        NewsItem("Federal Reserve interest rate decision - Reuters", "https://example.com/second", "Tue, 25 Aug 2026 06:25:00 GMT", "neutral"),
        NewsItem("Third headline should be capped", "https://example.com/third", None, "neutral"),
    ]
    text = news_alert_text(ASSETS["SOL"], items, "ar", "Asia/Riyadh")
    assert "فتح المصدر الأصلي" in text
    assert "What next as bitcoin tops" in text
    assert "CoinDesk" in text
    assert "very-long-id" in text  # retained only inside the clickable href
    assert "Third headline should be capped" not in text
    assert "الأهمية: <b>عالية</b>" in text