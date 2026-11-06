"""Updates: take the release this Pi's channel names, safely. Run as root.

    hyperwheel update check                  what the timer runs, every 6 hours
    hyperwheel update now                    check now (in hyperwheel-update.service); apply once nothing
                                             is arriving from the scanner, flagged releases included
    hyperwheel update hold [--reason TEXT]   keep this Pi on its version until release-hold
    hyperwheel update release-hold
    hyperwheel update status

A check:

1. **Hold:** a held Pi changes nothing.
2. **Channel file:** ``channel-<channel>/channel.json`` and its signature are
   downloaded and checked against ``/etc/hyperwheel/release_allowed_signers``. A
   file for another channel is refused.
3. **Only forwards:** a version newer than the current one, never one that has
   already failed on this Pi.
4. **The release:** the tar is downloaded and checked against the channel file
   (size, SHA-256), unpacked, and checked as the installer checks it (signature,
   every file), and its manifest must be the one the channel file names.
5. **Flags:** ``manual_only`` and ``kernel_or_firmware`` releases are applied only
   by ``update now``; ``orthanc_db_change`` never by the updater.
6. **Idle:** nothing being received or fetched, and nothing new from the scanner
   for 30 minutes (``update now``: 1 minute). Otherwise it waits, up to 5 hours,
   then tries again at the next check.
7. **Install:** with the new release's own ``install.sh``, the same steps as an
   install by hand. Site settings and keys are never touched.
8. **Health check:** Orthanc answers; Hyperwheel runs and writes its heartbeat; the
   Upload Monitor answers.
9. **Key check:** every project whose key worked before must get a GRIP token
   again. GRIP not answering is not a failure.
10. **Rollback** if 8 or 9 fails: the previous release's own ``install.sh`` puts it
    back, and the failed version is never tried again here. If the previous release
    can't use the key either, the key was revoked or expired, not broken by the
    release: the version may be tried again, and the key is flagged as usual.

Logs and the status file hold versions and file names only, never settings or keys.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

from .installer import Layout
from .release import (CHANNEL_FILE, CHANNEL_SIGNATURE, MANIFEST, Channel, ReleaseError, sha256_file,
                      verify_channel, verify_release, version_key)

log = logging.getLogger("hyperwheel.updater")

QUIET_S = 30 * 60
QUIET_NOW_S = 60
MAX_WAIT_S = 5 * 3600
POLL_S = 60
HEALTH_WAIT_S = 180
KEY_WAIT_S = 15 * 60
MAX_TAR_BYTES = 1024 ** 3
_LOCAL = {"localhost", "127.0.0.1", "::1"}


class UpdateError(Exception):
    """This check stopped; the message says why (safe to log). Nothing was changed."""


@dataclass(frozen=True)
class Paths:
    layout: Layout = field(default_factory=Layout)

    @property
    def signers(self) -> Path: return self.layout.etc / "release_allowed_signers"
    @property
    def hold(self) -> Path: return self.layout.etc / "update-hold"
    @property
    def site(self) -> Path: return self.layout.etc / "site.toml"
    @property
    def routing(self) -> Path: return self.layout.etc / "routing.toml"
    @property
    def image(self) -> Path: return self.layout.p("/etc/hyperwheel-image")
    @property
    def state(self) -> Path: return self.layout.p("/var/lib/hyperwheel-update")
    @property
    def downloads(self) -> Path: return self.state / "downloads"
    @property
    def status(self) -> Path: return self.state / "status.json"
    @property
    def failed(self) -> Path: return self.state / "failed.json"
    @property
    def lock(self) -> Path: return self.state / ".lock"
    @property
    def request(self) -> Path: return self.state / "now-requested"
    @property
    def service_unit(self) -> Path: return self.layout.units / "hyperwheel.service"


class Pi:
    """What the updater asks of the running Pi. Tests use a fake one."""

    def busy(self, quiet_s: float) -> str | None:
        """Why the Pi isn't idle now, or None."""
        raise NotImplementedError

    def orthanc_new_images(self, since: int | None) -> tuple[int | None, bool]:
        """Orthanc's latest change number, and whether a new image arrived after change ``since``.
        Other changes (a study turning stable, the service deleting what GRIP has) don't count."""
        raise NotImplementedError

    def working_keys(self) -> set[str]:
        """Projects whose key worked at the last check."""
        raise NotImplementedError

    def key_results(self, since: float) -> dict[str, str]:
        """Each project's key status, for checks made since ``since`` only."""
        raise NotImplementedError

    def health_problem(self, since: float) -> str | None:
        """What is wrong after a switch at ``since``, or None when all is well."""
        raise NotImplementedError


