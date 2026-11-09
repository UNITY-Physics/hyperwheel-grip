"""Pulls raw data (RRDF) from the scanner and assigns each file to its study.

The rule. The scanner keeps each exam's raw files in
``RRDF/rrdf_<StudyDate>_<StudyTime>/`` (scanner local time). A raw file is
assigned to a study only if both hold:

1. its folder name equals the StudyDate and StudyTime of exactly one study on
   this Pi, and that study is routed;
2. the timestamp in its own name lies between the study's start and its last
   acquisition plus a margin (60 min), and before the next study's start.

- A folder that matches no study, or only an unrouted one, is never pulled:
  that raw data never leaves the scanner.
- A file that fails rule 2, or a folder that matches more than one study, is
  pulled into ``stagingArea/_unassigned/<folder>/``, held and flagged. It is
  never uploaded or deleted on a guess.
- A file is pulled only once its size is the same on two listings, so a file
  still being written is never taken.
- Files keep their names. A file already pulled (or uploaded and deleted) is
  never pulled again: its row (folder, name, size, checksum; no patient data)
  stays in the state store for good, apart from the 7-day patient data rule.
  So when an operator sends an exam in parts, only the raw files that are new
  in the folder are fetched, and they join the same study.
- A file already fetched that shows up again with a different size is
  flagged, not fetched again: the Pi doesn't guess which version is right.
- A file held as unassigned is checked again when its study's window grows
  (more of the exam's DICOM arrived): if it now fits, it joins the study.

File modification times on the scanner are never used: they are not in the
scanner's local time.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shlex
import shutil
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO, Protocol

from . import checks
from .config import SiteConfig
from .dicominfo import parse_datetime
from .ids import is_rrdf_name, rrdf_folder_time, rrdf_timestamp
from .state import File, Store, Study

log = logging.getLogger("hyperwheel.rawpull")

UNASSIGNED = "_unassigned"
_SETTLED = ("checking", "held", "ready", "uploading", "uploaded")
_BLOCK = 1024 * 1024


class ScannerError(Exception):
    """The scanner could not be reached or read. Safe to log."""


class LoginRefused(ScannerError):
    """The scanner refused the raw-data login (wrong password, or a changed account)."""


class ScannerFiles(Protocol):
    def list_folders(self) -> list[str]: ...
    def list_files(self, folder: str) -> list[tuple[str, int]]: ...
    def open(self, folder: str, name: str) -> AbstractContextManager[BinaryIO]: ...
    def close(self) -> None: ...


@dataclass
class PullReport:
    pulled: int = 0
    unassigned: int = 0
    waiting: int = 0


class RawPuller:
    ALERT_AFTER_S = 30 * 60
    # After a refused login, wait this long before trying again: repeated failed
    # logins could get the account locked on the scanner.
    LOGIN_RETRY_S = 15 * 60

    def __init__(self, store: Store, site: SiteConfig, connect: Callable[[], ScannerFiles], *,
                 clock: Callable[[], float] = time.time,
                 free_bytes: Callable[[Path], int] | None = None):
        self.store = store
        self.site = site
        self.connect = connect
        self.clock = clock
        self.free_bytes = free_bytes or (lambda p: shutil.disk_usage(p).free)
        self._failing_since: float | None = None
        self._login_refused_at: float | None = None

    # ---- one pass -----------------------------------------------------------------

    def pull_once(self) -> PullReport:
        report = PullReport()
        if not self.site.rrdf.enabled:
            return report
        watched = self._watched()
        if not watched:
            return report
        if self._login_refused_at is not None and self.clock() - self._login_refused_at < self.LOGIN_RETRY_S:
            return report  # don't try the refused login again yet
        try:
            scanner = self.connect()
        except LoginRefused:
            self._login_refused_at = self.clock()
            self.store.raise_flag("RAW_LOGIN_REFUSED", "The scanner refused the raw-data login, most likely "
                                  "because its software was upgraded or it's missing the RRDF entitlement. Set the "
                                  "new password with: hyperwheel scanner password. If that doesn't help, tell "
                                  "your Hyperwheel admin. Raw data stays on the scanner until then. DICOM keeps "
                                  "uploading.",
                                  level="alert")
            log.error("raw data: the scanner refused the login. Trying again in %d min", self.LOGIN_RETRY_S // 60)
            return report
        except (ScannerError, OSError) as e:
            self._failed(str(e), watched)
            return report
        self._login_refused_at = None
        self.store.clear_flags("RAW_LOGIN_REFUSED")
        try:
            self._pass(scanner, watched, report)
        except (ScannerError, OSError) as e:
            self._failed(str(e), watched)
            return report
        finally:
            scanner.close()
        self._failing_since = None
        self.store.clear_flags("RAW_PULL_FAILING")
        return report

    def _watched(self) -> list[Study]:
        cutoff = self.clock() - self.site.rrdf.watch_days * 86400
        return [s for s in self.store.studies(states=_SETTLED)
                if s.project and s.stable_at is not None and s.stable_at >= cutoff]

    def _failed(self, why: str, watched: list[Study]) -> None:
        now = self.clock()
        if self._failing_since is None:
            self._failing_since = now
        log.warning("raw data: can't read the scanner (%s). Will try again", why)
        if now - self._failing_since >= self.ALERT_AFTER_S:
            self.store.raise_flag("RAW_PULL_FAILING", f"Raw data can't be fetched from the scanner ({why}). "
                                  "Check the scanner is on and its cable is plugged in.", level="alert")

    def _pass(self, scanner: ScannerFiles, watched: list[Study], report: PullReport) -> None:
        folders = {name: t for name in scanner.list_folders() if (t := rrdf_folder_time(name)) is not None}
        studies = self.store.studies()
        starts = {s.id: start for s in studies if (start := parse_datetime(s.study_date, s.study_time))}
        boundaries = sorted(set(starts.values()) | set(folders.values()))
        watched_ids = {s.id for s in watched}
        seen_folders = set()

        for folder, folder_time in sorted(folders.items()):
            matches = [s for s in studies if starts.get(s.id) == folder_time]
            if not matches or any(m.state in ("receiving", "stable") for m in matches):
                continue  # not ours, or its DICOM is still arriving
            routed = [m for m in matches if m.project and m.state in _SETTLED]
            if not routed or not any(m.id in watched_ids for m in routed):
                continue  # unrouted, held without a project, or past the watch period
            target = routed[0] if len(matches) == 1 else None
            seen_folders.update(m.id for m in routed)
            next_start = next((b for b in boundaries if b > folder_time), None)
            settled = True
            for name, size in scanner.list_files(folder):
                if not name.endswith(".h5"):
                    continue
                if not is_rrdf_name(name):
                    self.store.raise_flag("RAW_NAME_UNEXPECTED", f"A raw file in scanner folder {folder} has an "
                                          "unexpected name and was not fetched. Please contact your Hyperwheel admin.",
                                          level="alert", ref=f"{folder}/{name}")
                    continue
                row = self.store.rrdf_file(folder, name)
                if row is not None and row.state != "seen":
                    if row.size is not None and size != row.size:
                        self._changed_on_scanner(folder, row, size)
                    continue
                if row is None or row.size != size:
                    if row is None:
                        self.store.add_file(kind="rrdf", name=name, state="seen", study_id=None,
                                            scanner_folder=folder, size=size)
                    else:
                        self.store.update_file(row.id, size=size)
                    settled = False
                    report.waiting += 1
                    continue
                window_ok = target is not None and self._in_window(target, rrdf_timestamp(name), next_start)
                destination = target if window_ok else None
                if not self._pull(scanner, folder, row, destination, matches, report):
                    settled = False
            if target is not None:
                for row in self.store.files(kind="rrdf", states=("unassigned",)):
                    if row.scanner_folder == folder and self._in_window(target, rrdf_timestamp(row.name), next_start):
                        self._assign_held(folder, row, target, report)
                target = self.store.study(target.id)
                self.store.update_study(target.id, raw_checked_at=self.clock())
                if settled:
                    checks.apply(self.store, target, checks.raw_issues(self.store, target, True),
                                 checks.RAW_CHECK_CODES)

        grace = self.site.rrdf.folder_grace_minutes * 60
        for study in watched:
            if study.id in seen_folders:
                continue
            # The scanner makes DICOM from raw data, so a study's raw data is on the scanner before its DICOM
            # arrives. No folder now means no raw data: the study is complete without it. It's still watched,
            # in case a folder turns up.
            if study.raw_checked_at is None:
                self.store.update_study(study.id, raw_checked_at=self.clock())
            if self.clock() - (study.stable_at or 0) >= grace:
                checks.apply(self.store, study, checks.raw_issues(self.store, study, False), checks.RAW_CHECK_CODES)

    def _in_window(self, study: Study, ts: datetime | None, next_start: datetime | None) -> bool:
        start = parse_datetime(study.study_date, study.study_time)
        if ts is None or start is None or ts < start:
            return False
        last = datetime.fromisoformat(study.last_acquisition) if study.last_acquisition else start
        end = last + timedelta(minutes=self.site.rrdf.margin_minutes)
        if next_start is not None:
            end = min(end, next_start - timedelta(seconds=1))
        return ts <= end

    def _changed_on_scanner(self, folder: str, row: File, size: int) -> None:
        ref = f"{folder}/{row.name}"
        already = any(f.code == "RAW_CHANGED_ON_SCANNER" and f.ref == ref for f in self.store.open_flags())
        self.store.raise_flag("RAW_CHANGED_ON_SCANNER", f"Raw file {row.name} in scanner folder {folder} now has a "
                              f"different size on the scanner ({size} bytes) from when the Pi fetched it "
                              f"({row.size} bytes). The Pi has not fetched it again. Please "
                              "contact your Hyperwheel admin.", study_id=row.study_id, level="alert", ref=ref)
        if not already:
            log.warning("raw file %s in %s: size changed on the scanner after it was fetched. Flagged, not fetched "
                        "again", row.name, folder)

    def _assign_held(self, folder: str, row: File, study: Study, report: PullReport) -> None:
        """Move a held raw file into its study once the study's window covers it."""
        src = Path(row.path or "")
        directory = self.site.staging_dir / study.project / study.study_uid / "RRDF"
        directory.mkdir(parents=True, exist_ok=True)
        dest = directory / row.name
        if not src.is_file():
            return  # the held copy is missing; it stays held and flagged for a person
        if dest.exists():
            if not os.path.samefile(src, dest):
                return  # a different file of that name is already there; it stays held and flagged
        else:
            os.link(src, dest)  # never overwrites
            _fsync_dir(directory)
        self.store.move_file(row.id, "pulled", study_id=study.id, path=str(dest))
        src.unlink(missing_ok=True)
        _fsync_dir(src.parent)
        self.store.clear_flags("UNASSIGNED_RAW", file_id=row.id)
        if not any(f.scanner_folder == folder for f in self.store.files(kind="rrdf", states=("unassigned",))):
            self.store.clear_flags("RAW_FILE_UNASSIGNED", study_id=study.id)
        if self.store.study(study.id).state == "uploaded":
            self.store.move_study(study.id, "uploading")
        report.pulled += 1
        log.info("raw file %s: held until now, fits study %s after more of its images arrived", row.name,
                 study.study_uid)

    # ---- one file -----------------------------------------------------------------

    def _pull(self, scanner: ScannerFiles, folder: str, row: File, study: Study | None, matches: list[Study],
              report: PullReport) -> bool:
        if study is not None:
            directory = self.site.staging_dir / study.project / study.study_uid / "RRDF"
        else:
            directory = self.site.staging_dir / UNASSIGNED / folder
        directory.mkdir(parents=True, exist_ok=True)
        stop = int(self.site.disk.stop_free_gb * 1024 ** 3)
        if self.free_bytes(directory) - (row.size or 0) < stop:
            self.store.raise_flag("DISK_LOW", "The Pi's disk is nearly full, so raw data is not being fetched. "
                                  "Uploads free space as they finish.", level="alert")
            return False
        self.store.clear_flags("DISK_LOW")

        final = directory / row.name
        if final.exists() and study is not None:
            study, directory = None, self.site.staging_dir / UNASSIGNED / folder
            directory.mkdir(parents=True, exist_ok=True)
            final = directory / row.name
        if final.exists():
            self.store.raise_flag("RAW_NAME_CLASH", f"Raw file {row.name} from scanner folder {folder} has the same "
                                  "name as a file already on the Pi. It was not fetched.", level="alert",
                                  ref=f"{folder}/{row.name}")
            return False

        part = directory / f".{row.name}.part"
        digest = hashlib.sha256()
        written = 0
        try:
            with scanner.open(folder, row.name) as src, open(part, "wb") as dst:
                while block := src.read(_BLOCK):
                    dst.write(block)
                    digest.update(block)
                    written += len(block)
                dst.flush()
                os.fsync(dst.fileno())
            if written != row.size:
                raise ScannerError(f"{row.name}: got {written} of {row.size} bytes")
            os.link(part, final)  # never overwrites
        finally:
            part.unlink(missing_ok=True)
        _fsync_dir(directory)

        sha = digest.hexdigest()
        if study is not None:
            self.store.move_file(row.id, "pulled", study_id=study.id, path=str(final), sha256=sha)
            self.store.clear_flags("RAW_FILE_UNASSIGNED", study_id=study.id)
            current = self.store.study(study.id)
            if current.state == "uploaded":
                self.store.move_study(study.id, "uploading")
            report.pulled += 1
            log.info("raw file %s: fetched for study %s", row.name, study.study_uid)
            return True

        f = self.store.move_file(row.id, "unassigned", path=str(final), sha256=sha)
        why = ("its scanner folder matches more than one session" if len(matches) > 1
               else "its time doesn't fit the session's time window")
        self.store.raise_flag("UNASSIGNED_RAW", f"Raw file {row.name} from scanner folder {folder} is kept on the Pi "
                              f"but not uploaded: {why}. Please contact your Hyperwheel admin.", file_id=f.id,
                              level="hold")
        for m in matches:
            self.store.raise_flag("RAW_FILE_UNASSIGNED", "A raw file in this session's scanner folder could not be "
                                  "matched to it and is held on the Pi.", study_id=m.id)
        report.unassigned += 1
        log.warning("raw file %s from %s: not assigned (%s). Kept and held", row.name, folder, why)
        return True


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class SshScanner:
    """The scanner's RRDF folder over SSH: listings with ``ls``, downloads with the SCP protocol.

    The scanner doesn't offer SFTP (it closes the connection when SFTP starts),
    so this does what the old pipeline did: run ``ls`` and ``scp -f`` on the
    scanner. Only reads; nothing on the scanner is changed. Folder and file names
    are checked before they go into a command, and quoted. The password is never
    logged.

    Any host key is accepted (see hostkeys.py): ``host_key(host, key type, key
    bytes)`` is called with the key the scanner presented, before the password
    is sent. No keys are loaded into paramiko, which keeps only one key per
    address and would refuse the second of two scanners on the same address.
    """

    def __init__(self, host: str, port: int, username: str, password: Callable[[], str],
                 host_key: Callable[[str, str, bytes], None], rrdf_dir: str = "RRDF", timeout_s: float = 20.0,
                 sock=None):
        import paramiko

        if not re.fullmatch(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*", rrdf_dir) or ".." in rrdf_dir.split("/"):
            raise ScannerError("unexpected raw-data folder in the site settings")
        self._paramiko = paramiko
        self.rrdf_dir = rrdf_dir
        self.timeout_s = timeout_s
        self._client = paramiko.SSHClient()

        class Remember(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                try:
                    host_key(hostname, key.get_name(), key.asbytes())
                except Exception as e:  # e.g. the state database is busy: try again next round
                    log.warning("scanner key could not be recorded (%s). Not connecting this time",
                                type(e).__name__)
                    raise paramiko.SSHException("scanner key not recorded") from None

        try:
            self._client.set_missing_host_key_policy(Remember())
            self._client.connect(host, port=port, username=username, password=password(), look_for_keys=False,
                                 allow_agent=False, timeout=timeout_s, banner_timeout=timeout_s,
                                 auth_timeout=timeout_s, sock=sock)
        except paramiko.AuthenticationException:
            self._client.close()
            raise LoginRefused("the scanner refused the raw-data login") from None
        except (paramiko.SSHException, OSError, EOFError) as e:
            self._client.close()
            raise ScannerError(f"SSH to the scanner failed ({type(e).__name__})") from None

    def _channel(self, command: str):
        try:
            chan = self._client.get_transport().open_session(timeout=self.timeout_s)
            chan.settimeout(self.timeout_s)
            chan.exec_command(command)
            return chan
        except (self._paramiko.SSHException, OSError, EOFError, AttributeError) as e:
            raise ScannerError(f"reading the scanner failed ({type(e).__name__})") from None

    def _run(self, command: str) -> str:
        chan = self._channel(command)
        try:
            out = bytearray()
            while chunk := chan.recv(65536):
                out += chunk
            status = chan.recv_exit_status()
        except (self._paramiko.SSHException, OSError, EOFError) as e:
            raise ScannerError(f"reading the scanner failed ({type(e).__name__})") from None
        finally:
            chan.close()
        if status != 0:
            raise ScannerError(f"listing on the scanner failed (exit status {status})")
        return out.decode("utf-8", errors="replace")

    def list_folders(self) -> list[str]:
        return parse_ls_folders(self._run(f"LC_ALL=C ls -1 -p -- {shlex.quote(self.rrdf_dir)}"))

    def list_files(self, folder: str) -> list[tuple[str, int]]:
        if rrdf_folder_time(folder) is None:
            raise ScannerError("unexpected folder name")
        return parse_ls_files(self._run(f"LC_ALL=C ls -ln -- {shlex.quote(f'{self.rrdf_dir}/{folder}')}"))

    def open(self, folder: str, name: str):
        if rrdf_folder_time(folder) is None or not is_rrdf_name(name):
            raise ScannerError("unexpected file name")
        path = shlex.quote(f"{self.rrdf_dir}/{folder}/{name}")
        return ScpReader(self._channel(f"scp -f -- {path}"), name)

    def close(self) -> None:
        self._client.close()


def parse_ls_folders(text: str) -> list[str]:
    """Folder names from ``ls -1 -p`` (folders end in "/")."""
    return [line[:-1] for line in text.splitlines() if line.endswith("/") and "/" not in line[:-1]]


def parse_ls_files(text: str) -> list[tuple[str, int]]:
    """(name, size) of the regular files in ``ls -ln`` output."""
    out = []
    for line in text.splitlines():
        parts = line.split(None, 8)
        if len(parts) == 9 and line.startswith("-") and parts[4].isdigit():
            out.append((parts[8], int(parts[4])))
    return out


class ScpReader:
    """Receives one file with the SCP protocol (the "sink" side of ``scp -f``).

    Reads exactly the size the scanner announces; a short read, an error from
    the scanner or a name other than the one asked for raises ScannerError.
    """

    def __init__(self, chan, name: str):
        self._chan, self._name = chan, name
        self._left = 0
        try:
            self._send(b"\0")
            header = self._line()
            if header[:1] in (b"\x01", b"\x02"):
                raise ScannerError("the scanner refused to send the file")
            m = re.fullmatch(rb"C[0-7]{4} (\d+) (.+)", header)
            if not m or m.group(2).decode("utf-8", "replace") != name:
                raise ScannerError("unexpected answer from scp on the scanner")
            self._left = int(m.group(1))
            self._send(b"\0")
        except BaseException:
            chan.close()
            raise

    def _send(self, data: bytes) -> None:
        try:
            self._chan.sendall(data)
        except (OSError, EOFError) as e:
            raise ScannerError(f"reading the scanner failed ({type(e).__name__})") from None

    def _recv(self, n: int) -> bytes:
        try:
            data = self._chan.recv(n)
        except (OSError, EOFError) as e:
            raise ScannerError(f"reading the scanner failed ({type(e).__name__})") from None
        if not data:
            raise ScannerError(f"{self._name}: the scanner closed the connection early")
        return data

    def _line(self) -> bytes:
        line = bytearray()
        while not line.endswith(b"\n"):
            line += self._recv(1)
            if len(line) > 4096:
                raise ScannerError("unexpected answer from scp on the scanner")
        return bytes(line[:-1])

    @property
    def size(self) -> int:
        return self._left

    def read(self, n: int) -> bytes:
        if self._left == 0:
            return b""
        data = self._recv(min(n, self._left))
        self._left -= len(data)
        if self._left == 0:
            if self._recv(1) != b"\0":
                raise ScannerError(f"{self._name}: scp on the scanner reported an error after the file")
            self._send(b"\0")
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._chan.close()
