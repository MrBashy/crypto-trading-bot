"""Backtester: replays the strategy over historical candles.

Rules (chosen to avoid the usual backtest "cheats"):
  * signals are calculated on a candle's CLOSE and filled at the NEXT candle's OPEN
  * fees and slippage are charged on every fill
  * if a candle touches both the stop and the target, the stop is assumed to hit first
  * if the price gaps through a stop, the fill is at the (worse) open price
"""
from __future__ import annotations

from .config import Settings
from .strategy import compute_indicators, effective_stop, signal_at, stop_and_target


def run_backtest(candles: list[list], settings: Settings, starting_balance: float | None = None) -> dict:
    s = settings
    start = float(starting_balance if starting_balance is not None else s.paper_starting_balance)
    fee_rate, slip = s.fee_pct / 100, s.slippage_pct / 100
    ind = compute_indicators([c[4] for c in candles], s)

    cash = start
    position: dict | None = None
    pending: str | None = None
    trades: list[dict] = []
    equity_curve: list[dict] = []
    fees_paid = 0.0

    def close(time_ms: int, raw_price: float, reason: str) -> None:
        nonlocal cash, position, fees_paid
        price = raw_price * (1 - slip)
        gross = position["amount"] * price
        fee = gross * fee_rate
        proceeds = gross - fee
        cash += proceeds
        fees_paid += fee
        pnl = proceeds - position["cost"]
        trades.append({
            "entry_time": position["entry_time"] // 1000, "exit_time": time_ms // 1000,
            "entry_price": position["entry_price"], "exit_price": price, "amount": position["amount"],
            "pnl": pnl, "pnl_pct": pnl / position["cost"] * 100, "exit_reason": reason,
        })
        position = None

    for i, (t, o, h, low, c) in enumerate(row[:5] for row in candles):
        # 1. Fill the order decided at the previous candle's close, at this candle's open.
        if pending == "buy" and position is None:
            price = o * (1 + slip)
            budget = min(cash * s.position_size_pct / 100, s.max_trade_value, cash * 0.99)
            if budget >= 1:
                amount = budget / (price * (1 + fee_rate))
                cost = amount * price
                fee = cost * fee_rate
                cash -= cost + fee
                fees_paid += fee
                stop, target = stop_and_target(price, s)
                position = {"entry_time": t, "entry_price": price, "amount": amount, "cost": cost + fee,
                            "stop_price": stop, "take_profit_price": target, "highest_price": price}
        elif pending == "sell" and position is not None:
            close(t, o, "sell signal")
        pending = None

        # 2. Protective exits during this candle.
        if position is not None:
            stop, label = effective_stop(position, s)
            target = position["take_profit_price"]
            if low <= stop:
                close(t, min(stop, o), label)
            elif target is not None and h >= target:
                close(t, max(target, o), "take-profit")
            else:
                position["highest_price"] = max(position["highest_price"], h)

        # 3. Strategy signal on this candle's close -> executed at the next open.
        sig = signal_at(ind, i, s)
        if sig.action == "buy" and position is None:
            pending = "buy"
        elif sig.action == "sell" and position is not None:
            pending = "sell"

        equity_curve.append({"time": t // 1000, "equity": cash + (position["amount"] * c if position else 0)})

    final_equity = equity_curve[-1]["equity"] if equity_curve else start
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)

    peak, max_dd = start, 0.0
    for point in equity_curve:
        peak = max(peak, point["equity"])
        max_dd = max(max_dd, (peak - point["equity"]) / peak * 100 if peak else 0)

    return {
        "candles": len(candles),
        "start_time": candles[0][0] // 1000 if candles else None,
        "end_time": candles[-1][0] // 1000 if candles else None,
        "starting_balance": start,
        "final_equity": final_equity,
        "return_pct": (final_equity - start) / start * 100,
        "buy_and_hold_pct": ((candles[-1][4] - candles[0][1]) / candles[0][1] * 100) if candles else 0,
        "trades_count": len(trades),
        "win_rate": (len(wins) / len(trades) * 100) if trades else None,
        "avg_win": (gross_win / len(wins)) if wins else None,
        "avg_loss": (-gross_loss / len(losses)) if losses else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "max_drawdown_pct": max_dd,
        "fees_paid": fees_paid,
        "open_position": position is not None,
        "trades": trades,
        "equity_curve": equity_curve,
    }
