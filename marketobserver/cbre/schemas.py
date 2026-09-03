from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


Sentiment = Literal["panic", "greed", "neutral", "unknown"]
SignalClass = Literal["observe", "monitor", "confirmed_research"]


@dataclass(frozen=True)
class MessageEvent:
    event_id: str
    source: str
    external_id: str
    observed_at: datetime
    text: str
    language: str = "unknown"
    author_id_hash: str | None = None
    asset_key: str | None = None
    entity_confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketSnapshot:
    asset_key: str
    observed_at: datetime
    bid: float | None = None
    ask: float | None = None
    last_price: float | None = None
    volume: float | None = None
    depth: float | None = None
    provider: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None or self.bid <= 0:
            return None
        return max(0.0, self.ask - self.bid)


@dataclass(frozen=True)
class FeatureVector:
    asset_key: str
    window_start: datetime
    window_end: datetime
    values: dict[str, float]
    quality: float
    schema_version: str = "cbre-v1"


@dataclass(frozen=True)
class ResonanceSignal:
    asset_key: str
    observed_at: datetime
    class_name: SignalClass
    score: float
    probability: float | None
    regime: str
    reasons: tuple[str, ...]
    feature_snapshot: dict[str, float]
    quality: float
    model_version: str | None = None


@dataclass(frozen=True)
class ReplayEvent:
    observed_at: datetime
    kind: Literal["message", "market"]
    payload: MessageEvent | MarketSnapshot