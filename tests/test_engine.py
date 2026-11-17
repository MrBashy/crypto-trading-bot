import pytest

from bot import secrets_store
from bot.brokers import LiveBroker
from bot.engine import Engine, EngineError
from tests.conftest import FakeExchange

CROSS_UP = [100 - i for i in range(20)] + [85, 95]       # buy signal on last candle
CROSS_DOWN = [80 + i for i in range(20)] + [104, 92]     # sell signal on last candle
FLAT = [100.0] * 22                                       # no signal


def make_engine(db, settings, market, clock, **kw):
    return Engine(db, settings, market_factory=lambda ex: market, clock=clock, **kw)


def test_buys_on_signal_when_running(db, settings, market, clock):
    market.closes["BTC/USD"] = CROSS_UP
    market.prices["BTC/USD"] = 95.0
    e = make_engine(db, settings, market, clock)
    e.start_trading()
    e.tick()
    pos = db.open_positions("paper")
    assert len(pos) == 1
    p = pos[0]
    assert p["symbol"] == "BTC/USD"
    assert p["cost"] == pytest.approx(1000)                  # 10% of 10,000
    assert p["stop_price"] == pytest.approx(95 * 0.98)
    assert p["take_profit_price"] == pytest.approx(95 * 1.04)
    assert db.get("paper_cash") == pytest.approx(9000)
    assert e.snapshot()["equity"] == pytest.approx(9000 + p["amount"] * 95)
    # The same candle must never trigger a second buy.
    e.tick()
    assert len(db.open_positions("paper")) == 1
    assert len(db.query("SELECT * FROM orders")) == 1


def test_no_buy_when_stopped(db, settings, market, clock):
    market.closes["BTC/USD"] = CROSS_UP
    market.prices["BTC/USD"] = 95.0
    e = make_engine(db, settings, market, clock)
    e.tick()
    assert db.open_positions("paper") == []
    assert any("bot is stopped" in l["message"] for l in db.recent_logs())


def test_skips_candle_fetch_until_new_candle_closes(db, settings, market, clock):
    market.closes["BTC/USD"] = FLAT
    market.prices["BTC/USD"] = 100.0
    e = make_engine(db, settings, market, clock)
    e.tick()
    assert market.candle_calls == 1
    clock.advance(15)
    e.tick()
    assert market.candle_calls == 1      # same candle - no request
    clock.advance(300)
    e.tick()
    assert market.candle_calls == 2


def test_stop_loss_closes_position_and_books_pnl(db, settings, market, clock):
    market.closes["BTC/USD"] = CROSS_UP
    market.prices["BTC/USD"] = 95.0
    e = make_engine(db, settings, market, clock)
    e.start_trading()
    e.tick()
    amount = db.open_positions("paper")[0]["amount"]
    market.prices["BTC/USD"] = 93.0          # below the 2% stop (93.1)
    clock.advance(10)
    e.tick()
    assert db.open_positions("paper") == []
    t = db.trades("paper")[0]
    proceeds = amount * 93 * (1 - 0.001)
    assert t["pnl"] == pytest.approx(proceeds - 1000)
    assert t["exit_reason"].startswith("stop-loss")
    assert db.get("paper_cash") == pytest.approx(9000 + proceeds)


def test_take_profit(db, settings, market, clock):
    market.closes["BTC/USD"] = CROSS_UP
    market.prices["BTC/USD"] = 95.0
    e = make_engine(db, settings, market, clock)
    e.start_trading()
    e.tick()
    market.prices["BTC/USD"] = 99.0          # above the 4% target (98.8)
    e.tick()
    t = db.trades("paper")[0]
    assert t["exit_reason"].startswith("take-profit")
    assert t["pnl"] > 0


def test_positions_protected_even_when_stopped(db, settings, market, clock):
    market.closes["BTC/USD"] = CROSS_UP
    market.prices["BTC/USD"] = 95.0
    e = make_engine(db, settings, market, clock)
    e.start_trading()
    e.tick()
    e.stop_trading()
    market.prices["BTC/USD"] = 90.0
    e.tick()
    assert db.open_positions("paper") == []


def test_sell_signal_closes_position(db, settings, market, clock):
    s = settings.model_copy(update={"stop_loss_pct": 40, "take_profit_pct": 0})
    market.closes["BTC/USD"] = CROSS_UP
    market.prices["BTC/USD"] = 95.0
    e = make_engine(db, s, market, clock)
    e.start_trading()
    e.tick()
    clock.advance(300)
    market.closes["BTC/USD"] = CROSS_DOWN
    market.prices["BTC/USD"] = 92.0
    e.tick()
    assert db.open_positions("paper") == []
    assert db.trades("paper")[0]["exit_reason"].startswith("sell signal")


