"""``hyperwheel datasets``: the data managers' commands for this Pi's GRIP datasets.

- ``list``: each dataset, its scanner logins, and whether its key works.
- ``add``: a new dataset. It asks for the client ID (the dataset's name on this
  Pi, which GRIP gives with the key), the scanner logins and the secret.
- ``change``: a dataset's logins, or a new key for it.
- ``remove``: takes a dataset and its key off this Pi. Scans from its logins
  are then deleted on the Pi, never uploaded. Files still waiting to upload to
  it are deleted too, but only once the person removing it has confirmed that.

All four run as root. A data manager types just ``hyperwheel datasets add``:
Hyperwheel runs itself again through sudo (see ``cli``), and the sudo rule
(``/etc/sudoers.d/hyperwheel``, written by the installer) lets the data-manager
account run ``list`` without a password and the other three with its own.

A scanner login belongs to at most one dataset. GRIP's address is the site's
(``[grip]`` in ``site.toml``, the release's default unless the site file gave
one), so it's never asked for. Every key is checked with GRIP **before**
anything is saved: a key GRIP refuses, or one that can't be checked because
GRIP can't be reached, is never saved. The routing file is checked, then
replaced in one step. After a change, Hyperwheel and the Upload Monitor restart
so they use it (the upload service keeps its own copy of the keys, made when it
starts).

The secret is read hidden, and is never printed, logged or put in a process
argument. ``list`` shows only the last characters of a client ID it didn't name
the dataset after.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .config import CLIENT_ID, ConfigError, Project, Routing, SiteConfig, load_routing
from .grip.errors import GripError, KeyRejected
from .grip.http import TokenManager
from .ids import is_login, is_project_name, is_uid, login_from_operators_name
from .keys import Credentials, KeyMissing, KeyProblem, KeyStore
from .state import PENDING_FILE_STATES, Store

SERVICE = "hyperwheel.service"
DASHBOARD = "hyperwheel-dashboard.service"
ROUTING = Path("/etc/hyperwheel/routing.toml")
STATUS_TEXT = {
    "working": "working",
    "rejected": "refused by GRIP (wrong, revoked or disabled). Uploads for this dataset are paused",
    "missing": "no key stored. Uploads for this dataset are paused",
    "unreadable": "the key file can't be read. Uploads for this dataset are paused",
}
_SECRET = re.compile(r"[\x21-\x7e]{1,4096}")
# Studies of a removed dataset that are still on their way to GRIP: dropped once the person confirms.
_DROPPABLE = ("checking", "held", "ready", "uploading")


@dataclass
class Console:
    ask: Callable[[str], str] = input
    ask_hidden: Callable[[str], str] = getpass.getpass
    say: Callable[[str], None] = print


class Cancelled(Exception):
    pass


class Stop(Exception):
    """Stops the command with this message. Nothing has been changed."""


def check_with_grip(project: Project, creds: Credentials, timeout_s: float) -> None:
    """Ask GRIP for a token with this key. Raises KeyRejected if GRIP refuses it, GripError if it can't tell."""
    with requests.Session() as session:
        try:
            TokenManager(project.token_url, lambda: creds, session, timeout_s=timeout_s).get()
        except requests.RequestException as e:
            raise GripError(f"no answer ({type(e).__name__})") from None


def _active(unit: str) -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", unit], capture_output=True).returncode == 0


def restart_service(what: str = "key") -> str:
    """Restart the upload service if it's running. Returns what happened, in plain words."""
    if not _active(SERVICE):
        return f"Hyperwheel isn't running just now. It will use the new {what} when it starts."
    done = subprocess.run(["systemctl", "restart", SERVICE], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"systemctl restart ended with exit {done.returncode}")
    return "Hyperwheel was restarted to use it. Uploads carry on where they stopped."


def restart_both() -> str:
    """Restart Hyperwheel and the Upload Monitor, those that are running, so both use the new datasets."""
    if not _active(SERVICE):
        subprocess.run(["systemctl", "try-restart", DASHBOARD], capture_output=True)
        return "Hyperwheel isn't running just now. It will use the change when it starts."
    done = subprocess.run(["systemctl", "restart", SERVICE], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"systemctl restart ended with exit {done.returncode}")
    subprocess.run(["systemctl", "try-restart", DASHBOARD], capture_output=True)
    return "Hyperwheel was restarted to use it. Uploads carry on where they stopped."


