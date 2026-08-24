from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Asset:
    key: str
    name_ar: str
    name_en: str
    asset_class: str
    provider_symbol: str
    quote: str
    price_decimals: int
    supported: bool = True


ASSETS: dict[str, Asset] = {
    "BTC": Asset("BTC", "البيتكوين", "Bitcoin", "crypto", "BTC-USD",  "USD", 2),
    "ETH": Asset("ETH", "الإيثريوم", "Ethereum", "crypto", "ETH-USD", "USD", 2),
    "SOL": Asset("SOL", "سولانا", "Solana", "crypto", "SOL-USD", "USD", 4),
    "PAXG": Asset("PAXG", "ذهب رقمي", "PAX Gold", "crypto", "PAXG-USD", "USD", 2),
    "EURUSD": Asset("EURUSD", "اليورو/الدولار", "EUR/USD", "forex", "EURUSD=X", "USD", 5),
    "GBPUSD": Asset("GBPUSD", "الجنيه/الدولار", "GBP/USD", "forex", "GBPUSD=X", "USD", 5),
    "USDJPY": Asset("USDJPY", "الدولار/الين", "USD/JPY", "forex", "JPY=X", "USD", 3),
    "XAUUSD": Asset("XAUUSD", "الذهب - مرجع العقود الآجلة", "Gold Futures Reference", "metals", "GC=F", "USD", 2),
    "XAGUSD": Asset("XAGUSD", "الفضة - مرجع العقود الآجلة", "Silver Futures Reference", "metals", "SI=F", "USD", 3),
    "USO": Asset("USO", "صندوق النفط USO", "USO Oil ETF", "commodity", "USO", "USD", 2),
    "AAPL": Asset("AAPL", "أبل", "Apple", "stock", "AAPL", "USD", 2),
    "TSLA": Asset("TSLA", "تسلا", "Tesla", "stock", "TSLA", "USD", 2),
    "NVDA": Asset("NVDA", "إنفيديا", "NVIDIA", "stock", "NVDA", "USD", 2),
    "MSFT": Asset("MSFT", "مايكروسوفت", "Microsoft", "stock", "MSFT", "USD", 2),
    "SPY": Asset("SPY", "مؤشر S&P 500", "S&P 500 ETF", "index", "SPY", "USD", 2),
    "QQQ": Asset("QQQ", "مؤشر ناسداك", "Nasdaq 100 ETF", "index", "QQQ", "USD", 2),
}

ALIASES: dict[str, str] = {
    "الذهب": "XAUUSD", "ذهب": "XAUUSD", "xau": "XAUUSD", "xauusd": "XAUUSD", "gold": "XAUUSD",
    "paxg": "PAXG", "البيتكوين": "BTC", "بيتكوين": "BTC", "btc": "BTC", "bitcoin": "BTC",
    "الإيثريوم": "ETH", "إيثريوم": "ETH", "اثيريوم": "ETH", "eth": "ETH", "ethereum": "ETH",
    "سولانا": "SOL", "sol": "SOL", "solana": "SOL", "الفضة": "XAGUSD", "فضة": "XAGUSD", "silver": "XAGUSD", "xag": "XAGUSD",
    "النفط": "USO", "نفط": "USO", "oil": "USO", "crude": "USO", "تسلا": "TSLA", "tesla": "TSLA", "tsla": "TSLA",
    "انفيديا": "NVDA", "إنفيديا": "NVDA", "nvidia": "NVDA", "nvda": "NVDA", "ابل": "AAPL", "أبل": "AAPL", "apple": "AAPL", "aapl": "AAPL",
    "مايكروسوفت": "MSFT", "microsoft": "MSFT", "msft": "MSFT", "sp500": "SPY", "s&p500": "SPY", "nasdaq": "QQQ", "qqq": "QQQ",
    "اليورو": "EURUSD", "يورو": "EURUSD", "eurusd": "EURUSD", "eur/usd": "EURUSD", "euro": "EURUSD",
    "الاسترليني": "GBPUSD", "الجنيه": "GBPUSD", "gbpusd": "GBPUSD", "gbp/usd": "GBPUSD", "pound": "GBPUSD",
    "الين": "USDJPY", "usd/jpy": "USDJPY", "usdjpy": "USDJPY", "jpy": "USDJPY", "yen": "USDJPY",
}


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower().strip()
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    return re.sub(r"\s+", " ", text)


def resolve_asset(text: str | None) -> Asset | None:
    raw = text or ""
    normalized = normalize_text(raw)
    if not normalized:
        return None
    compact = normalized.replace(" ", "")
    candidates = sorted(set([normalized, compact] + re.findall(r"[a-zA-Z0-9&/=]+", normalized)), key=len, reverse=True)
    candidates += sorted((alias for alias in ALIASES if alias in normalized), key=len, reverse=True)
    for candidate in candidates:
        key = ALIASES.get(candidate) or (candidate.upper() if candidate.upper() in ASSETS else None)
        if key and key in ASSETS:
            return ASSETS[key]

    # Permit an explicit uppercase ticker that is not in the catalog; Yahoo can
    # resolve many additional listed instruments without requiring a code change.
    for token in re.findall(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9.=^-]{0,9}(?![A-Za-z0-9])", raw):
        if token in {"USD", "API", "RSI", "SMA", "ATR"}:
            continue
        return Asset(token, token, token, "custom", token, "USD", 4)
    return None