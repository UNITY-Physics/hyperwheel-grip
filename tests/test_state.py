import pytest

from hyperwheel.state import StateError, Store

UID = "1.2.826.0.1.3680043.8.498.1"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


def test_schema_created_and_reopened(tmp_path):
    s = Store(tmp_path / "state.db")
    study = s.add_study(UID, project="P")
    s.close()
    s = Store(tmp_path / "state.db")
    assert s.schema_version == 3
    assert s.study_by_uid(UID).id == study.id
    s.close()


def test_bad_uid_never_stored(store):
    with pytest.raises(StateError):
        store.add_study(" 1.2.3")
    with pytest.raises(StateError):
        store.add_file(kind="dicom", name="x.dcm", state="staged", study_id=None, sop_uid="../x")


def test_study_lifecycle_and_reopening(store):
    s = store.add_study(UID)
    for state in ("stable", "checking", "ready", "uploading", "uploaded", "receiving", "stable", "checking", "ready",
                  "uploading", "uploaded", "archived", "uploading"):
        s = store.move_study(s.id, state)
    with pytest.raises(StateError):
        store.move_study(s.id, "purged")
    u = store.add_study(UID + "1")
    store.move_study(u.id, "stable")
    store.move_study(u.id, "unrouted")
    store.move_study(u.id, "purged")
    with pytest.raises(StateError):
        store.move_study(u.id, "uploading")


def test_file_lifecycle(store):
    s = store.add_study(UID)
    f = store.add_file(kind="dicom", name="a.dcm", state="staged", study_id=s.id, sop_uid=UID + ".1", size=1,
                       sha256="0" * 64)
    with pytest.raises(StateError, match="staged to deleted"):
        store.move_file(f.id, "deleted")
    with pytest.raises(StateError):
        store.move_file(f.id, "confirmed")
    for state in ("sending", "retry_wait", "sending", "confirmed", "deleted"):
        f = store.move_file(f.id, state)
    r = store.add_file(kind="rrdf", name="x_rrdf_20261027_134807.h5", state="seen", study_id=None,
                       scanner_folder="rrdf_20261027_132802")
    for state in ("unassigned", "pulled", "sending", "refused", "pulled", "sending", "confirmed", "deleted"):
        r = store.move_file(r.id, state)
    for state in ("staged", "pulled"):  # a deleted raw file is never pulled or sent again
        with pytest.raises(StateError):
            store.move_file(r.id, state)


def test_resent_dicom_instance_can_be_staged_again(store):
    s = store.add_study(UID)
    f = store.add_file(kind="dicom", name="a.dcm", state="staged", study_id=s.id, sop_uid=UID + ".1")
    for state in ("sending", "confirmed", "deleted", "staged"):
        f = store.move_file(f.id, state)


def test_same_raw_file_recorded_once(store):
    store.add_file(kind="rrdf", name="x_rrdf_20261027_134807.h5", state="seen", study_id=None,
                   scanner_folder="rrdf_20261027_132802")
    with pytest.raises(Exception):
        store.add_file(kind="rrdf", name="x_rrdf_20261027_134807.h5", state="seen", study_id=None,
                       scanner_folder="rrdf_20261027_132802")


def test_flags_deduplicate_and_clear(store):
    s = store.add_study(UID)
    a = store.raise_flag("BLANK_PATIENT_ID", "blank", study_id=s.id)
    b = store.raise_flag("BLANK_PATIENT_ID", "still blank", study_id=s.id)
    assert a.id == b.id and store.open_flags(study_id=s.id)[0].detail == "still blank"
    assert store.clear_flags("BLANK_PATIENT_ID", study_id=s.id) == 1
    assert store.open_flags() == []


def test_patient_data_not_in_study_repr(store):
    s = store.add_study(UID, patient_id="SECRET-ID", patient_name="SECRET^NAME")
    assert "SECRET" not in repr(s)


def test_key_status(store):
    assert store.key_status("P") is None
    store.set_key_status("P", "rejected", "HTTP 401 (invalid_client)")
    assert store.key_status("P")[:2] == ("rejected", "HTTP 401 (invalid_client)")
    with pytest.raises(StateError):
        store.set_key_status("P", "fine")