def _is_root() -> bool:
    return os.geteuid() == 0


def _ending(client_id: str) -> str:
    return f"client ID ending {client_id[-4:]}" if len(client_id) > 8 else "client ID set"


def safe_name(client_id: str, taken: set[str]) -> str:
    """The dataset's name on this Pi (its folder and key file) from its client ID: the client ID itself when it
    can be one, else one made from it. Never one already taken (compared ignoring case)."""
    taken = {t.lower() for t in taken}
    if is_project_name(client_id) and client_id.lower() not in taken:
        return client_id
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", client_id).strip("-_")[:56].rstrip("-_") or "dataset"
    if not base[0].isalnum():
        base = "d" + base
    name, n = base, 2
    while name.lower() in taken:
        name, n = f"{base}-{n}", n + 1
    return name


@dataclass
class Datasets:
    """One run of a ``hyperwheel datasets`` command, with everything it touches passed in (tests use fakes)."""

    site: SiteConfig
    routing_path: Path = ROUTING
    console: Console = field(default_factory=Console)
    keys: KeyStore | None = None
    check: Callable[[Project, Credentials, float], None] = check_with_grip
    restart: Callable[[], str] = restart_both
    is_root: Callable[[], bool] = _is_root
    service_active: Callable[[], bool] = lambda: _active(SERVICE)
    service_stop: Callable[[], None] = lambda: subprocess.run(["systemctl", "stop", SERVICE], check=True,
                                                               capture_output=True)
    service_start: Callable[[], None] = lambda: subprocess.run(["systemctl", "start", SERVICE], check=True,
                                                                capture_output=True)

    def __post_init__(self):
        self.keys = self.keys or KeyStore(self.site.keys_dir)
        self.c = self.console

    # ---- the routing file ----

    def routing(self) -> Routing:
        try:
            return load_routing(self.routing_path, test_mode=self.site.test_mode, grip=self.site.grip)
        except ConfigError as e:
            raise Stop(f"This Pi's dataset settings can't be read ({e}). Ask your Hyperwheel admin.") from None

    def _raw(self) -> dict:
        with open(self.routing_path, "rb") as f:
            data = tomllib.load(f)
        return dict(data.get("projects", {}))

    def _write(self, projects: dict) -> None:
        """Replace the routing file with these datasets: checked first, then swapped in one step, flushed to disk."""
        from .sitesetup import render_routing_toml

        text = render_routing_toml(projects)
        tmp = self.routing_path.with_name(f".{self.routing_path.name}.new")
        tmp.unlink(missing_ok=True)
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, "w") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o644)
            load_routing(tmp, test_mode=self.site.test_mode, grip=self.site.grip)
            os.replace(tmp, self.routing_path)
            folder = os.open(self.routing_path.parent, os.O_RDONLY)
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
        except (ConfigError, OSError) as e:
            tmp.unlink(missing_ok=True)
            raise Stop(f"The dataset settings couldn't be saved ({str(e).replace(str(tmp), 'the new file')}). "
                       "They are as they were.") from None

    # ---- what's known ----

    def _store(self, readonly: bool = True) -> Store | None:
        if not self.site.state_db.exists():
            return None
        try:
            return Store(self.site.state_db, readonly=readonly)
        except Exception:  # noqa: BLE001 - only the status is missing then
            if not readonly:
                raise
            return None

    def describe(self, routing: Routing) -> dict[str, str]:
        """What is known about each dataset's key. Never shows a secret."""
        store = self._store()
        out = {}
        try:
            for name, project in routing.projects.items():
                try:
                    creds = self.keys.read(name)
                except KeyMissing:
                    out[name] = "no key stored. Uploads for this dataset are paused"
                    continue
                except KeyProblem as e:
                    out[name] = f"the key file can't be used ({e})"
                    continue
                status = store.key_status(name) if store is not None else None
                text = "not checked yet" if status is None else STATUS_TEXT.get(status[0], status[0])
                if creds.client_id != project.label:
                    text += f" ({_ending(creds.client_id)})"
                out[name] = text
        finally:
            if store is not None:
                store.close()
        return out

    # ---- asking ----

    def _pick(self, routing: Routing, verb: str) -> Project:
        names = list(routing.projects)
        self.c.say("GRIP datasets on this Pi:")
        for i, name in enumerate(names, 1):
            self.c.say(f"  {i}. {routing.projects[name].label}")
        for _ in range(3):
            answer = self.c.ask(f"Which dataset do you want to {verb}? Type its number (or press Enter to stop): ")
            answer = answer.strip()
            if not answer:
                raise Cancelled()
            if answer.isdigit() and 1 <= int(answer) <= len(names):
                return routing.projects[names[int(answer) - 1]]
            for project in routing.projects.values():
                if answer in (project.label, project.name):
                    return project
            self.c.say(f"  There's no dataset {answer!r}. Type a number from 1 to {len(names)}.")
        raise Cancelled()

    def _ask_client_id(self) -> str:
        for _ in range(3):
            value = self.c.ask("Client ID: ").strip()
            if not value:
                raise Cancelled()
            if CLIENT_ID.fullmatch(value):
                return value
            self.c.say("  That doesn't look like a client ID (it has a space or an unusual character, or is too "
                       "long). Paste only the client ID.")
        raise Cancelled()

    def _ask_secret(self) -> str:
        for _ in range(3):
            value = self.c.ask_hidden("Client secret (it stays hidden as you type or paste it): ").strip()
            if not value:
                raise Cancelled()
            if _SECRET.fullmatch(value):
                return value
            self.c.say("  That doesn't look like a secret (it has a space, a line break or an unusual character, "
                       "or is too long). Paste only the secret.")
        raise Cancelled()

    def _login(self, text: str, routing: Routing, own: str | None) -> str:
        """A typed login, checked: lower case, the part after the last '/', and not another dataset's."""
        typed = text.strip()
        login = login_from_operators_name(typed) if "/" in typed else typed.lower()
        if not login or not is_login(login):
            raise Stop(f"{typed!r} can't be a scanner login. A login uses lower case letters, digits, '.', "
                       "'-' and '_'. Nothing was changed.")
        other = routing.project_for_login(login)
        if other is not None and other.name != own:
            raise Stop(f"The login {login} is already used by the dataset {other.label}. A login can belong to "
                       "only one dataset. Nothing was changed.")
        return login

    def _ask_logins(self, routing: Routing, own: str | None = None) -> list[str]:
        self.c.say("The scanner login is the part of the operator name after the last /, such as study_one in "
                   "site_a/study_one.")
        first = self.c.ask("Scanner login: ").strip()
        if not first:
            raise Cancelled()
        logins = [self._login(first, routing, own)]
        while True:
            more = self.c.ask("Another login for this dataset? Type it, or press Enter if there are no more: ")
            if not more.strip():
                return logins
            login = self._login(more, routing, own)
            if login not in logins:
                logins.append(login)

    def _check(self, project: Project, creds: Credentials, kept: str) -> None:
        self.c.say("Checking the key with GRIP ...")
        try:
            self.check(project, creds, self.site.upload.timeout_s)
        except KeyRejected as e:
            raise Stop(f"GRIP refused this key (HTTP {e.status}). Nothing was saved. {kept}\nCheck that the client "
                       "ID and secret were copied in full, then try again.") from None
        except (GripError, KeyProblem) as e:
            raise Stop(f"GRIP couldn't be reached to check the key ({e}). Nothing was saved. {kept}\nCheck the "
                       "Pi's connection with: hyperwheel doctor\nThen try again.") from None
        self.c.say("GRIP accepted the key.")

    def _restarted(self) -> int:
        try:
            self.c.say(self.restart())
        except Exception as e:  # noqa: BLE001 - the change is saved, say so plainly
            self.c.say(f"Hyperwheel couldn't be restarted ({e}), so it uses the change only when it next starts. "
                       "Ask your Hyperwheel admin.")
            return 1
        return 0

    # ---- the commands ----

    def list(self) -> int:
        routing = self.routing()
        if not routing.projects:
            self.c.say("There are no GRIP datasets on this Pi yet. To add one, type: hyperwheel datasets add")
            return 0
        known = self.describe(routing)
        self.c.say("GRIP datasets on this Pi:")
        for name, project in routing.projects.items():
            self.c.say(f"  {project.label}")
            self.c.say(f"    Scanner login{'s' if len(project.logins) != 1 else ''}: {', '.join(project.logins)}")
            self.c.say(f"    Key: {known[name]}")
        self.c.say("Scans from any other login are deleted on this Pi, never uploaded.")
        return 0

    def add(self) -> int:
        routing = self.routing()
        self.c.say("Add a GRIP dataset. You need its client ID and secret from GRIP, and the scanner login whose "
                   "scans go to it.")
        client_id = self._ask_client_id()
        for project in routing.projects.values():
            if client_id in (project.label, project.client_id):
                raise Stop(f"{client_id} is already a dataset on this Pi. To change its logins or key, type: "
                           "hyperwheel datasets change")
        logins = self._ask_logins(routing)
        creds = Credentials(client_id, self._ask_secret())
        name = safe_name(client_id, set(routing.projects))
        project = Project(name, tuple(logins), self.site.grip.api_base_url, self.site.grip.token_url,
                          client_id=client_id)
        self._check(project, creds, "The dataset wasn't added.")
        projects = self._raw()
        projects[name] = {"client_id": client_id, "logins": logins}
        try:
            self.keys.write(name, creds)
        except OSError as e:
            raise Stop(f"The key works, but it couldn't be saved ({e.strerror}). The dataset wasn't added.") from None
        try:
            self._write(projects)
        except Stop:
            self.keys.path(name).unlink(missing_ok=True)
            raise
        self.c.say(f"Added {client_id}, for scans from {', '.join(logins)}.")
        code = self._restarted()
        self.c.say("In a minute, check its key shows as working with: hyperwheel datasets list")
        return code

    def change(self) -> int:
        routing = self.routing()
        if not routing.projects:
            raise Stop("There are no GRIP datasets on this Pi yet. To add one, type: hyperwheel datasets add")
        project = self._pick(routing, "change")
        self.c.say(f"Dataset: {project.label}. Scanner logins now: {', '.join(project.logins)}")
        self.c.say("  1. Change its scanner logins")
        self.c.say("  2. Replace its key")
        answer = self.c.ask("Type 1 or 2 (or press Enter to stop): ").strip()
        if answer == "1":
            return self._change_logins(routing, project)
        if answer == "2":
            return self._replace_key(project)
        raise Cancelled()

    def _change_logins(self, routing: Routing, project: Project) -> int:
        self.c.say("Type all of its logins, the ones it keeps and any new ones. Logins you leave out stop going "
                   "to it, and their scans are then deleted on this Pi, never uploaded.")
        logins = self._ask_logins(routing, own=project.name)
        if logins == list(project.logins):
            self.c.say("Those are its logins already. Nothing was changed.")
            return 0
        projects = self._raw()
        projects[project.name] = {**projects[project.name], "logins": logins}
        self._write(projects)
        self.c.say(f"{project.label} now takes scans from {', '.join(logins)}. Sessions already on this Pi stay "
                   "with the dataset they went to.")
        return self._restarted()

    def _replace_key(self, project: Project) -> int:
        self.c.say(f"Replace the key for {project.label}. You need its client ID and secret from GRIP.")
        client_id = self._ask_client_id()
        if project.client_id and client_id != project.client_id:
            raise Stop(f"That's another client ID. Each client ID is its own dataset, and this one is "
                       f"{project.client_id}. To add {client_id} as a new dataset, type: hyperwheel datasets add")
        creds = Credentials(client_id, self._ask_secret())
        try:
            old = self.keys.read(project.name)
        except KeyProblem:
            old = None
        kept = "The key already stored is unchanged." if old is not None else "It still has no key."
        self._check(project, creds, kept)
        if old == creds and project.client_id:
            self.c.say("It's the key already stored, so nothing needed to change.")
            return 0
        try:
            self.keys.write(project.name, creds)
        except OSError as e:
            raise Stop(f"The key works, but it couldn't be saved ({e.strerror}). {kept}") from None
        if not project.client_id:  # a dataset from before client IDs were its name: it's named after it now
            projects = self._raw()
            projects[project.name] = {"client_id": client_id, **projects[project.name]}
            self._write(projects)
        self.c.say(f"Saved the new key for {client_id}.")
        code = self._restarted()
        self.c.say("In a minute, check it shows as working with: hyperwheel datasets list")
        return code

    def remove(self) -> int:
        routing = self.routing()
        if not routing.projects:
            raise Stop("There are no GRIP datasets on this Pi, so there's none to remove.")
        project = self._pick(routing, "remove")
        label, logins = project.label, ", ".join(project.logins)
        waiting_studies, waiting_files = self._waiting(project.name)
        self.c.say(f"Removing {label} deletes its GRIP key from this Pi. Scans from {logins} will then be deleted "
                   "on this Pi, not uploaded. What's already on GRIP stays there.")
        if self.c.ask(f"To remove it, type its name ({label}): ").strip() != label:
            raise Cancelled()
        if waiting_files:
            self.c.say(f"{waiting_files} file{'s' if waiting_files != 1 else ''} from {waiting_studies} "
                       f"session{'s' if waiting_studies != 1 else ''} still wait to upload to {label}. If you go "
                       "on, they are deleted on this Pi and never reach GRIP.")
            if self.c.ask("To delete them, type delete them: ").strip().lower() != "delete them":
                raise Cancelled()

        was_running = self.service_active()
        if was_running:
            self.service_stop()  # nothing uploads while its files are taken away
        try:
            projects = self._raw()
            projects.pop(project.name, None)
            self._write(projects)
            dropped = self._drop(project.name)
            self.keys.path(project.name).unlink(missing_ok=True)
        finally:
            if was_running:
                self.service_start()
        self.c.say(f"Removed {label}." + (f" {dropped} waiting session{'s were' if dropped != 1 else ' was'} "
                                          "deleted on this Pi." if dropped else ""))
        code = self._restarted()
        self.c.say("To see the datasets left, type: hyperwheel datasets list")
        return code

    def _waiting(self, name: str) -> tuple[int, int]:
        store = self._store()
        if store is None:
            return 0, 0
        try:
            studies = [s for s in store.studies(states=_DROPPABLE) if s.project == name]
            files = sum(len([f for f in store.files(s.id) if f.state in PENDING_FILE_STATES]) for s in studies)
            return len(studies), files
        finally:
            store.close()

    def _drop(self, name: str) -> int:
        """The removed dataset's studies: those on their way to GRIP are dropped (deleted on the Pi and, by the
        organiser, in Orthanc), and those already uploaded stop being watched for late raw data."""
        if not self.site.state_db.exists():
            return 0
        db_owner = self.site.state_db.stat()
        store = self._store(readonly=False)
        dropped = 0
        try:
            for study in store.studies(states=(*_DROPPABLE, "uploaded")):
                if study.project != name:
                    continue
                if study.state == "uploaded":
                    store.move_study(study.id, "archived", patient_id=None, patient_name=None, patient_sex=None,
                                     patient_birth_date=None)
                    continue
                paths = store.drop_study(study.id)
                for path in paths:
                    Path(path).unlink(missing_ok=True)
                if is_uid(study.study_uid) and is_project_name(name):
                    shutil.rmtree(self.site.staging_dir / name / study.study_uid, ignore_errors=True)
                dropped += 1
            store.forget_project(name)
        finally:
            store.close()
            # The state store belongs to the upload service's account: anything sqlite left beside it must too.
            for extra in (self.site.state_db.with_name(self.site.state_db.name + s) for s in ("-wal", "-shm")):
                if extra.exists() and os.geteuid() == 0:
                    os.chown(extra, db_owner.st_uid, db_owner.st_gid)
        folder = self.site.staging_dir / name
        if is_project_name(name) and folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
        return dropped


USAGE = "Usage: hyperwheel datasets list | add | change | remove"


def main(site: SiteConfig, args: list[str], routing_path: Path = ROUTING, **kw) -> int:
    action = args[0] if len(args) == 1 else None
    run = Datasets(site, routing_path, **kw)
    if action not in ("list", "add", "change", "remove"):
        run.c.say(USAGE)
        return 2
    if not run.is_root():
        run.c.say(f"'hyperwheel datasets {action}' needs administrator rights. Run it as root.")
        return 2
    try:
        return getattr(run, action)()
    except Stop as e:
        for line in str(e).splitlines():
            run.c.say(line)
        return 1
    except (Cancelled, EOFError, KeyboardInterrupt):
        run.c.say("")
        run.c.say("Stopped. Nothing was changed.")
        return 1


__all__ = ["Console", "Datasets", "check_with_grip", "main", "restart_service", "safe_name"]
