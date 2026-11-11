"""Uploads staged files to GRIP and deletes each one once GRIP has confirmed it.

The delete rule: a local file is deleted only after a successful response for
that file's own upload, i.e.

- DICOM: STOW-RS answers 200 and lists the file's SOP Instance UID as stored
  (00081199);
- RRDF: GRIP accepts the file's last chunk with ``complete: true``.

Anything else (a timeout, an error, a 202/409, a 200 that doesn't list the
file) keeps the file. Transient problems park it in ``retry_wait``; refusals
park it in ``refused`` and raise a flag. A refused file is tried again once a
day (most refusals are something GRIP fixes on its side): if GRIP takes it, it
goes up and its flag clears, and if not, it stays refused and flagged.

Order: one file per request; every study's DICOM before any raw file. A
study's raw files go once GRIP has the study (at least one of its DICOM files
confirmed and none still unsent), as agreed with GRIP. A refused DICOM file
doesn't hold them back. Raw files are addressed by the study's
StudyInstanceUID and the file's own name, with no series.

Slow links: each request gets the time its body needs at the slowest link
rate in the settings. Raw chunks halve (down to ``min_chunk_bytes``) after a
chunk gets no answer or takes longer than ``target_chunk_s``, and grow back
after a run of quick ones. Each chunk is tried once per request, so a retry
goes with the smaller size.

Resume: if GRIP has part of a raw file of the same size, the upload continues
from GRIP's offset; otherwise (nothing there, a different size, or GRIP has
dropped the partial upload) it starts again from 0, which overwrites. Neither
raises a flag. If GRIP already has the whole file, only its last chunk is sent
again, to get this file's own ``complete: true``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

from . import reach
from .config import Project, Routing, SiteConfig
from .grip import AuthFailed, GripApi, Http, KeyRejected, Refused, TokenManager, Transient
from .keys import KeyMissing, KeyProblem, KeyStore
from .orthanc import Orthanc, OrthancError
from .state import DONE_FILE_STATES, PENDING_FILE_STATES, File, Store, Study

log = logging.getLogger("hyperwheel.upload")

STOW_REASONS = {
    0x0110: "GRIP could not process it",
    0x0111: "GRIP already has this SOP Instance UID with different content",
    0xA900: "the file belongs to another study",
    0xC000: "GRIP could not read it as DICOM",
}


class _ProjectBlocked(Exception):
    """Stop uploading this project for this run (its key doesn't work)."""


# A file GRIP refused is tried again after this long.
REFUSED_RETRY_S = 24 * 3600
# GRIP's refusals: those are tried again. A problem with the Pi's own copy (missing, changed, empty) isn't.
_REFUSED_FLAGS = ("STOW_REFUSED", "STOW_FAILED", "RAW_NAME_REFUSED", "RAW_REFUSED", "RAW_CHECKSUM")


@dataclass
class RunReport:
    confirmed: int = 0
    deleted: int = 0
    parked: int = 0
    refused: int = 0


class Uploader:
    def __init__(self, store: Store, site: SiteConfig, routing: Routing, keys: KeyStore, orthanc: Orthanc, *,
                 session_factory: Callable[[], requests.Session] = requests.Session,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, rng: Callable[[], float] = random.random,
                 extra_headers: Callable[[str, str, int], dict[str, str]] | None = None):
        self.store = store
        self.site = site
        self.settings = site.upload
        self.routing = routing
        self.keys = keys
        self.orthanc = orthanc
        self.session_factory = session_factory
        self.sleep = sleep
        self.clock = clock
        self.monotonic = monotonic
        self.rng = rng
        self.extra_headers = extra_headers
        self._apis: dict[str, GripApi] = {}
        self._report = RunReport()
        self._key_ok: set[str] = set()
        self._chunk_bytes = self.settings.chunk_bytes  # shrinks on a slow link, kept between runs
        self._grip_ok_at = 0.0

    # ---- entry point -------------------------------------------------------------

    def run_once(self) -> RunReport:
        """One pass: finish pending deletes, then DICOM for every study, then raw files."""
        self._report = RunReport()
        self._key_ok = set()
        self.finish_confirmed()
        self.retry_refused()
        for kind in ("dicom", "rrdf"):
            blocked: set[str] = set()
            for study in self.store.studies(states=("ready", "uploading", "uploaded", "archived")):
                if study.project in blocked or not self._due(study, kind):
                    continue
                try:
                    self._upload_study(self.store.study(study.id), kind)
                except _ProjectBlocked:
                    blocked.add(study.project)
        for study in self.store.studies(states=("uploading",)):
            self._maybe_done(study)
        return self._report

    def finish_confirmed(self) -> None:
        """Delete every file GRIP has confirmed but that is still on disk (e.g. after a power cut)."""
        for f in self.store.files(states=("confirmed",)):
            self._delete_local(f)

    def retry_refused(self) -> None:
        """Files GRIP refused at least a day ago go back in line to be tried again. Their flags stay until GRIP
        takes them."""
        cutoff = self.clock() - REFUSED_RETRY_S
        for f in self.store.files(states=("refused",)):
            if f.updated_at > cutoff:
                continue
            if not any(fl.code in _REFUSED_FLAGS for fl in self.store.open_flags(study_id=f.study_id)
                       if fl.file_id == f.id):
                continue  # not GRIP's refusal: a problem with the Pi's copy, which trying again won't change
            self.store.move_file(f.id, "staged" if f.kind == "dicom" else "pulled", checksum_resets=0)
            log.info("%s %s: refused by GRIP a day or more ago. Trying it again", _label(f), _ident(f))

    def _confirmed(self, f: File, **fields) -> None:
        self.store.move_file(f.id, "confirmed", last_error=None, **fields)
        for code in _REFUSED_FLAGS:  # refused before, and GRIP has taken it now
            self.store.clear_flags(code, study_id=f.study_id, file_id=f.id)

    # ---- per study ---------------------------------------------------------------

    def _due(self, study: Study, kind: str) -> bool:
        now = self.clock()
        return any(f.state != "retry_wait" or (f.next_attempt_at or 0) <= now
                   for f in self.store.files(study.id, kind=kind, states=PENDING_FILE_STATES))

    def _upload_study(self, study: Study, kind: str) -> None:
        project = self.routing.projects.get(study.project or "")
        if project is None:
            self.store.raise_flag("PROJECT_NOT_ROUTED",
                                  f"The dataset {study.project} is no longer on this Pi. Files are kept.",
                                  study_id=study.id, level="alert")
            return
        self.store.clear_flags("PROJECT_NOT_ROUTED", study_id=study.id)

        if kind == "rrdf" and not self._raw_may_go(study):
            return
        if study.state != "uploading":
            study = self.store.move_study(study.id, "uploading")

        api = self._api(project)
        try:
            api.check_key()  # before any file is touched, so a bad key leaves files as they are
            for f in self._due_files(study, kind):
                if kind == "dicom":
                    self._send_dicom(api, project, study, f)
                else:
                    self._send_raw(api, project, study, f)
        except KeyProblem as e:
            status = "missing" if isinstance(e, KeyMissing) else "unreadable"
            self._key_failed(project, status, str(e))
        except KeyRejected as e:
            self._key_failed(project, "rejected", str(e))
        except AuthFailed as e:
            self._key_failed(project, "rejected", str(e))
        except Transient as e:
            log.warning("study %s: %s. Will try again later", study.study_uid, e)

    def _due_files(self, study: Study, kind: str) -> list[File]:
        now = self.clock()
        return [f for f in self.store.files(study.id, kind=kind, states=PENDING_FILE_STATES)
                if f.state != "retry_wait" or (f.next_attempt_at or 0) <= now]

    def _raw_may_go(self, study: Study) -> bool:
        """Raw files go once GRIP has the study: at least one DICOM file confirmed, none still unsent.

        A refused or parked DICOM file doesn't hold the raw files back; it stays flagged on its own.
        """
        dicom = self.store.files(study.id, kind="dicom")
        if any(f.state in ("staged", "sending") for f in dicom):
            return False
        if any(f.state in DONE_FILE_STATES for f in dicom):
            self.store.clear_flags("RAW_WAITING_FOR_DICOM", study_id=study.id)
            return True
        self.store.raise_flag("RAW_WAITING_FOR_DICOM",
                              "RRDF files wait: no DICOM file of this study is on GRIP yet.", study_id=study.id)
        return False

    def _maybe_done(self, study: Study) -> None:
        files = self.store.files(study.id)
        if files and all(f.state == "deleted" for f in files):
            self.store.move_study(study.id, "uploaded")
            log.info("study %s: every file is on GRIP and deleted locally", study.study_uid)

    # ---- keys --------------------------------------------------------------------

    def _api(self, project: Project) -> GripApi:
        api = self._apis.get(project.name)
        if api is None:
            session = self.session_factory()
            tokens = TokenManager(project.token_url, lambda: self.keys.read(project.name), session,
                                  timeout_s=self.settings.timeout_s, clock=self.monotonic)
            http = Http(project.api_base_url, tokens, session, self.settings, sleep=self.sleep, rng=self.rng,
                        extra_headers=self.extra_headers)
            api = self._apis[project.name] = GripApi(http)
        return api

    def _key_working(self, project: str | None) -> None:
        if project is None or project in self._key_ok:
            return
        self._key_ok.add(project)
        self.store.set_key_status(project, "working")
        self.store.clear_flags("KEY_NOT_WORKING", project=project)

    def check_keys(self) -> dict[str, str]:
        """Ask GRIP for a token with every project's key, without uploading anything.

        Run when the service starts (so after an update) and now and then, so the
        Upload Monitor shows each key as working or not even when nothing is waiting.
        A key that fails pauses only its own project; GRIP not answering changes nothing.
        """
        results = {}
        for project in self.routing.projects.values():
            api = self._api(project)
            try:
                api.check_key(fresh=True)
            except Transient as e:
                results[project.name] = "not checked (GRIP didn't answer)"
                log.info("project %s: key not checked, GRIP didn't answer (%s)", project.name, e)
                continue
            except (KeyProblem, KeyRejected, AuthFailed) as e:
                status = ("missing" if isinstance(e, KeyMissing) else
                          "unreadable" if isinstance(e, KeyProblem) else "rejected")
                try:
                    self._key_failed(project, status, str(e))
                except _ProjectBlocked:
                    pass
                results[project.name] = status
                continue
            self._key_ok.discard(project.name)
            self._key_working(project.name)
            results[project.name] = "working"
            log.info("project %s: GRIP key checked, working", project.name)
        return results

    def _key_failed(self, project: Project, status: str, message: str) -> None:
        self.store.set_key_status(project.name, status, message)
        self.store.raise_flag("KEY_NOT_WORKING", f"GRIP key for {project.label}: {message}. Nothing is uploaded "
                              "for this dataset until the key works. Files are kept.", project=project.name,
                              level="alert")
        log.error("project %s: GRIP key not working (%s). Uploads paused for this project, files kept",
                  project.name, message)
        raise _ProjectBlocked()

    # ---- DICOM ---------------------------------------------------------------------

    def _send_dicom(self, api: GripApi, project: Project, study: Study, f: File) -> None:
        f = self.store.move_file(f.id, "sending", attempts=f.attempts + 1)
        data = self._read_checked(f)
        if data is None:
            return
        try:
            result = api.stow(study.study_uid, data)
        except Transient as e:
            self._park(f, str(e))
            return
        except Refused as e:
            self._refuse(f, "STOW_REFUSED", f"GRIP refused DICOM {f.sop_uid}: {e}")
            return
        except (KeyProblem, KeyRejected, AuthFailed):
            self._park(f, "GRIP key not working")
            raise
        self._key_working(project.name)
        self._grip_answered()

        if result.status == 200 and f.sop_uid in result.stored:
            self._confirmed(f)
            self._report.confirmed += 1
            log.info("DICOM %s of study %s: stored on GRIP", f.sop_uid, study.study_uid)
            self._delete_local(self.store.file(f.id))
        elif f.sop_uid in result.failed:
            reason = result.failed[f.sop_uid]
            meaning = STOW_REASONS.get(reason, "no reason given") if reason is not None else "no reason given"
            code = f"0x{reason:04X}" if reason is not None else "none"
            self._refuse(f, "STOW_FAILED", f"GRIP did not store DICOM {f.sop_uid} (HTTP {result.status}, "
                                           f"reason {code}: {meaning})")
        else:
            self._park(f, f"GRIP answered HTTP {result.status} without listing this file as stored")

    def _read_checked(self, f: File) -> bytes | None:
        """The staged file's bytes, if they still match the hash taken when it was staged."""
        try:
            data = Path(f.path or "").read_bytes()
        except OSError as e:
            self._refuse(f, "LOCAL_FILE_MISSING", f"staged file can't be read ({e.strerror})")
            return None
        if hashlib.sha256(data).hexdigest() != f.sha256:
            self._refuse(f, "LOCAL_FILE_CHANGED", "staged file no longer matches its checksum")
            return None
        return data

    # ---- raw files -----------------------------------------------------------------

    def _send_raw(self, api: GripApi, project: Project, study: Study, f: File) -> None:
        f = self.store.move_file(f.id, "sending", attempts=f.attempts + 1)
        try:
            size = os.stat(f.path or "").st_size
        except OSError as e:
            self._refuse(f, "LOCAL_FILE_MISSING", f"staged RRDF file can't be read ({e.strerror})")
            return
        if size != f.size:
            self._refuse(f, "LOCAL_FILE_CHANGED", "staged RRDF file has a different size from when it was pulled")
            return
        if size == 0:
            self._refuse(f, "RAW_FILE_EMPTY", "RRDF file is empty (kept for a person to check)")
            return
        chunk_bytes = self._chunk_bytes
        last_chunk_start = ((size - 1) // chunk_bytes) * chunk_bytes
        try:
            progress = api.raw_progress(study.study_uid, f.name)
            self._key_working(project.name)
            self._grip_answered()
            same_size = progress is not None and progress.length == size
            if same_size and progress.complete:
                # GRIP has it all, but this run never saw the confirmation:
                # send the last chunk again to get this file's own confirmation.
                offset = last_chunk_start
            elif same_size and 0 < progress.offset < size:
                offset = progress.offset
                log.info("RRDF file %s: GRIP has %d of %d bytes, continuing from there", f.name, offset, size)
            else:
                # Nothing on GRIP, a different size, or GRIP dropped the partial upload:
                # send the whole file again (it overwrites). Not a problem, so no flag.
                offset = 0
            self._send_chunks(api, study, f, size, offset, last_chunk_start)
        except Transient as e:
            self._park(f, str(e))
        except Refused as e:
            if e.invalid_name:
                self._refuse(f, "RAW_NAME_REFUSED", f"GRIP refused the name of RRDF file {f.name} as invalid. RRDF "
                                                    "files are never renamed, so tell your Hyperwheel admin")
            else:
                self._refuse(f, "RAW_REFUSED", f"GRIP refused RRDF file {f.name}: {e}")
        except (KeyProblem, KeyRejected, AuthFailed):
            self._park(f, "GRIP key not working")
            raise

    def _send_chunks(self, api: GripApi, study: Study, f: File, size: int, offset: int,
                     last_chunk_start: int) -> None:
        resets = f.checksum_resets
        resent_last = False
        restarted = offset == 0
        conflicts = failures = good = 0
        steps_left = 3 * (size // self.settings.min_chunk_bytes + 1) + 10  # guards against a server that never moves on
        with open(f.path or "", "rb") as fh:
            while steps_left > 0:
                steps_left -= 1
                if offset >= size:
                    if resent_last:
                        self._park(f, "GRIP has every byte but did not confirm the file")
                        return
                    resent_last = True
                    offset = min(last_chunk_start, max(0, size - self._chunk_bytes))
                fh.seek(offset)
                chunk = fh.read(self._chunk_bytes)
                started = self.monotonic()
                try:
                    result = api.raw_put(study.study_uid, f.name, chunk, offset, size, attempts=1)
                except Transient as e:
                    failures += 1
                    if failures >= self.settings.attempts_per_call:
                        raise
                    if e.no_answer and self._chunk_bytes > self.settings.min_chunk_bytes:
                        self._chunk_bytes = max(self.settings.min_chunk_bytes, self._chunk_bytes // 2)
                        log.warning("RRDF file %s: no answer for a chunk of %d KiB. Trying again, with chunks of "
                                    "at most %d KiB", f.name, len(chunk) // 1024, self._chunk_bytes // 1024)
                    else:
                        log.warning("RRDF file %s: %s. Trying again", f.name, e)
                    self.sleep(e.retry_after if e.retry_after is not None else api.http.backoff(failures))
                    continue
                except Refused as e:
                    if restarted or e.invalid_name:
                        raise
                    # GRIP wouldn't take the rest of an earlier upload: send it in full instead.
                    log.info("RRDF file %s: GRIP refused continuing at byte %d (HTTP %d). Sending it in full",
                             f.name, offset, e.status)
                    restarted, resent_last, offset = True, False, 0
                    self.store.update_file(f.id, upload_offset=0)
                    continue
                self._grip_answered()
                failures = 0
                if result.status == 200:
                    conflicts = 0
                    took = self.monotonic() - started
                    if took > self.settings.target_chunk_s and self._chunk_bytes > self.settings.min_chunk_bytes:
                        self._chunk_bytes = max(self.settings.min_chunk_bytes, self._chunk_bytes // 2)
                        good = 0
                        log.info("RRDF file %s: a chunk of %d KiB took %.0f s. Chunks of at most %d KiB from now",
                                 f.name, len(chunk) // 1024, took, self._chunk_bytes // 1024)
                    elif took <= self.settings.target_chunk_s / 4 and len(chunk) >= self._chunk_bytes:
                        good += 1
                        if good >= 8 and self._chunk_bytes < self.settings.chunk_bytes:
                            self._chunk_bytes = min(self.settings.chunk_bytes, self._chunk_bytes * 2)
                            good = 0
                    else:
                        good = 0
                    if result.complete:
                        self._confirmed(f, upload_offset=size)
                        self._report.confirmed += 1
                        log.info("RRDF file %s of study %s: stored on GRIP", f.name, study.study_uid)
                        self._delete_local(self.store.file(f.id))
                        return
                    new_offset = result.offset if result.offset is not None else offset + len(chunk)
                    if new_offset <= offset and offset != last_chunk_start:
                        self._park(f, "GRIP accepted a chunk but its offset did not move")
                        return
                    offset = new_offset
                    self.store.update_file(f.id, upload_offset=offset)
                elif result.status == 409:
                    conflicts += 1
                    if result.offset is None or conflicts > 3:
                        self._park(f, "GRIP's upload offset keeps disagreeing")
                        return
                    log.info("RRDF file %s: GRIP has %d bytes, continuing from there", f.name, result.offset)
                    offset = result.offset
                    if offset == 0:
                        restarted = True
                    self.store.update_file(f.id, upload_offset=offset)
                else:  # 422: chunk checksum mismatch
                    resets += 1
                    self.store.update_file(f.id, checksum_resets=resets, upload_offset=0)
                    if resets >= 2:
                        self._refuse(f, "RAW_CHECKSUM", f"GRIP reported a checksum mismatch for {f.name} twice")
                        return
                    log.warning("RRDF file %s: checksum mismatch reported by GRIP, starting again from 0", f.name)
                    offset = 0
                    resent_last = False
        self._park(f, "upload did not finish")

    def _grip_answered(self) -> None:
        """Note that GRIP answered: proof that the scanner and the internet work, even when busy."""
        now = self.clock()
        if now - self._grip_ok_at >= 10:
            self._grip_ok_at = now
            self.store.set_cursor(reach.GRIP_ANSWERED, int(now))

    # ---- outcomes ------------------------------------------------------------------

    def _park(self, f: File, why: str) -> None:
        delay = min(self.settings.retry_max_s, self.settings.retry_base_s * 2 ** max(0, f.attempts - 1))
        delay *= 0.5 + self.rng()
        self.store.move_file(f.id, "retry_wait", next_attempt_at=self.clock() + delay, last_error=why)
        self._report.parked += 1
        log.warning("%s %s: %s. Kept, will try again in %.0f s", _label(f), _ident(f), why, delay)

    def _refuse(self, f: File, code: str, detail: str) -> None:
        self.store.move_file(f.id, "refused", last_error=detail)
        self.store.raise_flag(code, f"{detail}. The file is kept on the Pi.", study_id=f.study_id, file_id=f.id,
                              level="alert")
        self._report.refused += 1
        log.error("%s %s: %s. File kept", _label(f), _ident(f), detail)

    def _delete_local(self, f: File) -> None:
        """Delete a confirmed file: the staged copy, then Orthanc's copy, then mark it deleted."""
        if f.state != "confirmed":
            raise AssertionError("only a file GRIP has confirmed may be deleted")
        try:
            if f.path:
                Path(f.path).unlink(missing_ok=True)
            if f.kind == "dicom" and f.orthanc_instance_id:
                self.orthanc.delete_instance(f.orthanc_instance_id)
        except (OSError, OrthancError) as e:
            self.store.raise_flag("LOCAL_DELETE_FAILED", f"On GRIP, but deleting the copy on the Pi failed ({e}).",
                                  study_id=f.study_id, file_id=f.id, level="alert")
            log.error("%s %s: on GRIP, but the local delete failed (%s). Will try again", _label(f), _ident(f), e)
            return
        self.store.move_file(f.id, "deleted")
        self.store.clear_flags("LOCAL_DELETE_FAILED", study_id=f.study_id, file_id=f.id)
        self._report.deleted += 1


def _label(f: File) -> str:
    return "DICOM" if f.kind == "dicom" else "RRDF file"


def _ident(f: File) -> str:
    return (f.sop_uid or "") if f.kind == "dicom" else f.name
