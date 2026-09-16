from __future__ import annotations

"""Live (tick-level) price quotes with multi-venue fallback.

Candle history in :mod:`marketobserver.market_data` answers "what happened";
this module answers "what is the price *right now*" for /price, the alert
loop and binary-expiry reference prices. Every quote carries its venue and
age so a caller can see staleness instead of trusting a silent number.

Venue order is fixed per asset class and every failure falls through to the
next venue — a blocked host degrades the quote, it never invents one:

* crypto: Binance spot ticker -> Kraken ticker -> Yahoo last price
* forex / metals / commodities / indices / stocks: Stooq CSV -> Yahoo last price
"""

import csv
import io
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
import yfinance as yf

from .assets import Asset

logger = logging.getLogger(__name__)

BINANCE_HOSTS = ("https://api.binance.com", "https://data-api.binance.vision")
KRAKEN_TICKER_URL = "https://api.kraken.com/0/public/Ticker"
STOOQ_URL = "https://stooq.com/q/l/"


class QuoteUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Quote:
    asset_key: str
    price: float
    source: str
    # When the quote was taken (UTC). `age_seconds` is measured against it.
    as_of: datetime
    age_seconds: float


class LivePriceProvider:
    """Thread-safe live quotes with a short TTL cache.

    `ttl_seconds` bounds how often the venues are hit: 1 means every call
    older than a second refetches. Values below ~2s are fine for one user
    asking /price, but a busy alert loop should stay at 2-5s to respect the
    free venues' rate limits.
    """

    def __init__(self, ttl_seconds: int = 3):
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._cache: dict[str, Quote] = {}
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "MarketObserverPro/1.0"})

    # ---------------- public API ----------------

    def get_quote(self, asset: Asset) -> Quote:
        now = time.time()
        with self._lock:
            cached = self._cache.get(asset.key)
            if cached and now - cached.as_of.timestamp() < self.ttl_seconds:
                return Quote(cached.asset_key, cached.price, cached.source,
                             cached.as_of, round(now - cached.as_of.timestamp(), 1))

        price, source = self._fetch(asset)
        if price is None or price <= 0:
            raise QuoteUnavailable(f"No live quote for {asset.key}")
        quote = Quote(asset.key, round(float(price), asset.price_decimals),
                      source, datetime.now(timezone.utc), 0.0)
        with self._lock:
            self._cache[asset.key] = quote
        return quote

    def invalidate(self, asset_key: str | None = None) -> None:
        with self._lock:
            if asset_key is None:
                self._cache.clear()
            else:
                self._cache.pop(asset_key, None)

    # ---------------- venue chain ----------------

    def _fetch(self, asset: Asset) -> tuple[float | None, str]:
        if asset.asset_class == "crypto":
            for name, getter in (("binance", self._binance),
                                 ("kraken", self._kraken),
                                 ("stooq", self._stooq),
                                 ("yahoo", self._yahoo)):
                price, label = getter(asset)
                if price:
                    return price, label or name
            return None, ""
        price, label = self._stooq(asset)
        if price:
            return price, label
        return self._yahoo(asset)

    # ---------------- venues ----------------

    def _binance(self, asset: Asset) -> tuple[float | None, str]:
        symbol = asset.binance
        if not symbol:
            base = (asset.provider_symbol or "").upper().removesuffix("-USD")
            symbol = f"{base}USDT" if base and base.isalnum() else None
        if not symbol:
            return None, ""
        for host in BINANCE_HOSTS:
            try:
                response = self.session.get(f"{host}/api/v3/ticker/price",
                                            params={"symbol": symbol}, timeout=(3, 6))
                if response.status_code != 200:
                    continue
                price = float(response.json().get("price", 0) or 0)
                if price > 0:
                    return price, f"binance:{symbol}"
            except Exception as exc:
                # Any transport/parse failure falls through to the next venue;
                # a quote layer must degrade, never raise, on provider errors.
                logger.info("binance quote failed on %s for %s: %s", host, asset.key, exc)
        return None, ""

    def _kraken(self, asset: Asset) -> tuple[float | None, str]:
        pair = asset.kraken
        if not pair:
            return None, ""
        try:
            response = self.session.get(KRAKEN_TICKER_URL, params={"pair": pair}, timeout=(3, 8))
            response.raise_for_status()
            results = response.json().get("result", {})
            if not isinstance(results, dict) or not results:
                return None, ""
            ticker = next(iter(results.values()))
            price = float((ticker.get("c") or [0])[0] or 0)
            if price > 0:
                return price, f"kraken:{pair}"
        except Exception as exc:
            logger.info("kraken quote failed for %s: %s", asset.key, exc)
        return None, ""

    def _stooq(self, asset: Asset) -> tuple[float | None, str]:
        symbol = asset.stooq
        if not symbol:
            return None, ""
        try:
            response = self.session.get(
                STOOQ_URL,
                params={"s": symbol, "f": "sd2t2ohlcv", "h": "", "e": "csv"},
                timeout=(3, 8),
            )
            response.raise_for_status()
            rows = list(csv.DictReader(io.StringIO(response.text)))
            if not rows:
                return None, ""
            row = rows[0]
            # Stooq returns "N/A" or "Exceeded the daily hits limit" when the
            # symbol is wrong or throttled — both must fall through, never parse.
            close = (row.get("Close") or "").strip()
            if not close or close.upper() == "N/A":
                logger.info("stooq quote unavailable for %s (%s)", asset.key, symbol)
                return None, ""
            price = float(close)
            if price > 0:
                return price, f"stooq:{symbol}"
        except Exception as exc:
            logger.info("stooq quote failed for %s: %s", asset.key, exc)
        return None, ""

    def _yahoo(self, asset: Asset) -> tuple[float | None, str]:
        try:
            ticker = yf.Ticker(asset.provider_symbol)
            price = float(ticker.fast_info.get("last_price", 0) or 0)
            if price > 0:
                return price, f"yahoo:{asset.provider_symbol}"
            frame = ticker.history(period="1d", interval="1m", auto_adjust=False, actions=False)
            if frame is not None and not frame.empty:
                last = float(frame["Close"].dropna().iloc[-1])
                if last > 0:
                    return last, f"yahoo:{asset.provider_symbol}"
        except Exception as exc:
            logger.info("yahoo quote failed for %s: %s", asset.key, exc)
        return None, ""
