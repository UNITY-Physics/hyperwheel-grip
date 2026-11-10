"""Raspberry Pi Connect: keeps a new Pi trying to join the organisation, and reports Connect's state.

``hyperwheel-connect.service`` runs ``hyperwheel connect-watch`` as root. Connect itself belongs to one
account: the one that has it enabled, or, on a new Pi, ``datamanager``.

**Until the Pi is set up** the watcher:

- turns lingering and Connect on for that account, so Connect runs from every boot with nobody logged in;
- signs in with a key the site pasted on the Upload Monitor, if there is one. An organisation key works
  once. Without the internet the key is kept and tried again. Refused repeatedly while the Pi is online,
  it's deleted, and the Monitor asks for a new one;
- otherwise keeps a sign-in link ready for the Monitor, which the site sends to the admin.

**Once the Pi is set up** it only reads the state. Nothing signs the Pi in or out.

It writes the state to a small file that holds no key, which the Upload Monitor and ``hyperwheel doctor``
read. A key is never logged or put on a command line: Connect reads it from a file (``@path``) that's
deleted straight after, and Connect's own output isn't kept, in case it repeats the key.
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

from . import reach
from .installer import DATA_MANAGER, Layout, is_set_up

log = logging.getLogger("hyperwheel.connect")

FOLDER = "/var/lib/hyperwheel/connect"
INBOX = "inbox"
KEY_FILE = "key"
STATE_FILE = "state.json"

API_HOST = "api.connect.raspberrypi.com"
SIGN_IN_LINK = re.compile(r"https://connect\.raspberrypi\.com/\S+")
ORGANISATION_KEY = re.compile(r"rpoak_[A-Za-z0-9_-]{8,200}")
ENABLED_LINK = ".config/systemd/user/default.target.wants/rpi-connect.service"

TICK_S = 5.0
CHECK_EVERY_S = 30.0
CHECK_EVERY_SIGNED_IN_S = 300.0
STALE_S = 900.0
# Refused this many times in a row while online, a key is taken as used or out of date. One refusal could
# be a passing fault on Connect's side, and the site would have to ask for a new key for nothing.
REFUSALS_BEFORE_GIVING_UP = 3

OK, PROBLEM, NOTE = "OK", "PROBLEM", "NOTE"


# ---- the key box on the Upload Monitor -------------------------------------------------------------------


def key_problem(text: str) -> str | None:
    """Why this can't be used as a key, in words for the person pasting it, or None."""
    if text.startswith("rpuak_"):
        return ("That's a personal key, so this Pi would join someone's own account. Use the organisation key your "
                "Hyperwheel admin sent you.")
    if not ORGANISATION_KEY.fullmatch(text):
        return "That doesn't look like a key. Copy the whole key your Hyperwheel admin sent you. It starts with rpoak_."
    return None


def accept_key(text: str, folder: Path = Path(FOLDER)) -> str | None:
    """Keep a pasted key for the watcher. Returns what's wrong with it, or None. Never logs the key."""
    text = text.strip()
    problem = key_problem(text)
    if problem:
        return problem
    inbox = folder / INBOX
    tmp = inbox / f".{KEY_FILE}.new"
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, inbox / KEY_FILE)
    return None


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
    online: Callable[[], bool] = lambda: reach.tcp_answers(API_HOST, 443)
    clock: Callable[[], float] = time.time
    uid_of: Callable[[str], int | None] = None  # type: ignore[assignment]
    gid_of: Callable[[str], int | None] = None  # type: ignore[assignment]
    session: LinkSession | None = None
    refusals: int = 0
    next_check: float = 0.0
    next_key_try: float = 0.0
    last: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.uid_of is None:
            self.uid_of = _uid_of
        if self.gid_of is None:
            self.gid_of = _gid_of

    @property
    def folder(self) -> Path:
        return self.layout.p(FOLDER)

    @property
    def inbox_key(self) -> Path:
        return self.folder / INBOX / KEY_FILE

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

    def try_key(self, account: str) -> tuple[bool, str]:
        """Sign in with the pasted key. Returns whether it worked, and what to tell the site."""
        self.stop_link()
        uid = self.uid_of(account)
        temporary = self.layout.p(f"/run/user/{uid}/hyperwheel-connect-key")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        except OSError:
            return False, "trying"  # the account's session isn't up yet
        try:
            with os.fdopen(fd, "w") as f:
                f.write(self.inbox_key.read_text())
            if os.geteuid() == 0:
                os.chown(temporary, uid, -1)
            code, _ = self.run(self._as(account, "signin", f"--auth-key=@{temporary}"))
        finally:
            temporary.unlink(missing_ok=True)
        if code == 0:
            self.inbox_key.unlink(missing_ok=True)  # it works once
            self.refusals = 0
            return True, "none"
        if not self.online():
            self.refusals = 0
            return False, "trying"
        self.refusals += 1
        if self.refusals < REFUSALS_BEFORE_GIVING_UP:
            return False, "trying"
        self.inbox_key.unlink(missing_ok=True)
        self.refusals = 0
        log.warning("Connect: a pasted key was refused %d times with the internet working, so it was deleted.",
                    REFUSALS_BEFORE_GIVING_UP)
        return False, "refused"

    def round(self) -> None:
        """One check. Cheap enough to run every few seconds: it does nothing until a check is due or a key arrives."""
        now = self.clock()
        key_waiting = self.inbox_key.exists() and now >= self.next_key_try
        if now < self.next_check and not key_waiting:
            return
        account = self.account()
        set_up = is_set_up(self.layout.etc)
        state = {"account": account, "checked_at": now, "signed_in": None, "key": self.last.get("key", "none"),
                 "link": None}
        if account is not None:
            if not set_up:
                self.turn_on(account)
            state["signed_in"] = self.signed_in(account)
        if state["signed_in"] is True:  # any key still waiting is left over
            self.stop_link()
            self.inbox_key.unlink(missing_ok=True)
            state["key"] = "none"
            self.next_check = now + CHECK_EVERY_SIGNED_IN_S
        else:
            self.next_check = now + CHECK_EVERY_S
            if account is not None and not set_up:
                state.update(self._sign_in(account, key_waiting))
        if state["signed_in"] != self.last.get("signed_in"):
            log.info("Connect: %s.", describe(state, now, set_up=set_up)[1])
        self.last = state
        _write_state(self.folder, state)

    def _sign_in(self, account: str, key_waiting: bool) -> dict:
        if key_waiting:
            self.next_key_try = self.clock() + CHECK_EVERY_S
            worked, key = self.try_key(account)
            if worked:
                self.next_check = 0.0  # check straight away
                return {"key": "none", "signed_in": self.signed_in(account)}
            return {"key": key}
        if self.inbox_key.exists():  # tried a moment ago, and it's tried again soon
            return {"key": "trying"}
        if self.session is None or not self.session.running():
            self.session = self.spawn(self._as(account, "signin"))
        return {"link": self.session.link}

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
