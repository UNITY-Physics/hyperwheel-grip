"""Per-project GRIP keys (client ID + client secret).

One JSON file per project, ``/etc/hyperwheel/keys/<project>.json``:
``{"client_id": "...", "client_secret": "..."}``, owned by root with mode 600.

The upload service doesn't run as root. systemd hands it a private copy of
the key files (``LoadCredential=keys:/etc/hyperwheel/keys``), named
``keys_<project>.json`` in ``$CREDENTIALS_DIRECTORY``. The key file is read
again before every token request; because that copy is made when the service
starts, storing a new key restarts the service.

Secrets never go in logs, exceptions or process arguments.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .ids import is_project_name


class KeyProblem(Exception):
    """A project's key can't be used. The message is safe to log."""


class KeyMissing(KeyProblem):
    pass


class KeyUnreadable(KeyProblem):
    pass


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str = field(repr=False)


class KeyStore:
    def __init__(self, directory: Path, prefix: str = "", *, systemd: bool = False):
        self.directory = Path(directory)
        self.prefix = prefix
        self.systemd = systemd  # a copy in systemd's private credentials folder

    @classmethod
    def for_process(cls, keys_dir: Path) -> "KeyStore":
        """The service's copy from systemd when there is one, else the key directory itself."""
        credentials = os.environ.get("CREDENTIALS_DIRECTORY")
        if credentials:
            return cls(Path(credentials), prefix="keys_", systemd=True)
        return cls(keys_dir)

    def path(self, project: str) -> Path:
        if not is_project_name(project):
            raise KeyUnreadable(f"not a valid project name: {project!r}")
        return self.directory / f"{self.prefix}{project}.json"

    def read(self, project: str) -> Credentials:
        path = self.path(project)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise KeyMissing(f"no key stored for project {project}") from None
        except OSError as e:
            raise KeyUnreadable(f"key file for project {project} can't be read ({e.strerror})") from None
        with os.fdopen(fd, "rb") as f:
            if _too_open(os.fstat(f.fileno()).st_mode, self.systemd):
                raise KeyUnreadable(
                    f"key file for project {project} is readable by other users; it must be mode 600"
                )
            raw = f.read()
        try:
            data = json.loads(raw)
            creds = Credentials(client_id=data["client_id"], client_secret=data["client_secret"])
        except (ValueError, KeyError, TypeError):
            raise KeyUnreadable(f"key file for project {project} is not in the expected format") from None
        if not (isinstance(creds.client_id, str) and creds.client_id and isinstance(creds.client_secret, str)
                and creds.client_secret):
            raise KeyUnreadable(f"key file for project {project} has an empty client ID or secret")
        return creds

    def write(self, project: str, creds: Credentials) -> Path:
        """Store a key: written to a temporary file with mode 600, synced, then renamed into place."""
        path = self.path(project)
        tmp = path.with_name(f".{path.name}.tmp")
        body = json.dumps({"client_id": creds.client_id, "client_secret": creds.client_secret}).encode()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        dir_fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return path


def read_secret_json(name: str, fallback: Path) -> dict:
    """A root-only JSON secret other than a project key (the scanner password, Orthanc's login).

    The service reads systemd's copy ($CREDENTIALS_DIRECTORY/<name>); a root command reads ``fallback``.
    """
    credentials = os.environ.get("CREDENTIALS_DIRECTORY")
    path = Path(credentials) / name if credentials else Path(fallback)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise KeyMissing(f"{name}: secret file not found") from None
    except OSError as e:
        raise KeyUnreadable(f"{name}: secret file can't be read ({e.strerror})") from None
    with os.fdopen(fd, "rb") as f:
        if _too_open(os.fstat(f.fileno()).st_mode, bool(credentials)):
            raise KeyUnreadable(f"{name}: secret file is readable by other users; it must be mode 600")
        raw = f.read()
    try:
        data = json.loads(raw)
    except ValueError:
        raise KeyUnreadable(f"{name}: secret file is not in the expected format") from None
    if not isinstance(data, dict):
        raise KeyUnreadable(f"{name}: secret file is not in the expected format")
    return data


def _too_open(mode: int, systemd: bool) -> bool:
    """A secret in /etc/hyperwheel must be mode 600. systemd's copy for the service (LoadCredential)
    may be group-readable: systemd makes it so, and its folder is private to the service."""
    if systemd:
        return bool(mode & (stat.S_IRWXO | stat.S_IWGRP | stat.S_IXGRP))
    return bool(mode & (stat.S_IRWXG | stat.S_IRWXO))
