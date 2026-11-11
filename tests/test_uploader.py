"""The uploader against the local fake GRIP: the delete rule, ordering, retries, resume and key problems."""

from __future__ import annotations

import hashlib
from urllib.parse import quote
import logging
from pathlib import Path

from conftest import PATIENT_ID, PROJECT, make_dicom

from hyperwheel.keys import Credentials


def states(env, study):
    return sorted(f.state for f in env.store.files(study.id))


def test_happy_path_uploads_everything_and_deletes_each_file(env):
    study = env.stage_study(n_dicom=3, raw_sizes=(2_500_000, 1_000_000))
    paths = [Path(f.path) for f in env.store.files(study.id)]

    report = env.uploader().run_once()

    assert report.confirmed == 5 and report.deleted == 5
    assert states(env, study) == ["deleted"] * 5
    assert not any(p.exists() for p in paths)
    assert len(env.orthanc.deleted) == 3
    assert env.store.study(study.id).state == "uploaded"
    on_grip = env.fake.study_for(study.study_uid)
    assert len(on_grip.stored) == 3 and all(r.complete for r in on_grip.raw.values())
    assert sorted(on_grip.raw) == sorted(f.name for f in env.store.files(study.id, kind="rrdf"))
    assert env.store.key_status(PROJECT)[0] == "working"


def test_all_dicom_goes_before_any_raw_file_across_studies(env):
    a = env.stage_study(n_dicom=2, raw_sizes=(1_500_000,))
    b = env.stage_study(n_dicom=2, raw_sizes=(1_500_000,))
    env.uploader().run_once()
    order = [(m, p) for m, p, _ in env.fake.requests if m in ("POST", "PUT") and ("/dicomweb/" in p or "/files/" in p)]
    kinds = ["dicom" if "/dicomweb/" in p else "raw" for _, p in order]
    assert kinds == ["dicom"] * 4 + ["raw"] * (len(kinds) - 4)
    assert env.store.study(a.id).state == env.store.study(b.id).state == "uploaded"


def test_one_dicom_file_per_request(env):
    env.stage_study(n_dicom=3, raw_sizes=())
    env.uploader().run_once()
    stows = env.fake.requests_matching("POST", r"/dicomweb/studies/")
    assert len(stows) == 3


def test_token_renewed_when_it_expires_mid_upload(env):
    study = env.stage_study(n_dicom=2, raw_sizes=(2_500_000,))
    env.fake.add_fault("PUT", r"/v1/studies/.*/raw/.*", "expire_tokens")
    env.uploader().run_once()
    assert states(env, study) == ["deleted"] * 3
    assert env.fake.token_requests == 2


