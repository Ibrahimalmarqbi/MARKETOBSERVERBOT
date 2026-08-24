from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
import yfinance as yf

from .assets import Asset

logger = logging.getLogger(__name__)


class DataUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class MarketDataProvider:
    def __init__(self, cache_seconds: int = 45):
        self.cache_seconds = cache_seconds
        self._cache: dict[tuple[str, str, int], tuple[float, list[Candle], str]] = {}
        self._source: dict[str, str] = {}
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "MarketObserverPro/1.0"})

    def get_candles(self, asset: Asset, interval: str = "1h", limit: int = 200) -> list[Candle]:
        key = (asset.key, interval, limit)
        with self._lock:
            cached = self._cache.get(key)
            if cached and time.time() - cached[0] < self.cache_seconds:
                self._source[asset.key] = cached[2]
                return cached[1]

        source = f"yahoo:{asset.provider_symbol}"
        if asset.asset_class == "crypto":
            candles = self._binance(asset, interval, limit)
            if candles:
                source = f"binance:{self._binance_pair(asset)}"
            else:
                candles = self._yahoo(asset, interval, limit)
        else:
            candles = self._yahoo(asset, interval, limit)

        if not candles or len(candles) < 50:
            raise DataUnavailable(f"No reliable market data for {asset.key}")
        with self._lock:
            self._cache[key] = (time.time(), candles, source)
            self._source[asset.key] = source
        return candles

    def last_source(self, asset_key: str) -> str | None:
        return self._source.get(asset_key)

    def _binance_pair(self, asset: Asset) -> str | None:
        return {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "PAXG": "PAXGUSDT"}.get(asset.key)

    def _binance(self, asset: Asset, interval: str, limit: int) -> list[Candle]:
        pair = self._binance_pair(asset)
        if not pair:
            return []
        try:
            response = self.session.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": pair, "interval": interval, "limit": min(limit, 1000)},
                timeout=(3, 8),
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                return []
            return [
                Candle(
                    timestamp=datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc),
                    open=float(row[1]), high=float(row[2]), low=float(row[3]),
                    close=float(row[4]), volume=float(row[5]),
                ) for row in rows if len(row) >= 6
            ]
        except (requests.RequestException, ValueError, TypeError, IndexError) as exc:
            logger.warning("Binance data failed for %s: %s", asset.key, exc)
            return []

    def _yahoo(self, asset: Asset, interval: str, limit: int) -> list[Candle]:
        try:
            period = "2y" if interval in {"1d", "1wk", "1mo"} else "60d"
            frame = yf.Ticker(asset.provider_symbol).history(period=period, interval=interval, auto_adjust=False, actions=False)
            if frame is None or frame.empty:
                return []
            frame = frame.dropna(subset=["Open", "High", "Low", "Close"]).tail(limit)
            candles: list[Candle] = []
            for timestamp, row in frame.iterrows():
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                candles.append(Candle(
                    timestamp=timestamp.to_pydatetime().astimezone(timezone.utc),
                    open=float(row["Open"]), high=float(row["High"]), low=float(row["Low"]),
                    close=float(row["Close"]), volume=float(row.get("Volume", 0) or 0),
                ))
            return candles
        except Exception as exc:
            logger.warning("Yahoo data failed for %s: %s", asset.key, exc)
            return []