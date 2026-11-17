import pytest

from bot.brokers import LiveBroker, OrderFailed, PaperBroker, TradeSkipped
from tests.conftest import FakeExchange

QUOTE = {"last": 50000.0, "bid": 49990.0, "ask": 50010.0}


def test_paper_buy_spends_budget_including_fee(db, settings):
    s = settings.model_copy(update={"fee_pct": 0.4, "slippage_pct": 0.1})
    b = PaperBroker(db, s)
    fill = b.buy("BTC/USD", 1000, QUOTE)
    assert fill.price == pytest.approx(50010 * 1.001)
    assert fill.cost + fill.fee == pytest.approx(1000)
    assert fill.fee == pytest.approx(fill.cost * 0.004)
    assert fill.amount == pytest.approx(fill.cost / fill.price)


def test_paper_buy_capped_by_cash(db, settings):
    db.set("paper_cash", 50.0)
    fill = PaperBroker(db, settings).buy("BTC/USD", 1000, QUOTE)
    assert fill.cost + fill.fee == pytest.approx(50)


def test_paper_buy_with_no_cash_is_skipped(db, settings):
    db.set("paper_cash", 0.0)
    with pytest.raises(TradeSkipped):
        PaperBroker(db, settings).buy("BTC/USD", 1000, QUOTE)


def test_paper_sell_uses_bid_minus_slippage(db, settings):
    s = settings.model_copy(update={"fee_pct": 0.4, "slippage_pct": 0.1})
    fill = PaperBroker(db, s).sell("BTC/USD", 0.02, QUOTE)
    assert fill.price == pytest.approx(49990 * 0.999)
    assert fill.fee == pytest.approx(fill.cost * 0.004)


def test_live_buy_rounds_amount_and_reads_quote_fee(settings):
    ex = FakeExchange()
    b = LiveBroker(ex, settings, sleep=lambda s: None)
    fill = b.buy("BTC/USD", 1000, QUOTE)
    symbol, type_, side, amount, price = ex.created[0]
    assert (symbol, type_, side, price) == ("BTC/USD", "market", "buy", None)
    assert amount == pytest.approx(0.01997)          # 1000 / (50010 * 1.001), truncated to 5 dp
    assert fill.amount == pytest.approx(amount)
    assert fill.price == 50000.0
    assert fill.fee == pytest.approx(amount * 50000 * 0.001)
    assert fill.order_id == "1"


def test_live_buy_passes_price_when_exchange_requires_it(settings):
    ex = FakeExchange(requires_price=True)
    LiveBroker(ex, settings, sleep=lambda s: None).buy("BTC/USD", 1000, QUOTE)
    assert ex.created[0][4] == 50010.0


def test_live_buy_fee_in_base_reduces_amount(settings):
    ex = FakeExchange(fee_currency="base")
    fill = LiveBroker(ex, settings, sleep=lambda s: None).buy("BTC/USD", 1000, QUOTE)
    ordered = ex.created[0][3]
    assert fill.amount == pytest.approx(ordered * 0.999)
    assert ex.balances["BTC"] == pytest.approx(fill.amount)
    assert fill.fee == pytest.approx(ordered * 0.001 * 50000)


def test_live_buy_below_minimum_is_skipped(settings):
    ex = FakeExchange(min_cost=10)
    with pytest.raises(TradeSkipped):
        LiveBroker(ex, settings, sleep=lambda s: None).buy("BTC/USD", 5, QUOTE)
    assert ex.created == []


def test_live_buy_waits_for_async_fill(settings):
    ex = FakeExchange(fill_immediately=False)
    sleeps = []
    fill = LiveBroker(ex, settings, sleep=sleeps.append).buy("BTC/USD", 1000, QUOTE)
    assert sleeps == [1]
    assert fill.amount > 0


def test_live_buy_rejected_raises_order_failed(settings):
    ex = FakeExchange()
    ex.reject_next = True
    with pytest.raises(OrderFailed):
        LiveBroker(ex, settings, sleep=lambda s: None).buy("BTC/USD", 1000, QUOTE)


def test_live_order_that_never_fills(settings):
    ex = FakeExchange(fill_immediately=False)
    b = LiveBroker(ex, settings, sleep=lambda s: None, fill_timeout_seconds=0)
    # make fetch_order always report "open / nothing filled"
    ex.fetch_order = lambda oid, symbol=None: {"id": oid, "status": "open", "filled": 0}
    with pytest.raises(OrderFailed):
        b.buy("BTC/USD", 1000, QUOTE)


def test_live_sell_never_sells_more_than_balance(settings):
    ex = FakeExchange()
    ex.balances["BTC"] = 0.015
    fill = LiveBroker(ex, settings, sleep=lambda s: None).sell("BTC/USD", 0.02, QUOTE)
    assert ex.created[0][2] == "sell"
    assert ex.created[0][3] == pytest.approx(0.015)
    assert fill.partial is False
    assert ex.balances["BTC"] == pytest.approx(0)


def test_live_sell_nothing_to_sell(settings):
    ex = FakeExchange()
    ex.balances["BTC"] = 0
    with pytest.raises(TradeSkipped):
        LiveBroker(ex, settings, sleep=lambda s: None).sell("BTC/USD", 0.02, QUOTE)


def test_live_cash(settings):
    ex = FakeExchange(quote_balance=1234.5)
    b = LiveBroker(ex, settings)
    assert b.cash() == 1234.5
    assert b.total_cash() == 1234.5