def test_retry_after_is_honoured(env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    env.fake.add_fault("POST", r"/dicomweb/studies/.*", 503, retry_after=5)
    env.uploader().run_once()
    assert 5.0 in env.sleeps
    assert states(env, study) == ["deleted"]


def test_server_errors_and_dropped_connections_are_retried(env):
    study = env.stage_study(n_dicom=1, raw_sizes=(1_200_000,))
    env.fake.add_fault("POST", r"/dicomweb/studies/.*", 500)
    env.fake.add_fault("PUT", r"/v1/studies/.*/raw/.*", "drop")
    env.uploader().run_once()
    assert states(env, study) == ["deleted", "deleted"]
    assert len(env.sleeps) == 2


def test_lost_answer_keeps_the_file_until_a_later_run_confirms_it(env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    f = env.store.files(study.id)[0]
    # GRIP stores the file but the answer never arrives, every try.
    env.fake.add_fault("POST", r"/dicomweb/studies/.*", "drop_after", count=3)
    report = env.uploader().run_once()
    f = env.store.file(f.id)
    assert report.parked == 1 and f.state == "retry_wait"
    assert Path(f.path).exists() and env.orthanc.deleted == []

    env.uploader().run_once()  # not due yet
    assert env.store.file(f.id).state == "retry_wait"
    env.advance(3600)
    env.uploader().run_once()
    assert env.store.file(f.id).state == "deleted" and not Path(f.path).exists()


def test_stow_200_that_does_not_list_the_file_keeps_it(env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    env.fake.add_fault("POST", r"/dicomweb/studies/.*", "stow_unlisted")
    env.uploader().run_once()
    f = env.store.files(study.id)[0]
    assert f.state == "retry_wait" and Path(f.path).exists()
    assert "without listing" in f.last_error


def test_changed_content_overwrites_on_grip(env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    env.uploader().run_once()
    f = env.store.files(study.id)[0]
    _, data = make_dicom(study.study_uid, f.series_uid, sop_uid=f.sop_uid)
    Path(f.path).write_bytes(data)
    env.store.move_file(f.id, "staged", sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    env.uploader().run_once()
    assert env.store.file(f.id).state == "deleted"
    assert env.fake.study_for(study.study_uid).stored[f.sop_uid][0] == hashlib.sha256(data).hexdigest()


def test_late_file_joins_the_study_on_grip(env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    env.uploader().run_once()
    late = env.add_dicom(study, "1.2.3.4")
    env.uploader().run_once()
    assert env.store.file(late).state == "deleted"
    assert len(env.fake.study_for(study.study_uid).stored) == 2


def test_raw_upload_resumes_from_grip_offset(env):
    study = env.stage_study(n_dicom=1, raw_sizes=(5 * 1024 * 1024 + 3,))
    raw = env.store.files(study.id, kind="rrdf")[0]
    # The connection dies on the third chunk, on every try in this run.
    calls = {"n": 0}
    original = env.fake._take_fault

    def third_chunk_drops(method, path):
        if method == "PUT":
            calls["n"] += 1
            if calls["n"] >= 3 and calls["n"] < 6:
                from fakegrip import Fault
                return Fault(method, path, "drop")
        return original(method, path)

    env.fake._take_fault = third_chunk_drops
    env.uploader().run_once()
    raw = env.store.file(raw.id)
    assert raw.state == "retry_wait" and raw.upload_offset == 2 * 1024 * 1024
    assert Path(raw.path).exists()

    env.fake._take_fault = original
    puts_before = len(env.fake.requests_matching("PUT", r"/raw/"))
    env.advance(3600)
    env.uploader().run_once()
    assert env.store.file(raw.id).state == "deleted"
    resumed = env.fake.requests_matching("PUT", r"/raw/")[puts_before]
    assert resumed[2]["Content-Range"].startswith(f"bytes {2 * 1024 * 1024}-")


def test_raw_checksum_reset_once_restarts_twice_refuses(env):
    study = env.stage_study(n_dicom=1, raw_sizes=(1_500_000,))
    env.fake.add_fault("PUT", r"/v1/studies/.*/raw/.*", 422)
    env.uploader().run_once()
    assert states(env, study) == ["deleted", "deleted"]

    study2 = env.stage_study(n_dicom=1, raw_sizes=(1_500_000,))
    env.fake.add_fault("PUT", r"/v1/studies/.*/raw/.*", 422, count=2)
    env.uploader().run_once()
    raw = env.store.files(study2.id, kind="rrdf")[0]
    assert raw.state == "refused" and Path(raw.path).exists()


def test_raw_complete_on_grip_but_not_recorded_gets_its_own_confirmation(env):
    study = env.stage_study(n_dicom=1, raw_sizes=(2_500_000,))
    raw = env.store.files(study.id, kind="rrdf")[0]
    # GRIP stores the last chunk but its answer is lost.
    calls = {"n": 0}
    original = env.fake._take_fault

    def last_chunk_answer_lost(method, path):
        if method == "PUT":
            calls["n"] += 1
            if calls["n"] == 3:
                from fakegrip import Fault
                return Fault(method, path, "drop_after")
        return original(method, path)

    env.fake._take_fault = last_chunk_answer_lost
    env.uploader(attempts_per_call=1).run_once()
    assert env.store.file(raw.id).state == "retry_wait"
    assert env.fake.study_for(study.study_uid).raw[raw.name].complete

    env.fake._take_fault = original
    puts_before = len(env.fake.requests_matching("PUT", r"/raw/"))
    env.advance(3600)
    env.uploader().run_once()
    assert env.store.file(raw.id).state == "deleted"
    assert len(env.fake.requests_matching("PUT", r"/raw/")) == puts_before + 1  # only the last chunk again


def test_one_refused_dicom_file_does_not_hold_back_the_raw_files(env):
    study = env.stage_study(n_dicom=2, raw_sizes=(1_000_000,))
    env.fake.add_fault("POST", r"/dicomweb/studies/.*", 400)
    env.uploader().run_once()
    assert states(env, study) == ["deleted", "deleted", "refused"]
    assert any(fl.code == "STOW_REFUSED" for fl in env.store.open_flags(study_id=study.id))


def test_raw_files_wait_until_grip_has_the_study(env):
    study = env.stage_study(n_dicom=1, raw_sizes=(1_000_000,))
    env.fake.add_fault("POST", r"/dicomweb/studies/.*", 400)
    env.uploader().run_once()
    assert states(env, study) == ["pulled", "refused"]
    assert not env.fake.requests_matching("PUT", r"/raw/")
    assert any(fl.code == "RAW_WAITING_FOR_DICOM" for fl in env.store.open_flags(study_id=study.id))


def test_missing_key_pauses_the_project_and_keeps_files(env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    (env.site.keys_dir / f"{PROJECT}.json").unlink()
    env.uploader().run_once()
    assert states(env, study) == ["staged"]
    assert env.store.key_status(PROJECT)[0] == "missing"
    assert any(fl.code == "KEY_NOT_WORKING" for fl in env.store.open_flags(project=PROJECT))


def test_rejected_key_is_reported_and_a_replaced_key_works_without_restart(env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    env.keys.write(PROJECT, Credentials("site-a-sandbox-client", "an-old-expired-secret"))
    uploader = env.uploader()
    uploader.run_once()
    assert states(env, study) == ["staged"]
    status, detail, _ = env.store.key_status(PROJECT)
    assert status == "rejected" and "invalid_client" in detail

    env.keys.write(PROJECT, Credentials("site-a-sandbox-client", "s3cret-value-not-for-logs"))
    uploader.run_once()
    assert states(env, study) == ["deleted"]
    assert env.store.key_status(PROJECT)[0] == "working"
    assert not env.store.open_flags(project=PROJECT)


def test_confirmed_file_whose_local_delete_failed_is_deleted_on_a_later_run(env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    env.orthanc.fail = True
    env.uploader().run_once()
    f = env.store.files(study.id)[0]
    assert f.state == "confirmed"
    env.orthanc.fail = False
    env.uploader().run_once()
    assert env.store.file(f.id).state == "deleted"
    assert len(env.fake.requests_matching("POST", r"/dicomweb/")) == 1  # not sent again


def test_staged_file_changed_on_disk_is_not_sent(env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    f = env.store.files(study.id)[0]
    Path(f.path).write_bytes(b"corrupted")
    env.uploader().run_once()
    assert env.store.file(f.id).state == "refused"
    assert not env.fake.requests_matching("POST", r"/dicomweb/")


def test_raw_file_name_is_url_encoded_and_unchanged(env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    name = "T2map_CALIPR_UNITY_v1100_beta1.user_rrdf_20261027_141317.h5"
    env.add_raw(study, name, 1_100_000)
    env.uploader().run_once()
    assert name in env.fake.study_for(study.study_uid).raw
    assert env.store.files(study.id, kind="rrdf")[0].state == "deleted"



def test_refused_raw_file_name_is_kept_flagged_and_never_renamed(env):
    study = env.stage_study(n_dicom=2, raw_sizes=(1_000_000,))
    bad = "T2map_CALIPR_UNITY_v1100_beta1.user_rrdf_20261027_141317.h5"
    bad_id = env.add_raw(study, bad, 500_000)
    env.fake.invalid_names.add(bad)
    env.uploader().run_once()
    # The bad name doesn't hold back the DICOM or the other raw file.
    by_name = {f.name: f for f in env.store.files(study.id)}
    assert [f.state for f in by_name.values() if f.kind == "dicom"] == ["deleted", "deleted"]
    assert by_name["fse_fast_t2_rrdf_20261027_134000.h5"].state == "deleted"
    kept = env.store.file(bad_id)
    assert kept.state == "refused" and Path(kept.path).name == bad and Path(kept.path).stat().st_size == 500_000
    flags = [fl for fl in env.store.open_flags(study_id=study.id) if fl.code == "RAW_NAME_REFUSED"]
    assert len(flags) == 1 and bad in flags[0].detail and "never renamed" in flags[0].detail
    assert not any(fl.code == "SESSION_REFUSED" for fl in env.store.open_flags(study_id=study.id))
    assert bad not in env.fake.study_for(study.study_uid).raw

def test_no_patient_data_or_secrets_in_logs(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.stage_study(n_dicom=2, raw_sizes=(1_200_000,))
    env.fake.add_fault("POST", r"/dicomweb/studies/.*", 503, retry_after=1)
    env.fake.add_fault("PUT", r"/v1/studies/.*/raw/.*", 422)
    env.uploader().run_once()
    text = caplog.text
    for secret in (PATIENT_ID, "DONOTLOG", "s3cret-value-not-for-logs", "Bearer"):
        assert secret not in text
    assert "stored on GRIP" in text


# ---- GRIP's API without sessions ---------------------------------------------------------


def test_no_sessions_and_raw_files_addressed_by_study_and_encoded_name(env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    name = "T2map_CALIPR_UNITY_v1100_beta1.user_rrdf_20261027_141317.h5"
    env.add_raw(study, name, 1_100_000)
    env.uploader().run_once()
    assert not [r for r in env.fake.requests if "session" in r[1]]
    puts = env.fake.requests_matching("PUT", r"/raw/")
    assert puts and all(r[1] == f"/api/v1/studies/{study.study_uid}/raw/{quote(name, safe='')}" for r in puts)
    assert name in env.fake.study_for(study.study_uid).raw


def _drop_puts(env, first: int, last: int):
    """Drop the connection on PUT number first..last (1-based) in this run."""
    calls = {"n": 0}
    original = env.fake._take_fault

    def drop(method, path):
        if method == "PUT":
            calls["n"] += 1
            if first <= calls["n"] <= last:
                from fakegrip import Fault
                return Fault(method, path, "drop")
        return original(method, path)

    env.fake._take_fault = drop
    return original


def test_grip_forgets_a_partial_upload_so_it_starts_again_from_0_without_a_flag(env):
    env.fake.forget_partials = True  # "a failed upload may have to be sent in full"
    study = env.stage_study(n_dicom=1, raw_sizes=(5 * 1024 * 1024 + 3,))
    raw = env.store.files(study.id, kind="rrdf")[0]
    original = _drop_puts(env, 3, 6)
    env.uploader().run_once()
    assert env.store.file(raw.id).state == "retry_wait"

    env.fake._take_fault = original
    puts_before = len(env.fake.requests_matching("PUT", r"/raw/"))
    env.advance(3600)
    env.uploader().run_once()
    assert env.store.file(raw.id).state == "deleted"
    first_again = env.fake.requests_matching("PUT", r"/raw/")[puts_before]
    assert first_again[2]["Content-Range"].startswith("bytes 0-")
    assert env.fake.study_for(study.study_uid).raw[raw.name].complete
    assert not env.store.open_flags(study_id=study.id)


def test_same_name_with_a_different_size_on_grip_is_sent_in_full_without_a_flag(env):
    study = env.stage_study(n_dicom=1, raw_sizes=(1_500_000,))
    raw = env.store.files(study.id, kind="rrdf")[0]
    # GRIP has part of an upload of that name with another size (e.g. an older attempt).
    from fakegrip import FakeStudy, RawUpload
    with env.fake.lock:
        s = env.fake.studies.setdefault(("site-a-sandbox-client", study.study_uid),
                                        FakeStudy("site-a-sandbox-client", study.study_uid))
        s.raw[raw.name] = RawUpload(999_999, bytearray(b"x" * 1000))
    env.uploader().run_once()
    assert env.store.file(raw.id).state == "deleted"
    assert env.fake.study_for(study.study_uid).raw[raw.name].size == 1_500_000
    assert env.fake.requests_matching("PUT", r"/raw/")[0][2]["Content-Range"].startswith("bytes 0-")
    assert not env.store.open_flags(study_id=study.id)


def test_grip_refusing_to_continue_means_sending_in_full_without_a_flag(env):
    study = env.stage_study(n_dicom=1, raw_sizes=(3 * 1024 * 1024 + 7,))
    raw = env.store.files(study.id, kind="rrdf")[0]
    original = _drop_puts(env, 2, 5)
    env.uploader().run_once()
    assert env.store.file(raw.id).upload_offset == 1024 * 1024

    env.fake._take_fault = original
    env.fake.add_fault("PUT", r"/v1/studies/.*/raw/.*", 410)  # GRIP won't take the rest
    env.advance(3600)
    env.uploader().run_once()
    assert env.store.file(raw.id).state == "deleted"
    assert env.fake.study_for(study.study_uid).raw[raw.name].complete
    assert not env.store.open_flags(study_id=study.id)


def test_continues_from_grip_offset_when_the_size_matches(env):
    study = env.stage_study(n_dicom=1, raw_sizes=(3 * 1024 * 1024 + 7,))
    raw = env.store.files(study.id, kind="rrdf")[0]
    original = _drop_puts(env, 3, 6)
    env.uploader().run_once()
    env.fake._take_fault = original
    puts_before = len(env.fake.requests_matching("PUT", r"/raw/"))
    env.advance(3600)
    env.uploader().run_once()
    assert env.store.file(raw.id).state == "deleted"
    resumed = env.fake.requests_matching("PUT", r"/raw/")[puts_before]
    assert resumed[2]["Content-Range"].startswith(f"bytes {2 * 1024 * 1024}-")


# ---- refused files are tried again once a day --------------------------------------------------------------


def test_a_refused_file_is_tried_again_after_a_day_and_its_flag_clears_once_grip_takes_it(env):
    from hyperwheel.uploader import REFUSED_RETRY_S
    study = env.stage_study(n_dicom=1, raw_sizes=())
    env.fake.add_fault("POST", r"/dicomweb/studies/.*", 400)  # once
    env.uploader().run_once()
    assert states(env, study) == ["refused"]
    env.advance(REFUSED_RETRY_S - 60)
    env.uploader().run_once()
    assert states(env, study) == ["refused"]  # not before a day
    env.advance(120)
    env.uploader().run_once()  # GRIP fixed it on its side
    assert states(env, study) == ["deleted"]
    assert not any(fl.code == "STOW_REFUSED" for fl in env.store.open_flags(study_id=study.id))


def test_a_file_grip_still_refuses_stays_flagged_and_waits_another_day(env):
    from hyperwheel.uploader import REFUSED_RETRY_S
    study = env.stage_study(n_dicom=0, raw_sizes=(500_000,))
    bad = env.store.files(study.id, kind="rrdf")[0]
    env.fake.invalid_names.add(bad.name)
    dicom = env.add_dicom(study, "1.2.3.4.5")
    env.uploader().run_once()
    assert env.store.file(bad.id).state == "refused"
    for _ in range(2):
        env.advance(REFUSED_RETRY_S + 60)
        env.uploader().run_once()
        assert env.store.file(bad.id).state == "refused"
        flags = [fl for fl in env.store.open_flags(study_id=study.id) if fl.code == "RAW_NAME_REFUSED"]
        assert len(flags) == 1
    assert env.store.file(dicom).state == "deleted"
    env.fake.invalid_names.clear()
    env.advance(REFUSED_RETRY_S + 60)
    env.uploader().run_once()
    assert env.store.file(bad.id).state == "deleted"
    assert not any(fl.code == "RAW_NAME_REFUSED" for fl in env.store.open_flags(study_id=study.id))


def test_a_problem_with_the_pis_own_copy_isnt_tried_again(env):
    from hyperwheel.uploader import REFUSED_RETRY_S
    study = env.stage_study(n_dicom=1, raw_sizes=())
    f = env.store.files(study.id)[0]
    Path(f.path).unlink()  # the staged copy is gone
    env.uploader().run_once()
    assert env.store.file(f.id).state == "refused"
    env.advance(REFUSED_RETRY_S + 60)
    env.uploader().run_once()
    assert env.store.file(f.id).state == "refused"
    assert any(fl.code == "LOCAL_FILE_MISSING" for fl in env.store.open_flags(study_id=study.id))
