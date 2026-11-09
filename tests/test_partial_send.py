"""An exam sent in parts ("Send Selected Sequences" mid-exam, the rest later).

Both parts have the same StudyInstanceUID, and all their raw files are in the
same scanner folder. The Pi must fetch only the raw files that are new in the
folder, and everything must end up in one staging folder and one GRIP session.
"""

from __future__ import annotations

import sqlite3

import pytest
from conftest import PATIENT_ID, PATIENT_NAME, PROJECT, make_dicom
from fakes import FakeScanner
from pydicom.uid import generate_uid
from test_rawpull import FOLDER, puller, raw

from hyperwheel.dashboard import HEARTBEAT, snapshot
from hyperwheel.organiser import Organiser

FIRST = [("DWI b0", "133429", ("ORIGINAL", "PRIMARY", "DIFFUSION")),
         ("FLAIR ISO", "134807", ("ORIGINAL", "PRIMARY", "FLUID"))]
SECOND = [("T2 AXI Fast", "135907", ("ORIGINAL", "PRIMARY", "T2")),
          ("T2 COR Fast", "140200", ("ORIGINAL", "PRIMARY", "T2")),
          ("T2 SAG Fast", "141031", ("ORIGINAL", "PRIMARY", "T2")),
          ("T2 Map CALIPR UNITY v1100 Beta1", "141317", ("ORIGINAL", "PRIMARY", "T2")),
          ("MT FA60 AXI", "142000", ("ORIGINAL", "PRIMARY", "OTHER"))]
RAW_FIRST = ["dwi_xyz_v1_b0_rrdf_20261027_133429.h5", "flair_iso_rrdf_20261027_134807.h5"]
RAW_SECOND = ["dwi_xyz_v1_x_rrdf_20261027_134400.h5", "dwi_xyz_v1_y_rrdf_20261027_133957.h5",
              "dwi_xyz_v1_z_rrdf_20261027_133555.h5", "fse_fast_t2_rrdf_20261027_135907.h5",
              "fse_fast_t2_cor_rrdf_20261027_140200.h5", "fse_fast_t2_sag_rrdf_20261027_141031.h5",
              "T2map_CALIPR_UNITY_v1100_beta1.user_rrdf_20261027_141317.h5",
              "MT_FA60_AXI_UNITY_v1011.user_rrdf_20261027_142000.h5"]


@pytest.fixture
def scanner(tmp_path):
    return FakeScanner(tmp_path / "scanner")


def send(env, uid: str, series) -> None:
    """The scanner sends some of the exam's series; Orthanc then reports the study stable."""
    for description, acq, image_type in series:
        _, data = make_dicom(uid, generate_uid(), acq=acq, description=description, image_type=image_type,
                             operators="site_a/sandbox", study_time="132802")
        env.orthanc.receive(data)
    env.orthanc.stable(uid)
    Organiser(env.store, env.site, env.routing, env.orthanc, clock=env.clock).follow()


def pull(env, scanner):
    p = puller(env, scanner)
    return p.pull_once(), p.pull_once()  # sizes are checked on two listings before fetching


def test_second_part_fetches_and_uploads_only_the_new_raw_files(env, scanner):
    uid = generate_uid()
    send(env, uid, FIRST)
    for name in RAW_FIRST:
        raw(scanner, FOLDER, name, size=300_000)
    _, second = pull(env, scanner)
    assert second.pulled == 2
    assert env.uploader().run_once().confirmed == 2 + 2
    study = env.store.study_by_uid(uid)
    assert study.state == "uploaded"
    assert not [p for p in env.site.staging_dir.rglob("*") if p.is_file()]  # local copies deleted

    # Half an hour later the rest of the exam arrives: 5 more DICOM files, 8 more raw files.
    env.advance(30 * 60)
    send(env, uid, SECOND)
    for name in RAW_SECOND:
        raw(scanner, FOLDER, name, size=300_000)
    first_listing, second_listing = pull(env, scanner)
    assert first_listing.waiting == 8 and first_listing.pulled == 0  # new names wait for a stable size
    assert second_listing.pulled == 8 and second_listing.unassigned == 0
    assert sorted(scanner.reads) == sorted(f"{FOLDER}/{n}" for n in RAW_FIRST + RAW_SECOND)  # each read once

    # Both parts in one staging folder ...
    pending = env.store.files(study.id, states=("staged", "pulled"))
    assert len(pending) == 5 + 8
    base = env.site.staging_dir / PROJECT / uid
    assert {str(p) for p in (base / "DICOM", base / "RRDF")} >= {f.path.rsplit("/", 1)[0] for f in pending}

    # ... and one GRIP session.
    assert env.uploader().run_once().confirmed == 5 + 8
    assert len(env.fake.studies) == 1
    session = env.fake.study_for(uid)
    assert len(session.stored) == 7 and sorted(session.raw) == sorted(RAW_FIRST + RAW_SECOND)
    assert all(r.complete for r in session.raw.values())
    assert env.store.study(study.id).state == "uploaded"

    # The Upload Monitor shows one session with both parts.
    env.store.set_cursor(HEARTBEAT, int(env.clock()))
    cards = snapshot(env.store, env.site, env.routing, now=env.clock(), free_bytes=lambda p: 50 * 1024 ** 3)["studies"]
    assert len(cards) == 1
    assert (cards[0]["dicom"]["total"], cards[0]["dicom"]["on_grip"]) == (7, 7)
    assert cards[0]["raw"]["total"] == 10 and cards[0]["raw"]["on_grip"] == 10


