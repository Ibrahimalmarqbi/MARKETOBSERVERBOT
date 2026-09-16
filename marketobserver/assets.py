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
    # Explicit live-feed symbols. Every venue mapping is declared here instead
    # of being guessed from the key, so a wrong derivation can never silently
    # point one asset at another asset's feed.
    binance: str | None = None   # e.g. BTCUSDT (spot ticker/klines)
    kraken: str | None = None    # e.g. XBTUSD (fallback crypto venue)
    stooq: str | None = None     # e.g. eurusd (fast free quote feed)


ASSETS: dict[str, Asset] = {
    # ---------------- Crypto (live: Binance -> Kraken -> Yahoo) ----------------
    "BTC": Asset("BTC", "البيتكوين", "Bitcoin", "crypto", "BTC-USD", "USD", 2, binance="BTCUSDT", kraken="XBTUSD", stooq="btcusd"),
    "ETH": Asset("ETH", "الإيثريوم", "Ethereum", "crypto", "ETH-USD", "USD", 2, binance="ETHUSDT", kraken="ETHUSD", stooq="ethusd"),
    "SOL": Asset("SOL", "سولانا", "Solana", "crypto", "SOL-USD", "USD", 4, binance="SOLUSDT", kraken="SOLUSD", stooq="solusd"),
    "BNB": Asset("BNB", "بينانس", "BNB", "crypto", "BNB-USD", "USD", 2, binance="BNBUSDT", stooq="bnbusd"),
    "XRP": Asset("XRP", "ريبل", "XRP", "crypto", "XRP-USD", "USD", 4, binance="XRPUSDT", kraken="XRPUSD", stooq="xrpusd"),
    "DOGE": Asset("DOGE", "دوجكوين", "Dogecoin", "crypto", "DOGE-USD", "USD", 5, binance="DOGEUSDT", kraken="DOGEUSD", stooq="dogeusd"),
    "ADA": Asset("ADA", "كاردانو", "Cardano", "crypto", "ADA-USD", "USD", 4, binance="ADAUSDT", kraken="ADAUSD", stooq="adausd"),
    "AVAX": Asset("AVAX", "أفالانش", "Avalanche", "crypto", "AVAX-USD", "USD", 3, binance="AVAXUSDT", kraken="AVAXUSD", stooq="avaxusd"),
    "LINK": Asset("LINK", "تشين لينك", "Chainlink", "crypto", "LINK-USD", "USD", 3, binance="LINKUSDT", kraken="LINKUSD", stooq="linkusd"),
    "DOT": Asset("DOT", "بولكادوت", "Polkadot", "crypto", "DOT-USD", "USD", 3, binance="DOTUSDT", kraken="DOTUSD", stooq="dotusd"),
    "LTC": Asset("LTC", "لايتكوين", "Litecoin", "crypto", "LTC-USD", "USD", 2, binance="LTCUSDT", kraken="LTCUSD", stooq="ltcusd"),
    "TRX": Asset("TRX", "ترون", "TRON", "crypto", "TRX-USD", "USD", 5, binance="TRXUSDT", kraken="TRXUSD", stooq="trxusd"),
    "ATOM": Asset("ATOM", "كوزموس", "Cosmos", "crypto", "ATOM-USD", "USD", 3, binance="ATOMUSDT", kraken="ATOMUSD", stooq="atomusd"),
    "NEAR": Asset("NEAR", "نير", "NEAR Protocol", "crypto", "NEAR-USD", "USD", 3, binance="NEARUSDT", kraken="NEARUSD", stooq="nearusd"),
    "ARB": Asset("ARB", "أربيترم", "Arbitrum", "crypto", "ARB-USD", "USD", 4, binance="ARBUSDT", kraken="ARBUSD"),
    "OP": Asset("OP", "أوبتيميزم", "Optimism", "crypto", "OP-USD", "USD", 4, binance="OPUSDT", kraken="OPUSD"),
    "TON": Asset("TON", "تونكوين", "Toncoin", "crypto", "TON-USD", "USD", 4, binance="TONUSDT"),
    "SUI": Asset("SUI", "سوي", "Sui", "crypto", "SUI-USD", "USD", 4, binance="SUIUSDT", kraken="SUIUSD"),
    "PEPE": Asset("PEPE", "بيبي", "Pepe", "crypto", "PEPE-USD", "USD", 8, binance="PEPEUSDT", kraken="PEPEUSD"),
    "SHIB": Asset("SHIB", "شيبا", "Shiba Inu", "crypto", "SHIB-USD", "USD", 8, binance="SHIBUSDT", kraken="SHIBUSD"),
    "PAXG": Asset("PAXG", "ذهب رقمي", "PAX Gold", "crypto", "PAXG-USD", "USD", 2, binance="PAXGUSDT"),
    # ---------------- Forex majors (binary-trading core) ----------------
    "EURUSD": Asset("EURUSD", "اليورو/الدولار", "EUR/USD", "forex", "EURUSD=X", "USD", 5, stooq="eurusd"),
    "GBPUSD": Asset("GBPUSD", "الجنيه/الدولار", "GBP/USD", "forex", "GBPUSD=X", "USD", 5, stooq="gbpusd"),
    "USDJPY": Asset("USDJPY", "الدولار/الين", "USD/JPY", "forex", "JPY=X", "JPY", 3, stooq="usdjpy"),
    "AUDUSD": Asset("AUDUSD", "الأسترالي/الدولار", "AUD/USD", "forex", "AUDUSD=X", "USD", 5, stooq="audusd"),
    "USDCAD": Asset("USDCAD", "الدولار/الكندي", "USD/CAD", "forex", "CAD=X", "CAD", 5, stooq="usdcad"),
    "USDCHF": Asset("USDCHF", "الدولار/الفرنك", "USD/CHF", "forex", "CHF=X", "CHF", 5, stooq="usdchf"),
    "NZDUSD": Asset("NZDUSD", "النيوزلندي/الدولار", "NZD/USD", "forex", "NZDUSD=X", "USD", 5, stooq="nzdusd"),
    # ---------------- Forex crosses (popular on binary platforms) ----------------
    "EURGBP": Asset("EURGBP", "اليورو/الجنيه", "EUR/GBP", "forex", "EURGBP=X", "GBP", 5, stooq="eurgbp"),
    "EURJPY": Asset("EURJPY", "اليورو/الين", "EUR/JPY", "forex", "EURJPY=X", "JPY", 3, stooq="eurjpy"),
    "GBPJPY": Asset("GBPJPY", "الجنيه/الين", "GBP/JPY", "forex", "GBPJPY=X", "JPY", 3, stooq="gbpjpy"),
    "AUDJPY": Asset("AUDJPY", "الأسترالي/الين", "AUD/JPY", "forex", "AUDJPY=X", "JPY", 3, stooq="audjpy"),
    "NZDJPY": Asset("NZDJPY", "النيوزلندي/الين", "NZD/JPY", "forex", "NZDJPY=X", "JPY", 3, stooq="nzdjpy"),
    "CADJPY": Asset("CADJPY", "الكندي/الين", "CAD/JPY", "forex", "CADJPY=X", "JPY", 3, stooq="cadjpy"),
    "CHFJPY": Asset("CHFJPY", "الفرنك/الين", "CHF/JPY", "forex", "CHFJPY=X", "JPY", 3, stooq="chfjpy"),
    "EURAUD": Asset("EURAUD", "اليورو/الأسترالي", "EUR/AUD", "forex", "EURAUD=X", "AUD", 5, stooq="euraud"),
    "EURCAD": Asset("EURCAD", "اليورو/الكندي", "EUR/CAD", "forex", "EURCAD=X", "CAD", 5, stooq="eurcad"),
    "EURNZD": Asset("EURNZD", "اليورو/النيوزلندي", "EUR/NZD", "forex", "EURNZD=X", "NZD", 5, stooq="eurnzd"),
    "GBPAUD": Asset("GBPAUD", "الجنيه/الأسترالي", "GBP/AUD", "forex", "GBPAUD=X", "AUD", 5, stooq="gbpaud"),
    "GBPCAD": Asset("GBPCAD", "الجنيه/الكندي", "GBP/CAD", "forex", "GBPCAD=X", "CAD", 5, stooq="gbpcad"),
    "AUDCAD": Asset("AUDCAD", "الأسترالي/الكندي", "AUD/CAD", "forex", "AUDCAD=X", "CAD", 5, stooq="audcad"),
    "AUDNZD": Asset("AUDNZD", "الأسترالي/النيوزلندي", "AUD/NZD", "forex", "AUDNZD=X", "NZD", 5, stooq="audnzd"),
    "NZDCAD": Asset("NZDCAD", "النيوزلندي/الكندي", "NZD/CAD", "forex", "NZDCAD=X", "CAD", 5, stooq="nzdcad"),
    # ---------------- Forex exotics ----------------
    "USDSEK": Asset("USDSEK", "الدولار/الكرونة السويدية", "USD/SEK", "forex", "SEK=X", "SEK", 5, stooq="usdsek"),
    "USDNOK": Asset("USDNOK", "الدولار/الكرونة النرويجية", "USD/NOK", "forex", "NOK=X", "NOK", 5, stooq="usdnok"),
    "USDSGD": Asset("USDSGD", "الدولار/الدولار السنغافوري", "USD/SGD", "forex", "SGD=X", "SGD", 5, stooq="usdsgd"),
    "USDMXN": Asset("USDMXN", "الدولار/البيزو المكسيكي", "USD/MXN", "forex", "MXN=X", "MXN", 5, stooq="usdmxn"),
    "USDZAR": Asset("USDZAR", "الدولار/الراند", "USD/ZAR", "forex", "ZAR=X", "ZAR", 5, stooq="usdzar"),
    "USDTRY": Asset("USDTRY", "الدولار/الليرة التركية", "USD/TRY", "forex", "TRY=X", "TRY", 5, stooq="usdtry"),
    # ---------------- Metals / commodities / indices / stocks ----------------
    "XAUUSD": Asset("XAUUSD", "الذهب - مرجع العقود الآجلة", "Gold Futures Reference", "metals", "GC=F", "USD", 2, stooq="xauusd"),
    "XAGUSD": Asset("XAGUSD", "الفضة - مرجع العقود الآجلة", "Silver Futures Reference", "metals", "SI=F", "USD", 3, stooq="xagusd"),
    "USO": Asset("USO", "صندوق النفط USO", "USO Oil ETF", "commodity", "USO", "USD", 2, stooq="uso.us"),
    "WTI": Asset("WTI", "خام غرب تكساس - مرجع العقود الآجلة", "WTI Crude Futures Reference", "commodity", "CL=F", "USD", 2, stooq="cl.f"),
    "BRENT": Asset("BRENT", "خام برنت - مرجع العقود الآجلة", "Brent Crude Futures Reference", "commodity", "BZ=F", "USD", 2),
    "GASOLINE": Asset("GASOLINE", "البنزين - مرجع العقود الآجلة", "RBOB Gasoline Futures Reference", "commodity", "RB=F", "USD", 4, stooq="rb.f"),
    "NATGAS": Asset("NATGAS", "الغاز الطبيعي - مرجع العقود الآجلة", "Natural Gas Futures Reference", "commodity", "NG=F", "USD", 3, stooq="ng.f"),
    "COPPER": Asset("COPPER", "النحاس - مرجع العقود الآجلة", "Copper Futures Reference", "commodity", "HG=F", "USD", 4, stooq="hg.f"),
    "DXY": Asset("DXY", "مؤشر الدولار", "US Dollar Index Futures Reference", "index", "DX-Y.NYB", "USD", 3, stooq="dx.f"),
    "AAPL": Asset("AAPL", "أبل", "Apple", "stock", "AAPL", "USD", 2, stooq="aapl.us"),
    "TSLA": Asset("TSLA", "تسلا", "Tesla", "stock", "TSLA", "USD", 2, stooq="tsla.us"),
    "NVDA": Asset("NVDA", "إنفيديا", "NVIDIA", "stock", "NVDA", "USD", 2, stooq="nvda.us"),
    "MSFT": Asset("MSFT", "مايكروسوفت", "Microsoft", "stock", "MSFT", "USD", 2, stooq="msft.us"),
    "SPY": Asset("SPY", "مؤشر S&P 500", "S&P 500 ETF", "index", "SPY", "USD", 2, stooq="spy.us"),
    "QQQ": Asset("QQQ", "مؤشر ناسداك", "Nasdaq 100 ETF", "index", "QQQ", "USD", 2, stooq="qqq.us"),
}

