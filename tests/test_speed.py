"""Upload speed to GRIP from RRDF files: the log line, the stored speeds and the Upload Monitor's footer."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fakegrip import FakeStudy, RawUpload

from hyperwheel import dashboard, speed
from hyperwheel.dashboard import snapshot
from hyperwheel.uploader import Uploader

CLIENT = "site-a-sandbox-client"


class Ticks:
    """A monotonic clock that moves on by ``step`` seconds each time it's read."""

    def __init__(self, step: float):
        self.t, self.step = 0.0, step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


def uploader(env, monotonic) -> Uploader:
    return Uploader(env.store, env.site, env.routing, env.keys, env.orthanc, sleep=env.sleeps.append,
                    clock=env.clock, monotonic=monotonic, rng=lambda: 0.5)


def dicom_on_grip(env, study):
    for f in env.store.files(study.id, kind="dicom"):
        for state in ("sending", "confirmed", "deleted"):
            env.store.move_file(f.id, state)


def snap(env):
    return snapshot(env.store, env.site, env.routing, now=env.clock(), free_bytes=lambda p: 50 * 1024 ** 3)


def test_an_rrdf_file_on_grip_logs_its_size_time_and_speed(env, caplog):
    study = env.stage_study(n_dicom=1, raw_sizes=(2_500_000,))  # three chunks of at most 1 MiB
    dicom_on_grip(env, study)
    with caplog.at_level(logging.INFO, logger="hyperwheel.upload"):
        uploader(env, Ticks(0.5)).run_once()
    line = next(r.getMessage() for r in caplog.records if "checksum matches" in r.getMessage())
    m = re.search(r"\((2\.5) MB in (\d+\.\d) s, (\d+\.\d+) MB/s\)$", line)
    assert m, line
    seconds = float(m.group(2))
    assert seconds > 0
    # One file so far: the average is its own speed, as logged.
    assert speed.average_mb_s(env.store) == float(m.group(3))


def test_files_under_1_mb_give_no_speed(env, caplog):
    study = env.stage_study(n_dicom=1, raw_sizes=(900_000,))
    dicom_on_grip(env, study)
    with caplog.at_level(logging.INFO, logger="hyperwheel.upload"):
        uploader(env, Ticks(0.5)).run_once()
    line = next(r.getMessage() for r in caplog.records if "checksum matches" in r.getMessage())
    assert re.search(r"\(0\.9 MB in \d+\.\d s\)$", line), line
    assert speed.average_mb_s(env.store) is None


def test_a_file_not_sent_whole_in_one_try_gives_no_speed(env):
    """GRIP already has it, so only its last chunk goes: that's no measure of the link."""
    study = env.stage_study(n_dicom=1, raw_sizes=(2_500_000,))
    dicom_on_grip(env, study)
    raw = env.store.files(study.id, kind="rrdf")[0]
    on_grip = env.fake.studies.setdefault((CLIENT, study.study_uid), FakeStudy(CLIENT, study.study_uid))
    on_grip.raw[raw.name] = RawUpload(raw.size, bytearray(Path(raw.path).read_bytes()))
    uploader(env, Ticks(0.5)).run_once()
    assert env.store.file(raw.id).state == "deleted"
    assert speed.average_mb_s(env.store) is None


def test_speeds_do_not_change_what_is_sent(env):
    study = env.stage_study(n_dicom=1, raw_sizes=(2_500_000,))
    uploader(env, Ticks(0.5)).run_once()
    assert sorted(f.state for f in env.store.files(study.id)) == ["deleted", "deleted"]
    assert len(env.fake.requests_matching("PUT", r"/raw/")) == 3