def download(url: str, dest: Path, max_bytes: int) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in _LOCAL):
        raise UpdateError(f"refusing to download over {parts.scheme}://")
    tmp = dest.with_name(f".{dest.name}.part")
    req = urllib.request.Request(url, headers={"User-Agent": "hyperwheel-updater"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as out:
            got = 0
            while block := r.read(1024 * 1024):
                got += len(block)
                if got > max_bytes:
                    raise UpdateError(f"{dest.name} is larger than expected")
                out.write(block)
    except OSError as e:  # URLError, HTTPError and timeouts are all OSErrors
        tmp.unlink(missing_ok=True)
        raise UpdateError(f"couldn't download {dest.name} ({getattr(e, 'code', None) or e.__class__.__name__})") \
            from None
    except UpdateError:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, dest)


def run_logged(cmd: Sequence[str]) -> int:
    """Run an install.sh; its lines go to the updater's log (they hold file names and versions only)."""
    proc = subprocess.run(list(cmd), capture_output=True, text=True)
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.strip():
            log.info("  %s", line.strip())
    return proc.returncode


@dataclass
class Updater:
    paths: Paths
    channel: str
    base_url: str
    pi: Pi
    fetch: Callable[[str, Path, int], None] = download
    run: Callable[[Sequence[str]], int] = run_logged
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep

    # ---- the whole check ----------------------------------------------------------------

    def check(self, *, now: bool = False) -> str:
        """One check; returns what happened, in one line (also kept in the status file)."""
        self.paths.state.mkdir(parents=True, exist_ok=True)
        self.paths.state.chmod(0o755)
        with open(self.paths.lock, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "another update check is already running"
            try:
                result = self._check(now)
            except UpdateError as e:
                result = f"no update: {e}"
            self._status(result)
            return result

    def _check(self, now: bool) -> str:
        current = self.current_version()
        if self.paths.hold.exists():
            return f"on hold at {current}: {self.hold_reason()}"
        channel = self.channel_file()
        if version_key(channel.version) <= version_key(current or "0.0.0"):
            self._prune_downloads(keep=None)
            return f"up to date: {current} ({self.channel} names {channel.version})"
        failed = self.failed_versions()
        if channel.version in failed:
            return f"staying on {current}: {channel.version} failed here before ({failed[channel.version]})"
        folder, manifest = self.fetch_release(channel)
        if manifest.flags.get("orthanc_db_change"):
            return f"{channel.version} changes Orthanc's database: it must be installed by hand, not by the updater"
        flagged = [f for f in ("manual_only", "kernel_or_firmware") if manifest.flags.get(f)]
        if flagged and not now:
            return f"{channel.version} is ready but marked {', '.join(flagged)}: apply it with 'hyperwheel update now'"
        if manifest.min_image and self.image_version() < manifest.min_image:
            return f"{channel.version} needs an image from {manifest.min_image} or later (this Pi: " \
                   f"{self.image_version() or 'unknown'})"
        if not self.wait_until_idle(QUIET_NOW_S if now else QUIET_S):
            return f"{channel.version} is ready; waiting for the Pi to be idle (will try again at the next check)"
        return self.apply(folder, channel.version, current)

    # ---- 2. the channel file -------------------------------------------------------------

    def channel_file(self) -> Channel:
        folder = self.paths.downloads / "channel"
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        folder.chmod(0o700)
        tag = quote(f"channel-{self.channel}")
        for name in (CHANNEL_FILE, CHANNEL_SIGNATURE):
            self.fetch(f"{self.base_url}/{tag}/{name}", folder / name, 64 * 1024)
        try:
            channel, signer = verify_channel(folder, self.paths.signers, expected=self.channel)
        except ReleaseError as e:
            raise UpdateError(f"the {self.channel} channel file can't be used: {e}") from None
        log.info("Channel %s names %s (signed by %s).", self.channel, channel.version, signer)
        return channel

    # ---- 4. the release --------------------------------------------------------------------

    def fetch_release(self, channel: Channel):
        if channel.tar_size > MAX_TAR_BYTES:
            raise UpdateError(f"{channel.tar} is larger than any release should be")
        free = shutil.disk_usage(self.paths.state).free
        if free < 4 * channel.tar_size:
            raise UpdateError(f"not enough free disk space for {channel.version}")
        self._prune_downloads(keep=channel.version)
        tar = self.paths.downloads / channel.tar
        if not (tar.exists() and tar.stat().st_size == channel.tar_size and sha256_file(tar) == channel.tar_sha256):
            log.info("Downloading %s (%.1f MB).", channel.tar, channel.tar_size / 1e6)
            self.fetch(f"{self.base_url}/{quote('v' + channel.version)}/{channel.tar}", tar, channel.tar_size)
            if tar.stat().st_size != channel.tar_size or sha256_file(tar) != channel.tar_sha256:
                tar.unlink()
                raise UpdateError(f"{channel.tar} doesn't match the channel file's checksum")
        unpacked = self.paths.downloads / f"hyperwheel-{channel.version}"
        if unpacked.exists():
            shutil.rmtree(unpacked)
        with tarfile.open(tar) as t:
            names = t.getnames()
            if not names or any(n != unpacked.name and not n.startswith(unpacked.name + "/") for n in names):
                raise UpdateError(f"{channel.tar} doesn't hold just hyperwheel-{channel.version}/")
            t.extractall(self.paths.downloads, filter="data")
        try:
            manifest, signer = verify_release(unpacked, self.paths.signers)
        except ReleaseError as e:
            raise UpdateError(f"release {channel.version} can't be used: {e}") from None
        if manifest.version != channel.version or sha256_file(unpacked / MANIFEST) != channel.manifest_sha256:
            raise UpdateError(f"release {channel.version} isn't the one the channel file names")
        log.info("Release %s downloaded and checked (signed by %s).", manifest.version, signer)
        return unpacked, manifest

    # ---- 6. idle ---------------------------------------------------------------------------

    def wait_until_idle(self, quiet_s: float) -> bool:
        """True once the Pi has been idle for quiet_s; False if it isn't within MAX_WAIT_S."""
        start = self.clock()
        quiet_since = start
        last, _ = self.pi.orthanc_new_images(None)
        while True:
            reason = self.pi.busy(quiet_s)
            change, new_images = self.pi.orthanc_new_images(last)
            if new_images:
                reason = reason or "Orthanc is receiving images"
            last = change if change is not None else last
            if reason:
                quiet_since = self.clock()
            elif self.clock() - quiet_since >= quiet_s:
                return True
            if self.clock() - start >= MAX_WAIT_S:
                log.info("Not idle for long enough within %d hours (last: %s).", MAX_WAIT_S // 3600,
                         reason or "waiting for a quiet spell")
                return False
            self.sleep(POLL_S)

    # ---- 7-10. install, check, roll back ----------------------------------------------------

    def apply(self, folder: Path, version: str, before: str | None) -> str:
        user = self.service_user()
        worked = self.pi.working_keys()
        log.info("Installing %s (now %s).", version, before)
        switched_at = self.clock()
        code = self.run(["sh", str(folder / "install.sh"), "--signers", str(self.paths.signers),
                         "--service-user", user])
        if self.current_version() != version:
            self._failed(version, f"the install stopped (exit {code})")
            return f"staying on {before}: installing {version} stopped before the switch (exit {code})"
        problem = None if code == 0 else f"the install ended with exit {code}"
        problem = problem or self._wait(lambda: self.pi.health_problem(switched_at), HEALTH_WAIT_S)
        lost: list[str] = []
        if problem is None and worked:
            results = self._wait_keys(switched_at, worked)
            lost = sorted(p for p in worked if results.get(p) not in (None, "working", "unknown"))
            if lost:
                problem = f"the key for {', '.join(lost)} worked before and doesn't now"
        if problem is None:
            self._prune_releases()
            self._prune_downloads(keep=None)
            return f"updated: {before} -> {version}"
        return self.roll_back(version, before, user, problem, lost)

    def roll_back(self, version: str, before: str | None, user: str, problem: str, lost: list[str]) -> str:
        log.warning("Rolling back %s: %s.", version, problem)
        if not before:
            self._failed(version, problem)
            return f"{version} failed ({problem}) and there is no earlier release to go back to"
        old = self.paths.layout.releases / before / "release"
        back_at = self.clock()
        code = self.run(["sh", str(old / "install.sh"), "--signers", str(self.paths.signers),
                         "--service-user", user])
        if self.current_version() != before:
            self._failed(version, problem)
            return f"ALERT: {version} failed ({problem}) and going back to {before} failed too (exit {code})"
        if lost:
            again = self._wait_keys(back_at, set(lost))
            if all(again.get(p) not in (None, "working", "unknown") for p in lost):
                # The key fails with the old release too: it was revoked or expired, which isn't
                # the new release's fault. Go forward again; the project stays flagged as usual.
                keys = ", ".join(lost)
                forward_at = self.clock()
                self.run(["sh", str(self.paths.downloads / f"hyperwheel-{version}" / "install.sh"), "--signers",
                          str(self.paths.signers), "--service-user", user])
                if self.current_version() == version and \
                        self._wait(lambda: self.pi.health_problem(forward_at), HEALTH_WAIT_S) is None:
                    self._prune_releases()
                    self._prune_downloads(keep=None)
                    return (f"updated: {before} -> {version}; the key for {keys} doesn't work with either "
                            "release, so it was revoked or has expired: it needs replacing")
                problem = f"{problem}; it also failed to start again after the key check"
        self._failed(version, problem)
        return f"rolled back to {before}: {version} failed ({problem}) and won't be tried again"

    def _wait(self, probe: Callable[[], str | None], limit_s: float) -> str | None:
        start = self.clock()
        while (problem := probe()) is not None and self.clock() - start < limit_s:
            self.sleep(10)
        return problem

    def _wait_keys(self, since: float, projects: set[str]) -> dict[str, str]:
        start = self.clock()
        while True:
            results = self.pi.key_results(since)
            if all(p in results for p in projects) or self.clock() - start >= KEY_WAIT_S:
                return results
            self.sleep(15)

    # ---- small parts ----------------------------------------------------------------------

    def current_version(self) -> str | None:
        current = self.paths.layout.current
        return Path(os.readlink(current)).name if current.is_symlink() else None

    def service_user(self) -> str:
        unit = self.paths.service_unit
        for line in unit.read_text().splitlines() if unit.exists() else []:
            if line.startswith("User="):
                return line.split("=", 1)[1].strip()
        return "hyperwheel"

    def image_version(self) -> str:
        try:
            return self.paths.image.read_text().split()[0]
        except (OSError, IndexError):
            return ""

    def hold_reason(self) -> str:
        try:
            return json.loads(self.paths.hold.read_text()).get("reason") or "no reason given"
        except (OSError, ValueError):
            return "no reason given"

    def failed_versions(self) -> dict[str, str]:
        try:
            return json.loads(self.paths.failed.read_text())
        except (OSError, ValueError):
            return {}

    def _failed(self, version: str, why: str) -> None:
        failed = self.failed_versions()
        failed[version] = why
        _write(self.paths.failed, json.dumps(failed, indent=2, sort_keys=True) + "\n")

    def _status(self, result: str) -> None:
        log.info("Update check: %s", result)
        _write(self.paths.status, json.dumps({
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.clock())),
            "channel": self.channel, "current": self.current_version(), "held": self.paths.hold.exists(),
            "result": result}, indent=2) + "\n")

    def _prune_releases(self) -> None:
        keep = {Path(os.readlink(p)).name for p in (self.paths.layout.current, self.paths.layout.previous)
                if p.is_symlink()}
        for folder in self.paths.layout.releases.iterdir():
            if folder.is_dir() and folder.name not in keep and not folder.name.startswith("."):
                shutil.rmtree(folder)
                log.info("Removed old release %s.", folder.name)

    def _prune_downloads(self, keep: str | None) -> None:
        if not self.paths.downloads.is_dir():
            self.paths.downloads.mkdir(parents=True)
        self.paths.downloads.chmod(0o700)
        wanted = {"channel"} | ({f"hyperwheel-{keep}", f"hyperwheel-{keep}-arm64.tar"} if keep else set())
        for item in self.paths.downloads.iterdir():
            if item.name not in wanted:
                shutil.rmtree(item) if item.is_dir() else item.unlink()


