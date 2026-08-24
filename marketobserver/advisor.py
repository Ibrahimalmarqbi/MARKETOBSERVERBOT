from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .analysis import Analysis, analyze
from .assets import Asset
from .market_data import Candle, DataUnavailable, MarketDataProvider


@dataclass(frozen=True)
class TimeframeView:
    timeframe: str
    analysis: Analysis


@dataclass(frozen=True)
class Advice:
    asset: Asset
    current_price: float
    action: str
    confidence: str
    reason: str
    entry_low: float | None
    entry_high: float | None
    invalidation: float | None
    target_one: float | None
    target_two: float | None
    risk_reward: float | None
    views: tuple[TimeframeView, ...]
    source: str | None
    as_of: str


def _round(value: float, decimals: int) -> float:
    return round(float(value), decimals)


def _get_view(provider: MarketDataProvider, asset: Asset, timeframe: str) -> TimeframeView:
    candles = provider.get_candles(asset, timeframe, 200)
    return TimeframeView(timeframe, analyze(candles, asset.price_decimals))


def build_advice(provider: MarketDataProvider, asset: Asset, preferred_timeframe: str | None = None) -> Advice:
    timeframes = ["1d", "1h"]
    if preferred_timeframe in {"15m", "4h"}:
        # The provider may not offer native 4h candles consistently. Keep the
        # requested timeframe visible only when the provider returns it; the
        # mandatory daily/hourly views remain the higher-confidence baseline.
        timeframes.insert(0, preferred_timeframe)
    views: list[TimeframeView] = []
    errors: list[str] = []
    for timeframe in timeframes:
        try:
            views.append(_get_view(provider, asset, timeframe))
        except (DataUnavailable, ValueError) as exc:
            errors.append(f"{timeframe}: {exc}")
    if not views:
        raise DataUnavailable(f"No analysis timeframe available for {asset.key}")

    daily = next((view.analysis for view in views if view.timeframe == "1d"), views[-1].analysis)
    execution = next((view.analysis for view in views if view.timeframe == "1h"), views[0].analysis)
    price = execution.price
    decimals = asset.price_decimals
    bullish = sum(view.analysis.trend == "bullish" for view in views)
    bearish = sum(view.analysis.trend == "bearish" for view in views)
    atr = max(execution.atr14, price * 0.001)

    if bullish >= 2 and execution.signal == "watch_long":
        action = "watch_long"
        confidence = "medium" if bullish < len(views) else "high"
        entry_low = price - atr * 0.35
        entry_high = price + atr * 0.10
        invalidation = price - atr * 1.25
        target_one = price + atr * 1.50
        target_two = price + atr * 2.50
        risk_reward = (target_one - entry_high) / max(entry_high - invalidation, 1e-9)
        reason = "The higher and execution timeframes align bullishly, while momentum remains below an extreme zone."
    elif bearish >= 2 and execution.signal == "watch_short":
        action = "watch_short"
        confidence = "medium" if bearish < len(views) else "high"
        entry_low = price - atr * 0.10
        entry_high = price + atr * 0.35
        invalidation = price + atr * 1.25
        target_one = price - atr * 1.50
        target_two = price - atr * 2.50
        risk_reward = (entry_low - target_one) / max(invalidation - entry_low, 1e-9)
        reason = "The higher and execution timeframes align bearishly, while downside momentum is not yet exhausted."
    else:
        action = "wait"
        confidence = "low" if (bullish and bearish) else "medium"
        entry_low = entry_high = invalidation = target_one = target_two = risk_reward = None
        reason = "The timeframes are mixed or momentum does not confirm a clean setup; waiting for confirmation is the safer state."

    as_of = max(view.analysis.candle_time for view in views)
    return Advice(
        asset=asset,
        current_price=_round(price, decimals),
        action=action,
        confidence=confidence,
        reason=reason + (f" Unavailable views: {', '.join(errors)}." if errors else ""),
        entry_low=_round(entry_low, decimals) if entry_low is not None else None,
        entry_high=_round(entry_high, decimals) if entry_high is not None else None,
        invalidation=_round(invalidation, decimals) if invalidation is not None else None,
        target_one=_round(target_one, decimals) if target_one is not None else None,
        target_two=_round(target_two, decimals) if target_two is not None else None,
        risk_reward=_round(risk_reward, 2) if risk_reward is not None else None,
        views=tuple(views),
        source=provider.last_source(asset.key),
        as_of=as_of,
    )