"""GRIP's region prefix on the two store calls, and the checksum check before any local file is deleted."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import PROJECT
from fakegrip import FakeStudy, RawUpload

from hyperwheel.config import ConfigError, GripSettings, Routing, check_region, load_routing, load_site
from hyperwheel.sitesetup import render_routing_toml

CLIENT = "site-a-sandbox-client"


def with_region(env, region="region-a"):
    project = replace(env.routing.projects[PROJECT], region=region)
    env.routing = Routing(projects={PROJECT: project})
    return env


def states(env, study):
    return sorted(f.state for f in env.store.files(study.id))


def flags(env, study):
    return sorted(f.code for f in env.store.open_flags(study_id=study.id))


# ---- region ----------------------------------------------------------------------------------------------


def test_only_the_store_calls_carry_the_region(env):
    with_region(env)
    study = env.stage_study(n_dicom=2, raw_sizes=(2_500_000,))
    env.uploader().run_once()
    assert states(env, study) == ["deleted"] * 3
    assert env.fake.store_regions and set(env.fake.store_regions) == {"region-a"}
    assert env.fake.study_for(study.study_uid).region == "region-a"
    paths = [(m, p) for m, p, _ in env.fake.requests]
    stows = [p for m, p in paths if m == "POST" and "/dicomweb/" in p]
    raws = [p for m, p in paths if "/raw/" in p]
    assert stows and all(p.startswith("/api/region-a/dicomweb/studies/") for p in stows)
    assert raws and all(p.startswith(f"/api/region-a/v1/studies/{study.study_uid}/raw/") for p in raws)
    # Token and study status: never a region.
    assert all(p == "/api/get/token" for m, p in paths if m == "POST" and "token" in p)
    status = [p for m, p in paths if m == "GET" and "/raw/" not in p]
    assert status and all(p == f"/api/v1/studies/{study.study_uid}" for p in status)


def test_a_pi_with_no_region_sends_without_a_prefix(env):
    study = env.stage_study(n_dicom=1, raw_sizes=(1_200_000,))
    env.uploader().run_once()
    assert states(env, study) == ["deleted"] * 2
    assert set(env.fake.store_regions) == {None}
    assert env.fake.study_for(study.study_uid).region is None


def test_a_region_is_url_safe_and_lower_case():
    assert check_region("region-a", "x") == "region-a"
    for bad in ("", "West", "a/b", "..", "west europe", "x" * 64, 3):
        with pytest.raises(ConfigError):
            check_region(bad, "x")


def site_text(tmp, grip_lines: str) -> str:
    return (f'schema_version = 1\nsite = "t"\nstaging_dir = "{tmp}/s"\nstate_db = "{tmp}/d"\nkeys_dir = "{tmp}/k"\n'
            f'\n[grip]\napi_base_url = "https://grip.example.org/api"\n'
            f'token_url = "https://grip.example.org/api/get/token"\n{grip_lines}')


def test_site_toml_region_is_optional_on_a_pi_and_checked(tmp_path):
    path = tmp_path / "site.toml"
    path.write_text(site_text(tmp_path, ""))
    assert load_site(path).grip.region is None
    path.write_text(site_text(tmp_path, 'region = "region-b"\n'))
    assert load_site(path).grip.region == "region-b"
    path.write_text(site_text(tmp_path, 'region = "East US"\n'))
    with pytest.raises(ConfigError, match="region"):
        load_site(path)


def test_the_region_goes_only_with_the_sites_address(tmp_path):
    grip = GripSettings("https://grip.example.org/api", "https://grip.example.org/api/get/token", "region-a")
    path = tmp_path / "routing.toml"
    path.write_text(render_routing_toml({
        "follows": {"logins": ["a"]},
        "names-the-same": {"logins": ["b"], "api_base_url": grip.api_base_url, "token_url": grip.token_url},
        "elsewhere": {"logins": ["c"], "api_base_url": "https://other.example.org/api",
                      "token_url": "https://other.example.org/api/get/token"}}))
    projects = load_routing(path, grip=grip).projects
    assert projects["follows"].region == projects["names-the-same"].region == "region-a"
    assert projects["elsewhere"].region is None


# ---- DICOM checksums --------------------------------------------------------------------------------------


def test_dicom_is_deleted_only_once_grips_checksum_matches(env):
    study = env.stage_study(n_dicom=3, raw_sizes=())
    env.uploader().run_once()
    assert states(env, study) == ["deleted"] * 3
    # One study status call checks all three.
    assert len(env.fake.requests_matching("GET", rf"^/api/v1/studies/{study.study_uid}$")) == 1


def test_dicom_whose_copy_on_grip_differs_is_sent_again_and_then_deleted(env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    f = env.store.files(study.id)[0]
    env.fake.corrupt[f.sop_uid] = 1
    env.uploader().run_once()
    f = env.store.file(f.id)
    assert f.state == "staged" and Path(f.path).exists() and env.orthanc.deleted == []
    env.uploader().run_once()
    assert env.store.file(f.id).state == "deleted"
    assert len(env.fake.requests_matching("POST", r"/dicomweb/studies/")) == 2
    assert flags(env, study) == []


def test_dicom_still_different_after_a_resend_is_kept_and_flagged(env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    f = env.store.files(study.id)[0]
    env.fake.corrupt[f.sop_uid] = 2
    for _ in range(3):
        env.uploader().run_once()
    f = env.store.file(f.id)
    assert f.state == "refused" and Path(f.path).exists() and env.orthanc.deleted == []
    assert flags(env, study) == ["DICOM_CHECKSUM"]
    assert len(env.fake.requests_matching("POST", r"/dicomweb/studies/")) == 2


def test_no_checksum_from_grip_keeps_every_file_and_flags_it(env):
    env.fake.checksums = False
    study = env.stage_study(n_dicom=2, raw_sizes=(1_200_000,))
    env.uploader().run_once()
    files = env.store.files(study.id)
    assert all(Path(f.path).exists() for f in files) and env.orthanc.deleted == []
    assert sorted(f.state for f in files if f.kind == "dicom") == ["refused", "refused"]
    assert "NO_CHECKSUM" in flags(env, study)
    # RRDF waits: no DICOM of the study is confirmed on GRIP.
    assert [f.state for f in files if f.kind == "rrdf"] == ["pulled"]

    # GRIP fixes it. A day later the files are tried again, checked and deleted, and the flag goes.
    env.fake.checksums = True
    env.advance(25 * 3600)
    env.uploader().run_once()
    assert states(env, study) == ["deleted"] * 3
    assert "NO_CHECKSUM" not in flags(env, study)


def test_a_stored_dicom_file_is_checked_after_a_restart_without_sending_it_again(env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    f = env.store.files(study.id)[0]
    env.fake.checksums = False
    env.fake.add_fault("GET", rf"/v1/studies/{study.study_uid}", "drop", count=10)
    env.uploader().run_once()  # stored, but the check gets no answer: the file stays stored
    assert env.store.file(f.id).state == "stored" and Path(f.path).exists()
    env.fake.faults.clear()
    env.fake.checksums = True
    env.uploader().run_once()  # a new uploader, as after a restart
    assert env.store.file(f.id).state == "deleted"
    assert len(env.fake.requests_matching("POST", r"/dicomweb/studies/")) == 1


def test_a_refused_status_call_keeps_the_files_and_says_so(env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    f = env.store.files(study.id)[0]
    env.fake.add_fault("GET", rf"/v1/studies/{study.study_uid}", 403)
    env.uploader().run_once()
    assert env.store.file(f.id).state == "stored" and Path(f.path).exists()
    assert flags(env, study) == ["CHECKSUM_NOT_CHECKED"]
    env.uploader().run_once()
    assert env.store.file(f.id).state == "deleted" and flags(env, study) == []


# ---- RRDF checksums ---------------------------------------------------------------------------------------


def test_rrdf_whose_copy_on_grip_differs_is_sent_again_from_the_start(env):
    study = env.stage_study(n_dicom=1, raw_sizes=(2_500_000,))
    raw = env.store.files(study.id, kind="rrdf")[0]
    data = Path(raw.path).read_bytes()
    env.fake.corrupt[raw.name] = 1
    env.uploader().run_once()
    assert env.store.file(raw.id).state == "deleted" and not Path(raw.path).exists()
    puts = env.fake.requests_matching("PUT", r"/raw/")
    starts = [h.get("Content-Range", "").split()[1].split("-")[0] for _, _, h in puts]
    assert starts.count("0") == 2  # sent in full twice
    assert bytes(env.fake.study_for(study.study_uid).raw[raw.name].data) == data


def test_rrdf_still_different_after_a_resend_is_kept_and_flagged(env):
    study = env.stage_study(n_dicom=1, raw_sizes=(1_500_000,))
    raw = env.store.files(study.id, kind="rrdf")[0]
    env.fake.corrupt[raw.name] = 2
    env.uploader().run_once()
    raw = env.store.file(raw.id)
    assert raw.state == "refused" and Path(raw.path).exists()
    assert flags(env, study) == ["RAW_CHECKSUM"]


def test_rrdf_with_no_checksum_from_grip_is_kept_and_flagged(env):
    study = env.stage_study(n_dicom=1, raw_sizes=(1_500_000,))
    raw = env.store.files(study.id, kind="rrdf")[0]
    dicom = env.store.files(study.id, kind="dicom")[0]
    for state in ("sending", "confirmed", "deleted"):  # its DICOM is already on GRIP
        env.store.move_file(dicom.id, state)
    env.fake.checksums = False
    env.uploader().run_once()
    raw = env.store.file(raw.id)
    assert raw.state == "refused" and Path(raw.path).exists()
    assert flags(env, study) == ["NO_CHECKSUM"]


def test_a_resumed_upload_whose_first_part_came_from_another_copy_is_caught_and_sent_again(env):
    """GRIP restarted part-way, or kept a partial upload of another version: resuming gives a wrong copy."""
    study = env.stage_study(n_dicom=1, raw_sizes=(3 * 1024 * 1024,))
    raw = env.store.files(study.id, kind="rrdf")[0]
    other = os.urandom(raw.size)
    on_grip = env.fake.studies.setdefault((CLIENT, study.study_uid), FakeStudy(CLIENT, study.study_uid))
    on_grip.raw[raw.name] = RawUpload(raw.size, bytearray(other[:1_000_000]))
    env.uploader().run_once()
    assert env.store.file(raw.id).state == "deleted"
    puts = env.fake.requests_matching("PUT", r"/raw/")
    first = puts[0][2]["Content-Range"]
    assert first.startswith("bytes 1000000-")  # it resumed
    data = bytes(env.fake.study_for(study.study_uid).raw[raw.name].data)
    assert data != other[:len(data)] and len(data) == raw.size
    assert flags(env, study) == []


def test_a_file_grip_already_has_whole_and_identical_needs_only_its_last_chunk(env):
    study = env.stage_study(n_dicom=1, raw_sizes=(2_500_000,))
    raw = env.store.files(study.id, kind="rrdf")[0]
    data = Path(raw.path).read_bytes()
    on_grip = env.fake.studies.setdefault((CLIENT, study.study_uid), FakeStudy(CLIENT, study.study_uid))
    on_grip.raw[raw.name] = RawUpload(raw.size, bytearray(data))
    env.uploader().run_once()
    assert env.store.file(raw.id).state == "deleted"
    assert len(env.fake.requests_matching("PUT", r"/raw/")) == 1


def test_the_checksum_comes_from_the_study_status_if_the_put_reply_has_none(env, monkeypatch):
    from hyperwheel.grip import api as grip_api

    real = grip_api._stored
    calls = {"n": 0}

    def no_reply_checksum(item):  # the first call is the PUT reply's: pretend it had no checksum
        calls["n"] += 1
        return grip_api.Stored(None, None) if calls["n"] == 1 else real(item)

    study = env.stage_study(n_dicom=0, raw_sizes=(500_000,))
    dicom_id = env.add_dicom(study, "1.2.3")
    for state in ("sending", "confirmed", "deleted"):
        env.store.move_file(dicom_id, state)
    monkeypatch.setattr(grip_api, "_stored", no_reply_checksum)
    env.uploader().run_once()
    raw = env.store.files(study.id, kind="rrdf")[0]
    assert raw.state == "deleted"
    assert env.fake.requests_matching("GET", rf"^/api/v1/studies/{study.study_uid}$")
