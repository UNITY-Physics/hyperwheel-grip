"""Install a verified release on a Pi, offline. Run as root:

    python3 -m hyperwheel.installer <release-folder> [--signers FILE] [--site-setup DIR]
                                    [--service-user NAME] [--no-start]

The same steps are used to build the image (release 1 pre-loaded) and, later,
by the updater:

1. Check the release: signature and every file (``hyperwheel.release``).
2. The service account: created as a system account if missing. An existing
   account of that name that isn't a system account is never taken over.
3. Orthanc and its libraries from the release's ``.deb`` files, from local
   files only, and only if a version differs.
4. The release copied to ``/opt/hyperwheel/releases/<version>/`` (read-only),
   with a venv built offline from its wheels.
5. Orthanc's settings, the systemd units and the data-manager sudo rule
   (``/etc/sudoers.d/hyperwheel``, checked with ``visudo`` before it's put in
   place), which belong to the release.
6. Site settings and secrets in ``/etc/hyperwheel``: added from ``--site-setup``
   only where missing, never overwritten, reset, printed or logged.
7. ``current`` switched to the new release (``previous`` kept), services
   restarted, and a health check.

Nothing is downloaded. Paths are relative to ``--root`` (tests use a scratch folder).
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .release import Manifest, ReleaseError, verify_release

SERVICE_USER = "hyperwheel"
ORTHANC_USER = "orthanc"
DATA_MANAGER = "datamanager"

# What the data-manager account may run as root: exactly these command lines and nothing else
# (sudo allows no other arguments when a rule lists them). Setting a key or the scanner password
# asks for the data manager's own password; looking at the status doesn't.
SUDO_RULE = f"""\
# Written by Hyperwheel's installer, and written again by every release: changes made here are lost.
# The {DATA_MANAGER} account may run only these Hyperwheel commands as root, with exactly these words.
Cmnd_Alias HYPERWHEEL_LOOK = /usr/local/bin/hyperwheel status, /usr/local/bin/hyperwheel doctor, \\
    /usr/local/bin/hyperwheel keys list, /usr/local/bin/hyperwheel update status
