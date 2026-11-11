"""The raw-data pull and the raw-data-to-session rule."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from conftest import PROJECT, make_dicom
from fakes import FakeScanner
from pydicom.uid import generate_uid

from hyperwheel.organiser import Organiser
from hyperwheel.rawpull import RawPuller

# A phantom study: series (description, acquisition time, image type) and the
# raw files in its scanner folder, as on the scanner.
PHANTOM_SERIES = [
    ("Localizer Auto Align", "133251", ("ORIGINAL", "PRIMARY", "LOCALIZER")),
    ("DWI b0", "133429", ("ORIGINAL", "PRIMARY", "DIFFUSION")),
    ("ADC", "134747", ("DERIVED", "PRIMARY", "DIFFUSION")),
    ("DWI", "134747", ("DERIVED", "PRIMARY", "DIFFUSION")),
    ("FLAIR ISO", "134807", ("ORIGINAL", "PRIMARY", "FLUID")),
    ("FLAIR ISO", "134807", ("ORIGINAL", "PRIMARY", "FLUID")),
    ("FLAIR ISO", "134807", ("ORIGINAL", "PRIMARY", "FLUID")),
    ("T2 AXI Fast", "135907", ("ORIGINAL", "PRIMARY", "T2")),
    ("T2 COR Fast", "140200", ("ORIGINAL", "PRIMARY", "T2")),
    ("T2 SAG Fast", "141031", ("ORIGINAL", "PRIMARY", "T2")),
    ("T2 Map CALIPR UNITY v1100 Beta1", "141317", ("ORIGINAL", "PRIMARY", "T2")),
]
PHANTOM_RAW = [
    "T2map_CALIPR_UNITY_v1100_beta1.user_rrdf_20261027_141317.h5",
    "dwi_xyz_v1_b0_rrdf_20261027_133429.h5",
    "dwi_xyz_v1_x_rrdf_20261027_134400.h5",
    "dwi_xyz_v1_y_rrdf_20261027_133957.h5",
    "dwi_xyz_v1_z_rrdf_20261027_133555.h5",
    "flair_iso_rrdf_20261027_134807.h5",
    "fse_fast_t2_cor_rrdf_20261027_140200.h5",
    "fse_fast_t2_rrdf_20261027_135907.h5",
    "fse_fast_t2_sag_rrdf_20261027_141031.h5",
]
FOLDER = "rrdf_20261027_132802"


@pytest.fixture
def scanner(tmp_path):
    return FakeScanner(tmp_path / "scanner")


def puller(env, scanner, free=10 ** 12) -> RawPuller:
    return RawPuller(env.store, env.site, scanner.connect, clock=env.clock, free_bytes=lambda p: free)


def phantom_study(env, *, operators="site_a/sandbox", study_time="132802", series=PHANTOM_SERIES) -> str:
    uid = generate_uid()
    for description, acq, image_type in series:
        _, data = make_dicom(uid, generate_uid(), acq=acq, description=description, image_type=image_type,
                             operators=operators, study_time=study_time)
        env.orthanc.receive(data)
    env.orthanc.stable(uid)
    Organiser(env.store, env.site, env.routing, env.orthanc, clock=env.clock).follow()
    return uid


def raw(scanner, folder, name, size=2000) -> bytes:
    data = os.urandom(size)
    scanner.add(folder, name, data)
    return data


def test_phantom_study_raw_files_all_assigned_unchanged(env, scanner):
    uid = phantom_study(env)
    contents = {name: raw(scanner, FOLDER, name) for name in PHANTOM_RAW}
    p = puller(env, scanner)
    first = p.pull_once()
    assert first.waiting == 9 and first.pulled == 0  # sizes are checked twice before pulling
    second = p.pull_once()
    assert second.pulled == 9
    study = env.store.study_by_uid(uid)
    files = env.store.files(study.id, kind="rrdf")
    assert sorted(f.name for f in files) == sorted(PHANTOM_RAW) and {f.state for f in files} == {"pulled"}
    for f in files:
        assert Path(f.path) == env.site.staging_dir / PROJECT / uid / "RRDF" / f.name
        assert Path(f.path).read_bytes() == contents[f.name]
        assert f.sha256 == hashlib.sha256(contents[f.name]).hexdigest()
    assert env.store.open_flags(study_id=study.id) == []
    assert p.pull_once().pulled == 0 and len(scanner.reads) == 9  # never pulled twice


def test_a_series_with_no_raw_file_isnt_flagged_and_what_is_there_uploads(env, scanner):
    """Raw files aren't matched to series: whatever raw data is there goes to the study, and nothing is flagged."""
    uid = phantom_study(env)
    present = [name for name in PHANTOM_RAW if "flair" not in name]
    for name in present:
        raw(scanner, FOLDER, name)
    p = puller(env, scanner)
    p.pull_once()
    p.pull_once()
    study = env.store.study_by_uid(uid)
    files = env.store.files(study.id, kind="rrdf")
    assert sorted(f.name for f in files) == sorted(present) and {f.state for f in files} == {"pulled"}
    assert env.store.open_flags(study_id=study.id) == []


