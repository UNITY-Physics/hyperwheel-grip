"""``hyperwheel doctor``: check the Pi's connections and services, in plain language.

It probes now (the scanner cable, the scanner, Orthanc, GRIP) rather than
relying on what the service last saw, and reads the rest from the state store.
It needs no keys or passwords, changes nothing, and prints no patient data.
"""

from __future__ import annotations

import platform
import shutil
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import __version__, reach
from .config import Routing, SiteConfig
from .dashboard import HEARTBEAT, KEY_TEXT, clock_synced
from .state import Store

OK, PROBLEM, NOTE = "OK", "PROBLEM", "NOTE"


def _orthanc_answers(url: str) -> bool:
    try:
        urllib.request.urlopen(url.rstrip("/") + "/system", timeout=5).close()
        return True
    except urllib.error.HTTPError:  # e.g. 401: Orthanc is up and asks for a login
        return True
    except OSError:
        return False


@dataclass
class Probes:
    link: Callable[[str], bool | None] = reach.link_up
    answers: Callable[[str, int], bool] = reach.tcp_answers
    orthanc_answers: Callable[[str], bool] = _orthanc_answers
    synced: Callable[[], bool] = clock_synced
    free_bytes: Callable[[Path], int] = lambda p: shutil.disk_usage(p).free
    now: Callable[[], float] = time.time


def run(site: SiteConfig, routing: Routing, probes: Probes | None = None) -> tuple[list[tuple[str, str, str]], int]:
    """The checks, as (result, what, detail), and the exit code (1 if anything is a problem)."""
    p = probes or Probes()
    out: list[tuple[str, str, str]] = []
    now = p.now()

    store = None
    try:
        store = Store(site.state_db, readonly=True)
    except Exception as e:  # noqa: BLE001 - reported, not raised
        out.append((PROBLEM, "State store", f"can't be read ({type(e).__name__}); has the service ever run?"))

    if store is not None:
        beat = store.cursor(HEARTBEAT)
        if beat and now - beat <= 300:
            out.append((OK, "Hyperwheel service", f"running (last active {now - beat:.0f} s ago)"))
        else:
            out.append((PROBLEM, "Hyperwheel service", "not running. Nothing is fetched or uploaded."))

    # The scanner, then the internet: the Pi's internet goes through the scanner.
    # An upload GRIP answered in the last 90 s proves both work, even when a quick
    # check times out behind the upload's own traffic on a slow link.
    answered = store.cursor(reach.GRIP_ANSWERED) if store is not None else 0
    busy = f"an upload got an answer from GRIP {now - answered:.0f} s ago; the link is busy or slow"
    recent = bool(answered) and now - answered <= reach.RECENT_S
    s = site.scanner
    scanner_ok = None
    if not s.host:
        out.append((NOTE, "Scanner", "no scanner address set up in site.toml"))
    else:
        state = reach.scanner_state(site, link=p.link, answers=p.answers)
        where = f"{s.host}, port {s.port}"
        scanner_ok = state == reach.SCANNER_OK or (recent and state != reach.SCANNER_NO_LINK)
        if state == reach.SCANNER_OK:
            out.append((OK, "Scanner", f"answering ({where})"))
        elif scanner_ok:
            out.append((OK, "Scanner", f"working: the quick check got no answer, but {busy}"))
        elif state == reach.SCANNER_NO_LINK:
            out.append((PROBLEM, "Scanner", f"no network link on {s.interface}: the scanner is off or the cable "
                                            "is unplugged"))
        elif state == reach.SCANNER_NO_ANSWER:
            out.append((PROBLEM, "Scanner", f"the cable is connected ({s.interface} has a link), but the scanner "
                                            f"isn't answering ({where}). If the scanner is on, restart it when no "
                                            "one is being scanned."))
        else:
            out.append((PROBLEM, "Scanner", f"not answering ({where}), and the link on {s.interface} can't be "
                                            "read. Check that the scanner is on and the cable is connected."))
        if store is not None:
            failing = [f for code in ("RAW_LOGIN_REFUSED", "RAW_PULL_FAILING")
                       for f in store.open_flags() if f.code == code]
            if failing:
                out.append((PROBLEM, "Raw data from the scanner", failing[0].detail))

    hosts = reach.grip_hosts(routing)
    if not hosts:
        out.append((NOTE, "GRIP", "no projects in routing.toml"))
    else:
        down = [hp for hp in hosts if not p.answers(*hp)]
        if not down:
            out.append((OK, "GRIP over the internet", f"answering ({reach.describe_hosts(hosts)})"))
        elif recent:
            out.append((OK, "GRIP over the internet", f"working: the quick check got no answer, but {busy}"))
        elif scanner_ok is False:
            out.append((PROBLEM, "GRIP over the internet", "not reachable, because the Pi can't reach the scanner "
                                                          "(the Pi's internet goes through it). Fix the scanner "
                                                          "first."))
        else:
            out.append((PROBLEM, "GRIP over the internet", f"not answering ({reach.describe_hosts(down)}), although "
                                                          "the scanner is. The internet through the scanner is down, "
                                                          "or GRIP is."))

    if p.orthanc_answers(site.orthanc.url):
        out.append((OK, "Orthanc (receives the scanner's DICOM)", "answering"))
    else:
        out.append((PROBLEM, "Orthanc (receives the scanner's DICOM)", f"not answering at {site.orthanc.url}"))

    if p.synced():
        out.append((OK, "Clock", "set from the network"))
    else:
        out.append((PROBLEM, "Clock", "not set from the network yet; uploads wait until it is"))

    try:
        free = p.free_bytes(site.staging_dir if site.staging_dir.exists() else Path("/")) / 1024 ** 3
        result = PROBLEM if free < site.disk.warn_free_gb else OK
        out.append((result, "Disk", f"{free:.1f} GB free"))
    except OSError as e:
        out.append((PROBLEM, "Disk", f"free space can't be read ({e.strerror})"))

    if store is not None:
        for name in routing.projects:
            status = store.key_status(name)
            if status is None:
                out.append((NOTE, f"GRIP key for {name}", "not used yet"))
            elif status[0] == "working":
                out.append((OK, f"GRIP key for {name}", "working"))
            else:
                out.append((PROBLEM, f"GRIP key for {name}", KEY_TEXT.get(status[0], status[0])))
        store.close()

    from .updater import Paths, summary
    out.append((NOTE, "Updates", summary(Paths())))
    out.append((NOTE, "Versions", f"Hyperwheel {__version__}, {_os_name()}, kernel {platform.release()}"))
    return out, int(any(r == PROBLEM for r, _, _ in out))


def _os_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.system()


def print_report(site: SiteConfig, results: list[tuple[str, str, str]]) -> None:
    print(f"Hyperwheel doctor, site {site.site}")
    for result, what, detail in results:
        print(f"  {result:<8} {what}: {detail}")