def test_max_open_positions(db, settings, market, clock):
    s = settings.model_copy(update={"symbols": ["BTC/USD", "ETH/USD"], "max_open_positions": 1})
    for sym in s.symbols:
        market.closes[sym] = CROSS_UP
        market.prices[sym] = 95.0
    e = make_engine(db, s, market, clock)
    e.start_trading()
    e.tick()
    assert len(db.open_positions("paper")) == 1


def test_trade_size_capped_by_max_trade_value(db, settings, market, clock):
    s = settings.model_copy(update={"position_size_pct": 50, "max_trade_value": 300})
    market.closes["BTC/USD"] = CROSS_UP
    market.prices["BTC/USD"] = 95.0
    e = make_engine(db, s, market, clock)
    e.start_trading()
    e.tick()
    assert db.open_positions("paper")[0]["cost"] == pytest.approx(300)


def test_daily_loss_limit_halts_until_next_day(db, settings, market, clock):
    s = settings.model_copy(update={"position_size_pct": 100, "max_trade_value": 1e9, "stop_loss_pct": 50,
                                    "take_profit_pct": 0, "max_daily_loss_pct": 5})
    market.closes["BTC/USD"] = FLAT
    market.prices["BTC/USD"] = 100.0
    e = make_engine(db, s, market, clock)
    e.tick()                                  # records today's starting equity (10,000)
    clock.advance(300)
    market.closes["BTC/USD"] = CROSS_UP
    e.start_trading()
    e.tick()                                  # buys with ~99% of the account
    assert len(db.open_positions("paper")) == 1
    market.prices["BTC/USD"] = 94.0          # ~6% loss on the account
    e.tick()
    assert db.open_positions("paper") == []
    assert e.trading_enabled is False
    assert e.halted_today is True
    with pytest.raises(EngineError):
        e.start_trading()
    clock.advance(86400)
    assert e.halted_today is False
    e.start_trading()
    assert e.trading_enabled is True


def test_engine_state_survives_restart(db, settings, market, clock):
    market.closes["BTC/USD"] = CROSS_UP
    market.prices["BTC/USD"] = 95.0
    e = make_engine(db, settings, market, clock)
    e.start_trading()
    e.tick()
    e2 = make_engine(db, settings, market, clock)
    assert e2.trading_enabled is True
    assert len(db.open_positions("paper")) == 1
    e2.tick()                                 # same candle after restart: no double buy
    assert len(db.query("SELECT * FROM orders")) == 1
    market.prices["BTC/USD"] = 90.0
    e2.tick()
    assert db.open_positions("paper") == []


def test_price_failure_is_logged_not_fatal(db, settings, market, clock):
    market.closes["BTC/USD"] = FLAT
    market.prices["BTC/USD"] = 100.0
    market.fail_price = True
    e = make_engine(db, settings, market, clock)
    e.tick()
    assert "network down" in e.state["last_error"]["message"]


def test_emergency_stop_and_manual_close(db, settings, market, clock):
    s = settings.model_copy(update={"symbols": ["BTC/USD", "ETH/USD"]})
    for sym in s.symbols:
        market.closes[sym] = CROSS_UP
        market.prices[sym] = 95.0
    e = make_engine(db, s, market, clock)
    e.start_trading()
    e.tick()
    positions = db.open_positions("paper")
    assert len(positions) == 2
    e.close_position(positions[0]["id"])
    assert len(db.open_positions("paper")) == 1
    assert e.emergency_stop() == []
    assert db.open_positions("paper") == []
    assert e.trading_enabled is False
    with pytest.raises(EngineError):
        e.close_position(12345)


def test_mode_switch_rules(db, settings, market, clock):
    market.closes["BTC/USD"] = CROSS_UP
    market.prices["BTC/USD"] = 95.0
    ex = FakeExchange()
    e = make_engine(db, settings, market, clock, live_client_factory=lambda ex_id, keys: ex,
                    live_broker_kwargs={"sleep": lambda s: None})
    with pytest.raises(EngineError, match="API keys"):
        e.set_mode("live")
    secrets_store.set_api_keys("key", "secret")
    e.start_trading()
    with pytest.raises(EngineError, match="Stop the bot"):
        e.set_mode("live")
    e.tick()
    e.stop_trading()
    with pytest.raises(EngineError, match="Close all open paper positions"):
        e.set_mode("live")
    e.emergency_stop()
    e.set_mode("live")
    assert e.mode == "live"
    assert isinstance(e.broker, LiveBroker)
    e.set_mode("paper")
    assert e.mode == "paper"


