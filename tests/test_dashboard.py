"""The Upload Monitor and the service rounds."""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest
from conftest import PATIENT_ID, PROJECT
from fakes import FakeScanner
from test_rawpull import FOLDER, PHANTOM_RAW, phantom_study, raw

from hyperwheel import dashboard
from hyperwheel.dashboard import HEARTBEAT, make_server, snapshot
from hyperwheel.service import Service
from hyperwheel.state import Store


@pytest.fixture
def scanner(tmp_path):
    return FakeScanner(tmp_path / "scanner")


def service(env, scanner) -> Service:
    return Service(env.site, env.routing, orthanc=lambda: env.orthanc, scanner=scanner.connect, keys=env.keys,
                   clock=env.clock, synced=lambda: True, scanner_probe=lambda site: 1,
                   internet_probe=lambda routing: True, sleep=env.sleeps.append, rng=lambda: 0.5)


def snap(env, **kw):
    return snapshot(env.store, env.site, env.routing, now=env.clock(), free_bytes=lambda p: 50 * 1024 ** 3, **kw)


def test_states_an_operator_sees(env, scanner):
    from hyperwheel.organiser import Organiser
    from hyperwheel.rawpull import RawPuller
    svc = service(env, scanner)
    organiser = Organiser(env.store, env.site, env.routing, env.orthanc, clock=env.clock)
    puller = RawPuller(env.store, env.site, scanner.connect, clock=env.clock, free_bytes=lambda p: 10 ** 12)

    s = snap(env)
    assert s["headline"] == "Hyperwheel is not running" and not s["on_pi_complete"]
    assert s["general"][0]["text"].startswith("The Hyperwheel service is not running")
    env.store.set_cursor(HEARTBEAT, int(env.clock()))
    assert snap(env)["headline"] == "Everything is on GRIP"

    uid = phantom_study(env)
    for name in PHANTOM_RAW:
        raw(scanner, FOLDER, name, size=200_000)
    env.store.set_cursor(HEARTBEAT, int(env.clock()))
    s = snap(env)
    assert s["headline"] == "Fetching raw data from the scanner" and not s["on_pi_complete"]
    card = s["studies"][0]
    assert card["session"] == "2026-10-27 13:28" and card["project"] == PROJECT
    assert card["patient_id"] == PATIENT_ID and card["dicom"]["text"] == "DICOM 0/11 on GRIP"

    svc.local_round(env.store, organiser, puller)
    svc.local_round(env.store, organiser, puller)
    s = snap(env)
    assert s["on_pi_complete"] and s["headline"] == "Uploading to GRIP"

    svc.upload_round(env.uploader())
    s = snap(env)
    assert s["headline"] == "Everything is on GRIP"
    assert s["studies"][0]["dicom"]["text"] == "DICOM 11/11 on GRIP"
    assert s["studies"][0]["raw"]["on_grip"] == 9 and s["studies"][0]["state"] == "On GRIP"
    assert s["keys"] == [{"project": PROJECT, "status": "Working", "ok": True}]
    assert env.store.study_by_uid(uid).state == "uploaded"


def test_counts_say_receiving_until_they_mean_something(env):
    study = env.store.add_study("1.2.3", state="receiving", project=None)
    card = snap(env)["studies"][0]
    assert card["dicom"]["text"] == "DICOM receiving" and card["raw"]["text"] == "Raw data still to come"
    # More images arriving for a study already counted: the count still means something.
    env.store.add_file(kind="dicom", name="a.dcm", state="staged", study_id=study.id, sop_uid="1.2.3.4")
    assert snap(env)["studies"][0]["dicom"]["text"] == "DICOM 0/1 on GRIP"


def test_a_study_with_no_raw_data_says_so(env):
    env.store.add_study("1.2.4", state="ready", project=PROJECT, raw_checked_at=env.clock())
    assert snap(env)["studies"][0]["raw"]["text"] == "No raw data"


def test_unrouted_studies_are_never_shown(env):
    phantom_study(env, operators="hospital/clinical")
    assert snap(env)["studies"] == []


