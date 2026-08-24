from __future__ import annotations

from dataclasses import dataclass
from statistics import mean

from .market_data import Candle


@dataclass(frozen=True)
class Analysis:
    price: float
    rsi: float
    sma20: float
    sma50: float
    atr14: float
    support: float
    resistance: float
    trend: str
    signal: str
    candle_time: str
    data_points: int


def _round(value: float, decimals: int = 6) -> float:
    return round(float(value), decimals)


def rsi_wilder(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for index in range(period, len(changes)):
        avg_gain = ((avg_gain * (period - 1)) + gains[index]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[index]) / period
    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0
    return round(100 - (100 / (1 + (avg_gain / avg_loss))), 2)


def atr_wilder(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    true_ranges = []
    for index, candle in enumerate(candles):
        previous_close = candles[index - 1].close if index else candle.close
        true_ranges.append(max(candle.high - candle.low, abs(candle.high - previous_close), abs(candle.low - previous_close)))
    atr = sum(true_ranges[1:period + 1]) / period
    for value in true_ranges[period + 1:]:
        atr = ((atr * (period - 1)) + value) / period
    return _round(atr)


def analyze(candles: list[Candle], decimals: int = 2) -> Analysis:
    if len(candles) < 50:
        raise ValueError("At least 50 candles are required")
    closes = [c.close for c in candles]
    current = closes[-1]
    sma20 = mean(closes[-20:])
    sma50 = mean(closes[-50:])
    atr14 = atr_wilder(candles)
    window = candles[-50:]
    support = min(c.low for c in window)
    resistance = max(c.high for c in window)
    trend = "bullish" if current > sma20 > sma50 else "bearish" if current < sma20 < sma50 else "sideways"
    rsi = rsi_wilder(closes)
    if trend == "bullish" and 50 <= rsi <= 70:
        signal = "watch_long"
    elif trend == "bearish" and 30 <= rsi <= 50:
        signal = "watch_short"
    else:
        signal = "neutral"
    return Analysis(
        price=round(current, decimals), rsi=rsi, sma20=round(sma20, decimals), sma50=round(sma50, decimals),
        atr14=round(atr14, decimals), support=round(support, decimals), resistance=round(resistance, decimals),
        trend=trend, signal=signal, candle_time=candles[-1].timestamp.isoformat(), data_points=len(candles),
    )
