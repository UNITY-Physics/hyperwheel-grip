"""Turns what Orthanc receives into studies and staged files.

Follows Orthanc's change log (``/changes``) instead of running inside Lua
callbacks, so Orthanc stays free to receive:

- NewInstance: the study is recorded (or sent back to ``receiving``).
- StableStudy: the study is routed by its login. A login with no routing
  entry is deleted from Orthanc and never uploaded. A routed study's DICOM is
  copied into ``stagingArea/<project>/<StudyInstanceUID>/DICOM/`` and hashed,
  then the checks run and the study becomes ``ready`` for the uploader.

The last change handled is saved after each change, so nothing is skipped or
handled twice after a restart (and handling one twice is harmless).
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from . import checks
from .config import Project, Routing, SiteConfig
from .dicominfo import InstanceInfo, instance_info
from .ids import is_uid
from .orthanc import Orthanc, OrthancError, OrthancNotFound
from .state import Store, Study

log = logging.getLogger("hyperwheel.organiser")

CURSOR = "orthanc_changes"
_ROUTED_ACTIVE = ("checking", "held", "ready", "uploading", "uploaded", "archived")


class Organiser:
    def __init__(self, store: Store, site: SiteConfig, routing: Routing, orthanc: Orthanc, *,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.site = site
        self.routing = routing
        self.orthanc = orthanc
        self.clock = clock

    # ---- change log --------------------------------------------------------------

    def follow(self, limit: int = 100) -> int:
        """Handle every new change in Orthanc's log. Returns how many were handled."""
        handled = 0
        while True:
            since = self.store.cursor(CURSOR)
            body = self.orthanc.changes(since, limit)
            changes = body.get("Changes") or []
            for change in changes:
                self._handle(change)
                self.store.set_cursor(CURSOR, int(change["Seq"]))
                handled += 1
            if body.get("Done", True) or not changes:
                last = body.get("Last")
                if isinstance(last, int) and last > self.store.cursor(CURSOR) and not changes:
                    self.store.set_cursor(CURSOR, last)
                return handled

    def _handle(self, change: dict) -> None:
        kind, resource, rid = change.get("ChangeType"), change.get("ResourceType"), change.get("ID")
        try:
            if kind == "NewInstance" and resource == "Instance":
                self.on_new_instance(rid)
            elif kind == "StableStudy" and resource == "Study":
                self.on_stable_study(rid)
        except OrthancNotFound:
            log.info("%s %s: no longer in Orthanc, skipped", kind, rid)

    # ---- new instance --------------------------------------------------------------

    def on_new_instance(self, instance_id: str) -> None:
        info = instance_info(self.orthanc.instance_tags(instance_id))
        if not self._uids_ok(info, instance_id):
            return
        study = self.store.study_by_uid(info.study_uid)
        if study is None:
            self.store.add_study(
                info.study_uid, state="receiving", study_date=info.study_date, study_time=info.study_time,
                patient_id=info.patient_id, patient_name=info.patient_name, operators_name=info.operators_name,
                patient_sex=info.patient_sex, patient_birth_date=info.patient_birth_date,
                last_acquisition=_iso(info.acquisition),
            )
            log.info("study %s: receiving", info.study_uid)
            return
        fields = {}
        if info.acquisition and (study.last_acquisition is None or _iso(info.acquisition) > study.last_acquisition):
            fields["last_acquisition"] = _iso(info.acquisition)
        if study.state == "purged":
            self.store.move_study(study.id, "unrouted", **fields)
        elif study.state in ("receiving", "unrouted"):
            if fields:
                self.store.update_study(study.id, **fields)
        else:
            self.store.move_study(study.id, "receiving", **fields)
            log.info("study %s: more images arrived. Receiving again", info.study_uid)

    def _uids_ok(self, info: InstanceInfo, instance_id: str) -> bool:
        if is_uid(info.study_uid) and is_uid(info.sop_uid) and is_uid(info.series_uid):
            self.store.clear_flags("BAD_UID", ref=instance_id)
            return True
        self.store.raise_flag(
            "BAD_UID", "An image has an invalid StudyInstanceUID, SeriesInstanceUID or SOP Instance UID. It is "
            "kept in Orthanc and not uploaded. Please contact your Hyperwheel admin.", level="hold", ref=instance_id)
        log.error("Orthanc instance %s: invalid UID. Kept in Orthanc, not uploaded", instance_id)
        return False

    # ---- stable study --------------------------------------------------------------

    def on_stable_study(self, orthanc_study_id: str) -> None:
        instances: list[tuple[str, InstanceInfo]] = []
        for iid in self.orthanc.study_instances(orthanc_study_id):
            info = instance_info(self.orthanc.instance_tags(iid))
            if self._uids_ok(info, iid):
                instances.append((iid, info))
        if not instances:
            return
        infos = [i for _, i in instances]
        uid = infos[0].study_uid
        study = self.store.study_by_uid(uid)
        if study is None:
            first = infos[0]
            study = self.store.add_study(
                uid, state="receiving", study_date=first.study_date, study_time=first.study_time,
                patient_id=first.patient_id, patient_name=first.patient_name, operators_name=first.operators_name,
                patient_sex=first.patient_sex, patient_birth_date=first.patient_birth_date)
        last = max((i.acquisition for i in infos if i.acquisition), default=None)
        fields = {"orthanc_id": orthanc_study_id, "stable_at": self.clock()}
        if last is not None:
            fields["last_acquisition"] = _iso(last)
        if study.state == "receiving":
            study = self.store.move_study(study.id, "stable", **fields)
        else:
            study = self.store.update_study(study.id, **fields)

        mixed = checks.mixed_logins(infos)
        if mixed is not None:
            if study.state == "stable":
                study = self.store.move_study(study.id, "checking")
            if study.state == "checking":
                study = self.store.move_study(study.id, "held")
            checks.apply(self.store, study, [mixed], ["MIXED_LOGINS"])
            log.warning("study %s: images from more than one login. Held", uid)
            return

        login = infos[0].login
        project = self.routing.project_for_login(login)
        if study.project and (project is None or project.name != study.project):
            # The routing changed after this study was routed: keep it with its first project.
            self.store.raise_flag("ROUTING_CHANGED", f"This study's login now routes to "
                                  f"{project.name if project else 'nothing'}. It stays with {study.project}.",
                                  study_id=study.id, level="alert")
            project = self.routing.projects.get(study.project)
        if project is None:
            self._purge(study, login)
            return

        if study.project is None:
            study = self.store.update_study(study.id, project=project.name)
        for iid, info in instances:
            self._stage(study, project, iid, info)

        if study.state == "stable":
            study = self.store.move_study(study.id, "checking")
        if study.state in ("checking", "held"):
            checks.apply(self.store, study, checks.study_issues(self.store, study, infos, self.site, project),
                         [c for c in checks.STUDY_CHECK_CODES if c != "MIXED_LOGINS"])
            self.store.clear_study_flags(study.id, ["MIXED_LOGINS"])
            if not any(f.level == "hold" for f in self.store.open_flags(study_id=study.id) if f.file_id is None):
                study = self.store.move_study(study.id, "ready")
                log.info("study %s: ready to upload to %s", uid, project.name)

    def _purge(self, study: Study, login: str | None) -> None:
        if study.state == "stable":
            study = self.store.move_study(study.id, "unrouted")
        elif study.state == "purged":
            study = self.store.move_study(study.id, "unrouted")
        if study.state != "unrouted":
            return
        log.info("study %s: login %r has no routing entry. Deleting it from the Pi, never uploaded",
                 study.study_uid, login or "")
        try:
            if study.orthanc_id:
                self.orthanc.delete_study(study.orthanc_id)
        except OrthancError as e:
            log.error("study %s: could not delete from Orthanc yet (%s). Will try again", study.study_uid, e)
            return
        self.store.move_study(study.id, "purged", patient_id=None, patient_name=None, patient_sex=None,
                              patient_birth_date=None, operators_name=None)

    def _stage(self, study: Study, project: Project, instance_id: str, info: InstanceInfo) -> None:
        existing = next(iter(f for f in self.store.files(study.id, kind="dicom") if f.sop_uid == info.sop_uid), None)
        if existing is not None and existing.state != "deleted":
            return  # already staged (Orthanc keeps one copy per SOP Instance UID)
        data = self.orthanc.instance_file(instance_id)
        folder = self.site.staging_dir / project.name / study.study_uid / "DICOM"
        path = folder / f"{info.sop_uid}.dcm"
        _write_durably(path, data)
        fields = dict(path=str(path), size=len(data), sha256=hashlib.sha256(data).hexdigest())
        descriptive = dict(series_description=info.series_description, acquisition_time=_iso(info.acquisition),
                           image_type=info.image_type, orthanc_instance_id=instance_id)
        if existing is None:
            self.store.add_file(kind="dicom", name=path.name, state="staged", study_id=study.id,
                                sop_uid=info.sop_uid, series_uid=info.series_uid, **fields, **descriptive)
        else:
            # The scanner sent an instance again after it was uploaded: upload it again.
            self.store.move_file(existing.id, "staged", **fields)
            self.store.update_descriptive(existing.id, **descriptive)

    # ---- housekeeping --------------------------------------------------------------

    def housekeeping(self) -> None:
        """Retry purges that failed, archive studies past the watch period, and clear retired raw-data flags."""
        if cleared := checks.clear_retired_flags(self.store):
            log.info("cleared %d raw-data flag(s) from an earlier release. The Pi no longer raises them", cleared)
        for study in self.store.studies(states=("unrouted",)):
            if study.stable_at is not None:
                self._purge(study, None)
        cutoff = self.clock() - self.site.rrdf.watch_days * 86400
        for study in self.store.studies(states=("uploaded",)):
            if study.stable_at is not None and study.stable_at < cutoff:
                # Nothing of this study is left on the Pi; keep no patient data either.
                self.store.move_study(study.id, "archived", patient_id=None, patient_name=None, patient_sex=None,
                                      patient_birth_date=None)


def _iso(when) -> str | None:
    return when.isoformat(timespec="seconds") if when is not None else None


def _write_durably(path: Path, data: bytes) -> None:
    """Write to a temporary name, sync, then rename into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
