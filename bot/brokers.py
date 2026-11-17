"""Order execution.

PaperBroker  - simulates fills against real live prices (no money involved).
LiveBroker   - places real market orders on the exchange with your API keys.

Both return a Fill describing what happened. The engine does all bookkeeping, so
paper and live trades are recorded in exactly the same way.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import ccxt

from .config import Settings


class TradeSkipped(Exception):
    """The trade was not placed (e.g. below the exchange minimum). Nothing happened."""


class OrderFailed(Exception):
    """The exchange rejected the order or it did not fill."""


@dataclass
class Fill:
    amount: float          # base currency actually bought or sold (net of any fee taken in base)
    price: float           # average fill price
    cost: float            # amount * price in quote currency, before fees
    fee: float             # total fee, expressed in quote currency
    order_id: str | None = None
    partial: bool = False  # True if a sell only part-filled (the rest is still held)


class PaperBroker:
    mode = "paper"

    def __init__(self, db, settings: Settings):
        self.db = db
        self.settings = settings

    def cash(self) -> float:
        cash = self.db.get("paper_cash")
        return float(self.settings.paper_starting_balance if cash is None else cash)

    def total_cash(self) -> float:
        return self.cash()

    def buy(self, symbol: str, budget: float, quote: dict) -> Fill:
        s = self.settings
        fee_rate = s.fee_pct / 100
        price = quote["ask"] * (1 + s.slippage_pct / 100)
        budget = min(budget, self.cash())
        # Spend exactly `budget` including the fee.
        amount = budget / (price * (1 + fee_rate))
        cost = amount * price
        if amount <= 0 or cost < 1:
            raise TradeSkipped(f"not enough paper cash to buy {symbol}")
        return Fill(amount=amount, price=price, cost=cost, fee=cost * fee_rate)

    def sell(self, symbol: str, amount: float, quote: dict) -> Fill:
        s = self.settings
        price = quote["bid"] * (1 - s.slippage_pct / 100)
        cost = amount * price
        return Fill(amount=amount, price=price, cost=cost, fee=cost * s.fee_pct / 100)


class LiveBroker:
    mode = "live"

    def __init__(self, client: ccxt.Exchange, settings: Settings, sleep=time.sleep,
                 fill_timeout_seconds: float = 15):
        self.client = client
        self.settings = settings
        self.sleep = sleep
        self.fill_timeout_seconds = fill_timeout_seconds
        self._markets_loaded = False

    def _market(self, symbol: str) -> dict:
        if not self._markets_loaded:
            self.client.load_markets()
            self._markets_loaded = True
        if symbol not in self.client.markets:
            raise TradeSkipped(f"{symbol} is not traded on {self.client.id}")
        return self.client.markets[symbol]

    def balance(self) -> dict:
        return self.client.fetch_balance()

    def cash(self) -> float:
        return float(self.balance().get("free", {}).get(self.settings.quote_currency) or 0)

    def total_cash(self) -> float:
        return float(self.balance().get("total", {}).get(self.settings.quote_currency) or 0)

    def _round_amount(self, symbol: str, amount: float) -> float:
        try:
            return float(self.client.amount_to_precision(symbol, amount))
        except (ccxt.InvalidOrder, ccxt.ArgumentsRequired, ValueError):
            return 0.0

    def _check_limits(self, symbol: str, amount: float, price: float) -> None:
        limits = self._market(symbol).get("limits", {})
        min_amount = (limits.get("amount") or {}).get("min") or 0
        min_cost = (limits.get("cost") or {}).get("min") or 0
        if amount <= 0 or amount < min_amount:
            raise TradeSkipped(f"{symbol}: order size {amount:.8g} is below the exchange minimum {min_amount:.8g}")
        if min_cost and amount * price < min_cost:
            raise TradeSkipped(f"{symbol}: order value {amount * price:.2f} is below the exchange minimum {min_cost:g}")

    def buy(self, symbol: str, budget: float, quote: dict) -> Fill:
        market = self._market(symbol)
        price = quote["ask"]
        fee_rate = self.settings.fee_pct / 100
        amount = self._round_amount(symbol, budget / (price * (1 + fee_rate)))
        self._check_limits(symbol, amount, price)
        # Some exchanges (e.g. Coinbase) need the price to work out a market buy's value.
        order_price = price if self.client.options.get("createMarketBuyOrderRequiresPrice") else None
        try:
            order = self.client.create_order(symbol, "market", "buy", amount, order_price)
        except ccxt.BaseError as e:
            raise OrderFailed(f"buy {symbol} rejected by exchange: {e}") from e
        return self._to_fill(self._wait_for_fill(order, symbol), market, "buy")

    def sell(self, symbol: str, amount: float, quote: dict) -> Fill:
        market = self._market(symbol)
        free_base = float(self.balance().get("free", {}).get(market["base"]) or 0)
        # Never try to sell more than we actually hold (fees may have been taken in the coin).
        amount = self._round_amount(symbol, min(amount, free_base))
        self._check_limits(symbol, amount, quote["bid"])
        try:
            order = self.client.create_order(symbol, "market", "sell", amount)
        except ccxt.BaseError as e:
            raise OrderFailed(f"sell {symbol} rejected by exchange: {e}") from e
        fill = self._to_fill(self._wait_for_fill(order, symbol), market, "sell")
        fill.partial = fill.amount < amount * 0.99
        return fill

    def _wait_for_fill(self, order: dict, symbol: str) -> dict:
        """Market orders fill almost instantly, but exchanges report them asynchronously."""
        order_id = order.get("id")
        deadline = time.monotonic() + self.fill_timeout_seconds
        while True:
            if order.get("status") == "closed" and (order.get("filled") or 0) > 0:
                return order
            if order.get("status") in ("canceled", "cancelled", "rejected", "expired"):
                break
            if time.monotonic() >= deadline:
                break
            self.sleep(1)
            try:
                order = self.client.fetch_order(order_id, symbol)
            except ccxt.BaseError:
                continue
        # Not fully filled in time: cancel any remainder and use whatever did fill.
        if order.get("status") == "open":
            try:
                self.client.cancel_order(order_id, symbol)
            except ccxt.BaseError:
                pass
            try:
                order = self.client.fetch_order(order_id, symbol)
            except ccxt.BaseError:
                pass
        if not (order.get("filled") or 0) > 0:
            raise OrderFailed(f"order {order_id} for {symbol} did not fill (status: {order.get('status')})")
        return order

    def _to_fill(self, order: dict, market: dict, side: str) -> Fill:
        filled = float(order["filled"])
        cost = float(order.get("cost") or 0)
        price = float(order.get("average") or 0) or (cost / filled if cost else 0) or float(order.get("price") or 0)
        if price <= 0:
            raise OrderFailed(f"order {order.get('id')} filled but the exchange did not report a price")
        if not cost:
            cost = filled * price
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        fee_quote = fee_base = 0.0
        reported = False
        for f in fees:
            if not f or f.get("cost") is None:
                continue
            reported = True
            if f.get("currency") == market["base"]:
                fee_base += float(f["cost"])
            else:
                fee_quote += float(f["cost"])
        if not reported:
            # Exchange didn't report the fee - estimate it from the configured fee rate.
            fee_quote = cost * self.settings.fee_pct / 100
        amount = filled - fee_base if side == "buy" else filled
        return Fill(amount=amount, price=price, cost=cost, fee=fee_quote + fee_base * price,
                    order_id=str(order.get("id")) if order.get("id") is not None else None)
