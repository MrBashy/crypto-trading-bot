"""Technical indicators written in plain Python so they are easy to read and test.

Each function returns a list the same length as its input. Positions where the
indicator does not have enough data yet are None.
"""
from __future__ import annotations


def ema(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average, seeded with the simple average of the first `period` values."""
    out: list[float | None] = [None] * len(values)
    if period < 1 or len(values) < period:
        return out
    k = 2 / (period + 1)
    current = sum(values[:period]) / period
    out[period - 1] = current
    for i in range(period, len(values)):
        current = values[i] * k + current * (1 - k)
        out[i] = current
    return out


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    """Relative Strength Index using Wilder's smoothing (the standard definition)."""
    out: list[float | None] = [None] * len(values)
    if period < 1 or len(values) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)