def test_fetched_list_survives_upload_delete_and_the_patient_data_rule(env, scanner):
    uid = generate_uid()
    send(env, uid, FIRST)
    for name in RAW_FIRST:
        raw(scanner, FOLDER, name, size=300_000)
    pull(env, scanner)
    env.uploader().run_once()

    # Past the 7-day watch: patient data is cleared, the list of fetched raw files stays.
    env.advance(8 * 86400)
    Organiser(env.store, env.site, env.routing, env.orthanc, clock=env.clock).housekeeping()
    study = env.store.study_by_uid(uid)
    assert study.state == "archived" and study.patient_id is None and study.patient_name is None
    kept = {f.name: f.size for f in env.store.files(kind="rrdf") if f.scanner_folder == FOLDER}
    assert kept == {n: 300_000 for n in RAW_FIRST}

    # The list holds no patient data.
    db = sqlite3.connect(env.site.state_db)
    rows = db.execute("SELECT * FROM files WHERE kind = 'rrdf'").fetchall()
    db.close()
    text = repr(rows)
    assert PATIENT_ID not in text and PATIENT_NAME not in text

    # A raw file of that exam that is still on the scanner is never fetched again.
    pull(env, scanner)
    assert len(scanner.reads) == 2


def test_fetched_name_with_a_different_size_is_flagged_not_fetched(env, scanner, caplog):
    uid = generate_uid()
    send(env, uid, FIRST)
    raw(scanner, FOLDER, RAW_FIRST[0], size=300_000)
    pull(env, scanner)
    env.uploader().run_once()
    row = env.store.rrdf_file(FOLDER, RAW_FIRST[0])
    assert row.state == "deleted"

    raw(scanner, FOLDER, RAW_FIRST[0], size=310_000)  # same name, different size on the scanner
    pull(env, scanner)
    assert len(scanner.reads) == 1  # not fetched again
    assert env.store.rrdf_file(FOLDER, RAW_FIRST[0]).size == 300_000
    flags = [f for f in env.store.open_flags() if f.code == "RAW_CHANGED_ON_SCANNER"]
    assert len(flags) == 1 and flags[0].level == "alert" and flags[0].study_id == row.study_id
    assert "310000" in flags[0].detail and "300000" in flags[0].detail
    pull(env, scanner)  # still changed: the flag stays, logged only once
    assert len([f for f in env.store.open_flags() if f.code == "RAW_CHANGED_ON_SCANNER"]) == 1
    assert sum("size changed on the scanner" in r.getMessage() for r in caplog.records) == 1


def test_raw_file_held_after_the_first_part_joins_once_the_rest_arrives(env, scanner):
    uid = generate_uid()
    send(env, uid, FIRST[:1])  # only the DWI b0 at 13:34:29: the window ends at 14:34:29
    late = "MT_FA60_AXI_UNITY_v1011.user_rrdf_20261027_144000.h5"
    raw(scanner, FOLDER, late, size=300_000)
    pull(env, scanner)
    held = env.store.rrdf_file(FOLDER, late)
    assert held.state == "unassigned"
    assert any(f.code == "UNASSIGNED_RAW" and f.file_id == held.id for f in env.store.open_flags())

    send(env, uid, SECOND)  # last acquisition now 14:20:00: the window ends at 15:20:00
    pull(env, scanner)
    study = env.store.study_by_uid(uid)
    moved = env.store.file(held.id)
    assert moved.state == "pulled" and moved.study_id == study.id
    assert moved.path == str(env.site.staging_dir / PROJECT / uid / "RRDF" / late)
    assert not (env.site.staging_dir / "_unassigned" / FOLDER / late).exists()
    assert not [f for f in env.store.open_flags() if f.code in ("UNASSIGNED_RAW", "RAW_FILE_UNASSIGNED")]
    assert len(scanner.reads) == 1  # moved on the Pi, not fetched again

    env.uploader().run_once()
    assert late in env.fake.study_for(uid).raw and env.store.file(held.id).state == "deleted"
