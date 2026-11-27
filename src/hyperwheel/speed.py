"""Upload speed to GRIP, from RRDF files: for the log and the Upload Monitor only.

Nothing here changes how files are sent. Each RRDF file of at least
``MIN_BYTES`` that goes up whole in one try (not resumed, not sent again, no
retry) gives a speed: its size over the time from its first chunk being sent to
GRIP's answer to the last.

The Upload Monitor shows the average of the last ``AVERAGE_OF`` such files:
their total bytes over their total seconds. They are kept in the state store's
cursors table (whole numbers: bytes and milliseconds, in ``AVERAGE_OF`` slots
used in turn), so the store's layout doesn't change.

MB here is 1,000,000 bytes.
"""

from __future__ import annotations

from .state import Store

MIN_BYTES = 1_000_000
AVERAGE_OF = 5

_PREFIX = "rrdf_speed_"
_NEXT = "rrdf_speed_next"  # the slot the next file goes in


def _bytes(slot: int) -> str:
    return f"{_PREFIX}{slot}_bytes"


def _ms(slot: int) -> str:
    return f"{_PREFIX}{slot}_ms"


_OURS = {_NEXT, *(_bytes(i) for i in range(AVERAGE_OF)), *(_ms(i) for i in range(AVERAGE_OF))}


def measured(sent_bytes: int, seconds: float) -> float | None:
    """Bytes per second, or None if it can't be worked out (too little sent, or no time measured)."""
    if sent_bytes < MIN_BYTES or seconds <= 0:
        return None
    return sent_bytes / seconds


def record(store: Store, size: int, seconds: float) -> None:
    """Keep one file's size and time, in place of the oldest of the last AVERAGE_OF."""
    slot = store.cursor(_NEXT) % AVERAGE_OF
    store.set_cursor(_bytes(slot), int(size))
    store.set_cursor(_ms(slot), max(1, round(seconds * 1000)))
    store.set_cursor(_NEXT, (slot + 1) % AVERAGE_OF)
    store.delete_cursors(_PREFIX, keep=_OURS)  # anything else under the prefix is from 0.1.20's speeds


def average_mb_s(store: Store) -> float | None:
    """The last AVERAGE_OF files' total MB over their total seconds, or None before the first."""
    total_bytes = total_ms = 0
    for slot in range(AVERAGE_OF):
        b, ms = store.cursor(_bytes(slot)), store.cursor(_ms(slot))
        if b > 0 and ms > 0:
            total_bytes += b
            total_ms += ms
    return mb_s(total_bytes * 1000 / total_ms) if total_ms else None


def mb_s(bytes_per_s: float) -> float:
    value = bytes_per_s / 1e6
    return round(value, 1) if value >= 1 else round(value, 2)


def describe(size: int, seconds: float, bytes_per_s: float | None) -> str:
    """For the log: '34.2 MB in 5.1 s, 6.7 MB/s', without the speed when there is none."""
    text = f"{size / 1e6:.1f} MB in {seconds:.1f} s"
    return f"{text}, {mb_s(bytes_per_s)} MB/s" if bytes_per_s is not None else text
