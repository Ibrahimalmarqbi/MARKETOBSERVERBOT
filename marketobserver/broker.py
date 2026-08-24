from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OrderRequest:
    asset_key: str
    side: str
    quantity: float
    order_type: str = "market"
    stop_loss: float | None = None
    take_profit: float | None = None


@dataclass(frozen=True)
class OrderResult:
    accepted: bool
    order_id: str
    mode: str
    message: str


class Broker(Protocol):
    def place_order(self, order: OrderRequest) -> OrderResult:
        ...


class PaperBroker:
    def place_order(self, order: OrderRequest) -> OrderResult:
        if order.side not in {"buy", "sell"}:
            return OrderResult(False, "", "paper", "Unsupported side")
        if order.quantity <= 0:
            return OrderResult(False, "", "paper", "Quantity must be positive")
        return OrderResult(True, f"paper-{order.asset_key}-{id(order)}", "paper", "Paper order accepted")


class LiveBrokerNotConfigured:
    """Intentional fail-closed adapter until a specific broker is selected and verified."""

    def place_order(self, order: OrderRequest) -> OrderResult:
        return OrderResult(False, "", "live", "Live broker is not configured")