def test_attention_and_key_problems(env):
    (env.site.keys_dir / f"{PROJECT}.json").unlink()
    env.stage_study(n_dicom=1, raw_sizes=())
    env.uploader().run_once()
    env.store.set_cursor(HEARTBEAT, int(env.clock()))
    s = snap(env)
    assert s["attention"]
    assert s["keys"][0]["status"] == "No key stored" and not s["keys"][0]["ok"]
    assert any("GRIP key" in f["text"] for f in s["general"])


def test_low_disk_and_clock_messages(env):
    env.store.set_cursor(HEARTBEAT, int(env.clock()))
    s = snapshot(env.store, env.site, env.routing, now=env.clock(), free_bytes=lambda p: 2 * 1024 ** 3,
                 clock_synced=False)
    texts = [f["text"] for f in s["general"]]
    assert any("nearly full" in t for t in texts) and any("clock" in t for t in texts)


def test_upload_waits_for_the_clock(env):
    env.stage_study(n_dicom=1, raw_sizes=())
    svc = Service(env.site, env.routing, orthanc=lambda: env.orthanc, keys=env.keys, clock=env.clock,
                  synced=lambda: False, internet_probe=lambda routing: True)
    svc.upload_round(env.uploader())
    assert env.fake.requests == []


def test_page_and_api_over_http(env):
    phantom_study(env)
    server = make_server(env.site, env.routing, bind="127.0.0.1", port=0, now=env.clock, synced=lambda: True)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/") as r:
            assert "Upload Monitor" in r.read().decode()
            assert "default-src 'self'" in r.headers["Content-Security-Policy"]
        with urllib.request.urlopen(base + "/api/status") as r:
            body = json.loads(r.read())
        assert body["studies"][0]["project"] == PROJECT
    finally:
        server.shutdown()
        server.server_close()


def test_page_never_inserts_data_as_html():
    assert "innerHTML" not in dashboard.SCRIPT and "insertAdjacentHTML" not in dashboard.SCRIPT


def test_dashboard_store_is_read_only(env):
    ro = Store(env.site.state_db, readonly=True)
    with pytest.raises(Exception):
        ro.set_cursor("x", 1)
    ro.close()


def test_a_second_service_on_the_same_state_store_refuses_to_start(env, tmp_path):
    import subprocess
    import sys
    from hyperwheel.service import AlreadyRunning, single_instance
    with single_instance(env.site.state_db):
        with pytest.raises(AlreadyRunning):
            with single_instance(env.site.state_db):
                pass
        # Also from another process (as two runs of `hyperwheel run` would be).
        code = ("import sys; from pathlib import Path; from hyperwheel.service import single_instance, AlreadyRunning\n"
                f"try:\n  with single_instance(Path({str(env.site.state_db)!r})): sys.exit(0)\n"
                "except AlreadyRunning: sys.exit(3)")
        assert subprocess.run([sys.executable, "-c", code]).returncode == 3
    with single_instance(env.site.state_db):  # free again once the first has stopped
        pass


def test_a_loop_that_cannot_start_stops_the_service(env):
    """Seen on the dev Pi: the Orthanc login couldn't be read, a loop thread died, and the process
    stayed 'active' doing nothing. Now the service stops with an error, so systemd restarts it."""
    def no_orthanc():
        raise RuntimeError("orthanc login can't be read")
    svc = Service(env.site, env.routing, orthanc=no_orthanc, keys=env.keys, clock=env.clock, synced=lambda: True)
    safety = threading.Timer(10, svc.stop.set)  # only if the service failed to stop by itself
    safety.daemon = True
    safety.start()
    svc.run()
    safety.cancel()
    assert svc.failed


def test_an_update_hold_is_shown(env):
    s = snap(env, update_hold=lambda: "Updates are on hold since 2026-11-06 15:00: site visit.")
    assert {"level": "flag", "text": "Updates are on hold since 2026-11-06 15:00: site visit."} in s["general"]
    assert not any("on hold" in g["text"] for g in snap(env, update_hold=lambda: None)["general"])
