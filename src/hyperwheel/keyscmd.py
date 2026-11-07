"""``hyperwheel keys list`` and ``hyperwheel keys set``: the data managers' key commands.

Both run as root: through sudo from the data-manager account, which may run only
these and the status commands (``/etc/sudoers.d/hyperwheel``, written by the
installer), or by an administrator.

``set`` asks which project, then its client ID and secret. It asks GRIP for a
token with them **before** saving anything. A key GRIP refuses, or one that can't
be checked because GRIP can't be reached, is never saved, and the key already
stored stays as it is. A key that works is saved root-only (mode 600), and the
upload service is restarted so it picks the key up (it keeps its own copy, made
when it starts).

The secret is read hidden, and is never printed, logged or put in a process
argument. ``list`` shows only the last characters of each client ID.
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

import requests

from .config import Project, Routing, SiteConfig
from .grip.errors import GripError, KeyRejected
from .grip.http import TokenManager
from .keys import Credentials, KeyMissing, KeyProblem, KeyStore
from .state import Store

SERVICE = "hyperwheel.service"
STATUS_TEXT = {
    "working": "working",
    "rejected": "refused by GRIP (wrong, revoked or disabled): uploads for this project are paused",
    "missing": "no key stored: uploads for this project are paused",
    "unreadable": "the key file can't be read: uploads for this project are paused",
}
_CLIENT_ID = re.compile(r"[\x21-\x7e]{1,256}")  # printable, no spaces
_SECRET = re.compile(r"[\x21-\x7e]{1,4096}")


@dataclass
class Console:
    ask: Callable[[str], str] = input
    ask_hidden: Callable[[str], str] = getpass.getpass
    say: Callable[[str], None] = print


class Cancelled(Exception):
    pass


def check_with_grip(project: Project, creds: Credentials, timeout_s: float) -> None:
    """Ask GRIP for a token with this key. Raises KeyRejected if GRIP refuses it, GripError if it can't tell."""
    with requests.Session() as session:
        try:
            TokenManager(project.token_url, lambda: creds, session, timeout_s=timeout_s).get()
        except requests.RequestException as e:
            raise GripError(f"no answer ({type(e).__name__})") from None


def restart_service() -> str:
    """Restart the upload service if it's running. Returns what happened, in plain words."""
    active = subprocess.run(["systemctl", "is-active", "--quiet", SERVICE], capture_output=True).returncode == 0
    if not active:
        return "Hyperwheel isn't running just now; it will use the new key when it starts."
    done = subprocess.run(["systemctl", "restart", SERVICE], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"systemctl restart ended with exit {done.returncode}")
    return "Hyperwheel was restarted to use it. Uploads carry on where they stopped."


def _is_root() -> bool:
    return os.geteuid() == 0


def _ending(client_id: str) -> str:
    return f"client ID ending {client_id[-4:]}" if len(client_id) > 8 else "client ID set"


def _status(store: Store | None, project: str) -> str:
    status = store.key_status(project) if store is not None else None
    if status is None:
        return "not checked yet"
    return STATUS_TEXT.get(status[0], status[0])


def _open_store(site: SiteConfig) -> Store | None:
    try:
        return Store(site.state_db, readonly=True)
    except Exception:  # noqa: BLE001 - only the status is missing then
        return None


def describe(routing: Routing, keys: KeyStore, store: Store | None) -> list[tuple[str, str]]:
    """(project, what is known about its key) for every project on this Pi. Never shows a secret."""
    out = []
    for name in routing.projects:
        try:
            creds = keys.read(name)
        except KeyMissing:
            out.append((name, "no key stored"))
            continue
        except KeyProblem as e:
            out.append((name, f"the key file can't be used ({e})"))
            continue
        out.append((name, f"key stored ({_ending(creds.client_id)}), {_status(store, name)}"))
    return out


def list_keys(site: SiteConfig, routing: Routing, console: Console | None = None, *,
              keys: KeyStore | None = None, is_root: Callable[[], bool] = _is_root) -> int:
    c = console or Console()
    if not is_root():
        c.say("This needs administrator rights. Run: sudo hyperwheel keys list")
        return 2
    if not routing.projects:
        c.say("There are no projects in this Pi's routing settings, so no keys are used.")
        return 0
    store = _open_store(site)
    try:
        c.say("GRIP keys on this Pi:")
        for name, text in describe(routing, keys or KeyStore(site.keys_dir), store):
            c.say(f"  {name}: {text}")
    finally:
        if store is not None:
            store.close()
    return 0


