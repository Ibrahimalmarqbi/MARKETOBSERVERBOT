from __future__ import annotations

"""Economic calendar from the free ForexFactory weekly feed.

Source: https://nfs.faireconomy.media/ff_calendar_thisweek.json
(no key required; refreshed by the publisher through the week).

Each row carries title/country/date/impact/forecast/previous. The file has no
"actual" column, so the release stage of the engine measures the market's own
verdict instead: the price reaction on the affected assets in the minutes
after the event. For trading purposes that reaction — not the printed number
— is what can actually make or lose money.
"""

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CACHE_SECONDS = 15 * 60

# country code -> (arabic name, flag)
COUNTRIES = {
    "USD": ("الولايات المتحدة", "🇺🇸"),
    "EUR": ("منطقة اليورو", "🇪🇺"),
    "GBP": ("بريطانيا", "🇬🇧"),
    "JPY": ("اليابان", "🇯🇵"),
    "AUD": ("أستراليا", "🇦🇺"),
    "CAD": ("كندا", "🇨🇦"),
    "CHF": ("سويسرا", "🇨🇭"),
    "NZD": ("نيوزلندا", "🇳🇿"),
    "CNY": ("الصين", "🇨🇳"),
}

# country -> assets that move first on its high-impact releases.
CURRENCY_ASSETS = {
    "USD": ("XAUUSD", "EURUSD", "GBPUSD", "DXY"),
    "EUR": ("EURUSD", "EURJPY", "EURGBP"),
    "GBP": ("GBPUSD", "GBPJPY", "EURGBP"),
    "JPY": ("USDJPY", "EURJPY", "GBPJPY"),
    "AUD": ("AUDUSD", "AUDJPY"),
    "CAD": ("USDCAD", "EURCAD"),
    "CHF": ("USDCHF",),
    "NZD": ("NZDUSD",),
    "CNY": ("AUDUSD", "USDCAD"),
}

# investing.com-style strength: three levels for push alerts.
IMPACT_STARS = {"High": "🔴🔴🔴", "Medium": "🟠🟠", "Low": "🟡"}
IMPACT_AR = {"High": "عالي جدًا", "Medium": "متوسط", "Low": "ضعيف"}
IMPACT_EN = {"High": "High", "Medium": "Medium", "Low": "Low"}


@dataclass(frozen=True)
class EconEvent:
    key: str
    title: str
    country: str
    impact: str
    event_time: datetime
    forecast: str
    previous: str

    @property
    def stars(self) -> str:
        return IMPACT_STARS.get(self.impact, "🟡")

    @property
    def assets(self) -> tuple[str, ...]:
        return CURRENCY_ASSETS.get(self.country, ())


def event_key(title: str, country: str, event_time: datetime) -> str:
    raw = f"{title.strip().casefold()}|{country}|{event_time.isoformat()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _parse_time(raw: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat((raw or "").replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def parse_feed(payload: list) -> list[EconEvent]:
    events: list[EconEvent] = []
    for row in payload or []:
        if not isinstance(row, dict):
            continue
        moment = _parse_time(row.get("date", ""))
        impact = str(row.get("impact", "")).strip().capitalize()
        title = str(row.get("title", "")).strip()
        country = str(row.get("country", "")).strip().upper()
        if not moment or not title or impact not in IMPACT_STARS:
            continue
        events.append(EconEvent(
            key=event_key(title, country, moment),
            title=title, country=country, impact=impact, event_time=moment,
            forecast=str(row.get("forecast", "") or "").strip(),
            previous=str(row.get("previous", "") or "").strip(),
        ))
    events.sort(key=lambda event: event.event_time)
    return events


class EconomicCalendar:
    """Cached weekly calendar with helpers for the alert engine."""

    def __init__(self, cache_seconds: int = CACHE_SECONDS):
        self.cache_seconds = cache_seconds
        self._events: list[EconEvent] = []
        self._fetched_at: float = 0.0
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "MarketObserverPro/1.0"})

    def events(self, force: bool = False) -> list[EconEvent]:
        with self._lock:
            fresh = self._events and (time.time() - self._fetched_at) < self.cache_seconds
            if fresh and not force:
                return list(self._events)
        try:
            response = self.session.get(FEED_URL, timeout=(4, 12))
            response.raise_for_status()
            parsed = parse_feed(response.json())
        except Exception as exc:
            logger.warning("economic calendar fetch failed: %s", exc)
            with self._lock:
                return list(self._events)
        with self._lock:
            if parsed:
                self._events = parsed
                self._fetched_at = time.time()
            return list(self._events)

    def upcoming(self, hours: int = 24, impacts: tuple[str, ...] = ("High",),
                 now: datetime | None = None) -> list[EconEvent]:
        moment = now or datetime.now(timezone.utc)
        return [event for event in self.events()
                if event.impact in impacts
                and event.event_time > moment
                and (event.event_time - moment).total_seconds() <= hours * 3600]

    def due_for_pre(self, minutes_before: int = 30, impacts: tuple[str, ...] = ("High",),
                    now: datetime | None = None) -> list[EconEvent]:
        """High-impact events starting within `minutes_before` (pre-brief)."""
        moment = now or datetime.now(timezone.utc)
        window = minutes_before * 60
        return [event for event in self.events()
                if event.impact in impacts
                and 0 < (event.event_time - moment).total_seconds() <= window]

    def due_for_release(self, impacts: tuple[str, ...] = ("High",),
                        now: datetime | None = None) -> list[EconEvent]:
        """Events whose time passed recently (release ping + price snapshot)."""
        moment = now or datetime.now(timezone.utc)
        return [event for event in self.events()
                if event.impact in impacts
                and 0 <= (moment - event.event_time).total_seconds() <= 15 * 60]

    def due_for_followup(self, minutes_after: int = 30, impacts: tuple[str, ...] = ("High",),
                         now: datetime | None = None) -> list[EconEvent]:
        """Events old enough to measure the market reaction."""
        moment = now or datetime.now(timezone.utc)
        return [event for event in self.events()
                if event.impact in impacts
                and (moment - event.event_time).total_seconds() >= minutes_after * 60]


def surprise_text(event: EconEvent, lang: str) -> str:
    """Expectation line from forecast vs previous (before the release)."""
    forecast, previous = event.forecast or "—", event.previous or "—"
    if lang == "ar":
        return f"🔮 المتوقع: {forecast} | السابق: {previous}"
    return f"🔮 Forecast: {forecast} | Previous: {previous}"


def reaction_text(asset_key: str, ref: float, current: float, decimals: int, lang: str) -> str:
    pct = (current - ref) / ref * 100 if ref else 0.0
    arrow = "🟢" if pct > 0.02 else "🔴" if pct < -0.02 else "⚪"
    if lang == "ar":
        return f"{arrow} {asset_key}: {ref:g} ← {current:g} ({pct:+.2f}%)"
    return f"{arrow} {asset_key}: {ref:g} → {current:g} ({pct:+.2f}%)"
