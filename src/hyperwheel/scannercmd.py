"""``hyperwheel scanner password``: set the scanner's raw-data password on this Pi.

The scanner's raw-data password changes with the scanner's software (from
version 12 every release has a new one), so after a scanner upgrade the Pi's
login is refused and raw data stays on the scanner until the new password is
set. DICOM keeps uploading meanwhile. Data managers and the admin run this; it
needs root (the data manager types just the command, see ``cli``) and asks for
the account's own password.

The new password is tried on the scanner **before** it is saved: an SSH login,
then a read-only listing of the raw-data folder. A password the scanner
refuses, or one that can't be tried because the scanner doesn't answer, is
never saved, and the stored one stays as it is. One that works is saved
root-only (mode 600), replacing the file in one step, and the Hyperwheel
service is restarted so it picks it up (it keeps its own copy, made when it
starts).

The password is read hidden, and is never printed, logged or put in a process
argument. This command doesn't record the scanner's SSH key (only the service
writes the state database); the service records it at its next fetch.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path

from . import reach
from .config import SiteConfig
from .keys import KeyMissing, KeyProblem, read_secret_json
from .keyscmd import Console, restart_service
from .rawpull import LoginRefused, ScannerError, SshScanner

PASSWORD_FILE = Path("/etc/hyperwheel/scanner.json")
MAX_LENGTH = 1024
ENTITLEMENT = ("If you're sure the password is right, the scanner may be missing the RRDF entitlement. "
               "Tell your Hyperwheel admin.")


class Cancelled(Exception):
    pass


def try_on_scanner(site: SiteConfig, password: str) -> int:
    """Log in with this password and list the raw-data folder. Returns the number of folders.

    Raises LoginRefused if the scanner refuses the login, ScannerError if it can't be tried.
    """
    s = site.scanner
    # Any scanner key is accepted here and none is recorded: the service records it (hostkeys.py).
    ssh = SshScanner(s.host, s.port, s.username, lambda: password, lambda host, key_type, key: None, s.rrdf_dir,
                     s.timeout_s)
    try:
        return len(ssh.list_folders())
    finally:
        ssh.close()


def why_unreachable(site: SiteConfig) -> str:
    state = reach.scanner_state(site)
    if state == reach.SCANNER_NO_LINK:
        return "the scanner cable has no link: check the scanner is on and the cable is plugged in at both ends"
    if state == reach.SCANNER_OK:
        return "the scanner answers, but the SSH connection to it failed"
    return "the scanner doesn't answer: check it is on and has finished starting"


def save(path: Path, password: str) -> None:
    """Write ``{"password": ...}`` root-only, replacing the file in one step (other fields kept)."""
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["password"] = password
    tmp = path.with_name(f".{path.name}.new")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _stored(path: Path) -> str | None:
    try:
        value = read_secret_json("scanner", path).get("password")
    except (KeyMissing, KeyProblem, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def _ask(c: Console) -> str:
    for _ in range(3):
        value = c.ask_hidden("New raw-data password (it stays hidden as you type or paste it): ")
        if not value.strip():
            raise Cancelled()
        if value != value.strip() or len(value) > MAX_LENGTH or not value.isprintable():
            c.say("  That has a space at the start or end, a line break or an unusual character. "
                  "Paste only the password.")
            continue
        return value
    raise Cancelled()


def set_password(site: SiteConfig, console: Console | None = None, *, path: Path = PASSWORD_FILE,
                 test: Callable[[SiteConfig, str], int] = try_on_scanner,
                 unreachable: Callable[[SiteConfig], str] = why_unreachable,
                 restart: Callable[[], str] = lambda: restart_service("password"),
                 is_root: Callable[[], bool] = lambda: os.geteuid() == 0) -> int:
    c = console or Console()
    if not is_root():
        c.say("'hyperwheel scanner password' needs administrator rights. Run it as root.")
        return 2
    s = site.scanner
    if not s.host:
        c.say("This Pi has no scanner address in its site settings yet. Ask your Hyperwheel admin.")
        return 1
    c.say("Set the scanner's raw-data password on this Pi. It changes when the scanner's software is upgraded; "
          "Your Hyperwheel admin can tell you the new one.")
    c.say(f"Scanner: {s.host}, login {s.username}")
    c.say("")
    try:
        password = _ask(c)
    except (Cancelled, EOFError, KeyboardInterrupt):
        c.say("")
        c.say("Stopped. Nothing was saved; the password on this Pi is as it was.")
        return 1

    old = _stored(path)
    kept = "The password already on this Pi is unchanged." if old is not None else "This Pi still has no password."
    c.say("Trying it on the scanner ...")
    # paramiko logs each login at INFO; keep this screen to what the person needs.
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    try:
        folders = test(site, password)
    except LoginRefused:
        c.say(f"The scanner refused this password. Nothing was saved. {kept}")
        c.say("Check the password and try again. " + ENTITLEMENT)
        return 1
    except (ScannerError, OSError):
        c.say(f"The password couldn't be tried: {unreachable(site)}. Nothing was saved. {kept}")
        c.say("Try again once the scanner is reachable. To check the connection, type: hyperwheel doctor")
        return 1
    c.say(f"The scanner accepted it ({folders} raw-data folder{'s' if folders != 1 else ''} found).")

    if old == password:
        c.say("It's the password already on this Pi, so nothing needed to change.")
        return 0
    try:
        save(path, password)
    except OSError as e:
        c.say(f"The password works, but it couldn't be saved ({e.strerror}). {kept}")
        return 1
    c.say("Saved.")
    try:
        c.say(restart())
    except Exception as e:  # noqa: BLE001 - the password is saved; say so plainly
        c.say(f"Hyperwheel couldn't be restarted ({e}), so it uses the new password only when it next starts. "
              "Ask your Hyperwheel admin.")
        return 1
    c.say("Raw data waiting on the scanner is fetched within a minute. To check, type: hyperwheel status")
    return 0


def main(site: SiteConfig, args: list[str]) -> int:
    if args == ["password"]:
        return set_password(site)
    print("Usage: hyperwheel scanner password")
    return 2