def _pick_project(routing: Routing, lines: dict[str, str], c: Console) -> Project:
    names = list(routing.projects)
    c.say("Projects on this Pi:")
    for i, name in enumerate(names, 1):
        c.say(f"  {i}. {name}  ({lines[name]})")
    for _ in range(3):
        answer = c.ask("Which project? Type its number (or press Enter to stop): ").strip()
        if not answer:
            raise Cancelled()
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            return routing.projects[names[int(answer) - 1]]
        if answer in routing.projects:
            return routing.projects[answer]
        c.say(f"  There's no project {answer!r}. Type a number from 1 to {len(names)}.")
    raise Cancelled()


def _ask_client_id(c: Console) -> str:
    for _ in range(3):
        value = c.ask("Client ID: ").strip()
        if not value:
            raise Cancelled()
        if _CLIENT_ID.fullmatch(value):
            return value
        c.say("  That doesn't look like a client ID (it has a space or an unusual character, or is too long). "
              "Paste only the client ID.")
    raise Cancelled()


def _ask_secret(c: Console) -> str:
    for _ in range(3):
        value = c.ask_hidden("Client secret (it stays hidden as you type or paste it): ").strip()
        if not value:
            raise Cancelled()
        if _SECRET.fullmatch(value):
            return value
        c.say("  That doesn't look like a secret (it has a space, a line break or an unusual character, or is "
              "too long). Paste only the secret.")
    raise Cancelled()


def set_key(site: SiteConfig, routing: Routing, console: Console | None = None, *,
            keys: KeyStore | None = None, check: Callable[[Project, Credentials, float], None] = check_with_grip,
            restart: Callable[[], str] = restart_service, is_root: Callable[[], bool] = _is_root) -> int:
    c = console or Console()
    if not is_root():
        c.say("This needs administrator rights. Run: sudo hyperwheel keys set")
        return 2
    if not routing.projects:
        c.say("There are no projects in this Pi's routing settings, so there's no key to set. "
              "Ask the maintainer to add the project first.")
        return 1
    keys = keys or KeyStore(site.keys_dir)
    c.say("Set the GRIP key for one project. You need the project's client ID and secret from GRIP.")
    c.say("")
    store = _open_store(site)
    try:
        lines = dict(describe(routing, keys, store))
    finally:
        if store is not None:
            store.close()
    try:
        project = _pick_project(routing, lines, c)
        c.say(f"Project: {project.name}")
        creds = Credentials(_ask_client_id(c), _ask_secret(c))
    except (Cancelled, EOFError, KeyboardInterrupt):
        c.say("")
        c.say("Stopped. Nothing was saved; the keys on this Pi are as they were.")
        return 1

    try:
        old = keys.read(project.name)
    except KeyProblem:
        old = None
    kept = (f"The key already stored for {project.name} is unchanged." if old is not None
            else f"{project.name} still has no key.")

    c.say("Checking the key with GRIP ...")
    try:
        check(project, creds, site.upload.timeout_s)
    except KeyRejected as e:
        c.say(f"GRIP refused this key (HTTP {e.status}). Nothing was saved. {kept}")
        c.say("Check that the client ID and secret are for this project and were copied in full, then try again.")
        return 1
    except (GripError, KeyProblem) as e:
        c.say(f"GRIP couldn't be reached to check the key ({e}). Nothing was saved. {kept}")
        c.say("Check the Pi's connection with: sudo hyperwheel doctor. Then try again.")
        return 1
    c.say(f"GRIP accepted the key for {project.name}.")

    if old == creds:
        c.say("It's the key already stored, so nothing needed to change.")
        return 0
    try:
        keys.write(project.name, creds)
    except OSError as e:
        c.say(f"The key works, but it couldn't be saved ({e.strerror}). {kept}")
        return 1
    c.say(f"Saved for {project.name}.")
    try:
        c.say(restart())
    except Exception as e:  # noqa: BLE001 - the key is saved; say so plainly
        c.say(f"Hyperwheel couldn't be restarted ({e}), so it still uses the old key until it next starts. "
              "Ask the maintainer.")
        return 1
    c.say("In a minute, check it shows as working with: sudo hyperwheel keys list")
    return 0


def main(site: SiteConfig, routing: Routing, args: list[str]) -> int:
    if args == ["list"]:
        return list_keys(site, routing)
    if args == ["set"]:
        return set_key(site, routing)
    print("Usage: hyperwheel keys list | hyperwheel keys set")
    return 2