Cmnd_Alias HYPERWHEEL_KEYS = /usr/local/bin/hyperwheel keys set, /usr/local/bin/hyperwheel scanner password
{DATA_MANAGER} ALL = (root) NOPASSWD: HYPERWHEEL_LOOK
{DATA_MANAGER} ALL = (root) HYPERWHEEL_KEYS
"""


class InstallError(Exception):
    """The install stopped; the message says why. Nothing already working was changed."""


@dataclass(frozen=True)
class Layout:
    root: Path = Path("/")

    def p(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    @property
    def opt(self) -> Path: return self.p("/opt/hyperwheel")
    @property
    def releases(self) -> Path: return self.opt / "releases"
    @property
    def current(self) -> Path: return self.opt / "current"
    @property
    def previous(self) -> Path: return self.opt / "previous"
    @property
    def etc(self) -> Path: return self.p("/etc/hyperwheel")
    @property
    def orthanc_conf(self) -> Path: return self.etc / "orthanc.d"
    @property
    def var(self) -> Path: return self.p("/var/lib/hyperwheel")
    @property
    def units(self) -> Path: return self.p("/etc/systemd/system")


def run_command(cmd: Sequence[str], env: dict[str, str] | None = None) -> str:
    out = subprocess.run(list(cmd), capture_output=True, text=True, env={**os.environ, **(env or {})})
    if out.returncode != 0:
        raise InstallError(f"{cmd[0]} failed (exit {out.returncode}): {out.stderr.strip()[-400:]}")
    return out.stdout


@dataclass
class Installer:
    release_dir: Path
    signers: Path
    layout: Layout = field(default_factory=Layout)
    service_user: str = SERVICE_USER
    site_setup: Path | None = None
    start: bool = True
    run: Callable[..., str] = run_command
    getpwnam: Callable[[str], pwd.struct_passwd] = pwd.getpwnam
    chown: Callable[[Path, str, str], None] = None  # type: ignore[assignment]
    say: Callable[[str], None] = print
    python: str = "/usr/bin/python3"

    def __post_init__(self):
        if self.chown is None:
            self.chown = lambda path, user, group: shutil.chown(path, user, group)

    # ---- the whole install ------------------------------------------------------------

    def install(self) -> Manifest:
        try:
            manifest, signer = verify_release(self.release_dir, self.signers)
        except ReleaseError as e:
            raise InstallError(f"the release can't be used: {e}") from None
        self.say(f"Release {manifest.version} ({manifest.channel}), signed by {signer}: checked.")
        self.service_account()
        self.debs()
        target = self.copy_release(manifest)
        self.venv(target, manifest)
        self.orthanc_settings(target)
        self.directories()
        configured = self.site_settings()
        self.units()
        self.sudo_rule()
        self.switch(target)
        self.restart(configured)
        return manifest

    # ---- 2. the service account ------------------------------------------------------

    def service_account(self) -> None:
        try:
            entry = self.getpwnam(self.service_user)
        except KeyError:
            self.run(["useradd", "--system", "--user-group", "--home-dir", "/var/lib/hyperwheel",
                      "--no-create-home", "--shell", "/usr/sbin/nologin", self.service_user])
            self.say(f"Service account {self.service_user}: created (system account, no login).")
            return
        if entry.pw_uid >= 1000 or not entry.pw_shell.endswith(("nologin", "/false")):
            raise InstallError(f"an account called {self.service_user} already exists and isn't a system account "
                               f"(UID {entry.pw_uid}, shell {entry.pw_shell}). It won't be taken over. "
                               "Use --service-user with another name on this Pi.")
        self.say(f"Service account {self.service_user}: already there.")

    # ---- 3. Orthanc and its libraries ---------------------------------------------------

    def debs(self) -> None:
        wanted = []
        for deb in sorted((self.release_dir / "debs").glob("*.deb")):
            package, version = self.run(["dpkg-deb", "-f", str(deb), "Package", "Version"]).split("\n")[:2]
            package, version = package.split(":", 1)[1].strip(), version.split(":", 1)[1].strip()
            try:
                have = self.run(["dpkg-query", "-W", "-f", "${Version}", package]).strip()
            except InstallError:
                have = ""
            if have != version:
                wanted.append(str(deb))
        if not wanted:
            self.say("Orthanc packages: already the release's versions.")
            return
        self.run(["apt-get", "install", "-y", "-q", "--no-download", "--no-install-recommends", *wanted],
                 env={"DEBIAN_FRONTEND": "noninteractive"})
        self.say(f"Orthanc packages: installed {len(wanted)} from the release (nothing downloaded).")

    # ---- 4. the release and its venv --------------------------------------------------

    def copy_release(self, manifest: Manifest) -> Path:
        target = self.layout.releases / manifest.version
        if target.exists():
            try:
                verify_release(target / "release", self.signers)
            except ReleaseError as e:
                raise InstallError(f"{target} exists but doesn't verify ({e}). Remove it to reinstall") from None
            self.say(f"Release folder {target}: already there and checked.")
            return target
        tmp = target.with_name(f".{manifest.version}.partial")
        if tmp.exists():
            shutil.rmtree(tmp)
        shutil.copytree(self.release_dir, tmp / "release", symlinks=False)
        for path in [tmp, *tmp.rglob("*")]:
            if path.is_dir():
                path.chmod(0o755)
            else:
                path.chmod(0o644 if not path.name.endswith(".so") else 0o755)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.rename(target)
        self.say(f"Release folder {target}: copied.")
        return target

    def venv(self, target: Path, manifest: Manifest) -> None:
        venv = target / "venv"
        if (venv / "bin" / "hyperwheel").exists():
            self.say("Python environment: already there.")
            return
        self.run([self.python, "-m", "venv", str(venv)])
        self.run([str(venv / "bin" / "pip"), "install", "-q", "--no-index", "--find-links",
                  str(target / "release" / "wheels"), f"hyperwheel=={manifest.version}"])
        self.say("Python environment: built offline from the release's wheels.")

    # ---- 5. Orthanc's settings and the units -----------------------------------------

    def orthanc_settings(self, target: Path) -> None:
        # Each release's settings load that release's own OE2, by its versioned path,
        # so switching back to an older release brings back its plugin too.
        plugins = []
        if (target / "release" / "oe2" / "libOrthancExplorer2.so").exists():
            plugins.append(f"/opt/hyperwheel/releases/{target.name}/release/oe2/libOrthancExplorer2.so")
        (target / "orthanc.json").write_text(json.dumps(orthanc_json(plugins), indent=2) + "\n")
        (target / "orthanc.json").chmod(0o644)

    def units(self) -> None:
        self.layout.units.mkdir(parents=True, exist_ok=True)
        for name, text in UNITS.items():
            path = self.layout.units / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text.replace("@USER@", self.service_user))
            path.chmod(0o644)
        self.say("Systemd units: written (hyperwheel, hyperwheel-dashboard, hyperwheel-update and its timer, "
                 "Orthanc's settings drop-in).")
        # The hyperwheel command (status, doctor, update ...), always the current release's.
        command = self.layout.p("/usr/local/bin/hyperwheel")
        command.parent.mkdir(parents=True, exist_ok=True)
        if not (command.is_symlink() and os.readlink(command) == "/opt/hyperwheel/current/venv/bin/hyperwheel"):
            _atomic_link(command, "/opt/hyperwheel/current/venv/bin/hyperwheel")

    def sudo_rule(self) -> None:
        folder = self.layout.p("/etc/sudoers.d")
        path = folder / "hyperwheel"
        if path.is_file() and not path.is_symlink() and path.read_text() == SUDO_RULE:
            self.say("Data-manager commands: sudo rule already there.")
            return
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / ".hyperwheel.new"  # sudo skips files with a dot in the name
        tmp.unlink(missing_ok=True)
        tmp.write_text(SUDO_RULE)
        tmp.chmod(0o440)
        try:
            self.run(["visudo", "-c", "-q", "-f", str(tmp)])
        except InstallError as e:
            tmp.unlink(missing_ok=True)
            raise InstallError(f"the data-manager sudo rule didn't pass visudo's check, so sudo was left as it "
                               f"was ({e})") from None
        os.replace(tmp, path)
        self.say(f"Data-manager commands: sudo rule written ({DATA_MANAGER} may run only keys, status, doctor "
                 "and update status as root).")

    # ---- 6. site settings and secrets: add what's missing, never overwrite ----------

    def directories(self) -> None:
        for path, mode in ((self.layout.etc, 0o755), (self.layout.etc / "keys", 0o700)):
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(mode)
        self.layout.var.mkdir(parents=True, exist_ok=True)
        self.layout.var.chmod(0o750)
        self.chown(self.layout.var, self.service_user, self.service_user)
        self.layout.orthanc_conf.mkdir(exist_ok=True)
        self.layout.orthanc_conf.chmod(0o750)
        self.chown(self.layout.orthanc_conf, "root", ORTHANC_USER)

    def site_settings(self) -> bool:
        etc = self.layout.etc
        added = []
        if self.site_setup is not None:
            for name, mode in (("site.toml", 0o644), ("routing.toml", 0o644), ("scanner_known_hosts", 0o644),
                               ("scanner.json", 0o600)):
                if self._add_if_missing(self.site_setup / name, etc / name, mode):
                    added.append(name)
            for key in sorted((self.site_setup / "keys").glob("*.json")) if (self.site_setup / "keys").is_dir() else []:
                if self._add_if_missing(key, etc / "keys" / key.name, 0o600):
                    added.append(f"keys/{key.name}")
        # Who may sign releases, for the updater: from --signers on the first install,
        # never replaced by an update (the image ships the real one).
        if self.signers.resolve() != (etc / "release_allowed_signers").resolve():
            if self._add_if_missing(self.signers, etc / "release_allowed_signers", 0o644):
                added.append("release_allowed_signers")
        # Orthanc's login: made once, here, and never changed by an update.
        login = etc / "orthanc.json"
        if not login.exists():
            self._write_secret(login, json.dumps({"username": self.service_user,
                                                  "password": secrets.token_urlsafe(24)}) + "\n")
            added.append("orthanc.json (new Orthanc login)")
        users = json.loads(login.read_text())
        users_file = self.layout.orthanc_conf / "users.json"
        self._write_secret(users_file, json.dumps({"RegisteredUsers": {users["username"]: users["password"]}}) + "\n",
                           group=ORTHANC_USER, mode=0o640)
        link = self.layout.orthanc_conf / "hyperwheel.json"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to("/opt/hyperwheel/current/orthanc.json")
        if added:
            self.say(f"Site settings added where missing: {', '.join(added)}.")
        just_added = {a.split(" ")[0] for a in added}
        present = [p.name for p in etc.iterdir() if p.is_file()]
        if (etc / "keys").is_dir():
            present += [f"keys/{p.name}" for p in (etc / "keys").iterdir() if p.is_file()]
        kept = sorted(name for name in present if name not in just_added)
        if kept:
            self.say(f"Site settings kept as they are: {', '.join(kept)}.")
        configured = (etc / "site.toml").exists() and (etc / "routing.toml").exists()
        if not configured:
            self.say("This Pi isn't set up yet (no site.toml or routing.toml): Hyperwheel won't start until it is.")
        return configured

    def _add_if_missing(self, src: Path, dest: Path, mode: int) -> bool:
        if not src.is_file() or dest.exists():
            return False
        self._write_secret(dest, src.read_bytes(), mode=mode)
        return True

    def _write_secret(self, dest: Path, data: str | bytes, *, group: str = "root", mode: int = 0o600) -> None:
        tmp = dest.with_name(f".{dest.name}.new")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data.encode() if isinstance(data, str) else data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        self.chown(tmp, "root", group)
        os.replace(tmp, dest)

    # ---- 7. switch, restart, check ----------------------------------------------------

    def switch(self, target: Path) -> None:
        current = self.layout.current
        if current.is_symlink():
            old = os.readlink(current)
            if Path(old).name == target.name:
                self.say(f"Current release: already {target.name}.")
                return
            _atomic_link(self.layout.previous, old)
        _atomic_link(current, f"releases/{target.name}")
        self.say(f"Current release: now {target.name}.")

    def restart(self, configured: bool) -> None:
        self.run(["systemctl", "daemon-reload"])
        self.run(["systemctl", "restart", "orthanc"])
        self.say("Orthanc: restarted with the release's settings.")
        if self.start:
            self.run(["systemctl", "enable", "--now", "hyperwheel-update.timer"])
            self.say("Update checks: on (every 6 hours).")
        if not (configured and self.start):
            return
        self.run(["systemctl", "enable", "hyperwheel.service", "hyperwheel-dashboard.service"])
        self.run(["systemctl", "restart", "hyperwheel.service", "hyperwheel-dashboard.service"])
        self.say("Hyperwheel and the Upload Monitor: started.")


def _atomic_link(link: Path, target: str) -> None:
    tmp = link.with_name(f".{link.name}.new")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target)
    os.replace(tmp, link)


def orthanc_json(plugins: list[str]) -> dict:
    """The release's Orthanc settings. Logins are in users.json beside it (site secret)."""
    return {
        "Name": "Hyperwheel",
        "DicomAet": "HYPERWHEEL",
        "DicomPort": 4242,
        "DicomCheckCalledAet": False,
        "HttpPort": 8042,
        "RemoteAccessAllowed": False,
        "AuthenticationEnabled": True,
        "StorageDirectory": "/var/lib/orthanc/db-v6",
        "IndexDirectory": "/var/lib/orthanc/db-v6",
        "StableAge": 60,
        "OrthancExplorerEnabled": False,
        "Plugins": plugins,
        "OrthancExplorer2": {"Enable": True, "IsDefaultOrthancUI": True},
    }