# Canonical display order for /assets and grouped keyboards.
ASSET_CLASS_ORDER = ("crypto", "forex", "metals", "commodity", "index", "stock")
ASSET_CLASS_NAMES = {
    "crypto": ("العملات الرقمية", "Crypto"),
    "forex": ("الفوركس", "Forex"),
    "metals": ("المعادن", "Metals"),
    "commodity": ("السلع", "Commodities"),
    "index": ("المؤشرات", "Indices"),
    "stock": ("الأسهم", "Stocks"),
}

# The most-traded pairs on binary-options platforms, used for quick buttons.
BINARY_POPULAR = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF",
                  "NZDUSD", "EURJPY", "GBPJPY", "EURGBP", "XAUUSD", "BTC")

ALIASES: dict[str, str] = {
    "الذهب": "XAUUSD", "ذهب": "XAUUSD", "xau": "XAUUSD", "xauusd": "XAUUSD", "gold": "XAUUSD",
    "paxg": "PAXG", "البيتكوين": "BTC", "بيتكوين": "BTC", "btc": "BTC", "bitcoin": "BTC",
    "الايثريوم": "ETH", "ايثريوم": "ETH", "اثيريوم": "ETH", "eth": "ETH", "ethereum": "ETH",
    "سولانا": "SOL", "sol": "SOL", "solana": "SOL",
    "بينانس": "BNB", "bnb": "BNB", "ريبل": "XRP", "xrp": "XRP", "ripple": "XRP",
    "دوجكوين": "DOGE", "دوج": "DOGE", "doge": "DOGE", "dogecoin": "DOGE",
    "كاردانو": "ADA", "ada": "ADA", "افالانش": "AVAX", "avax": "AVAX", "avalanche": "AVAX",
    "تشين لينك": "LINK", "تشينلينك": "LINK", "link": "LINK", "chainlink": "LINK",
    "بولكادوت": "DOT", "dot": "DOT", "polkadot": "DOT",
    "لايتكوين": "LTC", "ltc": "LTC", "litecoin": "LTC",
    "ترون": "TRX", "trx": "TRX", "tron": "TRX",
    "كوزموس": "ATOM", "atom": "ATOM", "cosmos": "ATOM",
    "نير": "NEAR", "near": "NEAR",
    "اربيترم": "ARB", "arb": "ARB", "arbitrum": "ARB",
    "اوبتيميزم": "OP", "op": "OP", "optimism": "OP",
    "تونكوين": "TON", "تون": "TON", "ton": "TON", "toncoin": "TON",
    "عمله سوي": "SUI", "sui": "SUI",
    "عمله بيبي": "PEPE", "pepe": "PEPE",
    "شيبا": "SHIB", "شيب": "SHIB", "shib": "SHIB", "shiba": "SHIB",
    "الفضة": "XAGUSD", "فضة": "XAGUSD", "الفضه": "XAGUSD", "فضه": "XAGUSD", "silver": "XAGUSD", "xag": "XAGUSD",
    "النفط": "WTI", "نفط": "WTI", "خام غرب تكساس": "WTI", "wti": "WTI", "oil": "WTI", "crude": "WTI", "برنت": "BRENT", "خام برنت": "BRENT", "brent": "BRENT", "البنزين": "GASOLINE", "بنزين": "GASOLINE", "gasoline": "GASOLINE", "rbob": "GASOLINE", "الغاز": "NATGAS", "الغاز الطبيعي": "NATGAS", "natural gas": "NATGAS", "natgas": "NATGAS", "النحاس": "COPPER", "copper": "COPPER", "مؤشر الدولار": "DXY", "dxy": "DXY", "تسلا": "TSLA", "tesla": "TSLA", "tsla": "TSLA",
    "انفيديا": "NVDA", "إنفيديا": "NVDA", "nvidia": "NVDA", "nvda": "NVDA", "ابل": "AAPL", "أبل": "AAPL", "apple": "AAPL", "aapl": "AAPL",
    "مايكروسوفت": "MSFT", "microsoft": "MSFT", "msft": "MSFT", "sp500": "SPY", "s&p500": "SPY", "nasdaq": "QQQ", "qqq": "QQQ",
    # ---- Forex majors ----
    "اليورو": "EURUSD", "يورو": "EURUSD", "eurusd": "EURUSD", "eur/usd": "EURUSD", "euro": "EURUSD",
    "الاسترليني": "GBPUSD", "الجنيه": "GBPUSD", "باوند": "GBPUSD", "gbpusd": "GBPUSD", "gbp/usd": "GBPUSD", "pound": "GBPUSD", "cable": "GBPUSD",
    "الين": "USDJPY", "دولار ين": "USDJPY", "الدولار ين": "USDJPY", "usd/jpy": "USDJPY", "usdjpy": "USDJPY", "jpy": "USDJPY", "yen": "USDJPY",
    "الاسترالي": "AUDUSD", "استرالي دولار": "AUDUSD", "audusd": "AUDUSD", "aud/usd": "AUDUSD", "aussie": "AUDUSD",
    "الكندي": "USDCAD", "دولار كندي": "USDCAD", "الدولار الكندي": "USDCAD", "usdcad": "USDCAD", "usd/cad": "USDCAD", "loonie": "USDCAD",
    "الفرنك": "USDCHF", "دولار فرنك": "USDCHF", "الدولار الفرنك": "USDCHF", "usdchf": "USDCHF", "usd/chf": "USDCHF", "swissy": "USDCHF",
    "النيوزلندي": "NZDUSD", "نيوزلندي دولار": "NZDUSD", "nzdusd": "NZDUSD", "nzd/usd": "NZDUSD", "kiwi": "NZDUSD",
    # ---- Forex crosses ----
    "يورو باوند": "EURGBP", "اليورو الباوند": "EURGBP", "eurgbp": "EURGBP", "eur/gbp": "EURGBP",
    "يورو ين": "EURJPY", "اليورو الين": "EURJPY", "eurjpy": "EURJPY", "eur/jpy": "EURJPY",
    "باوند ين": "GBPJPY", "الجنيه الين": "GBPJPY", "gbpjpy": "GBPJPY", "gbp/jpy": "GBPJPY",
    "استرالي ين": "AUDJPY", "audjpy": "AUDJPY", "aud/jpy": "AUDJPY",
    "نيوزلندي ين": "NZDJPY", "nzdjpy": "NZDJPY", "nzd/jpy": "NZDJPY",
    "كندي ين": "CADJPY", "cadjpy": "CADJPY", "cad/jpy": "CADJPY",
    "فرنك ين": "CHFJPY", "chfjpy": "CHFJPY", "chf/jpy": "CHFJPY",
    "يورو استرالي": "EURAUD", "euraud": "EURAUD", "eur/aud": "EURAUD",
    "يورو كندي": "EURCAD", "eurcad": "EURCAD", "eur/cad": "EURCAD",
    "يورو نيوزلندي": "EURNZD", "eurnzd": "EURNZD", "eur/nzd": "EURNZD",
    "باوند استرالي": "GBPAUD", "gbpaud": "GBPAUD", "gbp/aud": "GBPAUD",
    "باوند كندي": "GBPCAD", "gbpcad": "GBPCAD", "gbp/cad": "GBPCAD",
    "استرالي كندي": "AUDCAD", "audcad": "AUDCAD", "aud/cad": "AUDCAD",
    "استرالي نيوزلندي": "AUDNZD", "audnzd": "AUDNZD", "aud/nzd": "AUDNZD",
    "نيوزلندي كندي": "NZDCAD", "nzdcad": "NZDCAD", "nzd/cad": "NZDCAD",
    # ---- Forex exotics ----
    "دولار كرونه سويديه": "USDSEK", "usdsek": "USDSEK", "usd/sek": "USDSEK",
    "دولار كرونه نرويجيه": "USDNOK", "usdnok": "USDNOK", "usd/nok": "USDNOK",
    "دولار سنغافوري": "USDSGD", "usdsgd": "USDSGD", "usd/sgd": "USDSGD",
    "دولار بيزو": "USDMXN", "usdmxn": "USDMXN", "usd/mxn": "USDMXN",
    "دولار راند": "USDZAR", "usdzar": "USDZAR", "usd/zar": "USDZAR",
    "دولار ليره": "USDTRY", "usdlir": "USDTRY", "usdtry": "USDTRY", "usd/try": "USDTRY",
}


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower().strip()
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ة", "ه")
    return re.sub(r"\s+", " ", text)


