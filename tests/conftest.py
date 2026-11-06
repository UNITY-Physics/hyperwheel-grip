from __future__ import annotations

import hashlib
import io
import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ImplicitVRLittleEndian, generate_uid

sys.path.insert(0, str(Path(__file__).parent))

from fakegrip import FakeGrip  # noqa: E402
from fakes import FakeOrthanc  # noqa: E402

from hyperwheel.config import Project, Routing, SiteConfig, UploadSettings  # noqa: E402
from hyperwheel.keys import Credentials, KeyStore  # noqa: E402
from hyperwheel.state import Store, Study  # noqa: E402
from hyperwheel.uploader import Uploader  # noqa: E402

PROJECT = "SiteA-Sandbox"
PATIENT_ID = "PID-DO-NOT-LOG-4711"
PATIENT_NAME = "DONOTLOG^NAME"


def make_dicom(study_uid: str, series_uid: str, *, sop_uid: str | None = None, acq: str = "133429",
               description: str = "T2", patient_id: str | None = PATIENT_ID, patient_name: str = PATIENT_NAME,
               operators: str = "site_a/sandbox", study_date: str = "20261027",
               study_time: str = "132802", image_type: tuple[str, ...] = ("ORIGINAL", "PRIMARY", "T2"),
               sex: str = "F") -> tuple[str, bytes]:
    """A small synthetic MR image (implicit VR little endian, like the Swoop's)."""
    sop = sop_uid or generate_uid()
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.4"
    meta.MediaStorageSOPInstanceUID = sop
    meta.TransferSyntaxUID = ImplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = sop
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    if patient_id is not None:
        ds.PatientID = patient_id
    ds.PatientName = patient_name
    ds.PatientSex = sex
    ds.StudyDate = study_date
    ds.StudyTime = study_time
    ds.AcquisitionDateTime = f"{study_date}{acq}.000000"
    ds.ImageType = list(image_type)
    ds.OperatorsName = operators
    ds.Modality = "MR"
    ds.SeriesDescription = description
    ds.Rows = ds.Columns = 16
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 12, 11, 0
    ds.PixelData = os.urandom(16 * 16 * 2)
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return sop, buf.getvalue()


@dataclass
class Env:
    tmp: Path
    fake: FakeGrip
    store: Store
    site: SiteConfig
    routing: Routing
    keys: KeyStore
    orthanc: FakeOrthanc
    sleeps: list[float] = field(default_factory=list)
    now: list[float] = field(default_factory=lambda: [1_800_000_000.0])

    def clock(self) -> float:
        return self.now[0]

    def advance(self, seconds: float) -> None:
        self.now[0] += seconds

    def uploader(self, **overrides) -> Uploader:
        site = replace(self.site, upload=replace(self.site.upload, **overrides)) if overrides else self.site
        return Uploader(self.store, site, self.routing, self.keys, self.orthanc, sleep=self.sleeps.append,
                        clock=self.clock, rng=lambda: 0.5)

    def stage_study(self, *, n_dicom: int = 2, raw_sizes: tuple[int, ...] = (3 * 1024 * 1024 + 17,),
                    state: str = "ready") -> Study:
        study_uid = generate_uid()
        study = self.store.add_study(
            study_uid, state=state, project=PROJECT, study_date="20261027", study_time="132802",
            patient_id=PATIENT_ID, patient_name=PATIENT_NAME, operators_name="site_a/sandbox",
            orthanc_id=f"orthanc-{study_uid[-6:]}",
        )
        base = self.site.staging_dir / PROJECT / study_uid
        series = generate_uid()
        for i in range(n_dicom):
            self.add_dicom(study, series)
        (base / "RRDF").mkdir(parents=True, exist_ok=True)
        for i, size in enumerate(raw_sizes):
            self.add_raw(study, f"fse_fast_t2_rrdf_20261027_13{40 + i:02d}00.h5", size)
        return study

    def add_dicom(self, study: Study, series: str, *, sop_uid: str | None = None) -> int:
        sop, data = make_dicom(study.study_uid, series, sop_uid=sop_uid)
        path = self.site.staging_dir / PROJECT / study.study_uid / "DICOM" / f"{sop}.dcm"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        f = self.store.add_file(kind="dicom", name=path.name, state="staged", study_id=study.id, path=str(path),
                                sop_uid=sop, series_uid=series, orthanc_instance_id=f"inst-{sop[-8:]}",
                                size=len(data), sha256=hashlib.sha256(data).hexdigest())
        return f.id

    def add_raw(self, study: Study, name: str, size: int, *, data: bytes | None = None) -> int:
        data = data if data is not None else os.urandom(size)
        path = self.site.staging_dir / PROJECT / study.study_uid / "RRDF" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        f = self.store.add_file(kind="rrdf", name=name, state="pulled", study_id=study.id, path=str(path),
                                scanner_folder=f"rrdf_20261027_1328{study.id:02d}", size=len(data),
                                sha256=hashlib.sha256(data).hexdigest())
        return f.id


@pytest.fixture
def fake():
    server = FakeGrip(clients={"site-a-sandbox-client": "s3cret-value-not-for-logs"}).start()
    yield server
    server.stop()


@pytest.fixture
def env(tmp_path, fake):
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir(mode=0o700)
    site = SiteConfig(
        site="test", staging_dir=tmp_path / "stagingArea", state_db=tmp_path / "state.db", keys_dir=keys_dir,
        test_mode=True,
        upload=UploadSettings(chunk_bytes=1024 * 1024, timeout_s=5, attempts_per_call=3),
    )
    routing = Routing(projects={PROJECT: Project(PROJECT, ("sandbox",), fake.base_url, fake.token_url)})
    keys = KeyStore(keys_dir)
    keys.write(PROJECT, Credentials("site-a-sandbox-client", "s3cret-value-not-for-logs"))
    e = Env(tmp=tmp_path, fake=fake, store=None, site=site, routing=routing, keys=keys,  # type: ignore[arg-type]
            orthanc=FakeOrthanc())
    e.store = Store(site.state_db, clock=e.clock)
    yield e
    e.store.close()
