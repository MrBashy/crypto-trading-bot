import math

import pytest
from fastapi.testclient import TestClient

from bot import secrets_store
from bot.engine import Engine
from bot.web import create_app
from tests.conftest import FakeExchange

H = {"X-Requested-With": "fetch"}
PASSWORD = "correct-horse-battery"


class FakeClient:
    timeframes = {"1m": "1", "5m": "5", "15m": "15", "1h": "60"}

    def __init__(self, now_ms):
        self.now_ms = now_ms

    def milliseconds(self):
        return self.now_ms


class FakeChartMarket:
    def __init__(self, known=("BTC/USD", "ETH/USD")):
        self.known = set(known)
        self.client = FakeClient(1_800_000_000_000)

    def unknown_symbols(self, symbols):
        return [s for s in symbols if s not in self.known]

    def market(self, symbol):
        if symbol not in self.known:
            raise ValueError(f"{symbol} is not traded on fake")
        return {}

    def _rows(self, n, end_ms, tf_ms=300_000):
        start = end_ms - n * tf_ms
        rows = []
        for i in range(n):
            c = 100 + 10 * math.sin(i / 5)
            rows.append([start + i * tf_ms, c, c * 1.01, c * 0.99, c, 1.0])
        return rows

    def candles(self, symbol, timeframe, count, include_open=False):
        return self._rows(count, self.client.now_ms)

    def fetch_range(self, symbol, timeframe, start_ms, end_ms):
        n = int((end_ms - start_ms) // 300_000)
        return self._rows(n, end_ms)


@pytest.fixture
def app_client(db, settings, market, clock):
    secrets_store.set_password(PASSWORD)
    market.closes["BTC/USD"] = [100.0] * 30
    market.prices["BTC/USD"] = 100.0
    ex = FakeExchange()
    engine = Engine(db, settings, market_factory=lambda ex_id: market, clock=clock,
                    live_client_factory=lambda ex_id, keys: ex, live_broker_kwargs={"sleep": lambda s: None})
    chart = FakeChartMarket()
    app = create_app(engine=engine, start_engine=False, market_factory=lambda ex_id: chart)
    with TestClient(app) as c:
        c.engine = engine
        yield c


def login(c, password=PASSWORD):
    return c.post("/api/login", json={"password": password})


def test_pages_and_api_require_login(app_client):
    c = app_client
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/login").status_code == 200
    assert c.get("/api/status").status_code == 401
    assert c.post("/api/start", headers=H).status_code == 401
    assert c.get("/static/app.js").status_code == 200


def test_wrong_password_then_lockout(app_client):
    c = app_client
    for _ in range(5):
        assert login(c, "wrong-password").status_code == 401
    r = login(c)       # correct password, but locked out now
    assert r.status_code == 429


def test_login_and_dashboard(app_client):
    c = app_client
    assert login(c).status_code == 200
    assert c.get("/", follow_redirects=False).status_code == 200
    st = c.get("/api/status").json()
    assert st["mode"] == "paper"
    assert st["trading_enabled"] is False
    assert st["symbols"] == ["BTC/USD"]
    assert st["tradingview_prefix"] == "KRAKEN"
    assert c.post("/api/logout").status_code == 200
    assert c.get("/api/status").status_code == 401


def test_post_requires_csrf_header(app_client):
    c = app_client
    login(c)
    assert c.post("/api/start").status_code == 403
    assert c.post("/api/start", headers=H).status_code == 200
    assert c.get("/api/status").json()["trading_enabled"] is True
    assert c.post("/api/stop", headers=H).status_code == 200


def test_settings_roundtrip_and_validation(app_client):
    c = app_client
    login(c)
    d = c.get("/api/settings").json()
    assert "kraken" in d["exchanges"]
    new = dict(d["settings"], stop_loss_pct=2.5, symbols="btc/usd, eth/usd")
    r = c.post("/api/settings", json=new, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["symbols"] == ["BTC/USD", "ETH/USD"]
    assert c.app.state.engine.settings.stop_loss_pct == 2.5
    # invalid values are rejected with a readable message
    bad = dict(new, fast_ema=50, slow_ema=10)
    r = c.post("/api/settings", json=bad, headers=H)
    assert r.status_code == 400 and "fast_ema must be smaller" in r.json()["detail"]
    r = c.post("/api/settings", json=dict(new, stop_loss_pct=None), headers=H)
    assert r.status_code == 400 and "stop_loss_pct" in r.json()["detail"]
    r = c.post("/api/settings", json=dict(new, symbols="DOGE/USD"), headers=H)
    assert r.status_code == 400 and "DOGE/USD" in r.json()["detail"]
    r = c.post("/api/settings", json=dict(new, timeframe="4h"), headers=H)
    assert r.status_code == 400 and "4h" in r.json()["detail"]
    assert c.app.state.engine.settings.stop_loss_pct == 2.5


def test_settings_saved_to_disk(app_client, isolated_data_dir):
    from bot.config import load_settings
    c = app_client
    login(c)
    s = c.get("/api/settings").json()["settings"]
    c.post("/api/settings", json=dict(s, take_profit_pct=7), headers=H)
    assert load_settings().take_profit_pct == 7


def test_live_mode_needs_keys_and_confirmation(app_client):
    c = app_client
    login(c)
    r = c.post("/api/mode", json={"mode": "live", "confirm": "LIVE"}, headers=H)
    assert r.status_code == 400 and "API keys" in r.json()["detail"]
    r = c.post("/api/keys", json={"api_key": "abcdefghijkl", "api_secret": "secretsecret1234"}, headers=H)
    assert r.status_code == 200
    k = c.get("/api/keys").json()
    assert k["set"] is True and k["api_key"] == "••••ijkl" and "secret" not in k["api_secret"]
    r = c.post("/api/mode", json={"mode": "live", "confirm": "yes"}, headers=H)
    assert r.status_code == 400 and "LIVE" in r.json()["detail"]
    r = c.post("/api/mode", json={"mode": "live", "confirm": "live"}, headers=H)
    assert r.status_code == 200 and r.json()["mode"] == "live"
    assert c.get("/api/status").json()["mode"] == "live"
    # can't delete keys while live
    assert c.delete("/api/keys", headers=H).status_code == 400
    assert c.post("/api/mode", json={"mode": "paper"}, headers=H).json()["mode"] == "paper"
    assert c.delete("/api/keys", headers=H).status_code == 200
    assert c.get("/api/keys").json()["set"] is False


def test_keys_test_endpoint(app_client):
    c = app_client
    login(c)
    assert c.post("/api/keys/test", headers=H).status_code == 400
    c.post("/api/keys", json={"api_key": "abcdefghijkl", "api_secret": "secretsecret1234"}, headers=H)
    r = c.post("/api/keys/test", headers=H)
    assert r.status_code == 200 and r.json()["free"] == 5000.0


def test_password_change_logs_out_other_sessions(app_client):
    c = app_client
    login(c)
    other = TestClient(c.app)
    assert login(other).status_code == 200
    r = c.post("/api/password", json={"current": "nope-nope", "new": "new-password-1"}, headers=H)
    assert r.status_code == 400
    r = c.post("/api/password", json={"current": PASSWORD, "new": "short"}, headers=H)
    assert r.status_code == 400
    r = c.post("/api/password", json={"current": PASSWORD, "new": "new-password-1"}, headers=H)
    assert r.status_code == 200
    assert c.get("/api/status").status_code == 200          # this browser stays logged in
    assert other.get("/api/status").status_code == 401      # others are logged out
    assert login(other, "new-password-1").status_code == 200


def test_candles_backtest_equity_and_reset(app_client):
    c = app_client
    login(c)
    d = c.get("/api/candles", params={"symbol": "BTC/USD"}).json()
    assert len(d["candles"]) == 300 and d["fast"] and d["slow"]
    r = c.post("/api/backtest", json={"symbol": "BTC/USD", "candles": 500}, headers=H)
    assert r.status_code == 200, r.text
    bt = r.json()
    assert bt["candles"] == 500 and len(bt["candles_data"]) == 500 and bt["trades_count"] > 0
    r = c.post("/api/backtest", json={"symbol": "XRP/USD", "candles": 500}, headers=H)
    assert r.status_code == 400
    assert c.get("/api/equity").json() == {"points": []}
    assert c.post("/api/paper/reset", json={"confirm": "no"}, headers=H).status_code == 400
    assert c.post("/api/paper/reset", json={"confirm": "RESET"}, headers=H).status_code == 200


def test_engine_error_is_400_with_message(app_client):
    c = app_client
    login(c)
    r = c.post("/api/positions/999/close", headers=H)
    assert r.status_code == 400 and r.json()["detail"] == "Position not found"


def test_security_headers(app_client):
    r = app_client.get("/login")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_bad_keys_rejected_and_rolled_back_in_live_mode(app_client):
    import ccxt
    c = app_client
    login(c)
    c.post("/api/keys", json={"api_key": "goodkey12345", "api_secret": "goodsecret1234"}, headers=H)
    assert c.post("/api/mode", json={"mode": "live", "confirm": "LIVE"}, headers=H).status_code == 200
    ex = c.engine.broker.client
    ex.balance_error = ccxt.AuthenticationError("invalid key")
    r = c.post("/api/keys", json={"api_key": "badkey123456", "api_secret": "badsecret1234"}, headers=H)
    assert r.status_code == 400 and "rejected" in r.json()["detail"]
    assert secrets_store.get_api_keys()["api_key"] == "goodkey12345"