def test_every_h5_file_in_the_folder_is_the_studys_whatever_its_name_says(env, scanner):
    uid = phantom_study(env)
    phantom_study(env, study_time="142500", series=[("T2 AXI Fast", "142600", ("ORIGINAL", "PRIMARY", "T2"))])
    names = ["fse_fast_t2_rrdf_20261027_151400.h5",  # long after the last acquisition
             "fse_fast_t2_rrdf_20261027_132801.h5",  # before the study started
             "fse_fast_t2_rrdf_20261027_142700.h5",  # after the next study began
             "a scan with no time.h5"]
    for name in names:
        raw(scanner, FOLDER, name)
    raw(scanner, FOLDER, "notes.txt")  # not raw data
    p = puller(env, scanner)
    p.pull_once()
    report = p.pull_once()
    study = env.store.study_by_uid(uid)
    files = env.store.files(study.id, kind="rrdf")
    assert report.pulled == 4 and sorted(f.name for f in files) == sorted(names)
    assert {f.state for f in files} == {"pulled"}
    assert all(Path(f.path) == env.site.staging_dir / PROJECT / uid / "RRDF" / f.name for f in files)
    assert not [f for f in env.store.open_flags() if f.code.startswith(("RAW", "UNASSIGNED"))]
    assert not (env.site.staging_dir / "_unassigned").exists()


def test_a_name_grip_would_refuse_stays_on_the_scanner_and_is_flagged(env, scanner):
    phantom_study(env)
    raw(scanner, FOLDER, "x..y.h5")
    raw(scanner, FOLDER, "ok_rrdf_20261027_133500.h5")
    p = puller(env, scanner)
    p.pull_once()
    p.pull_once()
    assert [f.name for f in env.store.files(kind="rrdf")] == ["ok_rrdf_20261027_133500.h5"]
    flags = [f for f in env.store.open_flags() if f.code == "RAW_NAME_UNEXPECTED"]
    assert len(flags) == 1 and "x..y.h5" in flags[0].ref and "stays on the scanner" in flags[0].detail


def test_raw_files_an_earlier_release_held_as_unassigned_move_into_their_study(env, scanner):
    uid = phantom_study(env)
    held = env.site.staging_dir / "_unassigned" / FOLDER / "fse_fast_t2_rrdf_20261027_151400.h5"
    held.parent.mkdir(parents=True)
    held.write_bytes(b"raw")
    row = env.store.add_file(kind="rrdf", name=held.name, state="seen", study_id=None, scanner_folder=FOLDER,
                             size=3, path=None)
    env.store.move_file(row.id, "unassigned", path=str(held))
    env.store.raise_flag("UNASSIGNED_RAW", "held", file_id=row.id, level="hold")
    study = env.store.study_by_uid(uid)
    env.store.raise_flag("RAW_FILE_UNASSIGNED", "held", study_id=study.id)
    other = env.store.add_file(kind="rrdf", name="lost_rrdf_20261001_090000.h5", state="seen", study_id=None,
                               scanner_folder="rrdf_20261001_090000", size=1)
    env.store.move_file(other.id, "unassigned", path=str(env.tmp / "lost.h5"))
    puller(env, scanner).pull_once()
    moved = env.store.file(row.id)
    assert moved.state == "pulled" and moved.study_id == study.id
    assert Path(moved.path) == env.site.staging_dir / PROJECT / uid / "RRDF" / held.name
    assert Path(moved.path).read_bytes() == b"raw" and not held.exists()
    assert not (env.site.staging_dir / "_unassigned").exists()
    assert not [f for f in env.store.open_flags() if f.code in ("UNASSIGNED_RAW", "RAW_FILE_UNASSIGNED")]
    assert env.store.file(other.id).state == "unassigned"  # no study for its folder: left as it was


