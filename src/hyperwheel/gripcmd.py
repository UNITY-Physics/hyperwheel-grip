"""``hyperwheel grip address``: GRIP's address on this Pi (the ``[grip]`` section of ``site.toml``).

GRIP has one fixed address per region, and each site's comes from its site
file. This command changes it on a Pi that is already set up, for example when
GRIP moves a region to a new address. The data-manager account and the admin
can run it, like ``hyperwheel datasets change`` (it asks for the account's own
password).

It shows the address in use, then asks for the new one (the API address GRIP
gives) and the token address (by default the API address followed by
``/get/token``). It asks rather than taking the address after the command, so
the sudo rule still allows only exact words.

The new address is checked **before** it is saved: every dataset that uses the
site's address gets a token from GRIP at the new address with its own key. A
key the new address refuses, or one that can't be checked because GRIP doesn't
answer there, stops the command and nothing is saved. A dataset whose key is
missing can't be checked, and is said so. With no key to check, the new token
address must at least answer the way GRIP does (it refuses a made-up key).

Only the two lines of ``[grip]`` change. The rest of ``site.toml`` (comments
included) stays as it was, and the new file is read back and compared before it
replaces the old one in one step. Datasets with an address of their own keep
it. Hyperwheel and the Upload Monitor then restart to use the new address.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from pathlib import Path

import requests

from .config import ConfigError, GripSettings, Project, SiteConfig, check_url, load_site
from .datasetscmd import ROUTING, Cancelled, Datasets, Stop, _uninterrupted
from .grip.errors import GripError, KeyRejected
from .grip.http import TokenManager
from .keys import Credentials, KeyMissing, KeyProblem

SITE = Path("/etc/hyperwheel/site.toml")
TOKEN_PATH = "/get/token"
# A client ID no GRIP dataset has: a token request with it shows the address answers like GRIP.
NO_SUCH_CLIENT = Credentials("hyperwheel-address-check", "not-a-key")
_HEADER = re.compile(r"\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(#.*)?")
_LINE = re.compile(r"(\s*)(api_base_url|token_url)(\s*=\s*)\S.*")


def answers_like_grip(token_url: str, timeout_s: float) -> bool:
    """Whether this token address refuses a made-up key the way GRIP does (HTTP 400, 401 or 403)."""
    with requests.Session() as session:
        try:
            TokenManager(token_url, lambda: NO_SUCH_CLIENT, session, timeout_s=timeout_s).get()
        except KeyRejected:
            return True
        except (GripError, requests.RequestException):
            return False
    return False  # it gave a token for a key that doesn't exist: not GRIP


def with_address(text: str, grip: GripSettings) -> str:
    """``site.toml`` with the two lines of its ``[grip]`` section set to this address, all else unchanged.
    Raises Stop if the section isn't written the plain way (one ``[grip]`` header with both lines in it)."""
    lines = text.splitlines(keepends=True)
    section, headers, done = None, 0, {}
    out = []
    for line in lines:
        header = _HEADER.fullmatch(line.rstrip("\r\n"))
        if header:
            section = header.group(1)
            headers += section == "grip"
        elif section == "grip":
            m = _LINE.fullmatch(line.rstrip("\r\n"))
            if m:
                key = m.group(2)
                if key in done:
                    raise Stop(_HAND_WRITTEN)
                ending = line[len(line.rstrip("\r\n")):]
                line = f"{m.group(1)}{key}{m.group(3)}{json.dumps(getattr(grip, key))}{ending}"
                done[key] = True
        out.append(line)
    if headers != 1 or set(done) != {"api_base_url", "token_url"}:
        raise Stop(_HAND_WRITTEN)
    return "".join(out)


_HAND_WRITTEN = ("GRIP's address in this Pi's site settings isn't written the usual way, so this command can't "
                 "change it. Nothing was changed. Ask your Hyperwheel admin.")