def hold_text(paths: Paths) -> str | None:
    """What the Upload Monitor and the doctor say about a hold, or None."""
    if not paths.hold.exists():
        return None
    try:
        hold = json.loads(paths.hold.read_text())
    except (OSError, ValueError):
        hold = {}
    reason = f": {hold['reason']}" if hold.get("reason") else ""
    return f"Updates are on hold since {hold.get('since', '?')}{reason}. Release it with 'hyperwheel update release-hold'."


def summary(paths: Paths) -> str:
    """One line for the doctor: the last update check and any hold."""
    try:
        s = json.loads(paths.status.read_text())
        text = f"last check {s.get('checked_at')} ({s.get('channel')} channel): {s.get('result')}"
    except (OSError, ValueError):
        text = "no update check has run yet"
    held = hold_text(paths)
    return f"{held} {text[0].upper()}{text[1:]}" if held else text


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.new")
    tmp.write_text(text)
    tmp.chmod(0o644)
    os.replace(tmp, path)


# ---- the live Pi ---------------------------------------------------------------------------

class LivePi(Pi):
    """The real checks: the state store, Orthanc's REST API, systemd, the Upload Monitor."""

    def __init__(self, paths: Paths):
        from .config import load_routing, load_site
        self.paths = paths
        self.site = load_site(paths.site) if paths.site.exists() else None
        self.routing = (load_routing(paths.routing, test_mode=self.site.test_mode)
                        if self.site and paths.routing.exists() else None)

    def _store(self):
        from .state import Store
        if not self.site or not self.site.state_db.exists():
            return None
        return Store(self.site.state_db, readonly=True)

    def busy(self, quiet_s: float) -> str | None:
        from .dashboard import HEARTBEAT, snapshot
        store = self._store()
        if store is None:
            return None
        try:
            now = time.time()
            running = now - store.cursor(HEARTBEAT) <= 120
            if running and self.routing is not None:
                s = snapshot(store, self.site, self.routing, now=now, free_bytes=lambda p: 1 << 40)
                if not s["on_pi_complete"]:
                    return "images or raw data are still coming from the scanner"
            last = store.last_arrival()
            if last is not None and now - last < quiet_s:
                return f"something arrived from the scanner {int((now - last) // 60)} minutes ago"
            return None
        finally:
            store.close()

    def _orthanc(self, path: str):
        import requests
        login = json.loads((self.paths.layout.etc / "orthanc.json").read_text())
        url = self.site.orthanc.url if self.site else "http://127.0.0.1:8042"
        r = requests.get(f"{url}{path}", auth=(login["username"], login["password"]), timeout=10)
        r.raise_for_status()
        return r.json()

    def orthanc_new_images(self, since: int | None) -> tuple[int | None, bool]:
        try:
            if since is None:
                return int(self._orthanc("/changes?last")["Last"]), False
            r = self._orthanc(f"/changes?since={int(since)}&limit=1000")
            return int(r["Last"]), any(c.get("ChangeType") == "NewInstance" for c in r.get("Changes", []))
        except Exception:  # Orthanc not answering means it isn't receiving either
            return None, False

    def working_keys(self) -> set[str]:
        store = self._store()
        if store is None or self.routing is None:
            return set()
        try:
            return {p for p in self.routing.projects if (store.key_status(p) or ("",))[0] == "working"}
        finally:
            store.close()

    def key_results(self, since: float) -> dict[str, str]:
        store = self._store()
        if store is None or self.routing is None:
            return {}
        try:
            results = {}
            for p in self.routing.projects:
                status = store.key_status(p)
                if status and status[2] >= since:
                    results[p] = status[0]
            return results
        finally:
            store.close()

    def health_problem(self, since: float) -> str | None:
        from .dashboard import HEARTBEAT
        units = ["orthanc"] + (["hyperwheel.service", "hyperwheel-dashboard.service"] if self.routing else [])
        for unit in units:
            state = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True).stdout.strip()
            if state != "active":
                return f"{unit} is {state or 'not running'}"
        try:
            self._orthanc("/system")
        except Exception:
            return "Orthanc doesn't answer"
        if not self.routing:
            return None
        store = self._store()
        if store is None:
            return "Hyperwheel has no state database"
        try:
            if store.cursor(HEARTBEAT) < since:
                return "Hyperwheel hasn't started its work yet"
        finally:
            store.close()
        d = self.site.dashboard
        try:
            with urllib.request.urlopen(f"http://{d.bind}:{d.port}/api/status", timeout=10) as r:
                if r.status != 200:
                    return f"the Upload Monitor answers {r.status}"
        except OSError:
            return "the Upload Monitor doesn't answer"
        return None


