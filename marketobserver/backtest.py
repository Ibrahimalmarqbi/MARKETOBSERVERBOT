from __future__ import annotations

"""Walk-forward backtest for the binary engine on historical bars.

Honesty rules this module is built on:
1. It runs the REAL production `binary.decide` on every step — never a
   simplified copy — so the backtest measures the strategy you actually run.
2. No look-ahead: at step ``i`` the engine only sees bars ``<= i`` (slices,
   never the full series), and the outcome is read from bar ``i + 3``.
3. Binary payout is modeled realistically: a win pays `payout` (default 0.8
   units, the typical 80% broker payout) while a loss costs the full 1.0
   stake. That asymmetry is exactly why a 55% win rate can still lose money.
4. Data limits are reported, not hidden: crypto history is paginated from
   Binance klines, everything else comes from one Yahoo 5m pull (60d max).

A green backtest still proves nothing about the future — it only answers:
"were these rules profitable on THIS past window?"
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests
import yfinance as yf

from .assets import Asset
from .binary import EXPIRY_MINUTES, decide
from .learning import classify
from .market_data import BINANCE_HOSTS, Candle

logger = logging.getLogger(__name__)

WARMUP_5M = 60
WINDOW = 120
EXIT_BARS = EXPIRY_MINUTES // 5  # 15-min expiry = 3 five-minute bars
DEFAULT_PAYOUT = 0.8
MAX_DAYS = 60


class HistoryUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class TradeResult:
    time: datetime
    verdict: str
    ref_price: float
    exit_price: float
    outcome: str  # win | loss | flat
    pnl_units: float


@dataclass(frozen=True)
class BacktestResult:
    asset_key: str
    asset_ar: str
    asset_en: str
    days: int
    bars_5m: int
    payout: float
    trades: tuple[TradeResult, ...] = ()
    wins: int = 0
    losses: int = 0
    flats: int = 0
    net_units: float = 0.0
    max_losing_streak: int = 0
    data_from: str = ""
    data_to: str = ""
    source: str = "unknown"
    monthly: tuple[tuple[str, int, int, float], ...] = ()  # (YYYY-MM, trades, wins, net)
    entry_mode: str = "strict"  # strict (8/8) | scored (all 6 safety gates, enter >= 6/8)

    @property
    def decided(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        return (self.wins / self.decided) if self.decided else 0.0

    @property
    def expectancy(self) -> float:
        return (self.net_units / self.decided) if self.decided else 0.0

    @property
    def breakeven_rate(self) -> float:
        # win*payout - (1-win)*1 = 0  ->  win = 1/(1+payout)
        return 1.0 / (1.0 + self.payout)


def to_15m(candles_5m: list[Candle]) -> list[Candle]:
    """Aggregate 5m bars into 15m bars by flooring timestamps. Gaps (forex
    weekends) simply produce no bar — nothing is interpolated."""
    grouped: dict[datetime, list[Candle]] = {}
    for candle in candles_5m:
        floored = candle.timestamp.replace(minute=(candle.timestamp.minute // 15) * 15, second=0, microsecond=0)
        grouped.setdefault(floored, []).append(candle)
    out: list[Candle] = []
    for stamp in sorted(grouped):
        bars = grouped[stamp]
        out.append(Candle(timestamp=stamp, open=bars[0].open, high=max(bar.high for bar in bars),
                          low=min(bar.low for bar in bars), close=bars[-1].close,
                          volume=sum(bar.volume for bar in bars),
                          buy_volume=sum(bar.buy_volume for bar in bars if bar.buy_volume) or None))
    return out


def _binance_symbol(asset: Asset) -> str | None:
    if asset.binance:
        return asset.binance
    base = (asset.provider_symbol or "").upper().removesuffix("-USD")
    return f"{base}USDT" if base and base.isalnum() else None


def fetch_5m(asset: Asset, days: int, session: requests.Session | None = None) -> tuple[list[Candle], str]:
    """Full 5m history for the window. Raises HistoryUnavailable on failure."""
    days = max(1, min(MAX_DAYS, days))
    if asset.asset_class == "crypto":
        return _fetch_binance_5m(asset, days, session or requests.Session())
    return _fetch_yahoo_5m(asset, days)


def _fetch_binance_5m(asset: Asset, days: int, session: requests.Session) -> tuple[list[Candle], str]:
    symbol = _binance_symbol(asset)
    if not symbol:
        raise HistoryUnavailable(f"No Binance market for {asset.key}")
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    candles: list[Candle] = []
    for host in BINANCE_HOSTS:
        try:
            candles = []
            cursor = since
            while cursor < end:
                response = session.get(f"{host}/api/v3/klines",
                                       params={"symbol": symbol, "interval": "5m",
                                               "startTime": cursor, "limit": 1000},
                                       timeout=(4, 15))
                if response.status_code != 200:
                    raise HistoryUnavailable(f"Binance HTTP {response.status_code} for {symbol}")
                rows = response.json()
                if not rows:
                    break
                for row in rows:
                    candles.append(Candle(
                        timestamp=datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc),
                        open=float(row[1]), high=float(row[2]), low=float(row[3]),
                        close=float(row[4]), volume=float(row[5]),
                        buy_volume=float(row[9]) if row[9] not in (None, "") else None))
                cursor = rows[-1][0] + 1
                if len(rows) < 1000:
                    break
                if len(candles) > days * 320:  # >24h of 5m bars/day + margin
                    break
            if len(candles) >= WARMUP_5M + EXIT_BARS + 1:
                return sorted(candles, key=lambda item: item.timestamp), f"binance:{symbol}"
        except HistoryUnavailable:
            raise
        except Exception as exc:
            logger.warning("backtest history failed on %s: %s", host, exc)
    raise HistoryUnavailable(f"Not enough 5m history for {asset.key}")


def _fetch_yahoo_5m(asset: Asset, days: int) -> tuple[list[Candle], str]:
    try:
        frame = yf.Ticker(asset.provider_symbol).history(period="60d", interval="5m",
                                                          auto_adjust=False, actions=False)
    except Exception as exc:
        raise HistoryUnavailable(f"Yahoo history failed for {asset.key}: {exc}") from exc
    if frame is None or frame.empty:
        raise HistoryUnavailable(f"No Yahoo 5m history for {asset.key}")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    candles: list[Candle] = []
    for timestamp, row in frame.dropna(subset=["Open", "High", "Low", "Close"]).iterrows():
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        moment = timestamp.to_pydatetime().astimezone(timezone.utc)
        if moment < cutoff:
            continue
        candles.append(Candle(timestamp=moment, open=float(row["Open"]), high=float(row["High"]),
                              low=float(row["Low"]), close=float(row["Close"]),
                              volume=float(row.get("Volume", 0) or 0)))
    if len(candles) < WARMUP_5M + EXIT_BARS + 1:
        raise HistoryUnavailable(f"Only {len(candles)} 5m bars for {asset.key}, need 100+")
    return sorted(candles, key=lambda item: item.timestamp), f"yahoo:{asset.provider_symbol}"


def run(candles_5m: list[Candle], asset: Asset, payout: float = DEFAULT_PAYOUT,
        strict: bool = True) -> BacktestResult:
    """Walk-forward simulation. Pure function: no network, fully testable.

    ``strict`` must match the live engine's entry mode (see binary.decide),
    otherwise the backtest measures a different strategy than the one traded.
    """
    bars_15m = to_15m(candles_5m)
    pointer = 0
    trades: list[TradeResult] = []
    for index in range(WARMUP_5M, len(candles_5m) - EXIT_BARS):
        moment = candles_5m[index].timestamp
        while pointer + 1 < len(bars_15m) and bars_15m[pointer + 1].timestamp <= moment:
            pointer += 1
        window_15m = bars_15m[max(0, pointer - WINDOW + 1):pointer + 1]
        if len(window_15m) < 50 or bars_15m[pointer].timestamp > moment:
            continue
        window_5m = candles_5m[max(0, index - WINDOW + 1):index + 1]
        verdict = decide(asset.key, asset.name_ar, asset.name_en, asset.quote,
                         asset.price_decimals, window_5m, window_15m, "backtest", now=moment,
                         strict=strict)
        if verdict.verdict not in {"CALL", "PUT"}:
            continue
        ref = candles_5m[index].close
        exit_price = candles_5m[index + EXIT_BARS].close
        outcome = classify(ref, exit_price, verdict.verdict)
        pnl = payout if outcome == "win" else (-1.0 if outcome == "loss" else 0.0)
        trades.append(TradeResult(moment, verdict.verdict, ref, exit_price, outcome, pnl))

    wins = sum(1 for trade in trades if trade.outcome == "win")
    losses = sum(1 for trade in trades if trade.outcome == "loss")
    flats = len(trades) - wins - losses
    net = round(sum(trade.pnl_units for trade in trades), 3)
    streak = best = 0
    for trade in trades:
        streak = streak + 1 if trade.outcome == "loss" else 0
        best = max(best, streak)
    monthly_map: dict[str, list[float]] = {}
    for trade in trades:
        key = trade.time.strftime("%Y-%m")
        cell = monthly_map.setdefault(key, [0, 0, 0.0])
        cell[0] += 1
        cell[1] += 1 if trade.outcome == "win" else 0
        cell[2] += trade.pnl_units
    monthly = tuple((key, int(cell[0]), int(cell[1]), round(cell[2], 2)) for key, cell in sorted(monthly_map.items()))
    days = (candles_5m[-1].timestamp - candles_5m[0].timestamp).days if candles_5m else 0
    return BacktestResult(
        asset_key=asset.key, asset_ar=asset.name_ar, asset_en=asset.name_en, days=days,
        bars_5m=len(candles_5m), payout=payout, trades=tuple(trades), wins=wins,
        losses=losses, flats=flats, net_units=net, max_losing_streak=best,
        data_from=candles_5m[0].timestamp.isoformat() if candles_5m else "",
        data_to=candles_5m[-1].timestamp.isoformat() if candles_5m else "",
        monthly=monthly,
        entry_mode="strict" if strict else "scored",
    )


def backtest(asset: Asset, days: int = 30, payout: float = DEFAULT_PAYOUT,
             session: requests.Session | None = None, strict: bool = True) -> BacktestResult:
    candles, source = fetch_5m(asset, days, session)
    result = run(candles, asset, payout, strict=strict)
    return BacktestResult(
        asset_key=result.asset_key, asset_ar=result.asset_ar, asset_en=result.asset_en,
        days=result.days, bars_5m=result.bars_5m, payout=result.payout, trades=result.trades,
        wins=result.wins, losses=result.losses, flats=result.flats, net_units=result.net_units,
        max_losing_streak=result.max_losing_streak, data_from=result.data_from,
        data_to=result.data_to, source=source, monthly=result.monthly,
        entry_mode=result.entry_mode,
    )


def render(result: BacktestResult, lang: str = "ar") -> str:
    name = result.asset_ar if lang == "ar" else result.asset_en
    verdict_line = (
        f"✅ مربح على هذه الفترة (+{result.net_units} وحدة)" if result.net_units > 0
        else f"❌ خاسر على هذه الفترة ({result.net_units} وحدة)" if result.net_units < 0
        else "⚪ متعادل على هذه الفترة (0)")
    if lang == "ar":
        mode_ar = ("strict — كل البوابات الثمانية إلزامية" if result.entry_mode == "strict"
                   else "scored — الدخول إذا نجحت بوابات السلامة الست (≥ 6/8)")
        lines = [
            f"🧪 <b>باك تست الثنائي | {name} ({result.asset_key})</b>",
            f"الفترة: ~{result.days} يوم | الشموع: {result.bars_5m} (5m) | المصدر: {result.source}",
            f"وضع الدخول: {mode_ar}",
            f"العائد المفترض: {result.payout:.0%} | نقطة التعادل: دقة {result.breakeven_rate:.0%}",
            "",
            f"القرارات: {len(result.trades)} | ✅ {result.wins} | ❌ {result.losses} | ⚪ {result.flats}",
            f"الدقة: {result.win_rate:.0%} | التوقع/صفقة: {result.expectancy:+.3f} وحدة",
            f"أطول سلسلة خسائر: {result.max_losing_streak}",
            verdict_line,
        ]
        if result.monthly:
            lines += ["", "شهريًا:"]
            for month, count, won, net in result.monthly:
                lines.append(f"• {month}: {count} قرار | {won} فوز | {net:+.2f}")
        lines += ["",
                  "⚠️ الماضي ليس ضمانًا: السبريد والانزلاق وعائد الوسيط الحقيقي قد تختلف. "
                  "إن كانت الدقة تحت التعادل فالاستراتيجية خاسرة رياضيًا على هذه الفترة."]
        return "\n".join(lines)
    verdict_en = (
        f"✅ Profitable on this window (+{result.net_units} units)" if result.net_units > 0
        else f"❌ Losing on this window ({result.net_units} units)" if result.net_units < 0
        else "⚪ Flat on this window (0)")
    mode_en = ("strict — all 8 gates required" if result.entry_mode == "strict"
               else "scored — enter when all 6 safety gates pass (>= 6/8)")
    lines = [
        f"🧪 <b>Binary backtest | {name} ({result.asset_key})</b>",
        f"Window: ~{result.days}d | bars: {result.bars_5m} (5m) | source: {result.source}",
        f"Entry mode: {mode_en}",
        f"Assumed payout: {result.payout:.0%} | breakeven: {result.breakeven_rate:.0%} win rate",
        "",
        f"Signals: {len(result.trades)} | ✅ {result.wins} | ❌ {result.losses} | ⚪ {result.flats}",
        f"Win rate: {result.win_rate:.0%} | expectancy: {result.expectancy:+.3f} units",
        f"Longest losing streak: {result.max_losing_streak}",
        verdict_en,
    ]
    if result.monthly:
        lines += ["", "Monthly:"]
        for month, count, won, net in result.monthly:
            lines.append(f"• {month}: {count} signals | {won} wins | {net:+.2f}")
    lines += ["",
              "⚠️ Past is not a promise: spread, slippage and your broker's real payout may differ. "
              "Below breakeven, the rules lose mathematically on this window."]
    return "\n".join(lines)


def to_dict(result: BacktestResult) -> dict:
    return {
        "asset": result.asset_key,
        "days": result.days,
        "bars_5m": result.bars_5m,
        "payout": result.payout,
        "breakeven_rate": round(result.breakeven_rate, 4),
        "signals": len(result.trades),
        "wins": result.wins,
        "losses": result.losses,
        "flats": result.flats,
        "win_rate": round(result.win_rate, 4),
        "expectancy": round(result.expectancy, 4),
        "net_units": result.net_units,
        "max_losing_streak": result.max_losing_streak,
        "data_from": result.data_from,
        "data_to": result.data_to,
        "source": result.source,
        "entry_mode": result.entry_mode,
        "monthly": [{"month": month, "signals": count, "wins": won, "net": net}
                    for month, count, won, net in result.monthly],
    }