UNITS = {
    "hyperwheel.service": """[Unit]
Description=Hyperwheel: scanner to GRIP uploads
After=network-online.target orthanc.service
Wants=network-online.target orthanc.service

[Service]
User=@USER@
Group=@USER@
ExecStart=/opt/hyperwheel/current/venv/bin/hyperwheel run
LoadCredential=keys:/etc/hyperwheel/keys
LoadCredential=orthanc:/etc/hyperwheel/orthanc.json
LoadCredential=scanner:/etc/hyperwheel/scanner.json
Restart=on-failure
RestartSec=30
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/hyperwheel
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
""",
    "hyperwheel-dashboard.service": """[Unit]
Description=Hyperwheel Upload Monitor (read-only web page on the hotspot)
After=network-online.target hyperwheel.service

[Service]
User=@USER@
Group=@USER@
ExecStart=/opt/hyperwheel/current/venv/bin/hyperwheel dashboard
Restart=always
RestartSec=10
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
""",
    "hyperwheel-update.service": """[Unit]
Description=Hyperwheel: check for an update and apply it (hyperwheel update)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/opt/hyperwheel/current/venv/bin/hyperwheel update check
# It waits until the Pi is idle, up to 5 hours, before installing.
TimeoutStartSec=8h
""",
    "hyperwheel-update.timer": """[Unit]
Description=Hyperwheel: update check every 6 hours

[Timer]
OnBootSec=20min
OnUnitInactiveSec=6h
RandomizedDelaySec=30min

[Install]
WantedBy=timers.target
""",
    "orthanc.service.d/hyperwheel.conf": """# Orthanc reads the Hyperwheel release's settings (and the site's login), not Debian's.
[Service]
ExecStart=
ExecStart=/usr/sbin/Orthanc --logdir=/var/log/orthanc /etc/hyperwheel/orthanc.d
""",
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m hyperwheel.installer")
    ap.add_argument("release", type=Path, help="unpacked release folder (with manifest.json and its signature)")
    ap.add_argument("--signers", type=Path, default=Path("/etc/hyperwheel/release_allowed_signers"))
    ap.add_argument("--site-setup", type=Path, help="folder with site.toml, routing.toml, scanner.json, keys/ "
                                                     "(first setup: only missing files are added)")
    ap.add_argument("--service-user", default=SERVICE_USER)
    ap.add_argument("--no-start", action="store_true", help="install, but don't start Hyperwheel")
    ap.add_argument("--root", type=Path, default=Path("/"), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if os.geteuid() != 0 and args.root == Path("/"):
        print("Please run as root (sudo).", file=sys.stderr)
        return 1
    started = time.monotonic()
    try:
        manifest = Installer(args.release.resolve(), args.signers, Layout(args.root), args.service_user,
                             args.site_setup, not args.no_start).install()
    except InstallError as e:
        print(f"Install stopped: {e}", file=sys.stderr)
        return 1
    print(f"Hyperwheel {manifest.version} installed in {time.monotonic() - started:.0f} s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