def test_unrouted_and_unknown_exams_are_never_pulled(env, scanner):
    phantom_study(env, operators="hospital/clinical")
    raw(scanner, FOLDER, "fse_fast_t2_rrdf_20261027_135907.h5")
    raw(scanner, "rrdf_20261027_090000", "fse_fast_t2_rrdf_20261027_090100.h5")
    phantom_study(env, study_time="160000", series=[("T2", "160100", ("ORIGINAL", "PRIMARY", "T2"))])
    p = puller(env, scanner)
    p.pull_once()
    p.pull_once()
    assert env.store.files(kind="rrdf") == [] and scanner.reads == []


def test_growing_file_waits_until_its_size_settles(env, scanner):
    phantom_study(env)
    name = "fse_fast_t2_rrdf_20261027_135907.h5"
    raw(scanner, FOLDER, name, size=1000)
    p = puller(env, scanner)
    p.pull_once()
    raw(scanner, FOLDER, name, size=5000)
    assert p.pull_once().pulled == 0
    assert p.pull_once().pulled == 1
    assert env.store.files(kind="rrdf")[0].size == 5000


def test_study_still_receiving_waits(env, scanner):
    uid = generate_uid()
    _, data = make_dicom(uid, generate_uid(), acq="133429")
    env.orthanc.receive(data)
    Organiser(env.store, env.site, env.routing, env.orthanc, clock=env.clock).follow()
    raw(scanner, FOLDER, "dwi_xyz_v1_b0_rrdf_20261027_133429.h5")
    p = puller(env, scanner)
    p.pull_once()
    p.pull_once()
    assert env.store.files(kind="rrdf") == []


def test_late_raw_file_reopens_an_uploaded_study(env, scanner):
    uid = phantom_study(env)
    study = env.store.study_by_uid(uid)
    env.store.move_study(study.id, "uploading")
    env.store.move_study(study.id, "uploaded")
    raw(scanner, FOLDER, "fse_fast_t2_rrdf_20261027_135907.h5")
    p = puller(env, scanner)
    p.pull_once()
    p.pull_once()
    assert env.store.study(study.id).state == "uploading"


def test_deleted_raw_file_still_on_the_scanner_is_never_pulled_again(env, scanner):
    phantom_study(env)
    raw(scanner, FOLDER, "fse_fast_t2_rrdf_20261027_135907.h5")
    p = puller(env, scanner)
    p.pull_once()
    p.pull_once()
    f = env.store.files(kind="rrdf")[0]
    for state in ("sending", "confirmed", "deleted"):
        env.store.move_file(f.id, state)
    Path(f.path).unlink()
    p.pull_once()
    p.pull_once()
    assert env.store.file(f.id).state == "deleted" and not Path(f.path).exists() and len(scanner.reads) == 1


def test_scanner_unreachable_alerts_after_30_minutes_and_clears(env, scanner):
    phantom_study(env)
    scanner.fail = True
    p = puller(env, scanner)
    p.pull_once()
    assert not env.store.open_flags()
    env.advance(31 * 60)
    p.pull_once()
    assert [(f.code, f.level) for f in env.store.open_flags() if f.study_id is None] == [("RAW_PULL_FAILING", "alert")]
    scanner.fail = False
    p.pull_once()
    assert not [f for f in env.store.open_flags() if f.code == "RAW_PULL_FAILING"]



