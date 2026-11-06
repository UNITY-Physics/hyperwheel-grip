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
5. Orthanc's settings and the systemd units, which belong to the release.
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
                               f"(UID {entry.pw_uid}, shell {entry.pw_shell}); it won't be taken over. "
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
                raise InstallError(f"{target} exists but doesn't verify ({e}); remove it to reinstall") from None
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
        self.say("Systemd units: written (hyperwheel, hyperwheel-dashboard, Orthanc's settings drop-in).")

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
        kept = sorted(p.name for p in etc.iterdir() if p.is_file() and p.name not in {a.split(' ')[0] for a in added})
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
