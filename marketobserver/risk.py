from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class RiskResult:
    risk_amount: float
    stop_distance: float
    quantity: float
    notional: float


def calculate_position_size(
    capital: float,
    risk_percent: float,
    entry: float,
    stop_loss: float,
    point_value: float = 1.0,
    max_risk_percent: float = 2.0,
    max_notional: float | None = None,
) -> RiskResult:
    values = (capital, risk_percent, entry, stop_loss, point_value)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("All values must be finite")
    if capital <= 0 or not 0 < risk_percent <= max_risk_percent:
        raise ValueError(f"Risk must be greater than 0 and no more than {max_risk_percent}%")
    if entry <= 0 or stop_loss <= 0 or entry == stop_loss or point_value <= 0:
        raise ValueError("Entry, stop loss, and point value must be positive and distinct")
    risk_amount = capital * risk_percent / 100.0
    stop_distance = abs(entry - stop_loss)
    quantity = risk_amount / (stop_distance * point_value)
    notional = quantity * entry
    if max_notional is not None and notional > max_notional:
        quantity = max_notional / entry
        notional = max_notional
    return RiskResult(
        risk_amount=round(risk_amount, 2),
        stop_distance=round(stop_distance, 8),
        quantity=round(quantity, 8),
        notional=round(notional, 2),
    )