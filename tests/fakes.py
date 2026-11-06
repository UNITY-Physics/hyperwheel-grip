"""Local fakes of Orthanc's REST API and the scanner's RRDF folder, for unit tests."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pydicom
from pydicom.multival import MultiValue

from hyperwheel.orthanc import OrthancError, OrthancNotFound
from hyperwheel.rawpull import LoginRefused, ScannerError


def simplified_tags(data: bytes) -> dict[str, str]:
    """Top-level tags the way Orthanc's /simplified-tags shows them."""
    ds = pydicom.dcmread(io.BytesIO(data), stop_before_pixels=True)
    out = {}
    for element in ds:
        if not element.keyword or element.VR == "SQ":
            continue
        value = element.value
        out[element.keyword] = "\\".join(str(v) for v in value) if isinstance(value, MultiValue) else str(value)
    return out


class FakeOrthanc:
    def __init__(self):
        self.instances: dict[str, tuple[bytes, dict]] = {}
        self.studies: dict[str, list[str]] = {}
        self.log: list[dict] = []
        self.deleted: list[str] = []
        self.deleted_studies: list[str] = []
        self.fail = False

    @staticmethod
    def study_id(study_uid: str) -> str:
        return "s-" + hashlib.sha1(study_uid.encode()).hexdigest()[:12]

    def _change(self, kind: str, resource: str, rid: str) -> None:
        self.log.append({"ChangeType": kind, "ResourceType": resource, "ID": rid, "Seq": len(self.log) + 1})

    def receive(self, data: bytes) -> str:
        tags = simplified_tags(data)
        iid = "i-" + hashlib.sha1(tags["SOPInstanceUID"].encode()).hexdigest()[:12]
        sid = self.study_id(tags["StudyInstanceUID"])
        self.instances[iid] = (data, tags)
        self.studies.setdefault(sid, [])
        if iid not in self.studies[sid]:
            self.studies[sid].append(iid)
        self._change("NewInstance", "Instance", iid)
        return iid

    def stable(self, study_uid: str) -> None:
        self._change("StableStudy", "Study", self.study_id(study_uid))

    # ---- the API ---------------------------------------------------------------------

    def changes(self, since: int, limit: int = 100) -> dict:
        items = [c for c in self.log if c["Seq"] > since][:limit]
        last = items[-1]["Seq"] if items else since
        return {"Changes": items, "Done": last >= len(self.log), "Last": last}

    def instance_tags(self, iid: str) -> dict:
        if iid not in self.instances:
            raise OrthancNotFound(iid)
        return dict(self.instances[iid][1])

    def instance_file(self, iid: str) -> bytes:
        if iid not in self.instances:
            raise OrthancNotFound(iid)
        return self.instances[iid][0]

    def study_instances(self, sid: str) -> list[str]:
        if sid not in self.studies:
            raise OrthancNotFound(sid)
        return list(self.studies[sid])

    def delete_instance(self, iid: str) -> None:
        if self.fail:
            raise OrthancError("Orthanc did not answer (ConnectionError)")
        self.instances.pop(iid, None)
        for members in self.studies.values():
            if iid in members:
                members.remove(iid)
        self.deleted.append(iid)

    def delete_study(self, sid: str) -> None:
        if self.fail:
            raise OrthancError("Orthanc did not answer (ConnectionError)")
        for iid in self.studies.pop(sid, []):
            self.instances.pop(iid, None)
        self.deleted_studies.append(sid)


class FakeScanner:
    """A local folder laid out like the scanner's home: RRDF/rrdf_<date>_<time>/<files>."""

    def __init__(self, root: Path):
        self.root = Path(root) / "RRDF"
        self.root.mkdir(parents=True, exist_ok=True)
        self.fail = False
        self.refuse_login = False
        self.attempts = 0
        self.connections = 0
        self.reads: list[str] = []

    def add(self, folder: str, name: str, data: bytes) -> Path:
        path = self.root / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def connect(self) -> "FakeScanner":
        self.attempts += 1
        if self.refuse_login:
            raise LoginRefused("the scanner refused the raw-data login")
        if self.fail:
            raise ScannerError("SSH to the scanner failed (TimeoutError)")
        self.connections += 1
        return self

    def list_folders(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def list_files(self, folder: str) -> list[tuple[str, int]]:
        return sorted((p.name, p.stat().st_size) for p in (self.root / folder).iterdir() if p.is_file())

    def open(self, folder: str, name: str):
        self.reads.append(f"{folder}/{name}")
        return open(self.root / folder / name, "rb")

    def close(self) -> None:
        pass
