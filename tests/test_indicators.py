from decimal import Decimal
from itertools import pairwise

import pytest

from bot.services.indicators import ema_series, rsi_series

D = Decimal

# StockCharts' worked RSI(14) example (Wilder smoothing): the closes and the published RSI values.
RSI_CLOSES = [D(x) for x in ["44.3389", "44.0902", "44.1497", "43.6124", "44.3278", "44.8264", "45.0955", "45.4245", "45.8433", "46.0826", "45.8931", "46.0328", "45.6140", "46.2820", "46.2820", "46.0028", "46.0328", "46.4116", "46.2222", "45.6439", "46.2122", "46.2521", "45.7137", "46.4515", "45.7835", "45.3548", "44.0288", "44.1783", "44.2224", "44.5716", "43.4205", "42.6628", "43.1314"]]
RSI_PUBLISHED = [D(x) for x in ["70.53", "66.32", "66.55", "69.41", "66.36", "57.97", "62.93", "63.26", "56.06", "62.38", "54.71", "50.42", "39.99", "41.46", "41.87", "45.46", "37.30", "33.08", "37.77"]]

# StockCharts' worked EMA(10) example.
EMA_CLOSES = [D(x) for x in ["22.27", "22.19", "22.08", "22.17", "22.18", "22.13", "22.23", "22.43", "22.24", "22.29", "22.15", "22.39", "22.38", "22.61", "23.36", "24.05", "23.75", "23.83", "23.95", "23.63", "23.82", "23.87", "23.65", "23.19", "23.10", "23.33", "22.68", "23.10", "22.40", "22.17"]]
EMA_PUBLISHED = [D(x) for x in ["22.22", "22.21", "22.24", "22.27", "22.33", "22.52", "22.80", "22.97", "23.13", "23.28", "23.34", "23.43", "23.51", "23.54", "23.47", "23.40", "23.39", "23.26", "23.23", "23.08", "22.92"]]


def test_rsi_matches_the_published_worked_example():
    got = rsi_series(RSI_CLOSES, 14)
    assert len(got) == len(RSI_PUBLISHED)
    for mine, published in zip(got, RSI_PUBLISHED, strict=True):
        assert abs(mine - published) < D("0.1"), (mine, published)


def test_ema_matches_the_published_worked_example():
    got = ema_series(EMA_CLOSES, 10)
    assert len(got) == len(EMA_PUBLISHED)
    for mine, published in zip(got, EMA_PUBLISHED, strict=True):
        assert abs(mine - published) < D("0.01"), (mine, published)


def test_rsi_extremes():
    rising = [D(i) for i in range(1, 30)]
    falling = list(reversed(rising))
    assert rsi_series(rising, 14)[-1] == 100
    assert rsi_series(falling, 14)[-1] == 0
    assert rsi_series([D(5)] * 30, 14)[-1] == 50  # no movement: neutral, not a crash


def test_rsi_stays_between_0_and_100():
    import random

    rng = random.Random(7)
    closes = [D(100)]
    for _ in range(300):
        closes.append(max(D("0.01"), closes[-1] * (1 + D(str(round(rng.uniform(-0.05, 0.05), 4))))))
    assert all(0 <= v <= 100 for v in rsi_series(closes, 14))


def test_not_enough_data_gives_no_values_instead_of_guesses():
    assert rsi_series([D(1)] * 14, 14) == []
    assert ema_series([D(1)] * 8, 9) == []


def test_ema_of_a_constant_is_that_constant_and_follows_the_trend():
    assert ema_series([D(7)] * 20, 9)[-1] == 7
    up = ema_series([D(i) for i in range(1, 40)], 9)
    assert all(a < b for a, b in pairwise(up))


def test_periods_below_two_are_rejected():
    with pytest.raises(ValueError):
        rsi_series([D(1)] * 5, 1)
    with pytest.raises(ValueError):
        ema_series([D(1)] * 5, 1)
