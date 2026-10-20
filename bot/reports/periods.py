"""A report period: a year (2026) or a month (2026-10), as whole local days in a time zone."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo

PERIOD = re.compile(r"^(\d{4})(?:-(\d{1,2}))?$")


class PeriodError(ValueError):
    """The text is not a year (2026) or a month (2026-10)."""


@dataclass(frozen=True)
class Period:
    start: datetime  # inclusive, UTC
    end: datetime  # exclusive, UTC
    label: str


def parse_period(text: str, tz: tzinfo, *, now: datetime | None = None) -> Period:
    """`2026` or `2026-10`, as whole local days in `tz`, converted to UTC bounds."""
    match = PERIOD.match(text.strip())
    if not match:
        raise PeriodError(f"'{text}' is not a year (2026) or a month (2026-10)")
    year = int(match.group(1))
    month = int(match.group(2)) if match.group(2) else None
    latest = (now or datetime.now(UTC)).year + 1
    if not 2000 <= year <= latest or (month is not None and not 1 <= month <= 12):
        raise PeriodError(f"'{text}' is not a plausible year or month")
    if month is None:
        start, end, label = datetime(year, 1, 1, tzinfo=tz), datetime(year + 1, 1, 1, tzinfo=tz), str(year)
    else:
        ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
        start, end = datetime(year, month, 1, tzinfo=tz), datetime(ny, nm, 1, tzinfo=tz)
        label = f"{year}-{month:02d}"
    return Period(start.astimezone(UTC), end.astimezone(UTC), label)