def _normalized_aliases() -> dict[str, str]:
    # Alias keys are stored in display spelling; matching always happens on the
    # normalized form so hamza/ta-marbuta variants resolve identically.
    return {normalize_text(alias): key for alias, key in ALIASES.items()}


def resolve_asset(text: str | None) -> Asset | None:
    raw = text or ""
    normalized = normalize_text(raw)
    if not normalized:
        return None
    aliases = _normalized_aliases()
    compact = normalized.replace(" ", "")
    candidates = sorted(set([normalized, compact] + re.findall(r"[a-zA-Z0-9&/=]+", normalized)), key=len, reverse=True)
    # Substring matching is what lets Arabic names resolve inside a sentence,
    # but for short Latin tickers it would misfire ("op" inside "stop loss",
    # "ton" inside "button"). Short Latin aliases still resolve as standalone
    # tokens via the branch above; here they need word boundaries or length.
    def _contained(alias: str) -> bool:
        if alias not in normalized:
            return False
        if re.search(r"[^\x00-\x7F]", alias):
            return True
        if len(alias) >= 4:
            return True
        return re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", normalized) is not None
    candidates += sorted((alias for alias in aliases if _contained(alias)), key=len, reverse=True)
    for candidate in candidates:
        key = aliases.get(candidate) or (candidate.upper() if candidate.upper() in ASSETS else None)
        if key and key in ASSETS:
            return ASSETS[key]

    # Permit an explicit uppercase ticker that is not in the catalog; Yahoo can
    # resolve many additional listed instruments without requiring a code change.
    for token in re.findall(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9.=^-]{0,9}(?![A-Za-z0-9])", raw):
        if token in {"USD", "API", "RSI", "SMA", "ATR", "FVG", "BOS", "CHOCH", "OB", "SMC", "HH", "HL", "LH", "LL", "PDH", "PDL", "RR", "TP", "SL"}:
            continue
        return Asset(token, token, token, "custom", token, "USD", 4)
    return None


def assets_by_class(asset_class: str) -> list[Asset]:
    return [asset for asset in ASSETS.values() if asset.asset_class == asset_class and asset.supported]
