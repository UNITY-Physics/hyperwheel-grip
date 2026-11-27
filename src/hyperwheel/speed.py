"""Upload speed to GRIP, from RRDF files: for the log and the Upload Monitor only.

Nothing here changes how files are sent. Each RRDF file of at least
``MIN_BYTES`` that goes up whole in one try (not resumed, not sent again, no
retry) gives a speed: its size over the time from its first chunk being sent to
GRIP's answer to the last. Smaller files are left out: their time is mostly waiting for an answer.

The speeds are kept in the state store's cursors table (whole numbers), so the
store's layout doesn't change: the last speed and when it was measured, and
each day's top speed. The top shown is the best of the last ``TOP_DAYS``
days, today included. Days older than that are removed.

MB here is 1,000,000 bytes.
"""

from __future__ import annotations

from .state import Store

MIN_BYTES = 1_000_000
TOP_DAYS = 7
DAY_S = 86400

LAST = "rrdf_speed_last"  # bytes per second
LAST_AT = "rrdf_speed_last_at"  # seconds since the epoch
_DAY = "rrdf_speed_top_day_"  # + the day number (UTC days since the epoch) -> that day's top, bytes per second


def measured(sent_bytes: int, seconds: float) -> float | None:
    """Bytes per second, or None if it can't be worked out (too little sent, or no time measured)."""
    if sent_bytes < MIN_BYTES or seconds <= 0:
        return None
    return sent_bytes / seconds


def record(store: Store, bytes_per_s: float, now: float) -> None:
    rate = max(1, round(bytes_per_s))
    day = int(now // DAY_S)
    store.set_cursor(LAST, rate)
    store.set_cursor(LAST_AT, int(now))
    if rate > store.cursor(f"{_DAY}{day}"):
        store.set_cursor(f"{_DAY}{day}", rate)
    store.delete_cursors(_DAY, keep={f"{_DAY}{d}" for d in range(day - TOP_DAYS + 1, day + 1)})


def read(store: Store, now: float) -> dict[str, float | None]:
    """The last speed and the top of the last TOP_DAYS days, in MB/s (None for either if there is none)."""
    last = store.cursor(LAST)
    day = int(now // DAY_S)
    top = max((store.cursor(f"{_DAY}{d}") for d in range(day - TOP_DAYS + 1, day + 1)), default=0)
    return {"last_mb_s": mb_s(last) if last > 0 else None, "top_mb_s": mb_s(top) if top > 0 else None}


def mb_s(bytes_per_s: float) -> float:
    value = bytes_per_s / 1e6
    return round(value, 1) if value >= 1 else round(value, 2)


def describe(size: int, seconds: float, bytes_per_s: float | None) -> str:
    """For the log: '34.2 MB in 5.1 s, 6.7 MB/s', without the speed when there is none."""
    text = f"{size / 1e6:.1f} MB in {seconds:.1f} s"
    return f"{text}, {mb_s(bytes_per_s)} MB/s" if bytes_per_s is not None else text