# ---- command line -----------------------------------------------------------------------

def main(argv: list[str], paths: Paths | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hyperwheel update")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("check", help="what the timer runs")
    n = sub.add_parser("now", help="check now, and apply once nothing is arriving from the scanner")
    n.add_argument("--url", help=argparse.SUPPRESS)  # tests on the dev Pi: a local copy of the releases
    h = sub.add_parser("hold", help="keep this Pi on its version")
    h.add_argument("--reason", default="")
    sub.add_parser("release-hold", help="let this Pi take updates again")
    sub.add_parser("status", help="show the last update check")
    args = ap.parse_args(argv)
    paths = paths or Paths()

    if args.action == "status":
        try:
            s = json.loads(paths.status.read_text())
        except (OSError, ValueError):
            s = {"result": "no update check has run yet"}
        current = Updater(paths, "", "", Pi()).current_version()
        print(f"Release: {current or 'none'}" + (" (on hold)" if paths.hold.exists() else ""))
        print(f"Last check: {s.get('checked_at', '-')}, channel {s.get('channel', '-')}: {s['result']}")
        return 0
    if os.geteuid() != 0 and paths.layout.root == Path("/"):
        print("Please run as root (sudo).", file=sys.stderr)
        return 1
    if args.action == "hold":
        _write(paths.hold, json.dumps({"reason": args.reason, "since": time.strftime("%Y-%m-%d %H:%M")}) + "\n")
        print("This Pi is on hold: it keeps its version until 'hyperwheel update release-hold'.")
        return 0
    if args.action == "release-hold":
        paths.hold.unlink(missing_ok=True)
        print("Hold released: this Pi takes updates again at the next check.")
        return 0

    if args.action == "now":
        # The check runs in hyperwheel-update.service, not in this shell: closing the SSH
        # session or pressing Ctrl-C can't cut an install short.
        paths.state.mkdir(parents=True, exist_ok=True)
        _write(paths.request, json.dumps({"url": args.url}) + "\n")
        print("Checking for an update now (this waits until nothing is arriving from the scanner)...")
        subprocess.run(["systemctl", "start", "hyperwheel-update.service"])
        return main(["status"], paths)

    from .config import ConfigError, UpdateSettings, load_site
    try:
        settings = load_site(paths.site).updates if paths.site.exists() else UpdateSettings()
        pi = LivePi(paths)
    except ConfigError as e:
        print(f"Configuration problem: {e}", file=sys.stderr)
        return 2
    url, now = settings.url, False
    if paths.request.exists():  # from 'hyperwheel update now'
        try:
            url = json.loads(paths.request.read_text()).get("url") or url
        except (OSError, ValueError):
            pass
        paths.request.unlink(missing_ok=True)
        now = True
    result = Updater(paths, settings.channel, url.rstrip("/"), pi).check(now=now)
    print(result)
    return 1 if result.startswith(("ALERT", "rolled back")) else 0
