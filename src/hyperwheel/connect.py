"""Raspberry Pi Connect: keeps a new Pi trying to join the organisation, and reports Connect's state.

``hyperwheel-connect.service`` runs ``hyperwheel connect-watch`` as root. Connect itself belongs to one
account: the one that has it enabled, or, on a new Pi, ``datamanager``.

**Until the Pi is set up** the watcher:

- turns lingering and Connect on for that account, so Connect runs from every boot with nobody logged in;
- keeps a sign-in link ready for the Upload Monitor, which the site sends to the admin, who opens it.

**Once the Pi is set up** it only reads the state. Nothing signs the Pi in or out.

It writes the state to a small file, which the Upload Monitor and ``hyperwheel doctor`` read. Connect's
own output isn't logged.
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .installer import DATA_MANAGER, Layout, is_set_up

log = logging.getLogger("hyperwheel.connect")

FOLDER = "/var/lib/hyperwheel/connect"
STATE_FILE = "state.json"

SIGN_IN_LINK = re.compile(r"https://connect\.raspberrypi\.com/\S+")
ENABLED_LINK = ".config/systemd/user/default.target.wants/rpi-connect.service"

TICK_S = 5.0
CHECK_EVERY_S = 30.0
CHECK_EVERY_SIGNED_IN_S = 300.0
STALE_S = 900.0

OK, PROBLEM, NOTE = "OK", "PROBLEM", "NOTE"


# ---- the state -------------------------------------------------------------------------------------------


def read_state(folder: Path = Path(FOLDER)) -> dict | None:
    try:
        state = json.loads((folder / STATE_FILE).read_text())
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def describe(state: dict | None, now: float, *, set_up: bool) -> tuple[str, str]:
    """Connect's state in plain words, as (result, text), for the Upload Monitor and doctor."""
    if state is None:
        return NOTE, "not checked yet"
    if now - state.get("checked_at", 0) > STALE_S:
        return NOTE, "not checked in the last 15 minutes"
    signed_in = state.get("signed_in")
    if signed_in is True:
        return OK, "signed in"
    if signed_in is None:
        return NOTE, "its state can't be read"
    if not set_up:
        return NOTE, "not signed in yet"
    return PROBLEM, "not signed in. Your Hyperwheel admin can't reach this Pi remotely until it is."


def _write_state(folder: Path, state: dict) -> None:
    tmp = folder / f".{STATE_FILE}.new"
    tmp.write_text(json.dumps(state) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, folder / STATE_FILE)


# ---- the watcher -----------------------------------------------------------------------------------------


def run_quietly(cmd: Sequence[str], timeout: float = 60.0) -> tuple[int, str]:
    """Run a command and return its exit code and output, which callers must not log."""
    try:
        out = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return 124, ""
    return out.returncode, out.stdout + out.stderr


class LinkSession:
    """``rpi-connect signin``, which prints a sign-in link and waits for someone to open it."""

    def __init__(self, cmd: Sequence[str]):
        self.link: str | None = None
        self.process = subprocess.Popen(list(cmd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.process.stdout:
            found = SIGN_IN_LINK.search(line)
            if found:
                self.link = found.group(0)

    def running(self) -> bool:
        return self.process.poll() is None

    def stop(self) -> None:
        self.process.terminate()


@dataclass
class Watcher:
    layout: Layout = field(default_factory=Layout)
    run: Callable[..., tuple[int, str]] = run_quietly
    spawn: Callable[[Sequence[str]], LinkSession] = LinkSession
    clock: Callable[[], float] = time.time
    uid_of: Callable[[str], int | None] = None  # type: ignore[assignment]
    gid_of: Callable[[str], int | None] = None  # type: ignore[assignment]
    session: LinkSession | None = None
    next_check: float = 0.0
    last: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.uid_of is None:
            self.uid_of = _uid_of
        if self.gid_of is None:
            self.gid_of = _gid_of

    @property
    def folder(self) -> Path:
        return self.layout.p(FOLDER)

    def account(self) -> str | None:
        """The account Connect runs under: one that has it enabled, or datamanager on a new Pi."""
        home = self.layout.p("/home")
        enabled = sorted(p.name for p in home.iterdir()
                         if (p / ENABLED_LINK).exists() and self.uid_of(p.name) is not None) if home.is_dir() else []
        if DATA_MANAGER in enabled:
            return DATA_MANAGER
        if enabled:
            return enabled[0]
        return DATA_MANAGER if self.uid_of(DATA_MANAGER) is not None else None

    def _as(self, account: str, *words: str) -> list[str]:
        """rpi-connect run as the account. setpriv, not runuser: runuser opens a login session, and each one
        puts two lines in the journal, every few minutes for good."""
        uid, gid = self.uid_of(account), self.gid_of(account)
        return ["setpriv", f"--reuid={uid}", f"--regid={gid}", "--init-groups",
                "env", "-i", f"HOME=/home/{account}", f"USER={account}", f"LOGNAME={account}",
                "PATH=/usr/local/bin:/usr/bin:/bin", f"XDG_RUNTIME_DIR=/run/user/{uid}",
                f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus", "rpi-connect", *words]

    def signed_in(self, account: str) -> bool | None:
        code, out = self.run(self._as(account, "status"))
        if code != 0:
            return None
        for line in out.splitlines():
            if line.startswith("Signed in:"):
                return line.split(":", 1)[1].strip() == "yes"
        return None

    def turn_on(self, account: str) -> None:
        """Connect runs from every boot with nobody logged in."""
        if not self.layout.p(f"/var/lib/systemd/linger/{account}").exists():
            self.run(["loginctl", "enable-linger", account])
            log.info("Connect: lingering turned on for %s.", account)
        if not (self.layout.p(f"/home/{account}") / ENABLED_LINK).exists():
            self.run(self._as(account, "on"))
            log.info("Connect: turned on for %s.", account)

    def stop_link(self) -> None:
        if self.session is not None:
            self.session.stop()
            self.session = None

    def round(self) -> None:
        """One check. Cheap enough to run every few seconds: it does nothing until a check is due."""
        now = self.clock()
        if now < self.next_check:
            return
        account = self.account()
        set_up = is_set_up(self.layout.etc)
        state = {"account": account, "checked_at": now, "signed_in": None, "link": None}
        if account is not None:
            if not set_up:
                self.turn_on(account)
            state["signed_in"] = self.signed_in(account)
        if state["signed_in"] is True:
            self.stop_link()
            self.next_check = now + CHECK_EVERY_SIGNED_IN_S
        else:
            self.next_check = now + CHECK_EVERY_S
            if account is not None and not set_up:
                if self.session is None or not self.session.running():
                    self.session = self.spawn(self._as(account, "signin"))
                state["link"] = self.session.link
        if state["signed_in"] != self.last.get("signed_in"):
            log.info("Connect: %s.", describe(state, now, set_up=set_up)[1])
        self.last = state
        _write_state(self.folder, state)

    def forever(self, sleep: Callable[[float], None] = time.sleep) -> None:
        while True:
            try:
                self.round()
            except OSError as e:
                log.error("Connect: the check failed (%s). It tries again.", e.strerror)
            sleep(TICK_S)


def _uid_of(account: str) -> int | None:
    try:
        return pwd.getpwnam(account).pw_uid
    except KeyError:
        return None


def _gid_of(account: str) -> int | None:
    try:
        return pwd.getpwnam(account).pw_gid
    except KeyError:
        return None


def main(args: list[str]) -> int:
    if args:
        print("Usage: hyperwheel connect-watch")
        return 2
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    Watcher().forever()
    return 0
