import os
from decimal import Decimal

import pytest
from pydantic import ValidationError

from bot.config.settings import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    names = {name.upper() for name in Settings.model_fields}
    for key in list(os.environ):
        if key.upper() in names:
            monkeypatch.delenv(key, raising=False)


def make(**env) -> Settings:
    # _env_file=None: ignore the developer's real .env so tests are deterministic.
    return Settings(_env_file=None, **env)


def test_defaults_are_safe_and_match_the_plan():
    s = make()
    assert s.dry_run is True and s.binance_testnet is True
    assert s.take_profit_pct == Decimal("0.04")
    assert s.stop_loss_pct == Decimal("0.02")
    assert s.max_open_positions == 1
    assert s.allocation_pct == Decimal("0.98")
    assert s.daily_loss_limit_pct == Decimal("0.03")
    assert s.entry_timeout_days == 3
    assert s.telegram_enabled is False


def test_percentages_are_decimal_not_float():
    s = make(TAKE_PROFIT_PCT="0.005")
    assert isinstance(s.take_profit_pct, Decimal)
    assert s.take_profit_pct == Decimal("0.005")


def test_symbols_are_split_trimmed_and_uppercased(monkeypatch):
    monkeypatch.setenv("SYMBOLS", "btcusdt, solusdt ,gtcusdt")
    assert make().symbols == ["BTCUSDT", "SOLUSDT", "GTCUSDT"]


def test_symbols_must_use_the_quote_asset():
    with pytest.raises(ValidationError, match="must all end in USDT"):
        make(SYMBOLS="BTCUSDT,ETHBTC")


def test_empty_symbols_rejected(monkeypatch):
    monkeypatch.setenv("SYMBOLS", " , ")
    with pytest.raises(ValidationError):
        make()


@pytest.mark.parametrize(
    "field,value",
    [
        ("ALLOCATION_PCT", "1.5"),
        ("ALLOCATION_PCT", "0"),
        ("TAKE_PROFIT_PCT", "0"),
        ("STOP_LOSS_PCT", "1"),
        ("DAILY_LOSS_LIMIT_PCT", "0"),
        ("MAX_OPEN_POSITIONS", "0"),
        ("ENTRY_TIMEOUT_DAYS", "0"),
    ],
)
def test_out_of_range_values_rejected(field, value):
    with pytest.raises(ValidationError):
        make(**{field: value})


def test_secrets_are_not_printed():
    s = make(BINANCE_API_SECRET="super-secret", TELEGRAM_BOT_TOKEN="tok-12345")
    assert "super-secret" not in repr(s)
    assert "tok-12345" not in repr(s)


def test_telegram_enabled_needs_token_and_chat():
    assert make(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="1").telegram_enabled is True
    assert make(TELEGRAM_BOT_TOKEN="t").telegram_enabled is False


def test_binance_credentials_required_only_when_asked():
    with pytest.raises(RuntimeError):
        make().require_binance_credentials()
    make(BINANCE_API_KEY="k", BINANCE_API_SECRET="s").require_binance_credentials()


def test_settings_are_immutable():
    with pytest.raises(ValidationError):
        make().dry_run = False


# --- database url -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # already right: unchanged
        (
            "postgresql+asyncpg://u:p@localhost:5432/db",
            "postgresql+asyncpg://u:p@localhost:5432/db",
        ),
        # provider styles: scheme and sslmode are converted
        ("postgres://u:p@h:25060/db", "postgresql+asyncpg://u:p@h:25060/db"),
        ("postgresql://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        (
            "postgres://u:p@h:25060/db?sslmode=require",
            "postgresql+asyncpg://u:p@h:25060/db?ssl=require",
        ),
        (
            "postgresql+psycopg2://u:p@h/db?sslmode=verify-full",
            "postgresql+asyncpg://u:p@h/db?ssl=verify-full",
        ),
        # an explicit asyncpg-style ssl wins over a stray sslmode
        (
            "postgres://u:p@h/db?ssl=require&sslmode=disable",
            "postgresql+asyncpg://u:p@h/db?ssl=require",
        ),
    ],
)
def test_database_url_is_normalised_for_asyncpg(raw, expected):
    assert make(DATABASE_URL=raw).database_url.get_secret_value() == expected


def test_database_url_keeps_url_encoded_password_intact():
    url = make(DATABASE_URL="postgres://u:p%40ss%2Fword@h/db").database_url.get_secret_value()
    assert url == "postgresql+asyncpg://u:p%40ss%2Fword@h/db"


def test_database_url_must_be_postgres():
    with pytest.raises(ValidationError, match="PostgreSQL URL"):
        make(DATABASE_URL="mysql://u:p@h/db")


def test_database_password_is_not_printed():
    s = make(DATABASE_URL="postgres://u:hunter2-secret@h/db")
    assert "hunter2-secret" not in repr(s)
    assert "hunter2-secret" not in str(s)


# --- exchange settings --------------------------------------------------------------------


def test_blank_private_key_path_means_not_set(monkeypatch):
    monkeypatch.setenv("BINANCE_PRIVATE_KEY_PATH", "")
    assert make().binance_private_key_path is None


def test_credentials_need_key_and_secret_or_key_and_key_file():
    assert make().has_binance_credentials is False
    assert make(BINANCE_API_KEY="k").has_binance_credentials is False  # secret missing
    assert make(BINANCE_API_KEY="k", BINANCE_API_SECRET="s").has_binance_credentials is True
    # Ed25519: the key file replaces the secret
    ed = make(BINANCE_API_KEY="k", BINANCE_PRIVATE_KEY_PATH="/keys/ed25519.pem")
    assert ed.has_binance_credentials is True
    assert make(BINANCE_PRIVATE_KEY_PATH="/keys/ed25519.pem").has_binance_credentials is False


def test_order_id_prefix_is_defaulted_and_validated():
    assert make().order_id_prefix == "bot"
    assert make(ORDER_ID_PREFIX="tb2").order_id_prefix == "tb2"
    for bad in ("", "has-dash", "x" * 13, "sp ace"):
        with pytest.raises(ValidationError):
            make(ORDER_ID_PREFIX=bad)


# --- entry signal and retry settings ------------------------------------------------------


def test_signal_defaults():
    s = make()
    assert (s.scan_interval, s.rsi_period, s.rsi_oversold, s.rsi_lookback, s.ema_period) == (
        "1h", 14, Decimal(30), 6, 9,
    )
    assert s.entry_discount_pct == 0 and s.scan_every_seconds == 60 and s.exit_retry_seconds == 120


@pytest.mark.parametrize(
    "field,value",
    [
        ("SCAN_INTERVAL", "7m"),  # not a Binance candle size
        ("RSI_PERIOD", "1"),
        ("RSI_OVERSOLD", "0"),
        ("RSI_OVERSOLD", "100"),
        ("RSI_LOOKBACK", "0"),
        ("EMA_PERIOD", "1"),
        ("ENTRY_DISCOUNT_PCT", "-0.01"),
        ("ENTRY_DISCOUNT_PCT", "0.1"),
        ("SCAN_EVERY_SECONDS", "0"),
        ("EXIT_RETRY_SECONDS", "0"),
    ],
)
def test_out_of_range_signal_settings_are_rejected(field, value):
    with pytest.raises(ValidationError):
        make(**{field: value})