class GripAddress:
    """One run of ``hyperwheel grip address``. Reading the datasets, the update lock and the restart are the
    datasets command's (``datasets``), so tests pass the same fakes."""

    def __init__(self, site: SiteConfig, datasets: Datasets, site_path: Path = SITE,
                 answers=answers_like_grip):
        self.site, self.ds, self.site_path, self.answers = site, datasets, site_path, answers
        self.c = datasets.console

    def _ask_url(self, prompt: str, default: str | None = None) -> str | None:
        typed = self.c.ask(prompt).strip()
        if not typed:
            return default
        try:
            return check_url(typed, "", test_mode=self.site.test_mode)
        except ConfigError:
            raise Stop(f"{typed} isn't an address starting https://. Nothing was changed.") from None

    def _following(self) -> tuple[list[Project], list[Project]]:
        """The datasets that use the site's address, and those with an address of their own."""
        now = (self.site.grip.api_base_url, self.site.grip.token_url)
        projects = list(self.ds.routing().projects.values())
        return ([p for p in projects if (p.api_base_url, p.token_url) == now],
                [p for p in projects if (p.api_base_url, p.token_url) != now])

    def _check(self, new: GripSettings, following: list[Project]) -> None:
        checked = 0
        for project in following:
            try:
                creds = self.ds.keys.read(project.name)
            except KeyMissing:
                self.c.say(f"{project.label} has no key stored, so it can't be checked.")
                continue
            except KeyProblem:
                self.c.say(f"{project.label}'s key can't be read, so it can't be checked.")
                continue
            self.c.say(f"Checking {project.label}'s key with GRIP at the new address ...")
            try:
                self.ds.check(replace(project, api_base_url=new.api_base_url, token_url=new.token_url), creds,
                              self.site.upload.timeout_s)
            except KeyRejected as e:
                raise Stop(f"GRIP at the new address refused {project.label}'s key (HTTP {e.status}). Nothing was "
                           "changed.\nCheck the address with your Hyperwheel admin.\nIf that key no longer works "
                           "anywhere, replace it first with: hyperwheel datasets change") from None
            except (GripError, KeyProblem) as e:
                raise Stop(f"GRIP couldn't be reached at the new address ({e}). Nothing was changed.\nCheck the "
                           "address, and the Pi's connection with: hyperwheel doctor\nThen try again.") from None
            self.c.say("GRIP accepted it.")
            checked += 1
        if checked:
            return
        self.c.say("Checking that the new address answers like GRIP ...")
        if not self.answers(new.token_url, self.site.upload.timeout_s):
            raise Stop("The new token address doesn't answer like GRIP. Nothing was changed.\nCheck the address "
                       "with your Hyperwheel admin, and the Pi's connection with: hyperwheel doctor") from None
        self.c.say("It answers like GRIP. There was no dataset key to check it with.")

    def _save(self, new: GripSettings) -> None:
        """Replace site.toml with the new address: written beside it, read back and compared, then swapped in
        one step and flushed to disk. Datasets that name the old site address themselves are first written
        without it (the same address, so nothing changes for them), so they follow the new one."""
        path = self.site_path
        try:
            old_text = path.read_text()
            st = path.stat()
            before = load_site(path)
        except (OSError, ConfigError):
            raise Stop("This Pi's site settings can't be read. Nothing was changed. Ask your Hyperwheel "
                       "admin.") from None
        if before.grip != self.site.grip:
            raise Stop("GRIP's address on this Pi changed while you answered. Nothing was changed. Run "
                       "hyperwheel grip address again.")
        text = with_address(old_text, new)
        self.ds._write(self.ds._raw())  # the datasets that follow the site's address stop naming it
        tmp = path.with_name(f".{path.name}.new")
        tmp.unlink(missing_ok=True)
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, st.st_mode & 0o777)
            with os.fdopen(fd, "w") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, st.st_mode & 0o777)
            if os.geteuid() == 0:
                os.chown(tmp, st.st_uid, st.st_gid)
            if load_site(tmp) != replace(before, grip=new):
                raise ConfigError("read back different")
            os.replace(tmp, path)
            folder = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
        except (ConfigError, OSError) as e:
            tmp.unlink(missing_ok=True)
            why = e.strerror if isinstance(e, OSError) and e.strerror else "the new settings didn't read back"
            raise Stop(f"The new address couldn't be saved ({why}). Nothing was changed.") from None

    def address(self) -> int:
        now = self.site.grip
        self.c.say("GRIP's address on this Pi:")
        self.c.say(f"  {now.api_base_url}")
        self.c.say(f"  Token address: {now.token_url}")
        api = self._ask_url("New address from GRIP (starting https://), or press Enter to keep it: ")
        if api is None:
            self.c.say("Nothing was changed.")
            return 0
        default = api + TOKEN_PATH
        token = self._ask_url(f"Token address (press Enter for {default}): ", default)
        new = GripSettings(api, token)
        if new == now:
            self.c.say("That is the address already. Nothing was changed.")
            return 0
        following, own = self._following()
        self._check(new, following)
        with self.ds._no_update(), _uninterrupted():
            if self._following()[0] != following:
                raise Stop("The datasets changed while you answered. Nothing was changed. Run hyperwheel grip "
                           "address again.")
            self._save(new)
            code = self.ds._restarted()
        self.c.say(f"GRIP's address on this Pi is now {new.api_base_url}")
        for project in own:
            self.c.say(f"{project.label} has its own GRIP address and keeps it.")
        self.c.say("In a minute, check the keys show as working with: hyperwheel datasets list")
        return code


USAGE = "Use: hyperwheel grip address"


def main(site: SiteConfig, args: list[str], site_path: Path = SITE, routing_path: Path = ROUTING,
         answers=answers_like_grip, **kw) -> int:
    ds = Datasets(site, routing_path, **kw)
    if args != ["address"]:
        ds.console.say(USAGE)
        return 2
    if not ds.is_root():
        ds.console.say("'hyperwheel grip address' needs administrator rights. Run it as root.")
        return 2
    run = GripAddress(site, ds, site_path, answers)
    try:
        return run.address()
    except Stop as e:
        for line in str(e).splitlines():
            ds.console.say(line)
        return 1
    except (Cancelled, EOFError, KeyboardInterrupt):
        ds.console.say("")
        ds.console.say("Stopped. Nothing was changed.")
        return 1


__all__ = ["GripAddress", "answers_like_grip", "main", "with_address"]
