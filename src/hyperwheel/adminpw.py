"""The admin password: the same on every Pi, set from a signed hash. Run as root.

The release signer makes the password on their Mac (``tools/release/publish.py
admin-file``) and publishes only its hash, in one signed file that every channel
reads (see ``release``, "the admin password file"). Each Pi:

1. downloads ``admin.json`` and its signature from the ``admin-password``
   release, and checks the signature against its own list of release signers;
2. refuses a file whose serial is lower than the last one it took, so an old
   password can't be brought back by replaying its file;
3. sets that hash for the ``admin`` account with ``chpasswd -e`` (the hash on
   stdin, never on a command line), unless it already has it.

The updater does this at every check, on hold or not, and ``hyperwheel setup``
does it straight after it creates the admin account. Then, if setup was waiting
only for an admin password, the updater finishes setup's last step (see
``sitesetup.finish_waiting_setup``).

The password itself never reaches a Pi. Only the published hash does, which is
safe because the password is generated (``passwords``), not chosen.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .accounts import Accounts
from .installer import Layout
from .release import ADMIN_FILE, ADMIN_SIGNATURE, ADMIN_TAG, ADMIN_USER, ReleaseError, verify_admin

log = logging.getLogger("hyperwheel.admin")


class AdminError(Exception):
    """The admin password file couldn't be used. The message says why (safe to log)."""


def set_hash_with_chpasswd(user: str, hashed: str) -> None:
    out = subprocess.run(["chpasswd", "-e"], input=f"{user}:{hashed}\n", capture_output=True, text=True)
    if out.returncode != 0:
        raise AdminError(f"chpasswd didn't set it (exit {out.returncode})")


def _download(url: str, dest: Path, max_bytes: int) -> None:
    from .updater import UpdateError, download
    try:
        download(url, dest, max_bytes)
    except UpdateError as e:
        raise AdminError(str(e)) from None


@dataclass
class AdminPassword:
    base_url: str
    layout: Layout = field(default_factory=Layout)
    fetch: Callable[[str, Path, int], None] = _download
    set_hash: Callable[[str, str], None] = set_hash_with_chpasswd
    clock: Callable[[], float] = time.time

    @property
    def signers(self) -> Path:
        return self.layout.etc / "release_allowed_signers"

    @property
    def state(self) -> Path:
        return self.layout.p("/var/lib/hyperwheel-update")

    @property
    def record(self) -> Path:
        return self.state / "admin.json"

    def taken_serial(self) -> int:
        try:
            serial = json.loads(self.record.read_text()).get("serial", 0)
        except (OSError, ValueError, AttributeError):
            return 0
        return serial if isinstance(serial, int) else 0

    def check(self) -> str:
        """Take the published admin password if it's new. Returns what happened, in one line. Raises AdminError."""
        folder = self.state / "downloads" / "admin"
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        folder.chmod(0o700)
        tag = quote(ADMIN_TAG)
        for name in (ADMIN_FILE, ADMIN_SIGNATURE):
            self.fetch(f"{self.base_url}/{tag}/{name}", folder / name, 64 * 1024)
        try:
            admin, signer = verify_admin(folder, self.signers)
        except ReleaseError as e:
            raise AdminError(f"the admin password file can't be used: {e}") from None
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        taken = self.taken_serial()
        if admin.serial < taken:
            raise AdminError(f"the published admin password file (version {admin.serial}) is older than the one "
                             f"this Pi took (version {taken}), so it isn't used")
        accounts = Accounts.read(self.layout)
        if not accounts.exists(ADMIN_USER):
            return "admin password: this Pi has no admin account"
        if accounts.hashes.get(ADMIN_USER) == admin.hash:
            self._remember(admin.serial)
            return f"admin password: up to date (version {admin.serial})"
        self.set_hash(ADMIN_USER, admin.hash)
        self._remember(admin.serial)
        log.info("Admin password set from version %d of the admin password file (signed by %s).", admin.serial,
                 signer)
        return f"admin password: set (version {admin.serial}, signed by {signer})"

    def _remember(self, serial: int) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        tmp = self.record.with_name(f".{self.record.name}.new")
        tmp.write_text(json.dumps({"serial": serial, "at": datetime.fromtimestamp(self.clock())
                                   .isoformat(timespec="seconds")}) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.record)