def test_flag_levels_refs_and_cursor(store):
    a = store.raise_flag("BAD_UID", "x", level="hold", ref="orthanc-1")
    b = store.raise_flag("BAD_UID", "y", level="hold", ref="orthanc-2")
    assert a.id != b.id and {f.level for f in store.open_flags()} == {"hold"}
    store.clear_flags("BAD_UID", ref="orthanc-1")
    assert [f.ref for f in store.open_flags()] == ["orthanc-2"]
    assert store.cursor("orthanc") == 0
    store.set_cursor("orthanc", 42)
    assert store.cursor("orthanc") == 42


def test_upgrade_from_version_1_keeps_data(tmp_path):
    import sqlite3

    from hyperwheel import state
    db = sqlite3.connect(tmp_path / "v1.db", isolation_level=None)
    for statement in state._SCHEMA[0].split(";"):
        if statement.strip():
            db.execute(statement)
    db.execute("PRAGMA user_version = 1")
    db.execute("INSERT INTO studies (study_uid, state, created_at, updated_at) VALUES (?, 'ready', 0, 0)", (UID,))
    db.close()
    s = Store(tmp_path / "v1.db")
    assert s.schema_version == 3 and s.study_by_uid(UID).state == "ready"
    s.close()


# ---- an older release opens records written by a newer one ----------------------------

import re as _re

from hyperwheel import state as _state


def _newer(path):
    """Migrate like a future release would: one more version that only adds."""
    import sqlite3
    db = sqlite3.connect(path)
    db.executescript("""
        ALTER TABLE studies ADD COLUMN future_note TEXT;
        ALTER TABLE files ADD COLUMN future_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE flags ADD COLUMN future_flag TEXT;
        CREATE TABLE future_things (id INTEGER PRIMARY KEY, what TEXT);
    """)
    db.execute(f"PRAGMA user_version = {len(_state._SCHEMA) + 1}")
    db.commit()
    db.close()


def test_older_release_works_with_a_newer_database(tmp_path):
    path = tmp_path / "state.db"
    Store(path).close()
    _newer(path)
    s = Store(path)
    assert s.schema_version == len(_state._SCHEMA) + 1  # never downgraded
    study = s.add_study("1.2.3", state="ready", project="P")
    f = s.add_file(kind="dicom", name="a.dcm", state="staged", study_id=study.id, sop_uid="1.2.3.4")
    s.move_file(f.id, "sending")
    s.raise_flag("X", "detail", study_id=study.id)
    assert [x.state for x in s.files(study.id)] == ["sending"]
    assert s.open_flags(study_id=study.id)[0].code == "X"
    assert s.study_by_uid("1.2.3").project == "P"
    s.close()
    ro = Store(path, readonly=True)
    assert ro.studies()[0].study_uid == "1.2.3"
    ro.close()


def test_rows_in_states_this_release_does_not_know_are_left_alone(tmp_path):
    import sqlite3
    path = tmp_path / "state.db"
    s = Store(path)
    study = s.add_study("1.2.3", state="ready")
    f = s.add_file(kind="dicom", name="a.dcm", state="staged", study_id=study.id, sop_uid="1.2.3.4")
    db = sqlite3.connect(path)
    db.execute("UPDATE files SET state = 'future_state' WHERE id = ?", (f.id,))
    db.execute("UPDATE studies SET state = 'future_state' WHERE id = ?", (study.id,))
    db.commit()
    db.close()
    with pytest.raises(StateError):
        s.move_file(f.id, "sending")
    with pytest.raises(StateError):
        s.move_study(study.id, "uploading")
    assert s.file(f.id).state == "future_state"
    s.close()


def test_migrations_after_the_first_only_add():
    allowed = [
        r"ALTER TABLE \w+ ADD COLUMN \w+ \w+( NOT NULL DEFAULT \S+| DEFAULT \S+( NOT NULL)?)?",
        r"ALTER TABLE \w+ ADD COLUMN \w+ \w+ REFERENCES \w+\(\w+\)",
        r"CREATE TABLE \w+ \(.*\)",
        r"CREATE INDEX \w+ ON \w+\(.*\)",
    ]
    for number, script in enumerate(_state._SCHEMA[1:], start=2):
        for statement in script.split(";"):
            st = " ".join(statement.split())
            if st:
                assert any(_re.fullmatch(a, st, _re.S) for a in allowed), f"version {number}: {st}"
                if st.startswith("ALTER") and "NOT NULL" in st:
                    assert "DEFAULT" in st, f"version {number}: a NOT NULL column needs a default: {st}"
