"""Trading rules. Shared by the live/paper engine and the backtester so both behave identically.

Entry (BUY):  the fast EMA crosses ABOVE the slow EMA on the last closed candle,
              and RSI is below the "overbought" level (don't chase spikes).
Exit (SELL):  whichever happens first -
              * stop-loss:      price falls stop_loss_pct below the entry price
              * trailing stop:  price falls trailing_stop_pct below the highest price since entry (if enabled)
              * take-profit:    price rises take_profit_pct above the entry price (if enabled)
              * signal:         the fast EMA crosses BELOW the slow EMA on a closed candle
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .indicators import ema, rsi


@dataclass
class Indicators:
    fast: list[float | None]
    slow: list[float | None]
    rsi: list[float | None]


@dataclass
class Signal:
    action: str | None  # "buy", "sell" or None
    reason: str
    fast: float | None = None
    slow: float | None = None
    rsi: float | None = None


def compute_indicators(closes: list[float], settings: Settings) -> Indicators:
    return Indicators(
        fast=ema(closes, settings.fast_ema),
        slow=ema(closes, settings.slow_ema),
        rsi=rsi(closes, settings.rsi_period),
    )


def signal_at(ind: Indicators, i: int, settings: Settings) -> Signal:
    """Signal produced by the candle at index i (which must be a CLOSED candle)."""
    if i < 1:
        return Signal(None, "not enough data")
    f0, s0, f1, s1 = ind.fast[i - 1], ind.slow[i - 1], ind.fast[i], ind.slow[i]
    r = ind.rsi[i]
    if None in (f0, s0, f1, s1, r):
        return Signal(None, "not enough data", f1, s1, r)
    if f0 <= s0 and f1 > s1:
        if r < settings.rsi_overbought:
            return Signal("buy", f"EMA {settings.fast_ema} crossed above EMA {settings.slow_ema} (RSI {r:.1f})", f1, s1, r)
        return Signal(None, f"bullish cross ignored: RSI {r:.1f} >= {settings.rsi_overbought:g}", f1, s1, r)
    if f0 >= s0 and f1 < s1:
        return Signal("sell", f"EMA {settings.fast_ema} crossed below EMA {settings.slow_ema}", f1, s1, r)
    return Signal(None, "no crossover", f1, s1, r)


def latest_signal(closes: list[float], settings: Settings) -> Signal:
    """Signal from the most recent candle in `closes` (all candles must be closed)."""
    return signal_at(compute_indicators(closes, settings), len(closes) - 1, settings)


def stop_and_target(entry_price: float, settings: Settings) -> tuple[float, float | None]:
    stop = entry_price * (1 - settings.stop_loss_pct / 100)
    target = entry_price * (1 + settings.take_profit_pct / 100) if settings.take_profit_pct > 0 else None
    return stop, target


def effective_stop(position: dict, settings: Settings) -> tuple[float, str]:
    """The price at which the position should be sold for protection, and which rule set it."""
    stop, label = position["stop_price"], "stop-loss"
    if settings.trailing_stop_pct > 0:
        trail = position["highest_price"] * (1 - settings.trailing_stop_pct / 100)
        if trail > stop:
            stop, label = trail, "trailing stop"
    return stop, label


def exit_reason(position: dict, price: float, settings: Settings) -> str | None:
    """Should an open position be closed at the current price? Returns the reason or None."""
    stop, label = effective_stop(position, settings)
    if price <= stop:
        return f"{label} hit ({price:.6g} <= {stop:.6g})"
    target = position.get("take_profit_price")
    if target is not None and price >= target:
        return f"take-profit hit ({price:.6g} >= {target:.6g})"
    return None
