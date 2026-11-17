import math

import pytest
from pydantic import ValidationError

from bot.config import Settings
from bot.indicators import ema, rsi
from bot.strategy import exit_reason, latest_signal, stop_and_target


def test_ema_matches_hand_calculation():
    out = ema([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(2.0)          # seed = SMA(1,2,3)
    assert out[3] == pytest.approx(3.0)          # 4*0.5 + 2*0.5
    assert out[4] == pytest.approx(4.0)          # 5*0.5 + 3*0.5


def test_ema_too_short_input():
    assert ema([1, 2], 3) == [None, None]


def test_rsi_extremes_and_known_value():
    assert rsi([1, 2, 3, 4, 5, 6], 5)[-1] == 100.0
    assert rsi([6, 5, 4, 3, 2, 1], 5)[-1] == pytest.approx(0.0)
    assert rsi([5] * 7, 5)[-1] == 50.0
    # StockCharts' worked RSI example: avg gain 3.34/14, avg loss 1.40/14 -> RSI 70.46
    # (their table shows 70.53 because it rounds the averages first)
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
    expected = 100 - 100 / (1 + (3.34 / 14) / (1.40 / 14))
    assert rsi(closes, 14)[-1] == pytest.approx(expected, abs=1e-6)
    assert expected == pytest.approx(70.46, abs=0.01)


def _cross_up_series():
    # falling, then a sharp rise -> fast EMA crosses above slow EMA on the last candle
    return [100 - i for i in range(20)] + [85, 95]


def test_buy_signal_on_cross_up(settings):
    closes = _cross_up_series()
    # make sure the cross happens exactly on the last candle
    sig_prev = latest_signal(closes[:-1], settings)
    sig = latest_signal(closes, settings)
    assert sig_prev.action is None
    assert sig.action == "buy"
    assert sig.fast > sig.slow


def test_rsi_filter_blocks_buy(settings):
    s = settings.model_copy(update={"rsi_overbought": 50})
    sig = latest_signal(_cross_up_series(), s)
    assert sig.action is None
    assert "RSI" in sig.reason


def test_sell_signal_on_cross_down(settings):
    closes = [80 + i for i in range(20)] + [104, 92]
    assert latest_signal(closes[:-1], settings).action is None
    assert latest_signal(closes, settings).action == "sell"


def test_not_enough_data(settings):
    assert latest_signal([1, 2, 3], settings).action is None


def test_stop_target_and_exits(settings):
    stop, target = stop_and_target(100, settings)
    assert stop == pytest.approx(98)
    assert target == pytest.approx(104)
    pos = {"stop_price": stop, "take_profit_price": target, "highest_price": 100}
    assert exit_reason(pos, 99, settings) is None
    assert exit_reason(pos, 98, settings).startswith("stop-loss")
    assert exit_reason(pos, 104.5, settings).startswith("take-profit")


def test_take_profit_off(settings):
    s = settings.model_copy(update={"take_profit_pct": 0})
    _, target = stop_and_target(100, s)
    assert target is None
    assert exit_reason({"stop_price": 98, "take_profit_price": None, "highest_price": 100}, 1000, s) is None


def test_trailing_stop(settings):
    s = settings.model_copy(update={"trailing_stop_pct": 1, "take_profit_pct": 0})
    pos = {"stop_price": 98, "take_profit_price": None, "highest_price": 110}
    # trailing level = 108.9, above the fixed stop
    assert exit_reason(pos, 109, s) is None
    assert exit_reason(pos, 108.8, s).startswith("trailing stop")


def test_settings_validation():
    with pytest.raises(ValidationError):
        Settings(fast_ema=30, slow_ema=20)
    with pytest.raises(ValidationError):
        Settings(symbols=["BTC/USD", "ETH/EUR"])
    with pytest.raises(ValidationError):
        Settings(symbols="BTCUSD")
    with pytest.raises(ValidationError):
        Settings(exchange="nope")
    with pytest.raises(ValidationError):
        Settings(stop_loss_pct=0)
    with pytest.raises(ValidationError):
        Settings(unknown_field=1)
    s = Settings(symbols=" btc/usd, eth/usd ,BTC/USD")
    assert s.symbols == ["BTC/USD", "ETH/USD"]
    assert s.quote_currency == "USD"
    assert math.isclose(s.timeframe_seconds, 300)
