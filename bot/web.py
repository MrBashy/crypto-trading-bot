"""Password-protected web dashboard and JSON API (FastAPI)."""
from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, config, secrets_store
from .backtest import run_backtest
from .config import ROOT_DIR, SUPPORTED_EXCHANGES, TIMEFRAMES, Settings, load_settings, save_settings
from .db import Database
from .engine import Engine, EngineError
from .indicators import ema
from .market import MarketData

WEB_DIR = ROOT_DIR / "web"
STATIC_DIR = ROOT_DIR / "static"

MAX_LOGIN_FAILURES = 5
LOCKOUT_SECONDS = 300


class LoginLimiter:
    """Locks an IP address out for 5 minutes after 5 wrong passwords."""

    def __init__(self):
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}

    def locked_for(self, ip: str) -> float:
        with self._lock:
            now = time.time()
            recent = [t for t in self._fails.get(ip, []) if now - t < LOCKOUT_SECONDS]
            self._fails[ip] = recent
            if len(recent) >= MAX_LOGIN_FAILURES:
                return LOCKOUT_SECONDS - (now - recent[-MAX_LOGIN_FAILURES])
            return 0

    def fail(self, ip: str) -> None:
        with self._lock:
            self._fails.setdefault(ip, []).append(time.time())

    def reset(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


# ---------------------------------------------------------------- request bodies
class LoginBody(BaseModel):
    password: str


class ModeBody(BaseModel):
    mode: str
    confirm: str = ""


class KeysBody(BaseModel):
    api_key: str
    api_secret: str
    api_password: str = ""


class PasswordBody(BaseModel):
    current: str
    new: str


class BacktestBody(BaseModel):
    symbol: str
    candles: int = 1000


class ConfirmBody(BaseModel):
    confirm: str = ""


def _format_validation_error(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        field = ".".join(str(x) for x in err["loc"])
        msg = err["msg"].removeprefix("Value error, ")
        parts.append(f"{field}: {msg}" if field else msg)
    return "; ".join(parts)


def create_app(engine: Engine | None = None, db: Database | None = None, start_engine: bool = True,
               market_factory=None) -> FastAPI:
    market_factory = market_factory or (lambda ex_id: MarketData(ex_id, cache_seconds=10))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_engine:
            app.state.engine.start_thread()
        yield
        if start_engine:
            app.state.engine.stop_thread()

    app = FastAPI(title="Crypto Bot", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    if engine is None:
        db = db or Database(config.DATA_DIR / "bot.db")
        engine = Engine(db, load_settings())
    app.state.engine = engine
    app.state.limiter = LoginLimiter()
    app.state.chart_markets = {}
    chart_lock = threading.Lock()

    def chart_market(exchange_id: str) -> MarketData:
        with chart_lock:
            if exchange_id not in app.state.chart_markets:
                app.state.chart_markets[exchange_id] = market_factory(exchange_id)
            return app.state.chart_markets[exchange_id]

    app.add_middleware(SessionMiddleware, secret_key=secrets_store.get_session_secret(),
                       session_cookie="cryptobot_session", max_age=7 * 24 * 3600, same_site="strict",
                       https_only=False)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(EngineError)
    async def engine_error_handler(request: Request, exc: EngineError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    # ------------------------------------------------------------ auth helpers
    def is_authed(request: Request) -> bool:
        return bool(request.session.get("auth")) and request.session.get("pv") == secrets_store.password_version()

    def require_auth(request: Request) -> None:
        if not is_authed(request):
            raise HTTPException(401, "Not logged in")
        # Block cross-site form/script submissions: browsers can't add this header cross-origin.
        if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "fetch":
            raise HTTPException(403, "Missing X-Requested-With header")

    auth = [Depends(require_auth)]
    eng = lambda: app.state.engine  # noqa: E731

    # ------------------------------------------------------------ pages
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index(request: Request):
        if not is_authed(request):
            return RedirectResponse("/login", status_code=303)
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request):
        if is_authed(request):
            return RedirectResponse("/", status_code=303)
        return FileResponse(WEB_DIR / "login.html")

    @app.post("/api/login")
    def login(body: LoginBody, request: Request):
        ip = request.client.host if request.client else "unknown"
        wait = app.state.limiter.locked_for(ip)
        if wait > 0:
            raise HTTPException(429, f"Too many wrong passwords. Try again in {int(wait // 60) + 1} minute(s).")
        if not secrets_store.verify_password(body.password):
            app.state.limiter.fail(ip)
            time.sleep(0.5)
            raise HTTPException(401, "Wrong password")
        app.state.limiter.reset(ip)
        request.session.clear()
        request.session.update({"auth": True, "pv": secrets_store.password_version()})
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    # ------------------------------------------------------------ status & data
    @app.get("/api/status", dependencies=auth)
    def status():
        e = eng()
        snap = e.snapshot()
        snap["trades"] = e.db.trades(e.mode, limit=50)
        snap["logs"] = e.db.recent_logs(100)
        snap["symbols"] = e.settings.symbols
        snap["timeframe"] = e.settings.timeframe
        snap["tradingview_prefix"] = SUPPORTED_EXCHANGES[e.settings.exchange]
        snap["version"] = __version__
        return snap

    @app.get("/api/candles", dependencies=auth)
    def candles(symbol: str, limit: int = 300):
        e = eng()
        s = e.settings
        limit = max(50, min(limit, 1000))
        try:
            rows = chart_market(s.exchange).candles(symbol, s.timeframe, limit, include_open=True)
        except Exception as ex:
            raise HTTPException(502, f"Could not load candles for {symbol}: {ex}")
        closes = [r[4] for r in rows]
        times = [r[0] // 1000 for r in rows]

        def series(values):
            return [{"time": t, "value": v} for t, v in zip(times, values) if v is not None]

        since = rows[0][0] / 1000 if rows else 0
        markers = [{"time": o["time"], "side": o["side"], "price": o["price"], "amount": o["amount"],
                    "reason": o["reason"]} for o in e.db.orders_for_symbol(e.mode, symbol, since)]
        return {
            "symbol": symbol, "timeframe": s.timeframe, "timeframe_seconds": s.timeframe_seconds,
            "candles": [{"time": t, "open": r[1], "high": r[2], "low": r[3], "close": r[4]} for t, r in zip(times, rows)],
            "fast": series(ema(closes, s.fast_ema)), "slow": series(ema(closes, s.slow_ema)),
            "fast_period": s.fast_ema, "slow_period": s.slow_ema,
            "markers": markers,
        }

    @app.get("/api/equity", dependencies=auth)
    def equity(days: float = 7):
        e = eng()
        since = time.time() - max(0.04, min(days, 365)) * 86400
        return {"points": e.db.equity_history(e.mode, since)}

    # ------------------------------------------------------------ settings
    @app.get("/api/settings", dependencies=auth)
    def get_settings():
        return {"settings": eng().settings.model_dump(), "defaults": Settings().model_dump(),
                "exchanges": list(SUPPORTED_EXCHANGES), "timeframes": list(TIMEFRAMES)}

    @app.post("/api/settings", dependencies=auth)
    def post_settings(body: dict):
        try:
            new = Settings(**body)
        except ValidationError as ex:
            raise HTTPException(400, _format_validation_error(ex))
        except TypeError as ex:
            raise HTTPException(400, str(ex))
        try:
            market = chart_market(new.exchange)
            unknown = market.unknown_symbols(new.symbols)
            timeframes = market.client.timeframes or {}
        except Exception as ex:
            raise HTTPException(400, f"Could not check the pairs with {new.exchange}: {ex}")
        if unknown:
            raise HTTPException(400, f"Not available on {new.exchange}: {', '.join(unknown)}")
        if timeframes and new.timeframe not in timeframes:
            raise HTTPException(400, f"{new.exchange} does not offer {new.timeframe} candles")
        eng().apply_settings(new)
        save_settings(new)
        return {"ok": True, "settings": new.model_dump()}

    # ------------------------------------------------------------ bot control
    @app.post("/api/start", dependencies=auth)
    def start():
        eng().start_trading()
        return {"ok": True}

    @app.post("/api/stop", dependencies=auth)
    def stop():
        eng().stop_trading()
        return {"ok": True}

    @app.post("/api/emergency-stop", dependencies=auth)
    def emergency_stop():
        failures = eng().emergency_stop()
        return {"ok": not failures, "failures": failures}

    @app.post("/api/positions/{position_id}/close", dependencies=auth)
    def close_position(position_id: int):
        eng().close_position(position_id)
        return {"ok": True}

    @app.post("/api/mode", dependencies=auth)
    def set_mode(body: ModeBody):
        e = eng()
        if body.mode == "live":
            if body.confirm.strip().upper() != "LIVE":
                raise HTTPException(400, "Type LIVE to confirm you want to trade with real money.")
            e.test_live_connection()
        e.set_mode(body.mode)
        return {"ok": True, "mode": e.mode}

    @app.post("/api/paper/reset", dependencies=auth)
    def reset_paper(body: ConfirmBody):
        if body.confirm.strip().upper() != "RESET":
            raise HTTPException(400, "Type RESET to confirm.")
        eng().reset_paper()
        return {"ok": True}

    # ------------------------------------------------------------ API keys
    @app.get("/api/keys", dependencies=auth)
    def get_keys():
        k = secrets_store.get_api_keys()
        return {"set": secrets_store.api_keys_set(), "api_key": secrets_store.mask(k["api_key"]),
                "api_secret": secrets_store.mask(k["api_secret"]), "api_password": secrets_store.mask(k["api_password"])}

    @app.post("/api/keys", dependencies=auth)
    def set_keys(body: KeysBody):
        e = eng()
        if not body.api_key.strip() or not body.api_secret.strip():
            raise HTTPException(400, "API key and secret are both required.")
        if e.mode == "live" and e.trading_enabled:
            raise HTTPException(400, "Stop the bot before changing API keys.")
        secrets_store.set_api_keys(body.api_key, body.api_secret, body.api_password)
        e.reload_broker()
        e.info("Exchange API keys saved")
        return {"ok": True}

    @app.delete("/api/keys", dependencies=auth)
    def delete_keys():
        e = eng()
        if e.mode == "live":
            raise HTTPException(400, "Switch to paper mode before removing the API keys.")
        secrets_store.clear_api_keys()
        e.info("Exchange API keys removed")
        return {"ok": True}

    @app.post("/api/keys/test", dependencies=auth)
    def test_keys():
        return eng().test_live_connection()

    # ------------------------------------------------------------ backtest
    @app.post("/api/backtest", dependencies=auth)
    def backtest(body: BacktestBody):
        s = eng().settings
        count = max(100, min(body.candles, 5000))
        tf_ms = s.timeframe_seconds * 1000
        try:
            market = chart_market(s.exchange)
            market.market(body.symbol)
            now = market.client.milliseconds()
            rows = market.fetch_range(body.symbol, s.timeframe, now - (count + 1) * tf_ms, now)
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        except Exception as ex:
            raise HTTPException(502, f"Could not download history: {ex}")
        rows = [r for r in rows if r[0] + tf_ms <= now][-count:]
        if len(rows) < s.slow_ema + 10:
            raise HTTPException(400, f"Only {len(rows)} candles of history available - not enough to backtest.")
        result = run_backtest(rows, s)
        result["candles_data"] = [{"time": r[0] // 1000, "open": r[1], "high": r[2], "low": r[3], "close": r[4]}
                                  for r in rows]
        result["symbol"] = body.symbol
        result["timeframe"] = s.timeframe
        result["requested_candles"] = count
        return result

    # ------------------------------------------------------------ account
    @app.post("/api/password", dependencies=auth)
    def change_password(body: PasswordBody, request: Request):
        if not secrets_store.verify_password(body.current):
            raise HTTPException(400, "Current password is wrong.")
        try:
            secrets_store.set_password(body.new)
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        # Other browsers are logged out; keep this one logged in.
        request.session["pv"] = secrets_store.password_version()
        eng().info("Dashboard password changed")
        return {"ok": True}

    return app
