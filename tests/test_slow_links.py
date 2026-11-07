"""Slow and lossy links, as seen with tc netem on the dev Pi.

- A request body must be sent within the connect timeout (that's how urllib3 sends it), so
  each request gets the time its size needs at the slowest link rate in the settings.
- A raw chunk that gets no answer is retried smaller (halving down to min_chunk_bytes), and
  chunks grow back after a run of good ones.
- An upload GRIP answered recently proves the scanner and the internet work, even when the
  quick reachability check times out behind the upload's own traffic.
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace

import requests

from hyperwheel import doctor, reach
from hyperwheel.dashboard import HEARTBEAT, snapshot
from hyperwheel.doctor import OK, Probes

KIB = 1024


class SlowLink(requests.Session):
    """Drops any request whose body is bigger than the link can carry before the timeout."""

    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit
        self.sent: list[tuple[str, int, object]] = []  # (method, body size, timeout)

    def request(self, method, url, **kw):
        body = kw.get("data")
        size = len(body) if isinstance(body, (bytes, bytearray)) else 0
        self.sent.append((method, size, kw.get("timeout")))
        if size > self.limit:
            raise requests.ConnectionError("write timed out")
        return super().request(method, url, **kw)


def slow_uploader(env, link: SlowLink, **settings):
    from hyperwheel.uploader import Uploader
    site = replace(env.site, upload=replace(env.site.upload, **settings))
    return Uploader(env.store, site, env.routing, env.keys, env.orthanc, session_factory=lambda: link,
                    sleep=env.sleeps.append, clock=env.clock, rng=lambda: 0.5)


def chunk_sizes(link: SlowLink) -> list[int]:
    return [size for method, size, _ in link.sent if method == "PUT"]


def test_send_time_grows_with_the_body(env):
    link = SlowLink(limit=10 ** 9)
    env.stage_study(n_dicom=1, raw_sizes=(1_000_000,))
    slow_uploader(env, link, chunk_bytes=1024 * KIB, timeout_s=30, slowest_link_bytes_per_s=10_000).run_once()
    api_calls = [(size, timeout) for _, size, timeout in link.sent if isinstance(timeout, tuple)]  # not tokens
    assert api_calls
    for size, (connect, read) in api_calls:
        assert read == 30 and connect == max(30, size / 10_000)
    assert any(t[0] == 100 for _, t in api_calls)  # the 1 MB raw chunk: 100 s to send at 10 kB/s


def test_chunk_halves_until_the_link_carries_it_then_the_file_goes(env, caplog):
    caplog.set_level(logging.WARNING)
    study = env.stage_study(n_dicom=1, raw_sizes=(1_500_000,))
    link = SlowLink(limit=300 * KIB)
    slow_uploader(env, link, chunk_bytes=1024 * KIB, min_chunk_bytes=256 * KIB).run_once()
    raw = env.store.files(study.id, kind="rrdf")[0]
    assert raw.state == "deleted"
    sizes = chunk_sizes(link)
    assert sizes[:3] == [1024 * KIB, 512 * KIB, 256 * KIB]  # each tried once, then smaller
    assert all(s <= 256 * KIB for s in sizes[2:])
    assert env.fake.study_for(study.study_uid).raw[raw.name].complete
    assert not env.store.open_flags(study_id=study.id)
    assert sum("Trying again, with chunks" in r.getMessage() for r in caplog.records) == 2


def test_chunks_grow_back_once_the_link_is_good(env):
    study = env.stage_study(n_dicom=1, raw_sizes=(600 * KIB,))
    link = SlowLink(limit=300 * KIB)
    up = slow_uploader(env, link, chunk_bytes=1024 * KIB, min_chunk_bytes=256 * KIB)
    up.run_once()
    assert up._chunk_bytes == 256 * KIB
    link.limit = 10 ** 9  # the link recovers
    env.add_raw(study, "flair_iso_rrdf_20261027_134807.h5", 4 * 1024 * KIB)
    env.store.move_study(study.id, "uploading")
    before = len(chunk_sizes(link))
    up.run_once()
    later = chunk_sizes(link)[before:]
    assert later[0] == 256 * KIB and max(later) == 512 * KIB  # doubled after 8 good chunks
    assert env.store.files(study.id, kind="rrdf")[-1].state == "deleted"


def test_a_chunk_that_never_gets_through_is_parked_and_kept(env):
    study = env.stage_study(n_dicom=1, raw_sizes=(1_500_000,))
    link = SlowLink(limit=100 * KIB)  # below the smallest chunk
    slow_uploader(env, link, chunk_bytes=1024 * KIB, min_chunk_bytes=256 * KIB).run_once()
    raw = env.store.files(study.id, kind="rrdf")[0]
    assert raw.state == "retry_wait" and os.path.exists(raw.path)
    assert len(chunk_sizes(link)) == env.site.upload.attempts_per_call


def test_retry_after_is_still_honoured_for_single_try_chunks(env):
    study = env.stage_study(n_dicom=1, raw_sizes=(500_000,))
    env.uploader().run_once()  # DICOM goes; then a 429 on the first chunk of a later file
    env.add_raw(study, "flair_iso_rrdf_20261027_134807.h5", 500_000)
    env.store.move_study(study.id, "uploading")
    env.fake.add_fault("PUT", r"/v1/studies/.*/raw/.*", 429, retry_after=7)
    env.sleeps.clear()
    env.uploader().run_once()
    assert 7 in env.sleeps
    assert all(f.state == "deleted" for f in env.store.files(study.id))


def test_recent_grip_answer_beats_a_timed_out_quick_check(env):
    env.stage_study(n_dicom=1, raw_sizes=())
    env.uploader().run_once()  # GRIP answered just now
    reach.record_scanner(env.store, reach.SCANNER_NO_ANSWER, env.clock() - 600)
    reach.record_internet(env.store, False, env.clock() - 600)
    env.store.set_cursor(HEARTBEAT, int(env.clock()))
    s = snapshot(env.store, env.site, env.routing, now=env.clock(), free_bytes=lambda p: 50 * 1024 ** 3)
    assert not reach.messages(reach.read(env.store), env.clock())
    assert not any("reach" in g["text"] for g in s["general"])

    site = replace(env.site, scanner=replace(env.site.scanner, host="10.42.0.1"))
    results, code = doctor.run(site, env.routing, Probes(link=lambda i: True, answers=lambda h, p: False,
                                                         orthanc_answers=lambda u: True, synced=lambda: True,
                                                         free_bytes=lambda p: 50 * 1024 ** 3, now=env.clock))
    by = {w: (r, d) for r, w, d in results}
    assert by["Scanner"][0] == OK and "busy or slow" in by["Scanner"][1]
    assert by["GRIP over the internet"][0] == OK

    env.advance(reach.RECENT_S + 1)  # no answer for a while: the problem shows again
    assert reach.messages(reach.read(env.store), env.clock())


def test_chunks_that_take_too_long_get_smaller_so_grip_answers_often(env):
    """At 15% loss on the dev Pi an 8 MiB chunk took 4 minutes: no GRIP answer for that long."""
    study = env.stage_study(n_dicom=1, raw_sizes=(4 * 1024 * KIB,))
    link = SlowLink(limit=10 ** 9)
    ticks = {"t": 0.0}

    def slow_clock():  # every reading is 40 s later: each chunk "takes" 40 s
        ticks["t"] += 40
        return ticks["t"]

    from hyperwheel.uploader import Uploader
    site = replace(env.site, upload=replace(env.site.upload, chunk_bytes=1024 * KIB, min_chunk_bytes=256 * KIB,
                                            target_chunk_s=30))
    up = Uploader(env.store, site, env.routing, env.keys, env.orthanc, session_factory=lambda: link,
                  sleep=env.sleeps.append, clock=env.clock, monotonic=slow_clock, rng=lambda: 0.5)
    up.run_once()
    sizes = chunk_sizes(link)
    assert sizes[:3] == [1024 * KIB, 512 * KIB, 256 * KIB] and all(x <= 256 * KIB for x in sizes[2:])
    assert env.store.files(study.id, kind="rrdf")[0].state == "deleted"
    assert not env.store.open_flags(study_id=study.id)