def test_the_average_is_the_last_five_files_total_mb_over_their_total_seconds(env):
    speed.record(env.store, 50_000_000, 100.0)  # 0.5 MB/s: drops out once five newer files are in
    for size, seconds in [(50_000_000, 5.0), (10_000_000, 1.0), (40_000_000, 8.0), (50_000_000, 5.0),
                          (50_000_000, 6.0)]:
        speed.record(env.store, size, seconds)
    # 200 MB in 25 s, not the mean of the five speeds (10, 10, 5, 10, 8.3 -> 8.7).
    assert speed.average_mb_s(env.store) == 8.0


def test_fewer_than_five_files_average_what_there_is(env):
    assert speed.average_mb_s(env.store) is None
    speed.record(env.store, 30_000_000, 4.0)
    speed.record(env.store, 20_000_000, 1.0)
    assert speed.average_mb_s(env.store) == 10.0


def test_the_7_day_speeds_of_0_1_20_are_removed(env):
    for name in ("rrdf_speed_last", "rrdf_speed_last_at", "rrdf_speed_top_day_20830"):
        env.store.set_cursor(name, 1)
    env.store.set_cursor("reach_grip_answered", 5)  # someone else's cursor stays
    speed.record(env.store, 20_000_000, 2.0)
    names = {r["name"] for r in env.store._db.execute("SELECT name FROM cursors")}
    assert not names & {"rrdf_speed_last", "rrdf_speed_last_at", "rrdf_speed_top_day_20830"}
    assert "reach_grip_answered" in names and speed.average_mb_s(env.store) == 10.0


def test_the_footer_shows_an_up_arrow_and_the_speed_and_nothing_without_one(env):
    assert snap(env)["upload_mb_s"] is None
    speed.record(env.store, 11_700_000, 1.0)
    assert snap(env)["upload_mb_s"] == 11.7
    assert '" \u00b7 \u2191 " + s.upload_mb_s + " MB/s"' in dashboard.SCRIPT
    assert "s.upload_mb_s != null" in dashboard.SCRIPT and "upload to GRIP" not in dashboard.SCRIPT


def test_a_resumed_upload_gives_no_speed_even_when_over_1_mb_is_sent(env):
    study = env.stage_study(n_dicom=1, raw_sizes=(3 * 1024 * 1024,))
    dicom_on_grip(env, study)
    raw = env.store.files(study.id, kind="rrdf")[0]
    data = Path(raw.path).read_bytes()
    on_grip = env.fake.studies.setdefault((CLIENT, study.study_uid), FakeStudy(CLIENT, study.study_uid))
    on_grip.raw[raw.name] = RawUpload(raw.size, bytearray(data[:1_000_000]))  # about 2.1 MB still to send
    uploader(env, Ticks(0.5)).run_once()
    assert env.store.file(raw.id).state == "deleted"
    assert env.fake.requests_matching("PUT", r"/raw/")[0][2]["Content-Range"].startswith("bytes 1000000-")
    assert speed.average_mb_s(env.store) is None


def test_a_file_that_needed_a_retry_gives_no_speed(env):
    """The wait before a retry isn't the link's speed."""
    study = env.stage_study(n_dicom=1, raw_sizes=(2_500_000,))
    dicom_on_grip(env, study)
    env.fake.add_fault("PUT", r"/v1/studies/.*/raw/.*", 503)
    uploader(env, Ticks(0.5)).run_once()
    assert env.sleeps  # it did wait and try again
    assert env.store.files(study.id, kind="rrdf")[0].state == "deleted"
    assert speed.average_mb_s(env.store) is None


def test_a_speed_that_cant_be_saved_never_stops_an_upload(env, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(speed, "record", broken)
    first = env.stage_study(n_dicom=1, raw_sizes=(2_500_000,))
    second = env.stage_study(n_dicom=1, raw_sizes=(2_500_000,))
    for study in (first, second):
        dicom_on_grip(env, study)
    uploader(env, Ticks(0.5)).run_once()
    for study in (first, second):
        f = env.store.files(study.id, kind="rrdf")[0]
        assert f.state == "deleted" and not Path(f.path).exists()
