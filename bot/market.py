"""Public market data (prices and candles) from the exchange via the ccxt library.

No API keys are needed for market data, so paper trading works out of the box.
"""
from __future__ import annotations

import threading
import time

import ccxt

from .config import TIMEFRAMES

# Largest batch we ask for in one request. Every supported exchange accepts 300.
_BATCH = 300


def make_exchange(exchange_id: str, keys: dict | None = None) -> ccxt.Exchange:
    """Create a ccxt exchange client. Pass keys for trading; omit them for public data only."""
    cfg: dict = {
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "spot"},
    }
    if keys:
        cfg["apiKey"] = keys["api_key"]
        cfg["secret"] = keys["api_secret"]
        if keys.get("api_password"):
            cfg["password"] = keys["api_password"]
    return getattr(ccxt, exchange_id)(cfg)


class MarketData:
    """Thread-safe wrapper around one public ccxt client, with a short cache."""

    def __init__(self, exchange_id: str, client: ccxt.Exchange | None = None, cache_seconds: float = 0):
        self.exchange_id = exchange_id
        self.client = client or make_exchange(exchange_id)
        self.cache_seconds = cache_seconds
        self._lock = threading.RLock()
        self._markets_loaded = False
        self._cache: dict = {}

    # ---------- markets ----------
    def load_markets(self) -> dict:
        with self._lock:
            if not self._markets_loaded:
                self.client.load_markets()
                self._markets_loaded = True
            return self.client.markets

    def market(self, symbol: str) -> dict:
        markets = self.load_markets()
        if symbol not in markets:
            raise ValueError(f"{symbol} is not traded on {self.exchange_id}")
        return markets[symbol]

    def unknown_symbols(self, symbols: list[str]) -> list[str]:
        markets = self.load_markets()
        return [s for s in symbols if s not in markets or not markets[s].get("spot", True)]

    # ---------- prices ----------
    def price(self, symbol: str) -> dict:
        """Current {'last', 'bid', 'ask'} for a pair. Missing bid/ask fall back to last."""
        def fetch():
            t = self.client.fetch_ticker(symbol)
            last = t.get("last") or t.get("close")
            bid, ask = t.get("bid"), t.get("ask")
            if not last and bid and ask:
                last = (bid + ask) / 2
            if not last or last <= 0:
                raise ccxt.ExchangeError(f"no price available for {symbol}")
            return {"last": float(last), "bid": float(bid or last), "ask": float(ask or last)}
        return self._cached(("price", symbol), fetch)

    # ---------- candles ----------
    def candles(self, symbol: str, timeframe: str, count: int, include_open: bool = False) -> list[list]:
        """The most recent `count` candles [time_ms, open, high, low, close, volume].

        By default only CLOSED candles are returned, so trading decisions never use
        a candle that is still forming.
        """
        tf_ms = TIMEFRAMES[timeframe] * 1000

        def fetch():
            now = self.client.milliseconds()
            rows = self.fetch_range(symbol, timeframe, now - (count + 3) * tf_ms, now)
            if not include_open:
                rows = [c for c in rows if c[0] + tf_ms <= now]
            return rows[-count:]
        return self._cached(("candles", symbol, timeframe, count, include_open), fetch)

    def fetch_range(self, symbol: str, timeframe: str, start_ms: int, end_ms: int, max_requests: int = 40) -> list[list]:
        """All candles with open time in [start_ms, end_ms], fetched in batches."""
        tf_ms = TIMEFRAMES[timeframe] * 1000
        found: dict[int, list] = {}
        since = int(start_ms)
        with self._lock:
            for _ in range(max_requests):
                batch = self.client.fetch_ohlcv(symbol, timeframe, since=since, limit=_BATCH)
                if not batch:
                    break
                for c in batch:
                    if start_ms <= c[0] <= end_ms and None not in c[:5]:
                        found[int(c[0])] = [int(c[0])] + [float(x or 0) for x in c[1:6]]
                last = int(batch[-1][0])
                if last + tf_ms > end_ms or last < since:
                    break
                since = last + tf_ms
        return [found[k] for k in sorted(found)]

    def _cached(self, key, fn):
        if self.cache_seconds <= 0:
            with self._lock:
                return fn()
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < self.cache_seconds:
                return hit[1]
            value = fn()
            self._cache[key] = (time.monotonic(), value)
            if len(self._cache) > 200:
                self._cache.clear()
            return value
