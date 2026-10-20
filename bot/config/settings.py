"""Environment variables and runtime constants.

Everything configurable is read once from the environment (or `.env`) into a
single immutable `Settings` object. Money and percentages are `Decimal`, never
float. Percentages are fractions: 0.04 means 4%.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url


def normalize_database_url(raw: str) -> str:
    """Turn a provider-style PostgreSQL URL into one SQLAlchemy + asyncpg accepts.

    Managed services hand out `postgres://...?sslmode=require`. asyncpg has no
    `sslmode` argument (it crashes with a TypeError) and wants `ssl=require`.
    Checked against the installed driver on 2026-10-06.
    """
    raw = raw.strip()
    if raw.startswith("postgres://"):  # SQLAlchemy 2.x no longer accepts this alias
        raw = "postgresql://" + raw.removeprefix("postgres://")
    url = make_url(raw)
    if url.get_backend_name() != "postgresql":
        raise ValueError("DATABASE_URL must be a PostgreSQL URL (postgresql:// or postgres://)")
    query = dict(url.query)
    if "sslmode" in query:
        query.setdefault("ssl", query.pop("sslmode"))
    return url.set(drivername="postgresql+asyncpg", query=query).render_as_string(
        hide_password=False
    )


# The app name. Not a setting: a rename is this line, `pyproject.toml` and the compose project name.
APP_NAME = "Tidewatch"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True
    )

    log_level: str = "INFO"

    # Binance. Testnet and dry-run default to ON so a fresh checkout cannot trade real money.
    binance_api_key: SecretStr = SecretStr("")
    binance_api_secret: SecretStr = SecretStr("")
    # Path to an Ed25519 private key (PEM). Binance's current user-data stream needs
    # Ed25519 signing; leave empty to use the HMAC key/secret pair above.
    binance_private_key_path: Path | None = None
    binance_testnet: bool = True
    dry_run: bool = True
    # Prefix of every client order id the bot creates. Fixed on purpose (not the app name):
    # it is how orders are recognised after a restart, so it must not change with a rename.
    order_id_prefix: str = Field(default="bot", pattern=r"^[A-Za-z0-9]{1,12}$")

    # Database: just a connection string (local container or a managed service).
    # Provider URLs (postgres://..., ?sslmode=require) are accepted and converted for asyncpg.
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://tidewatch_app:changeme@localhost:5432/tidewatch_local"
    )
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=5, ge=0)

    # Strategy
    symbols: Annotated[list[str], NoDecode] = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    quote_asset: str = "USDT"
    # Dynamic list (ADR-009): off by default. When on, the scanner trades the most liquid
    # pairs of the quote asset instead of SYMBOLS (which stays the startup check and the fallback).
    symbols_auto: bool = False
    symbols_auto_count: int = Field(default=12, ge=1, le=50)
    # Minimum 24h traded value, in the quote asset.
    symbols_auto_min_volume: Decimal = Field(default=Decimal(5_000_000), ge=0)
    # Highest risk tier allowed: "low" (calm), "medium" or "high" (everything that passes the filters).
    symbols_auto_max_risk: Literal["low", "medium", "high"] = "medium"
    symbols_auto_refresh_minutes: int = Field(default=60, ge=5)
    entry_timeout_days: int = Field(default=3, ge=1)
    take_profit_pct: Decimal = Field(default=Decimal("0.04"), gt=0)
    stop_loss_pct: Decimal = Field(default=Decimal("0.02"), gt=0, lt=1)
    # Gap between the stop trigger and the stop-limit price, so the sell can still fill.
    stop_limit_offset_pct: Decimal = Field(default=Decimal("0.002"), ge=0, lt=1)

    # Entry signal (a starting point, not a backtested strategy): RSI was oversold within the
    # last RSI_LOOKBACK closed candles AND the latest close crossed up through the EMA.
    scan_interval: Literal["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"] = "1h"
    rsi_period: int = Field(default=14, ge=2)
    rsi_oversold: Decimal = Field(default=Decimal(30), gt=0, lt=100)
    rsi_lookback: int = Field(default=6, ge=1)
    ema_period: int = Field(default=9, ge=2)
    # Place the entry this far below the best bid (0 = at the bid).
    entry_discount_pct: Decimal = Field(default=Decimal(0), ge=0, lt=Decimal("0.1"))
    scan_every_seconds: int = Field(default=60, ge=1)

    # Risk
    # Share of the free quote balance used by one trade. 0.98 leaves room for rounding.
    allocation_pct: Decimal = Field(default=Decimal("0.98"), gt=0, le=1)
    max_open_positions: int = Field(default=1, ge=1)
    max_spread_pct: Decimal = Field(default=Decimal("0.002"), gt=0, lt=1)
    daily_loss_limit_pct: Decimal = Field(default=Decimal("0.03"), gt=0, lt=1)
    kill_switch_cooldown_hours: int = Field(default=24, ge=1)

    # Engine: how often it checks entry timeouts, unprotected positions and new entries.
    engine_tick_seconds: int = Field(default=30, ge=1)
    # Liveness: the engine touches this file every tick and the Docker HEALTHCHECK reads its age.
    heartbeat_path: Path = Path("/tmp/tidewatch-heartbeat")
    heartbeat_max_age_seconds: int = Field(default=180, ge=10)
    # A second instance on the same database waits this long for the first to stop, then exits.
    instance_lock_wait_seconds: int = Field(default=600, ge=0)
    # Minimum gap between attempts to place a refused exit again.
    exit_retry_seconds: int = Field(default=120, ge=1)

    # Notifications
    # While a position has no stop-loss, repeat the alert this often (0 = alert once only).
    unprotected_reminder_minutes: int = Field(default=15, ge=0)
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    # Answer read-only commands (/status) from that chat. Off by default: only one process can poll
    # a bot token at a time, so a laptop sharing the token with a server must leave it off (ADR-008).
    telegram_commands: bool = False
    # Dates in the sales report (bot.reports.sales_sheet) are days in this zone, not UTC.
    report_timezone: str = "America/Guayaquil"

    @field_validator("report_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"REPORT_TIMEZONE '{value}' is not a known time zone (for example America/Guayaquil)") from exc
        return value

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: SecretStr) -> SecretStr:
        return SecretStr(normalize_database_url(value.get_secret_value()))

    @field_validator("binance_private_key_path", mode="before")
    @classmethod
    def _blank_key_path_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("symbols", mode="before")
    @classmethod
    def _split_symbols(cls, value: object) -> object:
        if isinstance(value, str):
            return [s.strip().upper() for s in value.split(",") if s.strip()]
        return value

    @field_validator("symbols")
    @classmethod
    def _symbols_not_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("SYMBOLS must list at least one pair")
        return value

    @field_validator("quote_asset")
    @classmethod
    def _upper_quote(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def _symbols_use_quote_asset(self) -> Settings:
        wrong = [s for s in self.symbols if not s.endswith(self.quote_asset)]
        if wrong:
            raise ValueError(f"SYMBOLS must all end in {self.quote_asset}: {', '.join(wrong)}")
        return self

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token.get_secret_value() and self.telegram_chat_id)

    @property
    def has_binance_credentials(self) -> bool:
        has_key = bool(self.binance_api_key.get_secret_value())
        if self.binance_private_key_path is not None:  # Ed25519: key id + private key file
            return has_key
        return has_key and bool(self.binance_api_secret.get_secret_value())

    def require_binance_credentials(self) -> None:
        """Fail fast at startup instead of on the first signed request."""
        if not self.has_binance_credentials:
            raise RuntimeError(
                "BINANCE_API_KEY and BINANCE_API_SECRET (or BINANCE_PRIVATE_KEY_PATH) must be set"
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