def test_live_trading_round_trip(db, settings, market, clock):
    """Full buy -> stop-loss sell cycle through the engine against a fake exchange."""
    ex = FakeExchange(quote_balance=5000, fee_currency="base")
    secrets_store.set_api_keys("key", "secret")
    market.closes["BTC/USD"] = CROSS_UP
    market.prices["BTC/USD"] = 50000.0
    e = make_engine(db, settings, market, clock, live_client_factory=lambda ex_id, keys: ex,
                    live_broker_kwargs={"sleep": lambda s: None})
    e.set_mode("live")
    e.start_trading()
    e.tick()
    pos = db.open_positions("live")
    assert len(pos) == 1
    assert db.open_positions("paper") == []
    # 10% of 5,000 = 500 USD budget -> 0.00999 BTC ordered, 0.1% fee taken in BTC
    assert ex.created[0][3] == pytest.approx(0.00999)
    assert pos[0]["amount"] == pytest.approx(0.00999 * 0.999)
    assert pos[0]["amount"] == pytest.approx(ex.balances["BTC"])
    ex.price = 48000.0
    market.prices["BTC/USD"] = 48000.0        # below 2% stop (49,000)
    e.tick()
    assert db.open_positions("live") == []
    assert ex.created[1][2] == "sell"
    assert ex.created[1][3] == pytest.approx(0.00998)     # whole balance, truncated to precision
    t = db.trades("live")[0]
    assert t["pnl"] < 0
    assert db.trades("paper") == []


def test_live_sell_failure_retries_later(db, settings, market, clock):
    ex = FakeExchange(quote_balance=5000)
    secrets_store.set_api_keys("key", "secret")
    market.closes["BTC/USD"] = CROSS_UP
    market.prices["BTC/USD"] = 50000.0
    e = make_engine(db, settings, market, clock, live_client_factory=lambda ex_id, keys: ex,
                    live_broker_kwargs={"sleep": lambda s: None})
    e.set_mode("live")
    e.start_trading()
    e.tick()
    market.prices["BTC/USD"] = 40000.0
    ex.price = 40000.0
    ex.reject_next = True
    e.tick()
    assert len(db.open_positions("live")) == 1           # failed, still open
    assert "SELL FAILED" in e.state["last_error"]["message"]
    clock.advance(31)
    e.tick()
    assert db.open_positions("live") == []


def test_settings_change_rules(db, settings, market, clock):
    market.closes["BTC/USD"] = CROSS_UP
    market.prices["BTC/USD"] = 95.0
    e = make_engine(db, settings, market, clock)
    e.start_trading()
    e.tick()
    with pytest.raises(EngineError):
        e.apply_settings(settings.model_copy(update={"exchange": "coinbase"}))
    e.apply_settings(settings.model_copy(update={"stop_loss_pct": 3}))
    assert e.settings.stop_loss_pct == 3


def test_reset_paper(db, settings, market, clock):
    market.closes["BTC/USD"] = CROSS_UP
    market.prices["BTC/USD"] = 95.0
    e = make_engine(db, settings, market, clock)
    e.start_trading()
    e.tick()
    with pytest.raises(EngineError):
        e.reset_paper()
    e.stop_trading()
    e.reset_paper()
    assert db.open_positions("paper") == []
    assert db.get("paper_cash") == 10000
    assert db.trades("paper") == []


def test_raising_daily_limit_allows_restart_same_day(db, settings, market, clock):
    s = settings.model_copy(update={"position_size_pct": 100, "max_trade_value": 1e9, "stop_loss_pct": 50,
                                    "take_profit_pct": 0, "max_daily_loss_pct": 5})
    market.closes["BTC/USD"] = FLAT
    market.prices["BTC/USD"] = 100.0
    e = make_engine(db, s, market, clock)
    e.tick()
    clock.advance(300)
    market.closes["BTC/USD"] = CROSS_UP
    e.start_trading()
    e.tick()
    market.prices["BTC/USD"] = 94.0
    e.tick()
    assert e.halted_today
    e.apply_settings(s.model_copy(update={"max_daily_loss_pct": 5.5}))   # still below today's ~6% loss
    with pytest.raises(EngineError):
        e.start_trading()
    e.apply_settings(s.model_copy(update={"max_daily_loss_pct": 10}))
    e.start_trading()
    assert e.trading_enabled and not e.halted_today


def test_exchange_change_blocked_in_live_mode(db, settings, market, clock):
    market.closes["BTC/USD"] = FLAT
    market.prices["BTC/USD"] = 100.0
    secrets_store.set_api_keys("key", "secret")
    e = make_engine(db, settings, market, clock, live_client_factory=lambda ex_id, keys: FakeExchange())
    e.set_mode("live")
    with pytest.raises(EngineError, match="paper mode"):
        e.apply_settings(settings.model_copy(update={"exchange": "coinbase"}))
