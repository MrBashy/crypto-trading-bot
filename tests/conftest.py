"""Shared test fixtures: an isolated data dir, a fake market and a fake exchange."""
from __future__ import annotations

import sys
from pathlib import Path

import ccxt
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402
from bot.config import TIMEFRAMES, Settings  # noqa: E402
from bot.db import Database  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Every test gets its own empty data directory - never touches the real one."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    return tmp_path / "data"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


@pytest.fixture
def settings():
    return Settings(symbols=["BTC/USD"], timeframe="5m", fast_ema=3, slow_ema=6, rsi_period=5,
                    rsi_overbought=100, position_size_pct=10, max_trade_value=1000, max_open_positions=2,
                    stop_loss_pct=2, take_profit_pct=4, trailing_stop_pct=0, max_daily_loss_pct=5,
                    paper_starting_balance=10000, fee_pct=0.1, slippage_pct=0)


class FakeClock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeMarket:
    """Stands in for MarketData. Tests set prices and candle closes directly."""

    def __init__(self, clock: FakeClock, timeframe: str = "5m"):
        self.clock = clock
        self.tf = TIMEFRAMES[timeframe]
        self.prices: dict[str, float] = {}
        self.closes: dict[str, list[float]] = {}
        self.candle_calls = 0
        self.fail_price = False

    def price(self, symbol):
        if self.fail_price:
            raise ccxt.NetworkError("network down")
        p = self.prices[symbol]
        return {"last": p, "bid": p, "ask": p}

    def candles(self, symbol, timeframe, count, include_open=False):
        """Candles ending with the most recently CLOSED candle at the fake clock time."""
        self.candle_calls += 1
        closes = self.closes[symbol][-count:]
        tf_ms = self.tf * 1000
        newest = (int(self.clock() * 1000) // tf_ms) * tf_ms - tf_ms
        start = newest - (len(closes) - 1) * tf_ms
        return [[start + i * tf_ms, c, c, c, c, 1.0] for i, c in enumerate(closes)]


class FakeExchange:
    """Minimal ccxt-like exchange for testing live order handling."""

    id = "fake"

    def __init__(self, quote_balance=5000.0, fee_currency="quote", fill_immediately=True, min_amount=0.0001,
                 min_cost=1.0, requires_price=False):
        self.balances = {"USD": quote_balance, "BTC": 0.0}
        self.fee_currency = fee_currency
        self.fill_immediately = fill_immediately
        self.markets = {"BTC/USD": {"symbol": "BTC/USD", "base": "BTC", "quote": "USD", "spot": True,
                                    "limits": {"amount": {"min": min_amount}, "cost": {"min": min_cost}}}}
        self.options = {"createMarketBuyOrderRequiresPrice": True} if requires_price else {}
        self.price = 50000.0
        self.orders: dict[str, dict] = {}
        self.created: list[tuple] = []
        self.fee_rate = 0.001
        self.reject_next = False

    def load_markets(self):
        return self.markets

    def amount_to_precision(self, symbol, amount):
        # truncate to 5 decimals like many exchanges do for BTC
        return f"{int(amount * 100000) / 100000:.5f}"

    def fetch_balance(self):
        return {"free": dict(self.balances), "total": dict(self.balances)}

    def create_order(self, symbol, type_, side, amount, price=None):
        if self.reject_next:
            self.reject_next = False
            raise ccxt.InsufficientFunds("not enough funds")
        self.created.append((symbol, type_, side, amount, price))
        oid = str(len(self.orders) + 1)
        cost = amount * self.price
        fee = cost * self.fee_rate
        if side == "buy":
            self.balances["USD"] -= cost
            if self.fee_currency == "base":
                fee_obj = {"cost": amount * self.fee_rate, "currency": "BTC"}
                self.balances["BTC"] += amount * (1 - self.fee_rate)
            else:
                fee_obj = {"cost": fee, "currency": "USD"}
                self.balances["USD"] -= fee
                self.balances["BTC"] += amount
        else:
            self.balances["BTC"] -= amount
            self.balances["USD"] += cost - fee
            fee_obj = {"cost": fee, "currency": "USD"}
        done = {"id": oid, "status": "closed", "filled": amount, "average": self.price, "cost": cost,
                "fee": fee_obj, "fees": [fee_obj], "price": None}
        self.orders[oid] = done
        if self.fill_immediately:
            return dict(done)
        return {"id": oid, "status": "open", "filled": 0, "average": None, "cost": 0, "fee": None, "fees": []}

    def fetch_order(self, oid, symbol=None):
        return dict(self.orders[oid])

    def cancel_order(self, oid, symbol=None):
        return {}


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def market(clock):
    return FakeMarket(clock)
