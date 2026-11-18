"""The trading engine: a background thread that watches the market and trades.

Every `poll_interval_seconds` it:
  1. fetches current prices and protects open positions (stop-loss / trailing stop / take-profit)
  2. checks the daily loss limit
  3. when a new candle has closed, evaluates the strategy and buys/sells on crossovers
  4. records an equity snapshot for the dashboard chart

"Running" only controls whether NEW trades are opened. Open positions are always
protected while the program is running, even if the bot is stopped.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import ccxt

from . import secrets_store
from .brokers import Fill, LiveBroker, OrderFailed, PaperBroker, TradeSkipped
from .config import Settings
from .db import Database
from .market import MarketData, make_exchange
from .strategy import effective_stop, exit_reason, latest_signal, stop_and_target

log = logging.getLogger("bot")

EQUITY_SNAPSHOT_SECONDS = 60
EXIT_RETRY_SECONDS = 30


class EngineError(Exception):
    """A user action was refused (message is shown in the dashboard)."""


def utc_today(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class Engine:
    def __init__(self, db: Database, settings: Settings, market_factory=None, live_client_factory=None,
                 clock=time.time, live_broker_kwargs: dict | None = None):
        self.db = db
        self.settings = settings
        self.clock = clock
        self._market_factory = market_factory or (lambda ex_id: MarketData(ex_id))
        self._live_client_factory = live_client_factory or (lambda ex_id, keys: make_exchange(ex_id, keys))
        self._live_broker_kwargs = live_broker_kwargs or {}
        self.lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_equity_snapshot = 0.0
        self._exit_retry_after: dict[int, float] = {}
        # Exits that were decided but failed (e.g. exchange error) - retried every tick until done.
        self._pending_exits: dict[int, str] = {}
        self._last_error_logged: tuple[str, float] | None = None
        # Live state shown on the dashboard (refreshed every tick).
        self.state: dict = {"prices": {}, "signals": {}, "equity": None, "cash": None,
                            "last_tick": None, "last_error": None}
        if self.db.get("paper_cash") is None:
            self.db.set("paper_cash", settings.paper_starting_balance)
        self.market = self._market_factory(settings.exchange)
        self.broker = self._make_broker()

    # ------------------------------------------------------------------ basics
    @property
    def mode(self) -> str:
        return self.db.get("mode", "paper")

    @property
    def trading_enabled(self) -> bool:
        return bool(self.db.get("trading_enabled", False))

    @property
    def halted_today(self) -> bool:
        return self.db.get(f"halted_date:{self.mode}") == utc_today(self.clock())

    def _make_broker(self):
        if self.mode == "live":
            keys = secrets_store.get_api_keys()
            client = self._live_client_factory(self.settings.exchange, keys)
            return LiveBroker(client, self.settings, **self._live_broker_kwargs)
        return PaperBroker(self.db, self.settings)

    def info(self, msg: str) -> None:
        log.info(msg)
        self.db.log("info", msg)

    def warn(self, msg: str) -> None:
        log.warning(msg)
        self.db.log("warning", msg)

    def error(self, msg: str) -> None:
        """Log an error, but don't repeat the same error more than once every 5 minutes."""
        now = self.clock()
        self.state["last_error"] = {"time": now, "message": msg}
        if self._last_error_logged and self._last_error_logged[0] == msg and now - self._last_error_logged[1] < 300:
            return
        self._last_error_logged = (msg, now)
        log.error(msg)
        self.db.log("error", msg)

    # ------------------------------------------------------------------ thread
    def start_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="trading-engine", daemon=True)
        self._thread.start()

    def stop_thread(self, timeout: float = 30) -> None:
        self._stop_event.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def wake(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        self.info(f"Engine started ({self.mode.upper()} mode, {self.settings.exchange})")
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as e:  # never let the loop die
                log.exception("tick failed")
                self.error(f"Unexpected error: {type(e).__name__}: {e}")
            self._wake.wait(self.settings.poll_interval_seconds)
            self._wake.clear()
        log.info("engine thread stopped")

    # ------------------------------------------------------------------ main loop
    def tick(self) -> None:
        with self.lock:
            s = self.settings
            mode = self.mode
            now = self.clock()
            positions = self.db.open_positions(mode)

            # 1. Prices + position protection
            symbols = list(dict.fromkeys(s.symbols + [p["symbol"] for p in positions]))
            prices = {}
            for sym in symbols:
                try:
                    prices[sym] = self.market.price(sym)
                except Exception as e:
                    self.error(f"Could not get price for {sym}: {e}")
            self.state["prices"].update(prices)

            for p in positions:
                quote = prices.get(p["symbol"])
                if not quote:
                    continue
                if quote["last"] > p["highest_price"]:
                    self.db.update_highest_price(p["id"], quote["last"])
                    p["highest_price"] = quote["last"]
                reason = exit_reason(p, quote["last"], s) or self._pending_exits.get(p["id"])
                if reason:
                    self._close(p, quote, reason)

            # 2. Equity + daily loss limit
            equity, cash = self._equity()
            if equity is not None:
                self._check_daily_loss(equity)

            # 3. Strategy signals on newly closed candles
            for sym in s.symbols:
                try:
                    self._process_signals(sym, prices.get(sym))
                except Exception as e:
                    self.error(f"Strategy check for {sym} failed: {e}")

            # 4. Equity history
            equity, cash = self._equity()
            if equity is not None and now - self._last_equity_snapshot >= EQUITY_SNAPSHOT_SECONDS:
                self.db.add_equity_point(mode, equity, cash)
                self._last_equity_snapshot = now
            self.state["last_tick"] = now

    def _equity(self) -> tuple[float | None, float | None]:
        """Account value = quote cash + current value of the bot's open positions."""
        try:
            cash = self.broker.total_cash()
        except Exception as e:
            self.error(f"Could not read account balance: {e}")
            return None, None
        value = 0.0
        for p in self.db.open_positions(self.mode):
            quote = self.state["prices"].get(p["symbol"])
            value += p["amount"] * (quote["last"] if quote else p["entry_price"])
        equity = cash + value
        self.state["equity"], self.state["cash"] = equity, cash
        return equity, cash

    def _todays_loss_pct(self) -> float | None:
        """How far the account is down today, in % of the day's starting value (negative = up)."""
        day = self.db.get(f"day_start:{self.mode}")
        equity, _ = self._equity()
        if not day or day.get("date") != utc_today(self.clock()) or equity is None or day["equity"] <= 0:
            return None
        return (day["equity"] - equity) / day["equity"] * 100

    def _check_daily_loss(self, equity: float) -> None:
        mode, today = self.mode, utc_today(self.clock())
        key = f"day_start:{mode}"
        day = self.db.get(key)
        if not day or day.get("date") != today:
            day = {"date": today, "equity": equity}
            self.db.set(key, day)
        self.state["day_start_equity"] = day["equity"]
        if day["equity"] <= 0 or not self.trading_enabled:
            return
        loss_pct = (day["equity"] - equity) / day["equity"] * 100
        if loss_pct >= self.settings.max_daily_loss_pct:
            self.warn(f"Daily loss limit hit: down {loss_pct:.2f}% today "
                      f"(limit {self.settings.max_daily_loss_pct:g}%). Closing all positions and stopping until tomorrow (UTC).")
            self.db.set(f"halted_date:{mode}", today)
            self.db.set("trading_enabled", False)
            self._close_all("daily loss limit")

    def _process_signals(self, symbol: str, quote: dict | None) -> None:
        s, mode = self.settings, self.mode
        tf = s.timeframe_seconds * 1000
        key = f"last_candle:{mode}:{s.exchange}:{symbol}:{s.timeframe}"
        processed = self.db.get(key)
        newest_closed = (int(self.clock() * 1000) // tf) * tf - tf
        if processed is not None and processed >= newest_closed:
            return  # nothing new yet - skip the network call
        candles = self.market.candles(symbol, s.timeframe, s.candles_needed)
        if len(candles) < s.slow_ema + 2:
            self.error(f"{symbol}: only {len(candles)} candles available, need at least {s.slow_ema + 2}")
            return
        last_ts = candles[-1][0]
        if processed is not None and last_ts <= processed:
            return  # the exchange hasn't published the new candle yet
        self.db.set(key, last_ts)
        sig = latest_signal([c[4] for c in candles], s)
        self.state["signals"][symbol] = {"time": last_ts, "action": sig.action, "reason": sig.reason,
                                         "fast": sig.fast, "slow": sig.slow, "rsi": sig.rsi}
        if sig.action is None:
            return
        position = next((p for p in self.db.open_positions(mode) if p["symbol"] == symbol), None)
        if quote is None:
            quote = self.market.price(symbol)
        if sig.action == "sell" and position:
            self._close(position, quote, f"sell signal: {sig.reason}")
        elif sig.action == "buy" and not position:
            self._open(symbol, quote, sig.reason)

    # ------------------------------------------------------------------ trading
    def _open(self, symbol: str, quote: dict, reason: str) -> None:
        s, mode = self.settings, self.mode
        if not self.trading_enabled:
            self.info(f"{symbol}: buy signal ignored - bot is stopped ({reason})")
            return
        if self.halted_today:
            self.info(f"{symbol}: buy signal ignored - daily loss limit reached")
            return
        open_count = len(self.db.open_positions(mode))
        if open_count >= s.max_open_positions:
            self.info(f"{symbol}: buy signal ignored - already at max open positions ({s.max_open_positions})")
            return
        equity, _ = self._equity()
        if equity is None:
            return
        try:
            cash = self.broker.cash()
        except Exception as e:
            self.error(f"{symbol}: could not read balance before buying: {e}")
            return
        budget = min(equity * s.position_size_pct / 100, s.max_trade_value, cash * 0.99)
        if budget < 1:
            self.info(f"{symbol}: buy signal ignored - not enough {s.quote_currency} available ({cash:.2f})")
            return
        try:
            fill = self.broker.buy(symbol, budget, quote)
        except TradeSkipped as e:
            self.info(f"{symbol}: buy skipped - {e}")
            return
        except (OrderFailed, ccxt.BaseError) as e:
            self.error(f"{symbol}: BUY FAILED - {e}")
            return
        self._record_entry(symbol, fill, reason)
        if fill.estimated:
            self.warn(f"{symbol}: the exchange did not confirm the buy, so the amount was worked out from your "
                      f"balance and the price is estimated. Check the order on the exchange.")

    def _record_entry(self, symbol: str, fill: Fill, reason: str) -> None:
        s, mode, now = self.settings, self.mode, self.clock()
        stop, target = stop_and_target(fill.price, s)
        total = fill.cost + fill.fee
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO positions(mode, exchange, symbol, amount, entry_price, cost, entry_fee, entry_time,"
                " stop_price, take_profit_price, highest_price) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (mode, s.exchange, symbol, fill.amount, fill.price, total, fill.fee, now, stop, target, fill.price))
            c.execute(
                "INSERT INTO orders(mode, exchange, symbol, side, amount, price, cost, fee, time, reason, exchange_order_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (mode, s.exchange, symbol, "buy", fill.amount, fill.price, fill.cost, fill.fee, now, reason, fill.order_id))
            if mode == "paper":
                self.db.set("paper_cash", self.broker.cash() - total, conn=c)
        tp = f", take-profit {target:.6g}" if target else ""
        self.info(f"BUY {fill.amount:.8g} {symbol} @ {fill.price:.6g} = {total:.2f} {s.quote_currency} "
                  f"(stop {stop:.6g}{tp}) - {reason}")

    def _close(self, position: dict, quote: dict, reason: str) -> bool:
        """Sell a position. Returns True if it was (fully) closed."""
        pid = position["id"]
        now = self.clock()
        if self._exit_retry_after.get(pid, 0) > now:
            return False
        try:
            fill = self.broker.sell(position["symbol"], position["amount"], quote)
        except TradeSkipped as e:
            if self.mode == "live":
                # Nothing sellable left (e.g. coins were sold manually on the exchange).
                self.warn(f"{position['symbol']}: cannot sell ({e}). Removing position from tracking - "
                          f"check your exchange account.")
                fill = Fill(amount=position["amount"], price=quote["bid"], cost=position["amount"] * quote["bid"], fee=0)
                self._record_exit(position, fill, f"{reason} (untracked: {e})")
                return True
            self.error(f"{position['symbol']}: sell skipped - {e}")
            return False
        except (OrderFailed, ccxt.BaseError) as e:
            self._exit_retry_after[pid] = now + EXIT_RETRY_SECONDS
            self._pending_exits[pid] = reason
            self.error(f"{position['symbol']}: SELL FAILED (retrying every {EXIT_RETRY_SECONDS}s) - {e}")
            return False
        self._exit_retry_after.pop(pid, None)
        self._record_exit(position, fill, reason)
        if fill.partial:
            self._pending_exits[pid] = reason   # sell the rest on the next tick
        else:
            self._pending_exits.pop(pid, None)
        return not fill.partial

    def _record_exit(self, position: dict, fill: Fill, reason: str) -> None:
        s, mode, now = self.settings, self.mode, self.clock()
        if fill.partial:
            fraction = min(fill.amount / position["amount"], 1.0)
        else:
            fraction = 1.0
        cost_basis = position["cost"] * fraction
        proceeds = fill.cost - fill.fee
        pnl = proceeds - cost_basis
        pnl_pct = pnl / cost_basis * 100 if cost_basis else 0.0
        fees = position["entry_fee"] * fraction + fill.fee
        with self.db.tx() as c:
            if fill.partial:
                c.execute("UPDATE positions SET amount = amount - ?, cost = cost - ?, entry_fee = entry_fee * ? WHERE id = ?",
                          (fill.amount, cost_basis, 1 - fraction, position["id"]))
            else:
                c.execute("DELETE FROM positions WHERE id = ?", (position["id"],))
            c.execute(
                "INSERT INTO trades(mode, exchange, symbol, amount, entry_price, exit_price, entry_time, exit_time,"
                " cost, proceeds, fees, pnl, pnl_pct, exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mode, position["exchange"], position["symbol"], fill.amount, position["entry_price"], fill.price,
                 position["entry_time"], now, cost_basis, proceeds, fees, pnl, pnl_pct, reason))
            c.execute(
                "INSERT INTO orders(mode, exchange, symbol, side, amount, price, cost, fee, time, reason, exchange_order_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (mode, position["exchange"], position["symbol"], "sell", fill.amount, fill.price, fill.cost, fill.fee,
                 now, reason, fill.order_id))
            if mode == "paper":
                self.db.set("paper_cash", self.broker.cash() + proceeds, conn=c)
        part = " (PARTIAL fill - remainder still open)" if fill.partial else ""
        if fill.estimated:
            part += " (exchange did not confirm the fill: amount from balance change, price estimated)"
        self.info(f"SELL {fill.amount:.8g} {position['symbol']} @ {fill.price:.6g} - P&L {pnl:+.2f} {s.quote_currency} "
                  f"({pnl_pct:+.2f}%) - {reason}{part}")

    def _close_all(self, reason: str) -> list[str]:
        failures = []
        for p in self.db.open_positions(self.mode):
            try:
                quote = self.market.price(p["symbol"])
            except Exception as e:
                failures.append(f"{p['symbol']}: no price ({e})")
                self.error(f"{p['symbol']}: could not get price to close position: {e}")
                continue
            self._exit_retry_after.pop(p["id"], None)
            if not self._close(p, quote, reason):
                failures.append(p["symbol"])
        return failures

    # ------------------------------------------------------------------ dashboard actions
    def start_trading(self) -> None:
        with self.lock:
            if self.halted_today:
                loss = self._todays_loss_pct()
                if loss is None or loss >= self.settings.max_daily_loss_pct:
                    raise EngineError("The daily loss limit was reached today. Trading resumes automatically "
                                      "tomorrow (00:00 UTC), or raise the daily loss limit in Settings.")
                self.db.delete(f"halted_date:{self.mode}")
                self.info(f"Daily loss limit raised to {self.settings.max_daily_loss_pct:g}% "
                          f"(today: -{loss:.2f}%) - trading allowed again")
            if self.mode == "live" and not secrets_store.api_keys_set():
                raise EngineError("Live mode needs API keys.")
            self.db.set("trading_enabled", True)
            self.info(f"Bot STARTED in {self.mode.upper()} mode")
        self.wake()

    def stop_trading(self) -> None:
        with self.lock:
            self.db.set("trading_enabled", False)
            n = len(self.db.open_positions(self.mode))
            extra = f" {n} open position(s) are still protected by stop-loss/take-profit." if n else ""
            self.info(f"Bot STOPPED - no new trades will be opened.{extra}")

    def emergency_stop(self) -> list[str]:
        with self.lock:
            self.db.set("trading_enabled", False)
            self.warn("EMERGENCY STOP: closing all positions")
            failures = self._close_all("emergency stop")
            if failures:
                self.error(f"Emergency stop could not close: {', '.join(failures)}")
            return failures

    def close_position(self, position_id: int) -> None:
        with self.lock:
            p = self.db.get_position(position_id)
            if not p or p["mode"] != self.mode:
                raise EngineError("Position not found")
            self._exit_retry_after.pop(position_id, None)
            try:
                quote = self.market.price(p["symbol"])
            except Exception as e:
                raise EngineError(f"Could not get the current {p['symbol']} price: {e}") from e
            if not self._close(p, quote, "closed manually"):
                raise EngineError(f"Could not close {p['symbol']} - see the activity log")

    def apply_settings(self, new: Settings) -> None:
        with self.lock:
            old = self.settings
            if new.exchange != old.exchange and self.mode == "live":
                raise EngineError("Switch to paper mode before changing exchange (your API keys belong to "
                                  f"{old.exchange}).")
            if new.exchange != old.exchange and self.db.open_positions(self.mode):
                raise EngineError("Close all open positions before changing exchange.")
            if new.quote_currency != old.quote_currency and self.db.open_positions(self.mode):
                raise EngineError("Close all open positions before changing the quote currency.")
            self.settings = new
            if new.exchange != old.exchange:
                self.market = self._market_factory(new.exchange)
                self.state["prices"], self.state["signals"] = {}, {}
            self.broker = self._make_broker()
            self.info("Settings updated")
        self.wake()

    def set_mode(self, mode: str) -> None:
        with self.lock:
            if mode not in ("paper", "live"):
                raise EngineError("mode must be 'paper' or 'live'")
            if mode == self.mode:
                return
            if self.trading_enabled:
                raise EngineError("Stop the bot before switching mode.")
            if self.db.open_positions(self.mode):
                raise EngineError(f"Close all open {self.mode} positions before switching mode.")
            if mode == "live" and not secrets_store.api_keys_set():
                raise EngineError("Enter and save your exchange API keys first.")
            self.db.set("mode", mode)
            self.broker = self._make_broker()
            self.state["equity"] = self.state["cash"] = None
            self._last_equity_snapshot = 0.0
            self.warn(f"Switched to {mode.upper()} mode" + (" - REAL MONEY" if mode == "live" else ""))
        self.wake()

    def reload_broker(self) -> None:
        with self.lock:
            self.broker = self._make_broker()

    def test_live_connection(self) -> dict:
        """Check the saved API keys by reading the account balance (places no orders)."""
        keys = secrets_store.get_api_keys()
        if not (keys["api_key"] and keys["api_secret"]):
            raise EngineError("No API keys saved.")
        try:
            client = self._live_client_factory(self.settings.exchange, keys)
            bal = client.fetch_balance()
        except ccxt.AuthenticationError as e:
            raise EngineError(f"The exchange rejected the API keys: {e}") from e
        except ccxt.BaseError as e:
            raise EngineError(f"Could not connect to {self.settings.exchange}: {e}") from e
        except Exception as e:  # e.g. a badly pasted private key that can't be parsed
            raise EngineError(f"The API keys could not be used: {type(e).__name__}: {e}") from e
        q = self.settings.quote_currency
        return {"quote": q, "free": float(bal.get("free", {}).get(q) or 0),
                "total": float(bal.get("total", {}).get(q) or 0)}

    def reset_paper(self) -> None:
        with self.lock:
            if self.mode != "paper":
                raise EngineError("Switch to paper mode to reset the paper account.")
            if self.trading_enabled:
                raise EngineError("Stop the bot before resetting the paper account.")
            self.db.clear_mode_history("paper")
            self._pending_exits.clear()
            self._exit_retry_after.clear()
            self.db.set("paper_cash", self.settings.paper_starting_balance)
            for key in ("day_start:paper", "halted_date:paper"):
                self.db.delete(key)
            self.state["equity"] = self.state["cash"] = None
            self._last_equity_snapshot = 0.0
            self.info(f"Paper account reset to {self.settings.paper_starting_balance:,.2f} {self.settings.quote_currency}")
        self.wake()

    # ------------------------------------------------------------------ dashboard data
    def snapshot(self) -> dict:
        s, mode = self.settings, self.mode
        positions = []
        for p in self.db.open_positions(mode):
            quote = self.state["prices"].get(p["symbol"])
            price = quote["last"] if quote else None
            value = p["amount"] * price if price else None
            stop, stop_label = effective_stop(p, s)
            positions.append({
                "id": p["id"], "symbol": p["symbol"], "amount": p["amount"], "entry_price": p["entry_price"],
                "entry_time": p["entry_time"], "price": price, "value": value, "cost": p["cost"],
                "pnl": (value - p["cost"]) if value is not None else None,
                "pnl_pct": ((value - p["cost"]) / p["cost"] * 100) if value is not None and p["cost"] else None,
                "stop": stop, "stop_label": stop_label, "target": p["take_profit_price"],
            })
        equity = self.state.get("equity")
        day_start = (self.db.get(f"day_start:{mode}") or {})
        day_pnl = None
        if equity is not None and day_start.get("date") == utc_today(self.clock()):
            day_pnl = equity - day_start["equity"]
        return {
            "mode": mode,
            "exchange": s.exchange,
            "quote": s.quote_currency,
            "trading_enabled": self.trading_enabled,
            "halted_today": self.halted_today,
            "engine_alive": bool(self._thread and self._thread.is_alive()),
            "equity": equity,
            "cash": self.state.get("cash"),
            "day_pnl": day_pnl,
            "positions": positions,
            "stats": self.db.trade_stats(mode),
            "prices": dict(self.state["prices"]),
            "signals": dict(self.state["signals"]),
            "last_tick": self.state["last_tick"],
            "last_error": self.state["last_error"],
            "api_keys_set": secrets_store.api_keys_set(),
        }
