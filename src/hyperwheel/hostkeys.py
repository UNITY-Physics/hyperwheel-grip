"""Every scanner SSH key this Pi has seen.

A scanner's SSH key changes when its software is upgraded, and at a site with
two scanners and one Pi, the Pi is moved to the other scanner when one breaks
down. Both must just work, so a key the Pi hasn't seen is accepted and
remembered, never refused. ``hyperwheel status`` and the Upload Monitor show a
short notice for a day after a new one appears. Keys are matched by their
fingerprint, not by the scanner's address (both scanners at a site answer on
the same address), so moving the Pi back and forth is quiet after the first
time.

What this gives up: a device plugged into the scanner cable could pose as the
scanner. The raw-data password would reach it, and the Pi would fetch from it.

The first key the Pi ever sees is its first scanner and raises no notice. Keys
listed in the site's ``scanner_known_hosts`` file (optional) are taken as seen
when the list is still empty. Only the Hyperwheel service writes this list;
commands run as root only read it.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from collections.abc import Callable
from pathlib import Path

from .state import HostKey, Store

log = logging.getLogger("hyperwheel.hostkeys")

NOTICE_HOURS = 24


def fingerprint(key_bytes: bytes) -> str:
    """The key's fingerprint as OpenSSH shows it (``ssh-keygen -l``): SHA256 and base64 without padding."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(key_bytes).digest()).decode().rstrip("=")


def read_known_hosts(path: Path) -> list[tuple[str, str, str]]:
    """(host, key type, fingerprint) for each key line of an OpenSSH known_hosts file.

    Comments, markers (``@revoked``, ``@cert-authority``) and lines that don't
    parse are skipped. A missing or unreadable file gives an empty list.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[0].startswith(("#", "@")):
            continue
        try:
            blob = base64.b64decode(parts[2], validate=True)
        except ValueError:
            continue
        out.append((parts[0], parts[1], fingerprint(blob)))
    return out


class HostKeyBook:
    """Decides nothing: records each scanner key the Pi connects to, and says which ones are new."""

    def __init__(self, store: Store, known_hosts: Path | None = None, *, clock: Callable[[], float] = time.time):
        self.store, self.clock = store, clock
        if known_hosts is not None and not store.host_keys():
            for host, key_type, fp in read_known_hosts(known_hosts):
                store.remember_host_key(fp, key_type, host, quiet=True)

    def seen(self, host: str, key_type: str, key_bytes: bytes) -> None:
        """Called for every connection to the scanner, with the key it presented."""
        fp = fingerprint(key_bytes)
        first = not self.store.host_keys()
        if self.store.remember_host_key(fp, key_type, host, quiet=first):
            if first:
                log.info("scanner at %s: first scanner key, %s %s, remembered", host, key_type, fp)
            else:
                log.warning("scanner at %s: a key this Pi hasn't seen before, %s %s; accepted and remembered "
                            "(a scanner upgrade, or a different scanner)", host, key_type, fp)


def new_key_notices(store: Store, now: float) -> list[str]:
    """Notices for keys first seen in the last day, newest first."""
    since = now - NOTICE_HOURS * 3600
    keys: list[HostKey] = [k for k in store.host_keys() if not k.quiet and k.first_seen > since]
    return [notice(k) for k in sorted(keys, key=lambda k: k.first_seen, reverse=True)]


def notice(k: HostKey) -> str:
    t = time.localtime(k.first_seen)
    when = f"{t.tm_hour:02d}:{t.tm_min:02d} on {t.tm_mday} {time.strftime('%b', t)}"
    return (f"At {when} this Pi connected to a scanner it hadn't seen before, or the scanner's software was "
            f"upgraded (scanner key {k.fingerprint[:19]}…). The Pi remembers this scanner now. If no one "
            "upgraded or swapped the scanner, tell support.")
