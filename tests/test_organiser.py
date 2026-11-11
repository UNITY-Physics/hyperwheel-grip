"""The organiser: Orthanc's change log -> studies, routing, staging and checks."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from conftest import PATIENT_ID, PROJECT, make_dicom
from pydicom.uid import generate_uid

from hyperwheel.config import Project, Routing
from hyperwheel.organiser import CURSOR, Organiser


def organiser(env, **site_changes) -> Organiser:
    site = replace(env.site, **site_changes) if site_changes else env.site
    return Organiser(env.store, site, env.routing, env.orthanc, clock=env.clock)


def scan(env, n=3, *, study_uid=None, stable=True, **kw) -> str:
    study_uid = study_uid or generate_uid()
    series = generate_uid()
    for i in range(n):
        _, data = make_dicom(study_uid, series, acq=f"1334{10 + i:02d}", **kw)
        env.orthanc.receive(data)
    if stable:
        env.orthanc.stable(study_uid)
    return study_uid


def codes(env, study):
    return {f.code for f in env.store.open_flags(study_id=study.id)}


def test_routed_study_is_staged_unchanged_and_ready(env):
    uid = scan(env)
    assert organiser(env).follow() == 4
    study = env.store.study_by_uid(uid)
    assert study.state == "ready" and study.project == PROJECT
    assert study.last_acquisition == "2026-10-27T13:34:12"
    files = env.store.files(study.id, kind="dicom")
    assert len(files) == 3
    for f in files:
        path = Path(f.path)
        assert path == env.site.staging_dir / PROJECT / uid / "DICOM" / f"{f.sop_uid}.dcm"
        original = env.orthanc.instances[f.orthanc_instance_id][0]
        assert path.read_bytes() == original and f.sha256 == hashlib.sha256(original).hexdigest()
    assert env.store.cursor(CURSOR) == 4
    assert organiser(env).follow() == 0  # nothing new


def test_unrouted_login_is_deleted_and_never_staged(env):
    uid = scan(env, operators="hospital/clinical")
    organiser(env).follow()
    study = env.store.study_by_uid(uid)
    assert study.state == "purged" and study.project is None
    assert study.patient_id is None and study.patient_name is None
    assert env.orthanc.deleted_studies == [env.orthanc.study_id(uid)]
    assert env.store.files(study.id) == []
    assert not (env.site.staging_dir).exists() or not any(env.site.staging_dir.rglob("*.dcm"))


def test_login_without_slash_is_unrouted(env):
    uid = scan(env, operators="admin")
    organiser(env).follow()
    assert env.store.study_by_uid(uid).state == "purged"


def test_mixed_logins_hold_the_study(env):
    uid = generate_uid()
    scan(env, n=1, study_uid=uid, stable=False)
    scan(env, n=1, study_uid=uid, operators="hospital/clinical")
    organiser(env).follow()
    study = env.store.study_by_uid(uid)
    assert study.state == "held"
    flags = env.store.open_flags(study_id=study.id)
    assert [(f.code, f.level) for f in flags] == [("MIXED_LOGINS", "hold")]
    assert env.store.files(study.id) == [] and env.orthanc.deleted_studies == []


def test_bad_uid_is_held_in_orthanc(env):
    _, data = make_dicom("1.2.3.abc", generate_uid())
    iid = env.orthanc.receive(data)
    env.orthanc.stable("1.2.3.abc")
    organiser(env).follow()
    assert env.store.studies() == []
    flags = env.store.open_flags()
    assert [(f.code, f.level, f.ref) for f in flags] == [("BAD_UID", "hold", iid)]
    assert iid in env.orthanc.instances


def test_late_instance_reopens_the_study_and_is_staged(env):
    uid = scan(env, n=2)
    organiser(env).follow()
    study = env.store.study_by_uid(uid)
    for state in ("uploading", "uploaded"):
        env.store.move_study(study.id, state)
    _, data = make_dicom(uid, generate_uid(), acq="141317")
    env.orthanc.receive(data)
    organiser(env).follow()
    assert env.store.study(study.id).state == "receiving"
    env.orthanc.stable(uid)
    organiser(env).follow()
    study = env.store.study(study.id)
    assert study.state == "ready" and study.last_acquisition == "2026-10-27T14:13:17"
    assert len(env.store.files(study.id, kind="dicom")) == 3


def test_instance_sent_again_after_upload_is_staged_again(env):
    uid = scan(env, n=1)
    organiser(env).follow()
    study = env.store.study_by_uid(uid)
    f = env.store.files(study.id)[0]
    for state in ("sending", "confirmed", "deleted"):
        env.store.move_file(f.id, state)
    Path(f.path).unlink()
    env.store.move_study(study.id, "uploading")
    env.orthanc.receive(env.orthanc.instances[f.orthanc_instance_id][0])
    env.orthanc.stable(uid)
    organiser(env).follow()
    assert env.store.file(f.id).state == "staged" and Path(f.path).exists()


def test_patient_id_problems_are_flagged_but_sent_unchanged(env):
    uid = scan(env, n=1, patient_id=" R2-0042")
    organiser(env).follow()
    study = env.store.study_by_uid(uid)
    assert study.state == "ready" and study.patient_id == " R2-0042"
    assert codes(env, study) == {"PATIENT_ID_SPACES"}

    blank = scan(env, n=1, patient_id="")
    organiser(env).follow()
    assert codes(env, env.store.study_by_uid(blank)) == {"BLANK_PATIENT_ID"}
    assert env.store.study_by_uid(blank).state == "ready"
    assert "MISSING_" in env.store.open_flags(study_id=env.store.study_by_uid(blank).id)[0].detail


def test_patient_id_0_is_flagged_as_blank_and_says_what_the_scanner_sent(env):
    """Swoop software 9.0.1 sends 0 when the field is left empty."""
    uid = scan(env, n=1, patient_id="0")
    organiser(env).follow()
    study = env.store.study_by_uid(uid)
    assert study.state == "ready" and study.patient_id == "0"  # flagged, not held, sent unchanged
    flags = env.store.open_flags(study_id=study.id)
    assert [f.code for f in flags] == ["BLANK_PATIENT_ID"]
    assert flags[0].detail.startswith("The Patient ID is blank (the scanner sent 0).")
    assert "MISSING_" not in flags[0].detail


def test_patient_id_pattern_and_name(env):
    env.routing = Routing(projects={PROJECT: replace(env.routing.projects[PROJECT], patient_id_pattern=r"R2-\d{4}")})
    uid = scan(env, n=1, patient_id="R2-042", patient_name="R2-0042^Baby")
    organiser(env).follow()
    assert codes(env, env.store.study_by_uid(uid)) == {"PATIENT_ID_PATTERN", "PATIENT_ID_NAME_DIFFER"}


def test_phantom_checks_only_with_a_phantom_id(env):
    uid = scan(env, n=1, patient_id="R2-0001", description="Temperature Vial Measurement")
    organiser(env).follow()
    assert codes(env, env.store.study_by_uid(uid)) == set()

    uid = scan(env, n=1, patient_id="R2-0002", description="Temperature Vial Measurement")
    organiser(env, phantom_patient_id="PH-0001").follow()
    assert codes(env, env.store.study_by_uid(uid)) == {"PHANTOM_UNDER_PARTICIPANT_ID"}

    uid = scan(env, n=1, patient_id="PH-0001", description="T2_AXI")
    organiser(env, phantom_patient_id="PH-0001").follow()
    assert codes(env, env.store.study_by_uid(uid)) == {"PARTICIPANT_UNDER_PHANTOM_ID"}


def test_same_id_twice_a_day_and_different_sex(env):
    scan(env, n=1, patient_id="R2-0007", sex="F")
    organiser(env).follow()
    uid = scan(env, n=1, patient_id="R2-0007", sex="M", study_time="150000")
    organiser(env).follow()
    assert codes(env, env.store.study_by_uid(uid)) == {"SAME_ID_SAME_DAY", "DEMOGRAPHICS_DIFFER"}


def test_routing_change_keeps_the_first_project(env):
    uid = scan(env, n=1)
    organiser(env).follow()
    study = env.store.study_by_uid(uid)
    env.store.move_study(study.id, "receiving")
    env.routing = Routing(projects={"Other": Project("Other", ("sandbox",), "https://h/api", "https://h/t"),
                                    PROJECT: Project(PROJECT, ("old",), "https://h/api", "https://h/t")})
    env.orthanc.stable(uid)
    organiser(env).follow()
    study = env.store.study(study.id)
    assert study.project == PROJECT and "ROUTING_CHANGED" in codes(env, study)


def test_change_for_an_instance_orthanc_no_longer_has_is_skipped(env):
    uid = scan(env, n=1, stable=False)
    env.orthanc.instances.clear()
    env.orthanc.studies.clear()
    env.orthanc.stable(uid)
    assert organiser(env).follow() == 2
    assert env.store.cursor(CURSOR) == 2


def test_failed_purge_is_retried(env):
    uid = scan(env, n=1, operators="hospital/clinical")
    env.orthanc.fail = True
    organiser(env).follow()
    assert env.store.study_by_uid(uid).state == "unrouted"
    env.orthanc.fail = False
    organiser(env).housekeeping()
    assert env.store.study_by_uid(uid).state == "purged"


def test_archive_after_watch_period_drops_patient_data(env):
    uid = scan(env, n=1)
    organiser(env).follow()
    study = env.store.study_by_uid(uid)
    env.store.move_study(study.id, "uploading")
    env.store.move_study(study.id, "uploaded")
    organiser(env).housekeeping()
    assert env.store.study(study.id).state == "uploaded"
    env.advance(8 * 86400)
    organiser(env).housekeeping()
    study = env.store.study(study.id)
    assert study.state == "archived" and study.patient_id is None and study.patient_name is None


def test_patient_data_not_logged(env, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    scan(env, n=2)
    scan(env, n=1, operators="hospital/clinical")
    organiser(env).follow()
    assert PATIENT_ID not in caplog.text and "DONOTLOG" not in caplog.text


def test_a_study_deleted_before_its_login_had_a_dataset_goes_through_when_sent_again(env):
    uid = scan(env, operators="site_a/later")
    organiser(env).follow()
    study = env.store.study_by_uid(uid)
    assert study.state == "purged" and env.store.files(study.id) == []
    later = Project("SiteA-Later", ("later",), env.fake.base_url, env.fake.token_url)
    env.routing = Routing(projects={**env.routing.projects, later.name: later})
    scan(env, study_uid=uid, operators="site_a/later")  # the scanner sends it again
    organiser(env).follow()
    study = env.store.study_by_uid(uid)
    assert study.state == "ready" and study.project == "SiteA-Later" and study.patient_id == PATIENT_ID
    assert len(env.store.files(study.id, kind="dicom")) == 3
    assert "SENT_AGAIN" in codes(env, study)  # only what was sent again goes up: check it's complete
    organiser(env).housekeeping()
    assert env.store.study_by_uid(uid).state == "ready"  # not purged again


def test_a_re_sent_study_isnt_purged_while_it_arrives(env):
    uid = scan(env, operators="site_a/later")
    organiser(env).follow()
    assert env.store.study_by_uid(uid).state == "purged"
    later = Project("SiteA-Later", ("later",), env.fake.base_url, env.fake.token_url)
    env.routing = Routing(projects={**env.routing.projects, later.name: later})
    scan(env, n=2, study_uid=uid, operators="site_a/later", stable=False)  # the first images of the re-send
    organiser(env).follow()
    deleted = list(env.orthanc.deleted_studies)
    organiser(env).housekeeping()  # runs while the rest is still arriving
    assert env.orthanc.deleted_studies == deleted and env.store.study_by_uid(uid).state == "unrouted"
    scan(env, n=3, study_uid=uid, operators="site_a/later")  # the rest, then stable
    organiser(env).follow()
    study = env.store.study_by_uid(uid)
    assert study.state == "ready" and len(env.store.files(study.id, kind="dicom")) == 5


def test_a_re_sent_study_whose_login_still_has_no_dataset_is_purged_once_stable(env):
    uid = scan(env, operators="hospital/clinical")
    organiser(env).follow()
    scan(env, n=1, study_uid=uid, operators="hospital/clinical")
    organiser(env).follow()
    organiser(env).housekeeping()
    assert env.store.study_by_uid(uid).state == "purged"
    assert env.orthanc.deleted_studies.count(env.orthanc.study_id(uid)) == 2


def test_a_re_sent_unrouted_study_that_never_gets_stable_is_purged_after_an_hour_if_its_login_has_no_dataset(env):
    from hyperwheel.organiser import STUCK_S
    uid = scan(env, operators="hospital/clinical")
    organiser(env).follow()
    scan(env, n=1, study_uid=uid, operators="hospital/clinical", stable=False)  # its StableStudy never comes
    organiser(env).follow()
    organiser(env).housekeeping()
    assert env.store.study_by_uid(uid).state == "unrouted"
    env.advance(STUCK_S + 1)
    organiser(env).housekeeping()
    assert env.store.study_by_uid(uid).state == "purged"


def test_a_re_sent_study_that_never_finishes_arriving_for_a_dataset_raises_an_alert_and_is_kept(env):
    from hyperwheel.organiser import STUCK_S
    uid = scan(env, operators="site_a/later")
    organiser(env).follow()
    later = Project("SiteA-Later", ("later",), env.fake.base_url, env.fake.token_url)
    env.routing = Routing(projects={**env.routing.projects, later.name: later})
    scan(env, n=1, study_uid=uid, operators="site_a/later", stable=False)  # its StableStudy never comes
    organiser(env).follow()
    env.advance(STUCK_S + 1)
    organiser(env).housekeeping()
    assert env.store.study_by_uid(uid).state == "unrouted"  # kept, in Orthanc
    flags = [f for f in env.store.open_flags() if f.code == "SENT_AGAIN_UNFINISHED"]
    assert len(flags) == 1 and flags[0].level == "alert" and "Send it again from the scanner" in flags[0].detail
    scan(env, n=3, study_uid=uid, operators="site_a/later")  # sent again, complete this time
    organiser(env).follow()
    assert env.store.study_by_uid(uid).state == "ready"
    assert not [f for f in env.store.open_flags() if f.code == "SENT_AGAIN_UNFINISHED"]
