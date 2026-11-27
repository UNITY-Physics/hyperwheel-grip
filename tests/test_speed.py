"""Upload speed to GRIP from RRDF files: the log line, the stored speeds and the Upload Monitor's footer."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from conftest import PROJECT
from fakegrip import FakeStudy, RawUpload

from hyperwheel import dashboard, speed
from hyperwheel.dashboard import snapshot
from hyperwheel.uploader import Uploader

CLIENT = "site-a-sandbox-client"
DAY = speed.DAY_S


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
    rate = env.store.cursor(speed.LAST)
    assert abs(rate - 2_500_000 / seconds) / rate < 0.05  # the stored speed is the logged one
    shown = speed.read(env.store, env.clock())
    assert shown["last_mb_s"] == shown["top_mb_s"] == speed.mb_s(rate) == float(m.group(3))


def test_files_under_1_mb_give_no_speed(env, caplog):
    study = env.stage_study(n_dicom=1, raw_sizes=(900_000,))
    dicom_on_grip(env, study)
    with caplog.at_level(logging.INFO, logger="hyperwheel.upload"):
        uploader(env, Ticks(0.5)).run_once()
    line = next(r.getMessage() for r in caplog.records if "checksum matches" in r.getMessage())
    assert re.search(r"\(0\.9 MB in \d+\.\d s\)$", line), line
    assert speed.read(env.store, env.clock()) == {"last_mb_s": None, "top_mb_s": None}


def test_a_file_not_sent_whole_in_one_try_gives_no_speed(env):
    """GRIP already has it, so only its last chunk goes: that's no measure of the link."""
    study = env.stage_study(n_dicom=1, raw_sizes=(2_500_000,))
    dicom_on_grip(env, study)
    raw = env.store.files(study.id, kind="rrdf")[0]
    on_grip = env.fake.studies.setdefault((CLIENT, study.study_uid), FakeStudy(CLIENT, study.study_uid))
    on_grip.raw[raw.name] = RawUpload(raw.size, bytearray(Path(raw.path).read_bytes()))
    uploader(env, Ticks(0.5)).run_once()
    assert env.store.file(raw.id).state == "deleted"
    assert env.store.cursor(speed.LAST) == 0


def test_speeds_do_not_change_what_is_sent(env):
    study = env.stage_study(n_dicom=1, raw_sizes=(2_500_000,))
    uploader(env, Ticks(0.5)).run_once()
    assert sorted(f.state for f in env.store.files(study.id)) == ["deleted", "deleted"]
    assert len(env.fake.requests_matching("PUT", r"/raw/")) == 3


def test_top_is_the_best_of_the_last_seven_days_and_older_days_go(env):
    now = 1_800_000_000.0
    speed.record(env.store, 9e6, now - 8 * DAY)  # too old
    speed.record(env.store, 5e6, now - 6 * DAY)
    speed.record(env.store, 2e6, now)
    assert speed.read(env.store, now) == {"last_mb_s": 2.0, "top_mb_s": 5.0}
    assert speed.read(env.store, now + DAY) == {"last_mb_s": 2.0, "top_mb_s": 2.0}  # the 5 MB/s day is now 8 days old
    days = [r["name"] for r in env.store._db.execute("SELECT name FROM cursors WHERE name LIKE 'rrdf_speed_top_day_%'")]
    assert len(days) == 2  # the 8-day-old one was removed when the next speed was recorded


def test_a_slower_file_the_same_day_keeps_that_days_top(env):
    now = 1_800_000_000.0
    speed.record(env.store, 7e6, now)
    speed.record(env.store, 3e6, now + 60)
    assert speed.read(env.store, now + 60) == {"last_mb_s": 3.0, "top_mb_s": 7.0}


def test_the_footer_shows_last_and_top_and_nothing_without_a_speed(env):
    assert snap(env)["upload_speed"] == {"last_mb_s": None, "top_mb_s": None}
    speed.record(env.store, 6.74e6, env.clock())
    assert snap(env)["upload_speed"] == {"last_mb_s": 6.7, "top_mb_s": 6.7}
    assert 'upload to GRIP: ' in dashboard.SCRIPT and 'up.last_mb_s != null' in dashboard.SCRIPT
    assert PROJECT  # the fixture's dataset is routed, so the page is the set-up one
