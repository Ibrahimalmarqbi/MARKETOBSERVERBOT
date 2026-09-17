"""Calendar engine, news fixes, journal learning and trade plans."""

import json
from datetime import datetime, timedelta, timezone

import pytest

import app
from marketobserver.calendar import EconEvent, event_key, parse_feed
from marketobserver.db import cooldown_active
from marketobserver.learning import accuracy, calibration_for, classify, stats_report
from marketobserver.market_data import Candle
from marketobserver.research import NewsItem, headline_age_hours, is_fresh, pick_top


NOW = datetime.now(timezone.utc)


def news_item(title: str, age_hours: float | None = 0.5, sentiment: str = "neutral") -> NewsItem:
    if age_hours is None:
        return NewsItem(title, "https://example.com/x", None, sentiment)
    pub = (NOW - timedelta(hours=age_hours)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    return NewsItem(title, "https://example.com/x", pub, sentiment)


# ---------------- cooldown / seen fixes ----------------

def test_cooldown_handles_naive_sqlite_datetimes():
    future_naive = (NOW + timedelta(hours=1)).replace(tzinfo=None)
    past_naive = (NOW - timedelta(hours=1)).replace(tzinfo=None)
    assert cooldown_active(future_naive, NOW) is True
    assert cooldown_active(past_naive, NOW) is False
    assert cooldown_active(None, NOW) is False
    assert cooldown_active(NOW + timedelta(minutes=1), NOW) is True


def test_news_seen_is_per_user(tmp_path):
    from marketobserver.db import Database
    db = Database(f"sqlite:///{tmp_path / 'seen.db'}")
    db.create_all()
    db.mark_news_seen("fp-1", chat_id=11)
    assert db.news_was_seen("fp-1", chat_id=11) is True
    assert db.news_was_seen("fp-1", chat_id=22) is False


def test_freshness_and_top_pick():
    old = news_item("Federal Reserve rate decision rocks markets", age_hours=30)
    fresh = news_item("CPI inflation data beats expectations", age_hours=0.2)
    mild = news_item("Gold steady in quiet trade", age_hours=0.1)
    assert is_fresh(old, 6.0, NOW) is False
    assert is_fresh(fresh, 6.0, NOW) is True
    assert is_fresh(news_item("undated story", age_hours=None), 6.0, NOW) is True
    assert headline_age_hours(fresh, NOW) < 1
    assert pick_top([mild, fresh, old], NOW).title.startswith("CPI")


def test_compact_news_alert_stays_short():
    from marketobserver.assets import ASSETS
    item = news_item("Federal Reserve announces interest rate decision - Reuters", 0.3, "negative")
    text = app.news_alert_text(ASSETS["BTC"], [item], "ar", "Asia/Riyadh")
    assert "فتح المصدر الأصلي" in text
    assert "الأهمية: <b>عالية</b>" in text
    assert len(text) < 1200


# ---------------- calendar ----------------

FEED = [
    {"title": "CPI m/m", "country": "USD", "date": "2026-09-16T08:30:00-04:00",
     "impact": "High", "forecast": "0.3%", "previous": "0.2%"},
    {"title": "Core CPI m/m", "country": "USD", "date": "2026-09-16T08:30:00-04:00",
     "impact": "High", "forecast": "0.2%", "previous": "0.2%"},
    {"title": "Lagarde Speaks", "country": "EUR", "date": "2026-09-16T11:15:00-04:00",
     "impact": "Medium", "forecast": "", "previous": ""},
    {"title": "Broken row", "country": "USD", "date": "not-a-date", "impact": "High"},
]


def test_parse_feed_skips_bad_rows_and_sorts():
    events = parse_feed(FEED)
    assert [event.title for event in events] == ["CPI m/m", "Core CPI m/m", "Lagarde Speaks"]
    assert events[0].stars == "🔴🔴🔴"
    assert events[2].stars == "🟠🟠"
    assert events[0].assets[:2] == ("XAUUSD", "EURUSD")
    assert events[0].event_time.tzinfo is not None
    assert event_key("CPI m/m", "USD", events[0].event_time) == events[0].key


def test_grouping_merges_same_window_same_country():
    events = parse_feed(FEED)
    groups = app._group_calendar_events(events)
    assert len(groups) == 2
    assert len(groups[0]) == 2  # the two USD CPIs become one message


def test_calendar_texts_carry_strength_and_guidance():
    events = parse_feed(FEED)
    group = [events[0]]
    pre = app.calendar_pre_text(group, "ar", "Asia/Riyadh")
    assert "🔴🔴🔴" in pre and "CPI" in pre and "XAUUSD" in pre
    release = app.calendar_release_text(group, {"XAUUSD": 4715.5}, "ar")
    assert "صدر الآن" in release and "4715.5" in release
    follow = app.calendar_followup_text(group, [("XAUUSD", 4700.0, 4715.5)], "⚖️ x", None, "ar")
    assert "+0.33%" in follow


def up_candles(count: int = 60) -> list[Candle]:
    base = NOW - timedelta(minutes=5 * count)
    price, out = 1.0800, []
    pattern = [0.0002, -0.0002, 0.0002]
    for index in range(count):
        move = pattern[index % 3]
        price += move
        out.append(Candle(base + timedelta(minutes=5 * index), price - move,
                          price + abs(move) * 0.2, price - abs(move) * 0.2, price, 1000.0))
    return out


def test_trade_plan_uses_capital_risk_and_atr(monkeypatch):
    from marketobserver.assets import ASSETS
    monkeypatch.setattr(app.market, "get_candles", lambda asset, interval, limit: up_candles())
    plan = app.event_trade_plan(ASSETS["EURUSD"], "BUY", 2000.0, 1.0, "ar")
    assert plan and "شراء" in plan and "لوت" in plan and "20.00$" in plan
    assert "وقف" in plan and "هدف1" in plan


# ---------------- journal / learning ----------------

def test_classify_and_accuracy(tmp_path):
    from marketobserver.db import Database
    assert classify(100.0, 101.0, "CALL") == "win"
    assert classify(100.0, 99.0, "CALL") == "loss"
    assert classify(100.0, 99.0, "PUT") == "win"
    assert classify(100.0, 100.0, "CALL") == "flat"
    assert classify(100.0, None, "CALL") == "flat"
    db = Database(f"sqlite:///{tmp_path / 'j.db'}")
    db.create_all()
    for index in range(20):
        entry = db.journal_add("binary", "EURUSD", "CALL", 1.0800, 0, note="t")
        db.journal_resolve(entry.id, "win" if index < 6 else "loss", 1.0810)
    trades, wins, rate = accuracy(db, kind="binary", asset_key="EURUSD", verdict="CALL")
    assert (trades, wins) == (20, 6)
    assert rate == 0.3
    cal = calibration_for(db, "EURUSD", "CALL")
    assert cal.downgrade is True and "30%" in cal.note_ar
    report = stats_report(db, "ar")
    assert "30%" in report and "EURUSD" in report


def test_calibration_stays_silent_without_history(tmp_path):
    from marketobserver.db import Database
    db = Database(f"sqlite:///{tmp_path / 'empty.db'}")
    db.create_all()
    assert calibration_for(db, "BTC", "PUT").downgrade is False
    assert "لا توجد" in stats_report(db, "ar")


# ---------------- telegram commands ----------------

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


def test_capital_command_roundtrip(telegram):
    app.bot.process_new_updates([message_update("/capital 2500 2")])
    assert SENT and "2500" in SENT[-1]
    SENT.clear()
    app.bot.process_new_updates([message_update("/capital")])
    assert "2500" in SENT[-1] and "2%" in SENT[-1]


def test_calendar_command_lists_events(telegram, monkeypatch):
    events = parse_feed(FEED)
    monkeypatch.setattr(app.calendar_feed, "upcoming", lambda hours, impacts=("High",): events)
    app.bot.process_new_updates([message_update("/calendar")])
    assert SENT and "CPI" in SENT[-1] and "🔴🔴🔴" in SENT[-1]


def test_stats_command_reads_journal(telegram):
    app.bot.process_new_updates([message_update("/stats")])
    assert SENT and ("دقة" in SENT[-1] or "لا توجد" in SENT[-1])


def test_calendar_and_stats_nl_intents():
    from marketobserver.nlp import parse_request
    assert parse_request("ما أحداث التقويم اليوم؟").intent == "calendar"
    assert parse_request("كم دقتك يا بوت؟").intent == "stats"
    assert parse_request("Should I buy BTC now?").intent == "advice"


def test_stats_endpoint_shape():
    client = app.app.test_client()
    response = client.get("/stats?lang=en")
    assert response.status_code == 200
    assert "win_rate" in response.json and "report" in response.json
    assert client.get("/stats?kind=nope").status_code == 400


def test_calendar_endpoint_shape(monkeypatch):
    monkeypatch.setattr(app.calendar_feed, "upcoming", lambda hours, impacts: parse_feed(FEED))
    client = app.app.test_client()
    response = client.get("/calendar?hours=24")
    assert response.status_code == 200
    assert response.json["count"] == 3
    assert response.json["events"][0]["assets"]


# ---------------- full stage flow ----------------

def test_scan_calendar_runs_three_stages_exactly_once(telegram, monkeypatch):
    first, second = cpi_event(10), cpi_event(10)
    usd_group = [first, second]
    monkeypatch.setattr(app.calendar_feed, "due_for_pre", lambda *a, **k: list(usd_group))
    monkeypatch.setattr(app.calendar_feed, "due_for_release", lambda *a, **k: [])
    monkeypatch.setattr(app.calendar_feed, "due_for_followup", lambda *a, **k: [])
    app.db.upsert_user(4242, "t", "ar")
    app.scan_calendar(NOW)
    assert len(SENT) == 1 and "CPI" in SENT[0]  # grouped single message
    app.scan_calendar(NOW)
    assert len(SENT) == 1  # exactly once: no duplicate pre-brief


def cpi_event(offset_minutes: int = 0) -> EconEvent:
    """A USD CPI firing near 'now' — unique key per run, no cross-run pollution."""
    moment = NOW + timedelta(minutes=offset_minutes)
    return EconEvent(key=event_key(f"CPI m/m {moment.isoformat()}", "USD", moment),
                     title="CPI m/m", country="USD", impact="High",
                     event_time=moment, forecast="0.3%", previous="0.2%")


def test_release_snapshots_and_followup_measures(telegram, monkeypatch):
    from marketobserver.assets import ASSETS
    group = [cpi_event()]
    monkeypatch.setattr(app.calendar_feed, "due_for_pre", lambda *a, **k: [])
    monkeypatch.setattr(app.calendar_feed, "due_for_release", lambda *a, **k: list(group))
    monkeypatch.setattr(app.calendar_feed, "due_for_followup", lambda *a, **k: [])

    prices = {"XAUUSD": 4700.0, "EURUSD": 1.0800, "GBPUSD": 1.2700, "DXY": 99.0}
    monkeypatch.setattr(app, "live_price_for", lambda key: prices.get(key))
    app.db.upsert_user(4242, "t", "ar")
    app.scan_calendar(NOW)
    assert SENT and "صدر الآن" in SENT[-1]

    # 30 minutes later the market moved; follow-up must measure it.
    monkeypatch.setattr(app.calendar_feed, "due_for_release", lambda *a, **k: [])
    monkeypatch.setattr(app.calendar_feed, "due_for_followup", lambda *a, **k: list(group))
    prices["XAUUSD"] = 4723.5  # +0.5%
    fake_verdict = type("V", (), {"verdict": "CALL", "confidence": "medium"})()
    monkeypatch.setattr(app, "binary_verdict_for", lambda asset, force=False, entry_mode=None: fake_verdict)
    monkeypatch.setattr(app.market, "get_candles", lambda asset, interval, limit: up_candles())
    SENT.clear()
    app.scan_calendar(NOW + timedelta(minutes=31))
    assert SENT and "+0.50%" in SENT[-1] and "شراء" in SENT[-1] and "لوت" in SENT[-1]


def test_news_impact_followup_only_when_big(telegram, monkeypatch):
    from marketobserver.assets import ASSETS
    app.db.upsert_user(4242, "t", "ar")
    entry = app.db.journal_add("news", "BTC", "CALL", 100000.0, 0, chat_id=4242, note="big headline")
    monkeypatch.setattr(app, "live_price_for", lambda key: 101500.0)  # +1.5% > 0.8% threshold
    app.resolve_journal_and_notify()
    assert SENT and "+1.50%" in SENT[-1]
    SENT.clear()
    entry2 = app.db.journal_add("news", "BTC", "CALL", 100000.0, 0, chat_id=4242, note="small headline")
    monkeypatch.setattr(app, "live_price_for", lambda key: 100100.0)  # +0.1% < threshold
    app.resolve_journal_and_notify()
    assert SENT == []  # small reactions stay silent
