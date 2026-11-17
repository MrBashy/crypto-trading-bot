import pytest

from bot.backtest import run_backtest

TF = 300_000


def candles_from(rows):
    """rows: list of (open, high, low, close)"""
    return [[i * TF, o, h, l, c, 1.0] for i, (o, h, l, c) in enumerate(rows)]


def flat(closes):
    return [(c, c, c, c) for c in closes]


def test_signal_fills_at_next_open_not_same_close(settings):
    s = settings.model_copy(update={"take_profit_pct": 0, "stop_loss_pct": 40})
    closes = [100 - i for i in range(20)] + [85, 95]
    rows = flat(closes) + [(97, 97, 97, 97)]
    r = run_backtest(candles_from(rows), s, starting_balance=10000)
    assert r["trades_count"] == 0
    assert r["open_position"] is True
    # entry happened at the open of the candle AFTER the signal (97, not 95)
    eq = r["equity_curve"][-1]["equity"]
    amount = 1000 / (97 * 1.001)
    assert eq == pytest.approx(9000 + amount * 97)


def test_stop_loss_and_gap_fill(settings):
    s = settings.model_copy(update={"take_profit_pct": 0})
    closes = [100 - i for i in range(20)] + [85, 95]
    rows = flat(closes) + [(100, 100, 100, 100), (90, 90, 90, 90)]   # gaps far below the 98 stop
    r = run_backtest(candles_from(rows), s, starting_balance=10000)
    assert r["trades_count"] == 1
    t = r["trades"][0]
    assert t["exit_price"] == pytest.approx(90)          # filled at the (worse) gap open, not at 98
    assert t["exit_reason"] == "stop-loss"


def test_stop_wins_when_both_hit_same_candle(settings):
    closes = [100 - i for i in range(20)] + [85, 95]
    rows = flat(closes) + [(100, 110, 90, 100)]
    r = run_backtest(candles_from(rows), settings, starting_balance=10000)
    assert r["trades"][0]["exit_reason"] == "stop-loss"


def test_take_profit(settings):
    closes = [100 - i for i in range(20)] + [85, 95]
    rows = flat(closes) + [(100, 100, 100, 100), (101, 105, 101, 103)]
    r = run_backtest(candles_from(rows), settings, starting_balance=10000)
    t = r["trades"][0]
    assert t["exit_reason"] == "take-profit"
    assert t["exit_price"] == pytest.approx(104)
    assert t["pnl"] > 0
    assert r["win_rate"] == 100


def test_cash_is_conserved(settings):
    """Final equity must equal start + sum of trade P&L when flat at the end."""
    import math
    closes = [100 + 10 * math.sin(i / 3) for i in range(300)]
    rows = [(c, c * 1.01, c * 0.99, c) for c in closes]
    r = run_backtest(candles_from(rows), settings, starting_balance=10000)
    assert r["trades_count"] > 3
    if not r["open_position"]:
        assert r["final_equity"] == pytest.approx(10000 + sum(t["pnl"] for t in r["trades"]))
    assert 0 <= r["max_drawdown_pct"] < 100
