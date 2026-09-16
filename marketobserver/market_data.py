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

# Binance rejects some regions on api.binance.com while the public market data
# mirror stays reachable; both serve the identical klines payload, so trying the
# second host is a reliability fix and never a different data source.
BINANCE_HOSTS = ("https://api.binance.com", "https://data-api.binance.vision")
BINANCE_PAIRS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "PAXG": "PAXGUSDT"}
BINANCE_INTERVALS = {"15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d"}
KRAKEN_PAIRS = {"BTC": "XXBTZUSD", "ETH": "XETHZUSD", "SOL": "XSOLZUSD"}
KRAKEN_INTERVALS = {"15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "1440"}


class DataUnavailable(RuntimeError):
    pass


def load_candle_csv(path: str) -> list[Candle]:
    """Read closed candles from a local CSV dump. Header must be
    ``ts,open,high,low,close,volume[,buy_volume]`` with ``ts`` in milliseconds —
    the same layout ``tools/smc_report.py --csv-prefix`` and the offline replays
    in this repository use. Used for audits and tests so a report can always be
    reproduced from the exact bars it was computed on. Rows are sorted by
    timestamp, because exchange endpoints return their klines newest-first and a
    reversed series would silently invert the whole analysis.
    """
    from datetime import datetime as _datetime

    candles: list[Candle] = []
    with open(path, encoding="utf-8") as handle:
        header = handle.readline().strip().split(",")
        for line in handle:
            values = line.strip().split(",")
            if len(values) < len(header) or not values[0].isdigit():
                continue
            row = dict(zip(header, values))
            buy = (row.get("buy_volume") or "").strip()
            candles.append(Candle(
                timestamp=_datetime.fromtimestamp(int(row["ts"]) / 1000, tz=timezone.utc),
                open=float(row["open"]), high=float(row["high"]), low=float(row["low"]),
                close=float(row["close"]), volume=float(row["volume"]),
                buy_volume=float(buy) if buy else None,
            ))
    return sorted(candles, key=lambda candle: candle.timestamp)


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    # Taker buy base volume, when the provider returns it. It lets the volume
    # module read real aggressive buy/sell pressure instead of guessing flow.
    buy_volume: float | None = None


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
        candles: list[Candle] = []
        if asset.asset_class == "crypto":
            # Exchange klines are the reference feed for crypto: native 4h and
            # 15m bars plus taker buy volume. Yahoo stays last so a blocked or
            # rate-limited exchange host cannot silently redefine a timeframe.
            candles = self._binance(asset, interval, limit)
            if candles:
                source = f"binance:{self._binance_pair(asset)}"
            else:
                candles = self._kraken(asset, interval, limit)
                if candles:
                    source = f"kraken:{self._kraken_pair(asset)}"
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
        if asset.key in BINANCE_PAIRS:
            return BINANCE_PAIRS[asset.key]
        # Other USD-quoted crypto listings map 1:1 onto Binance pairs
        # (DOGE-USD -> DOGEUSDT) without needing a code change.
        symbol = (asset.provider_symbol or "").upper()
        if symbol.endswith("-USD"):
            base = symbol[:-4]
            if base and base.isalnum():
                return f"{base}USDT"
        return None

    def _binance(self, asset: Asset, interval: str, limit: int) -> list[Candle]:
        pair = self._binance_pair(asset)
        timeframe = BINANCE_INTERVALS.get(interval)
        if not pair or not timeframe:
            return []
        params = {"symbol": pair, "interval": timeframe, "limit": min(limit, 1000)}
        for host in BINANCE_HOSTS:
            try:
                response = self.session.get(f"{host}/api/v3/klines", params=params, timeout=(3, 8))
                if response.status_code != 200:
                    logger.info("binance %s returned HTTP %s for %s", host, response.status_code, pair)
                    continue
                rows = response.json()
                if not isinstance(rows, list):
                    continue
                candles: list[Candle] = []
                for row in rows:
                    if len(row) < 10:
                        continue
                    buy = float(row[9]) if row[9] not in (None, "") else None
                    candles.append(Candle(
                        timestamp=datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc),
                        open=float(row[1]), high=float(row[2]), low=float(row[3]),
                        close=float(row[4]), volume=float(row[5]), buy_volume=buy,
                    ))
                if candles:
                    return candles
            except (requests.RequestException, ValueError, TypeError, IndexError) as exc:
                logger.warning("Binance data failed on %s for %s: %s", host, asset.key, exc)
        return []

    def _kraken_pair(self, asset: Asset) -> str | None:
        if asset.key in KRAKEN_PAIRS:
            return KRAKEN_PAIRS[asset.key]
        symbol = (asset.provider_symbol or "").upper()
        if symbol.endswith("-USD"):
            base = symbol[:-4]
            if base and base.isalnum():
                return f"X{base}ZUSD"
        return None

    def _kraken(self, asset: Asset, interval: str, limit: int) -> list[Candle]:
        """Second crypto venue. Kraken serves up to 720 bars, which covers the
        4H/1H/15m windows this engine consumes."""
        pair = self._kraken_pair(asset)
        timeframe = KRAKEN_INTERVALS.get(interval)
        if not pair or not timeframe:
            return []
        try:
            response = self.session.get(
                "https://api.kraken.com/0/public/OHLC",
                params={"pair": pair, "interval": timeframe},
                timeout=(3, 10),
            )
            response.raise_for_status()
            payload = response.json()
            rows = [row for row in payload.get("result", {}).values() if isinstance(row, list)]
            if not rows:
                return []
            candles: list[Candle] = []
            for row in rows[-limit:]:
                if len(row) < 7:
                    continue
                candles.append(Candle(
                    timestamp=datetime.fromtimestamp(float(row[0]), tz=timezone.utc),
                    open=float(row[1]), high=float(row[2]), low=float(row[3]),
                    close=float(row[4]), volume=float(row[6]),
                ))
            return candles
        except (requests.RequestException, ValueError, TypeError, KeyError, IndexError) as exc:
            logger.warning("Kraken data failed for %s: %s", asset.key, exc)
            return []

    def _yahoo(self, asset: Asset, interval: str, limit: int) -> list[Candle]:
        try:
            period = "2y" if interval in {"1d", "1wk", "1mo"} else "60d"
            fetch_interval = "1h" if interval == "4h" else interval
            frame = yf.Ticker(asset.provider_symbol).history(period=period, interval=fetch_interval, auto_adjust=False, actions=False)
            if interval == "4h" and frame is not None and not frame.empty:
                frame = frame.resample("4h").agg({
                    "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum",
                }).dropna(subset=["Open", "High", "Low", "Close"])
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