def test_refused_login_alerts_at_once_and_is_not_retried_for_15_minutes(env, scanner):
    """Repeated failed logins could lock the raw-data account on the scanner."""
    phantom_study(env)
    raw(scanner, FOLDER, PHANTOM_RAW[0])
    scanner.refuse_login = True
    p = puller(env, scanner)
    p.pull_once()
    flags = [f for f in env.store.open_flags() if f.code == "RAW_LOGIN_REFUSED"]
    assert len(flags) == 1 and flags[0].level == "alert"
    assert flags[0].detail.startswith(
        "The scanner refused the raw-data login, most likely because its software was upgraded or it's missing "
        "the RRDF entitlement. Set the new password with: hyperwheel scanner password. If that doesn't help, "
        "tell your Hyperwheel admin.")
    for _ in range(20):  # a local round every 15 s for 5 minutes: no new tries
        env.advance(15)
        p.pull_once()
    assert scanner.attempts == 1
    env.advance(15 * 60)
    p.pull_once()
    assert scanner.attempts == 2  # one try after 15 minutes
    scanner.refuse_login = False  # the right login is set
    env.advance(15 * 60)
    p.pull_once()
    p.pull_once()
    assert not [f for f in env.store.open_flags() if f.code == "RAW_LOGIN_REFUSED"]
    assert env.store.rrdf_file(FOLDER, PHANTOM_RAW[0]).state == "pulled"

def test_low_disk_stops_pulling(env, scanner):
    phantom_study(env)
    raw(scanner, FOLDER, "fse_fast_t2_rrdf_20261027_135907.h5")
    p = puller(env, scanner, free=3 * 1024 ** 3)
    p.pull_once()
    p.pull_once()
    assert env.store.files(kind="rrdf")[0].state == "seen"
    assert "DISK_LOW" in {f.code for f in env.store.open_flags()}


def test_no_scanner_folder_isnt_flagged(env, scanner):
    uid = phantom_study(env)
    p = puller(env, scanner)
    p.pull_once()
    env.advance(60 * 60)
    p.pull_once()
    study = env.store.study_by_uid(uid)
    assert env.store.open_flags(study_id=study.id) == []
    assert study.raw_checked_at is not None  # complete without raw data


def test_raw_data_flags_from_an_earlier_release_are_cleared(env, scanner):
    from hyperwheel.organiser import Organiser
    uid = phantom_study(env)
    study = env.store.study_by_uid(uid)
    env.store.raise_flag("RAW_FOLDER_NOT_FOUND", "an old flag", study_id=study.id)
    env.store.raise_flag("RAW_MISSING_FOR_SERIES", "an old flag", study_id=study.id)
    env.store.raise_flag("BLANK_PATIENT_ID", "another kind of flag", study_id=study.id)
    Organiser(env.store, env.site, env.routing, env.orthanc, clock=env.clock).housekeeping()
    assert [f.code for f in env.store.open_flags(study_id=study.id)] == ["BLANK_PATIENT_ID"]


def test_no_raw_pull_when_disabled(env, scanner):
    from dataclasses import replace
    phantom_study(env)
    raw(scanner, FOLDER, "fse_fast_t2_rrdf_20261027_135907.h5")
    env.site = replace(env.site, rrdf=replace(env.site.rrdf, enabled=False))
    p = puller(env, scanner)
    p.pull_once()
    p.pull_once()
    assert scanner.connections == 0


def test_end_to_end_orthanc_to_grip(env, scanner):
    """Scanner -> Orthanc -> organiser -> raw pull -> uploader -> everything on GRIP, nothing left on the Pi."""
    uid = phantom_study(env)
    for name in PHANTOM_RAW:
        raw(scanner, FOLDER, name, size=300_000)
    p = puller(env, scanner)
    p.pull_once()
    p.pull_once()
    report = env.uploader().run_once()
    assert report.confirmed == 11 + 9
    study = env.store.study_by_uid(uid)
    assert study.state == "uploaded"
    assert env.orthanc.instances == {}
    assert not [p for p in env.site.staging_dir.rglob("*") if p.is_file()]
    session = env.fake.study_for(uid)
    assert len(session.stored) == 11 and sorted(session.raw) == sorted(PHANTOM_RAW)
    assert all(r.complete for r in session.raw.values())
