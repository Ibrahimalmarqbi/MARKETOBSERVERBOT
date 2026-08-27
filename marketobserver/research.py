from __future__ import annotations

import hashlib
import html
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from .assets import Asset

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsItem:
    title: str
    link: str
    published: str | None
    sentiment: str


@dataclass(frozen=True)
class ResearchSnapshot:
    asset: Asset
    items: tuple[NewsItem, ...]
    positive: int
    negative: int
    neutral: int
    source: str
    as_of: str


_POSITIVE = {
    "surge", "rally", "gain", "gains", "bullish", "growth", "strong", "beat", "positive", "up", "ارتفاع", "صعود", "نمو", "قوي", "ايجابي", "إيجابي", "مكاسب"
}
_NEGATIVE = {
    "fall", "drop", "loss", "losses", "bearish", "weak", "risk", "crash", "negative", "down", "هبوط", "هبوط", "خسائر", "ضعيف", "مخاطر", "سلبي", "انخفاض", "انهيار"
}


_IMPORTANCE_TERMS = {
    "fed", "federal reserve", "interest rate", "rate decision", "cpi", "inflation", "jobs report", "nonfarm", "sanctions", "war", "ceasefire", "opec", "earnings", "guidance", "bankruptcy", "default", "hack", "etf", "approval", "sec", "رفع الفائدة", "خفض الفائدة", "التضخم", "الاحتياطي الفيدرالي", "أوبك", "أرباح", "إفلاس", "اختراق", "عقوبات", "حرب"
}


def headline_importance(item: NewsItem) -> tuple[bool, str]:
    lowered = item.title.casefold()
    matches = [term for term in _IMPORTANCE_TERMS if term in lowered]
    if matches:
        return True, "matched macro or market-moving term: " + ", ".join(matches[:3])
    return False, "no high-impact keyword detected"


def headline_fingerprint(item: NewsItem) -> str:
    raw = f"{item.title.strip().casefold()}|{item.link.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sentiment(title: str) -> str:
    words = set(re.findall(r"[A-Za-z]+|[\u0600-\u06ff]+", title.lower()))
    score = len(words & _POSITIVE) - len(words & _NEGATIVE)
    return "positive" if score > 0 else "negative" if score < 0 else "neutral"


class MarketResearch:
    def __init__(self, timeout: tuple[int, int] = (3, 8)):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "MarketObserverPro/1.0"})

    def important_news(self, asset: Asset, limit: int = 8) -> ResearchSnapshot:
        snapshot = self.news(asset, limit=max(limit, 12))
        important = tuple(item for item in snapshot.items if headline_importance(item)[0])[:limit]
        positive = sum(item.sentiment == "positive" for item in important)
        negative = sum(item.sentiment == "negative" for item in important)
        neutral = len(important) - positive - negative
        return ResearchSnapshot(asset, important, positive, negative, neutral, snapshot.source, snapshot.as_of)

    def news(self, asset: Asset, limit: int = 8) -> ResearchSnapshot:
        query = urllib.parse.quote(f"{asset.name_en} {asset.key}")
        url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
        items: list[NewsItem] = []
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            for item in root.findall("./channel/item")[:limit]:
                title = html.unescape((item.findtext("title") or "").strip())
                link = (item.findtext("link") or "").strip()
                published = (item.findtext("pubDate") or "").strip() or None
                if title:
                    items.append(NewsItem(title, link, published, _sentiment(title)))
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            logger.warning("news research failed for %s: %s", asset.key, exc)
        positive = sum(item.sentiment == "positive" for item in items)
        negative = sum(item.sentiment == "negative" for item in items)
        neutral = len(items) - positive - negative
        return ResearchSnapshot(
            asset=asset,
            items=tuple(items),
            positive=positive,
            negative=negative,
            neutral=neutral,
            source="Google News RSS",
            as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        )