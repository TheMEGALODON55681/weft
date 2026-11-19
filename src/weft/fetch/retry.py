"""Backoff and Retry-After for the fetch retry loop."""

import math
import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def backoff_delay(attempt: int, base_s: float) -> float:
    """Exponential backoff with jitter: between half of and all of base * 2**attempt. Callers
    that failed together spread out, and none comes straight back."""
    ceiling = base_s * 2.0**attempt
    return ceiling / 2 + random.uniform(0, ceiling / 2)  # noqa: S311 (jitter, not security)


def parse_retry_after(value: str | None, now: datetime | None = None) -> float:
    """Seconds to wait according to a Retry-After header, as a number or an HTTP date.
    0 when it is absent, malformed or already past."""
    if not value:
        return 0.0
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return 0.0
        if when.tzinfo is None:  # a date with no zone: HTTP dates are GMT
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(UTC))).total_seconds()
    return seconds if math.isfinite(seconds) and seconds > 0 else 0.0
