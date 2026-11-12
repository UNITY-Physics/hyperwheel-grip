"""The uploader against GRIP's hosted mock API, with synthetic data only.

Skipped unless these are set:
    HYPERWHEEL_GRIP_MOCK_BASE      the mock's API base URL (https://.../api)
    HYPERWHEEL_GRIP_MOCK_KEY_FILE  a mode-600 JSON file {"client_id": ..., "client_secret": ...}
Optional:
    HYPERWHEEL_GRIP_MOCK_REGION    a GRIP region (e.g. region-a) for the two store calls. Unset: no region.

Faults are injected with the mock's X-Mock-Fault header. GRIP's API has no
sessions: DICOM by STOW-RS, raw files by PUT /v1/studies/{uid}/raw/{name}, and
GET /v1/studies/{uid} shows what GRIP has.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import PROJECT, Env, FakeOrthanc, make_dicom
from pydicom.uid import generate_uid
from fakes import FakeScanner
from test_partial_send import FIRST, RAW_FIRST, RAW_SECOND, SECOND, pull, send
from test_rawpull import FOLDER, raw
from hyperwheel.config import Project, Routing, SiteConfig, UploadSettings, check_region, check_url
from hyperwheel.keys import KeyStore
from hyperwheel.state import Store
from hyperwheel.uploader import Uploader

BASE = os.environ.get("HYPERWHEEL_GRIP_MOCK_BASE")
KEY_FILE = os.environ.get("HYPERWHEEL_GRIP_MOCK_KEY_FILE")
REGION = os.environ.get("HYPERWHEEL_GRIP_MOCK_REGION") or None

pytestmark = [
    pytest.mark.grip_mock,
    pytest.mark.skipif(not (BASE and KEY_FILE), reason="GRIP mock settings not given"),
]


@pytest.fixture
def mock_env(tmp_path):
    base = check_url(BASE, "HYPERWHEEL_GRIP_MOCK_BASE", test_mode=False)  # HTTPS only
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir(mode=0o700)
    dest = keys_dir / f"{PROJECT}.json"
    shutil.copyfile(KEY_FILE, dest)
    os.chmod(dest, 0o600)
    site = SiteConfig(site="mock-test", staging_dir=tmp_path / "stagingArea", state_db=tmp_path / "state.db",
                      keys_dir=keys_dir, upload=UploadSettings(chunk_bytes=1024 * 1024, timeout_s=60))
    region = check_region(REGION, "HYPERWHEEL_GRIP_MOCK_REGION") if REGION else None
    routing = Routing(projects={PROJECT: Project(PROJECT, ("sandbox",), base, base + "/get/token", region=region)})
    e = Env(tmp=tmp_path, fake=None, store=None, site=site, routing=routing,  # type: ignore[arg-type]
            keys=KeyStore(keys_dir), orthanc=FakeOrthanc())
    e.store = Store(site.state_db, clock=e.clock)
    yield e
    e.store.close()


def uploader(env: Env, faults: dict[tuple[str, int], str] | None = None, **settings) -> Uploader:
    """faults: {(path fragment, attempt): status} -> X-Mock-Fault on that attempt of the first matching call."""
    pending = dict(faults or {})

    def extra_headers(method: str, path: str, attempt: int) -> dict[str, str]:
        for (fragment, n), status in list(pending.items()):
            if fragment in path and attempt == n:
                del pending[(fragment, n)]
                return {"X-Mock-Fault": status}
        return {}

    site = replace(env.site, upload=replace(env.site.upload, **settings)) if settings else env.site
    return Uploader(env.store, site, env.routing, env.keys, env.orthanc, clock=env.clock, sleep=time.sleep,
                    extra_headers=extra_headers)


def test_full_upload_with_retries_and_token_renewal(mock_env):
    study = mock_env.stage_study(n_dicom=2, raw_sizes=(2 * 1024 * 1024 + 11,))
    # A real scanner name pattern, with a dot before "_rrdf_".
    mock_env.add_raw(study, "T2map_CALIPR_UNITY_v1100_beta1.user_rrdf_20261027_141317.h5", 700_000)
    paths = [Path(f.path) for f in mock_env.store.files(study.id)]
    report = uploader(mock_env, {("/dicomweb/studies/", 1): "503", ("/raw/", 1): "401"}).run_once()
    files = mock_env.store.files(study.id)
    assert [f.state for f in files] == ["deleted"] * 4, [(f.name, f.state, f.last_error) for f in files]
    assert report.confirmed == 4
    assert not any(p.exists() for p in paths)
    assert mock_env.store.study(study.id).state == "uploaded"
    on_grip = grip_study(mock_env, study.study_uid)
    assert on_grip["instanceCount"] == 2 and on_grip["rawFileCount"] == 2
    names = {r["name"]: r for r in on_grip["rawFiles"]}
    assert "T2map_CALIPR_UNITY_v1100_beta1.user_rrdf_20261027_141317.h5" in names  # name kept, dot and all
    assert all(r["complete"] for r in names.values())
    assert not any("series" in k.lower() for r in on_grip["rawFiles"] for k in r)  # raw files have no series


def test_raw_upload_resumes_after_failures(mock_env):
    study = mock_env.stage_study(n_dicom=1, raw_sizes=(3 * 1024 * 1024 + 5,))
    raw = mock_env.store.files(study.id, kind="rrdf")[0]
    # The second chunk fails, and the first run makes only one try per call.
    calls = {"n": 0}

    def fail_second_put(method, path, attempt):
        if method == "PUT":
            calls["n"] += 1
            if calls["n"] == 2:
                return {"X-Mock-Fault": "500"}
        return {}

    site = replace(mock_env.site, upload=replace(mock_env.site.upload, attempts_per_call=1))
    first = Uploader(mock_env.store, site, mock_env.routing, mock_env.keys, mock_env.orthanc, clock=mock_env.clock,
                     extra_headers=fail_second_put)
    first.run_once()
    raw = mock_env.store.file(raw.id)
    assert raw.state == "retry_wait" and raw.upload_offset == 1024 * 1024, (raw.state, raw.last_error)

    mock_env.advance(3600)
    uploader(mock_env).run_once()
    raw = mock_env.store.file(raw.id)
    assert raw.state == "deleted", (raw.state, raw.last_error)


def grip_study(env: Env, study_uid: str) -> dict:
    return uploader(env)._api(env.routing.projects[PROJECT]).study_status(study_uid)


def test_nothing_on_grip_yet_is_404(mock_env):
    assert grip_study(mock_env, "1.2.826.0.1.3680043.8.498.1") is None


def test_resend_overwrites_dicom_and_raw(mock_env):
    study = mock_env.stage_study(n_dicom=1, raw_sizes=(600_000,))
    rrdf = mock_env.store.files(study.id, kind="rrdf")[0]
    up = uploader(mock_env)
    api = up._api(mock_env.routing.projects[PROJECT])
    api.check_key()
    # GRIP already has a complete raw file of that name with other content and size.
    assert api.raw_put(study.study_uid, rrdf.name, os.urandom(4000), 0, 4000).complete
    up.run_once()
    dicom = mock_env.store.files(study.id, kind="dicom")[0]
    assert [f.state for f in mock_env.store.files(study.id)] == ["deleted", "deleted"]
    assert [r["size"] for r in grip_study(mock_env, study.study_uid)["rawFiles"]] == [600_000]

    # The scanner sends the same SOP Instance UID again with new content.
    _, data = make_dicom(study.study_uid, dicom.series_uid, sop_uid=dicom.sop_uid, description="changed, longer")
    Path(dicom.path).parent.mkdir(parents=True, exist_ok=True)
    Path(dicom.path).write_bytes(data)
    mock_env.store.move_file(dicom.id, "staged", sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    uploader(mock_env).run_once()
    assert mock_env.store.file(dicom.id).state == "deleted"
    on_grip = grip_study(mock_env, study.study_uid)
    assert [i["size"] for i in on_grip["instances"] if i["sopInstanceUid"] == dicom.sop_uid] == [len(data)]
    assert on_grip["instanceCount"] == 1 and on_grip["rawFileCount"] == 1


def test_late_files_join_the_study(mock_env):
    study = mock_env.stage_study(n_dicom=1, raw_sizes=(300_000,))
    uploader(mock_env).run_once()
    assert mock_env.store.study(study.id).state == "uploaded"
    mock_env.add_dicom(study, generate_uid())
    mock_env.add_raw(study, "flair_iso_rrdf_20261027_134807.h5", 250_000)
    mock_env.store.move_study(study.id, "uploading")
    uploader(mock_env).run_once()
    assert all(f.state == "deleted" for f in mock_env.store.files(study.id))
    on_grip = grip_study(mock_env, study.study_uid)
    assert on_grip["instanceCount"] == 2 and on_grip["rawFileCount"] == 2


def test_exam_sent_in_two_parts_is_one_study_on_grip(mock_env, tmp_path):

    scanner = FakeScanner(tmp_path / "scanner")
    uid = generate_uid()
    send(mock_env, uid, FIRST)
    for name in RAW_FIRST:
        raw(scanner, FOLDER, name, size=300_000)
    pull(mock_env, scanner)
    assert uploader(mock_env).run_once().confirmed == 4

    mock_env.advance(30 * 60)
    send(mock_env, uid, SECOND)
    for name in RAW_SECOND:
        raw(scanner, FOLDER, name, size=300_000)
    _, second = pull(mock_env, scanner)
    assert second.pulled == 8  # only the new raw files
    assert uploader(mock_env).run_once().confirmed == 5 + 8
    on_grip = grip_study(mock_env, uid)
    assert on_grip["instanceCount"] == 7 and on_grip["rawFileCount"] == 10
    assert all(r["complete"] for r in on_grip["rawFiles"])


def test_partial_of_another_size_on_grip_is_sent_again_in_full(mock_env):
    study = mock_env.stage_study(n_dicom=1, raw_sizes=(1_200_000,))
    rrdf = mock_env.store.files(study.id, kind="rrdf")[0]
    up = uploader(mock_env)
    api = up._api(mock_env.routing.projects[PROJECT])
    api.check_key()
    # Leave a partial upload of that name with another size on GRIP.
    result = api.raw_put(study.study_uid, rrdf.name, b"x" * 1000, 0, 5000)
    assert result.status == 200 and not result.complete
    up.run_once()
    assert mock_env.store.file(rrdf.id).state == "deleted"
    on_grip = grip_study(mock_env, study.study_uid)
    assert [(r["size"], r["complete"]) for r in on_grip["rawFiles"]] == [(1_200_000, True)]
    assert not mock_env.store.open_flags(study_id=study.id)


def test_grip_stores_in_the_region_and_its_checksums_match_the_pis(mock_env):
    study = mock_env.stage_study(n_dicom=2, raw_sizes=(2 * 1024 * 1024 + 7,))
    hashes = {(f.sop_uid or f.name): (f.sha256, f.size) for f in mock_env.store.files(study.id)}
    uploader(mock_env).run_once()
    files = mock_env.store.files(study.id)
    assert [f.state for f in files] == ["deleted"] * 3, [(f.name, f.state, f.last_error) for f in files]
    on_grip = grip_study(mock_env, study.study_uid)
    if REGION:
        assert on_grip.get("region") == REGION
    listed = {i["sopInstanceUid"]: (i["sha256"], i["size"]) for i in on_grip["instances"]}
    listed.update({r["name"]: (r["sha256"], r["size"]) for r in on_grip["rawFiles"]})
    assert listed == hashes


def test_a_wrong_copy_on_grip_is_found_and_sent_again(mock_env):
    """A partial upload of other bytes is on GRIP (as after a restart part-way): resuming makes a wrong copy, the
    checksum shows it, and the file is sent again in full."""
    study = mock_env.stage_study(n_dicom=1, raw_sizes=(2 * 1024 * 1024 + 3,))
    uploader(mock_env).run_once()  # the DICOM, and the RRDF file once
    raw = mock_env.store.files(study.id, kind="rrdf")[0]
    assert raw.state == "deleted"
    name, size = "second_rrdf_20261027_141500.h5", 2 * 1024 * 1024 + 3
    data = os.urandom(size)
    api = uploader(mock_env)._api(mock_env.routing.projects[PROJECT])
    api.raw_put(study.study_uid, name, os.urandom(1024 * 1024), 0, size)  # another version's first part
    raw_id = mock_env.add_raw(study, name, size, data=data)
    mock_env.store.move_study(study.id, "uploading")
    uploader(mock_env).run_once()
    raw = mock_env.store.file(raw_id)
    assert raw.state == "deleted", (raw.state, raw.last_error)
    on_grip = {r["name"]: r for r in grip_study(mock_env, study.study_uid)["rawFiles"]}
    assert on_grip[name]["sha256"] == hashlib.sha256(data).hexdigest() and on_grip[name]["size"] == size
