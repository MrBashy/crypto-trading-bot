"""Bot settings: defaults, validation, and load/save to data/settings.json.

Everything in Settings can be changed from the dashboard. Secrets (API keys,
dashboard password) are NOT stored here - see secrets_store.py.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("BOT_DATA_DIR", ROOT_DIR / "data"))

# Exchanges the bot supports, mapped to their TradingView chart prefix.
SUPPORTED_EXCHANGES: dict[str, str] = {
    "kraken": "KRAKEN",
    "coinbase": "COINBASE",
    "binanceus": "BINANCEUS",
    "binance": "BINANCE",
    "bitstamp": "BITSTAMP",
    "gemini": "GEMINI",
    "bybit": "BYBIT",
    "okx": "OKX",
    "kucoin": "KUCOIN",
}

# Candle sizes offered in the dashboard -> seconds per candle.
TIMEFRAMES: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
}


class Settings(BaseModel):
    """All tunable bot settings. Percentages are written as e.g. 1.5 for 1.5%."""

    model_config = ConfigDict(extra="forbid")

    # --- Market ---
    exchange: str = "kraken"
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USD", "ETH/USD"])
    timeframe: str = "5m"

    # --- Strategy: EMA crossover with RSI filter ---
    fast_ema: int = Field(9, ge=2, le=100)
    slow_ema: int = Field(21, ge=3, le=200)
    rsi_period: int = Field(14, ge=2, le=50)
    rsi_overbought: float = Field(70, ge=50, le=100)

    # --- Risk management ---
    position_size_pct: float = Field(10, ge=1, le=100)
    max_trade_value: float = Field(1000, gt=0)
    max_open_positions: int = Field(2, ge=1, le=10)
    stop_loss_pct: float = Field(1.5, ge=0.1, le=50)
    take_profit_pct: float = Field(3.0, ge=0, le=100)
    trailing_stop_pct: float = Field(0, ge=0, le=50)
    max_daily_loss_pct: float = Field(5, ge=0.5, le=100)

    # --- Paper trading simulation ---
    paper_starting_balance: float = Field(10000, ge=10, le=1e9)
    fee_pct: float = Field(0.4, ge=0, le=5)
    slippage_pct: float = Field(0.05, ge=0, le=5)

    # --- Engine ---
    poll_interval_seconds: int = Field(15, ge=5, le=300)

    @field_validator("exchange")
    @classmethod
    def _check_exchange(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in SUPPORTED_EXCHANGES:
            raise ValueError(f"exchange must be one of: {', '.join(SUPPORTED_EXCHANGES)}")
        return v

    @field_validator("timeframe")
    @classmethod
    def _check_timeframe(cls, v: str) -> str:
        if v not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of: {', '.join(TIMEFRAMES)}")
        return v

    @field_validator("symbols", mode="before")
    @classmethod
    def _split_symbols(cls, v):
        if isinstance(v, str):
            v = v.split(",")
        return v

    @field_validator("symbols")
    @classmethod
    def _check_symbols(cls, v: list[str]) -> list[str]:
        cleaned: list[str] = []
        for s in v:
            s = s.strip().upper()
            if not s:
                continue
            parts = s.split("/")
            if len(parts) != 2 or not parts[0] or not parts[1]:
                raise ValueError(f"'{s}' is not a valid pair - use the BASE/QUOTE format, e.g. BTC/USD")
            if s not in cleaned:
                cleaned.append(s)
        if not 1 <= len(cleaned) <= 10:
            raise ValueError("enter between 1 and 10 trading pairs")
        quotes = {s.split("/")[1] for s in cleaned}
        if len(quotes) > 1:
            raise ValueError("all pairs must use the same quote currency (e.g. all /USD)")
        return cleaned

    @model_validator(mode="after")
    def _check_emas(self) -> "Settings":
        if self.fast_ema >= self.slow_ema:
            raise ValueError("fast_ema must be smaller than slow_ema")
        return self

    @property
    def quote_currency(self) -> str:
        return self.symbols[0].split("/")[1]

    @property
    def timeframe_seconds(self) -> int:
        return TIMEFRAMES[self.timeframe]

    @property
    def candles_needed(self) -> int:
        """How many closed candles the strategy needs for stable indicators."""
        return max(self.slow_ema * 4, self.rsi_period * 4, 100)


def settings_path() -> Path:
    return DATA_DIR / "settings.json"


def load_settings() -> Settings:
    path = settings_path()
    if not path.exists():
        settings = Settings()
        save_settings(settings)
        return settings
    data = json.loads(path.read_text())
    return Settings(**data)


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    """Write a file atomically so a crash can never leave it half-written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def save_settings(settings: Settings) -> None:
    atomic_write(settings_path(), json.dumps(settings.model_dump(), indent=2))
