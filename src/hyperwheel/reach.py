"""Can the Pi reach the scanner, and GRIP over the internet?

The Pi's internet goes through the scanner, so "no internet" often means the
scanner itself is off or has stopped answering. The two are told apart so the
Upload Monitor and ``hyperwheel doctor`` can say which one it is:

- no link on the scanner cable: the scanner is off or the cable is unplugged;
- a link but no answer: the scanner is on the cable but has stopped answering
  on the network (seen in the field; restarting the scanner fixed it);
- the scanner answers but GRIP doesn't: the internet through the scanner is
  down, or GRIP is.

The service records what it finds in the state store (as cursors), because
the Upload Monitor only reads the store.
"""

from __future__ import annotations

import ipaddress
import secrets
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .config import Routing, SiteConfig
from .state import Store

# Scanner states.
SCANNER_OK = 1
SCANNER_NO_ANSWER = 0  # the cable has a link, but the scanner doesn't answer
SCANNER_NO_LINK = -1  # no link on the cable
SCANNER_UNKNOWN_LINK = -2  # no answer, and the link can't be read

_SCANNER, _SCANNER_SINCE = "reach_scanner", "reach_scanner_since"
_INTERNET, _INTERNET_SINCE = "reach_internet", "reach_internet_since"

# A problem shows only once it has lasted this long, so a single slow answer
# doesn't flash a warning. No link on the cable shows at once.
GRACE_S = 120

# When GRIP answered an upload this recently, the scanner and the internet work, whatever a
# quick check says: on a slow link the check can time out behind the upload's own traffic.
GRIP_ANSWERED = "reach_grip_answered"
RECENT_S = 90

# Name lookups: slower than this and people notice it. Longer than the timeout counts as no answer.
SLOW_LOOKUP_S = 3.0
LOOKUP_TIMEOUT_S = 30.0
_NO_SUCH_NAME = {socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)}


def link_up(interface: str, sys_net: Path = Path("/sys/class/net")) -> bool | None:
    """Whether the network interface has a link (cable connected, other end on). None if unknown."""
    try:
        return (sys_net / interface / "carrier").read_text().strip() == "1"
    except OSError:  # no such interface, or the interface is down
        return None


def tcp_answers(host: str, port: int, timeout_s: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:  # includes a failed name look-up
        return False


IP_FREEBIND = 15  # <linux/in.h>. Python's socket module has the name only from 3.14, and Trixie has 3.13.


def allow_unassigned_address(sock: socket.socket) -> None:
    """Let a listening socket bind an address the Pi doesn't have yet, such as the hotspot's before it's up
    (Linux only). It starts answering there as soon as the address appears."""
    if sys.platform.startswith("linux"):
        sock.setsockopt(socket.IPPROTO_IP, IP_FREEBIND, 1)


def lookup_seconds(name: str, timeout_s: float = LOOKUP_TIMEOUT_S) -> float | None:
    """How long the Pi takes to get an answer for a name, or None if none comes in time.

    "There's no such name" is an answer too: this measures the way to the name server, not the name.
    """
    answers: list[float] = []

    def look() -> None:
        started = time.monotonic()
        try:
            socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            if e.errno not in _NO_SUCH_NAME:
                return
        except OSError:
            return
        answers.append(time.monotonic() - started)

    # getaddrinfo has no timeout of its own, so it runs in a thread that's left behind if it takes too long.
    worker = threading.Thread(target=look, daemon=True)
    worker.start()
    worker.join(timeout_s)
    return answers[0] if answers else None


def new_lookup_name() -> str:
    """A name no name server has an answer for yet, so it goes all the way and shows the slowest case."""
    return f"hyperwheel-check-{secrets.token_hex(6)}.github.com"


def scanner_state(site: SiteConfig, *, link: Callable[[str], bool | None] = link_up,
                  answers: Callable[[str, int], bool] = tcp_answers) -> int | None:
    """None if no scanner address is set up."""
    s = site.scanner
    if not s.host:
        return None
    if answers(s.host, s.port):
        return SCANNER_OK
    has_link = link(s.interface)
    if has_link is None:
        return SCANNER_UNKNOWN_LINK
    return SCANNER_NO_ANSWER if has_link else SCANNER_NO_LINK


def grip_hosts(routing: Routing) -> list[tuple[str, int]]:
    hosts = []
    for p in routing.projects.values():
        for url in (p.api_base_url, p.token_url):
            u = urlsplit(url)
            if u.hostname:
                hp = (u.hostname, u.port or (443 if u.scheme == "https" else 80))
                if hp not in hosts:
                    hosts.append(hp)
    return hosts


def internet_state(routing: Routing, *, answers: Callable[[str, int], bool] = tcp_answers) -> bool | None:
    """Whether every GRIP address answers. None if no project is set up."""
    hosts = grip_hosts(routing)
    if not hosts:
        return None
    return all(answers(h, p) for h, p in hosts)


# ---- recording and reading back -------------------------------------------------------


def record_scanner(store: Store, state: int | None, now: float) -> None:
    _record(store, _SCANNER, _SCANNER_SINCE, state, now)


def record_internet(store: Store, ok: bool | None, now: float) -> None:
    _record(store, _INTERNET, _INTERNET_SINCE, None if ok is None else int(ok), now)


def _record(store: Store, key: str, since_key: str, value: int | None, now: float) -> None:
    if value is None:
        return
    # The cursor table holds integers; store state + 10 so that "never checked" (0) stays distinct.
    if store.cursor(key) != value + 10:
        store.set_cursor(key, value + 10)
        store.set_cursor(since_key, int(now))


@dataclass(frozen=True)
class Reach:
    scanner: int | None  # one of the SCANNER_* states, or None if never checked
    scanner_since: float
    internet: bool | None
    internet_since: float
    grip_answered: float = 0.0

    def grip_recent(self, now: float) -> bool:
        return now - self.grip_answered <= RECENT_S

    def scanner_down(self, now: float) -> bool:
        if self.scanner is None or self.scanner == SCANNER_OK:
            return False
        if self.scanner != SCANNER_NO_LINK and self.grip_recent(now):
            return False
        return self.scanner == SCANNER_NO_LINK or now - self.scanner_since >= GRACE_S

    def internet_down(self, now: float) -> bool:
        return self.internet is False and not self.grip_recent(now) and now - self.internet_since >= GRACE_S


def read(store: Store) -> Reach:
    s, i = store.cursor(_SCANNER), store.cursor(_INTERNET)
    return Reach(s - 10 if s else None, store.cursor(_SCANNER_SINCE),
                 bool(i - 10) if i else None, store.cursor(_INTERNET_SINCE), store.cursor(GRIP_ANSWERED))


def messages(r: Reach, now: float) -> list[dict]:
    """Plain-language lines for the Upload Monitor and ``hyperwheel status``."""
    out = []
    if r.scanner_down(now):
        if r.scanner == SCANNER_NO_LINK:
            out.append({"level": "flag", "text": "The Pi has no network link to the scanner: the scanner is off "
                                                 "or the cable is unplugged. Nothing can be fetched or uploaded "
                                                 "until it's back. Files on the Pi are kept."})
        elif r.scanner == SCANNER_NO_ANSWER:
            out.append({"level": "alert", "text": "The Pi can't reach the scanner. The cable is connected, but the "
                                                  "scanner isn't answering on the network, so the Pi also has no "
                                                  "internet. If the scanner is on, restart it when no one is being "
                                                  "scanned. Files on the Pi are kept."})
        else:
            out.append({"level": "alert", "text": "The Pi can't reach the scanner, so it also has no internet. "
                                                  "Check that the scanner is on and the cable is connected. "
                                                  "Files on the Pi are kept."})
    elif r.internet_down(now):
        out.append({"level": "alert", "text": "The Pi can reach the scanner but not GRIP: the internet connection "
                                              "through the scanner is down, or GRIP is. Uploads wait. Files on the "
                                              "Pi are kept."})
    return out


def lookup_names(routing: Routing) -> list[str]:
    """The names the Pi depends on: each project's GRIP address, and GitHub for updates."""
    names = {host for host, _ in grip_hosts(routing) if not _is_address(host)}
    return sorted(names | {"github.com"})


def _is_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def describe_hosts(hosts: Iterable[tuple[str, int]]) -> str:
    return ", ".join(f"{h}:{p}" for h, p in hosts)
