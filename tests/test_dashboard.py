"""The Upload Monitor and the service rounds."""

from __future__ import annotations

from dataclasses import replace

import json
import socket
import sys
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
    study = env.store.add_study("1.2.3", state="receiving", project=None, operators_name="site_a/sandbox")
    card = snap(env)["studies"][0]
    assert card["dicom"]["text"] == "DICOM receiving" and card["raw"]["text"] == "Raw data still to come"
    assert card["project"] == PROJECT  # from the routing, before the study is routed
    # More images arriving for a study already counted: the count still means something.
    env.store.add_file(kind="dicom", name="a.dcm", state="staged", study_id=study.id, sop_uid="1.2.3.4")
    assert snap(env)["studies"][0]["dicom"]["text"] == "DICOM 0/1 on GRIP"


def test_files_waiting_for_a_key_say_so_not_uploading(env):
    env.store.set_cursor(HEARTBEAT, int(env.clock()))
    study = env.store.add_study("1.2.5", state="ready", project=PROJECT, raw_checked_at=env.clock())
    env.store.add_file(kind="dicom", name="a.dcm", state="staged", study_id=study.id, sop_uid="1.2.5.1")
    assert snap(env)["headline"] == "Uploading to GRIP"  # the key not checked yet: nothing known to stop it
    for status in ("missing", "unreadable", "rejected"):
        env.store.set_key_status(PROJECT, status, "test")
        s = snap(env)
        assert s["headline"] == "Waiting for a GRIP key", status
        assert f"Uploads for {PROJECT} start once a working GRIP key is set" in s["subline"]
        assert not s["safe_to_switch_off"] and ";" not in s["subline"]  # nothing is on GRIP yet
    # Without a key nothing reaches GRIP: the scanner stays on, as for any file still to upload.
    s = snapshot(env.store, env.site, env.routing, now=env.clock(), free_bytes=lambda p: 1024 ** 3,
                 connect_state=lambda: None, update_hold=lambda: None)
    assert s["headline"] == "Waiting for a GRIP key" and not s["safe_to_switch_off"]
    assert any("nearly full" in g["text"] for g in s["general"])
    # Another project's files still go up, so the Pi is uploading (the key alert names the one that isn't).
    env.routing.projects["Other"] = replace(env.routing.projects[PROJECT], name="Other", logins=("other",))
    other = env.store.add_study("1.2.6", state="ready", project="Other", raw_checked_at=env.clock())
    env.store.add_file(kind="dicom", name="b.dcm", state="staged", study_id=other.id, sop_uid="1.2.6.1")
    env.store.set_key_status("Other", "working")
    assert snap(env)["headline"] == "Uploading to GRIP"
    env.store.set_key_status("Other", "missing")
    s = snap(env)
    assert s["headline"] == "Waiting for a GRIP key"
    assert f"Uploads for Other and {PROJECT} start" in s["subline"]
    env.store.set_key_status("Other", "working")
    env.store.set_key_status(PROJECT, "working")
    assert snap(env)["headline"] == "Uploading to GRIP"
    # A project taken out of the routing settings can't get a key, so the headline doesn't ask for one.
    env.store.set_key_status("Other", "missing")
    env.store.set_key_status(PROJECT, "missing")
    assert snap(env)["headline"] == "Waiting for a GRIP key"
    gone = env.store.add_study("1.2.7", state="ready", project="Gone", raw_checked_at=env.clock())
    env.store.add_file(kind="dicom", name="c.dcm", state="staged", study_id=gone.id, sop_uid="1.2.7.1")
    env.store.set_key_status("Gone", "missing")
    assert snap(env)["headline"] == "Uploading to GRIP"


def test_a_study_with_no_raw_data_says_so(env):
    env.store.add_study("1.2.4", state="ready", project=PROJECT, raw_checked_at=env.clock())
    assert snap(env)["studies"][0]["raw"]["text"] == "No raw data"


def test_connect_state_is_shown(env):
    now = env.clock()
    s = snap(env, connect_state=lambda: {"signed_in": True, "checked_at": now})
    assert s["connect"] == {"ok": True, "problem": False, "text": "signed in"}
    s = snap(env, connect_state=lambda: {"signed_in": False, "checked_at": now})
    assert s["connect"]["problem"] and "not signed in" in s["connect"]["text"]
    assert snap(env, connect_state=lambda: None)["connect"]["text"] == "not checked yet"


def test_unrouted_studies_are_never_shown(env):
    phantom_study(env, operators="hospital/clinical")
    assert snap(env)["studies"] == []


def test_unrouted_studies_are_not_shown_while_their_images_arrive(env):
    env.store.add_study("1.2.5", state="receiving", project=None, operators_name="hospital/clinical")
    env.store.add_study("1.2.6", state="stable", project=None, operators_name=None)
    env.store.set_cursor(HEARTBEAT, int(env.clock()))
    s = snap(env)
    assert s["studies"] == []
    # Hidden, but still arriving: never "safe to switch off" while they are.
    assert s["headline"] == "Receiving images from the scanner" and not s["safe_to_switch_off"]


def test_the_row_leaves_out_the_dot_with_no_project():
    assert 'st.project ? st.session + " · " + st.project : st.session' in dashboard.SCRIPT


def test_a_held_study_with_no_project_is_still_shown(env):
    # Held for images from two logins, before it was routed: the data manager has to see it.
    study = env.store.add_study("1.2.7", state="held", project=None, operators_name="hospital/clinical")
    env.store.raise_flag("MIXED_LOGINS", "Images from more than one login.", study_id=study.id, level="hold")
    card = snap(env)["studies"][0]
    assert card["project"] == "" and card["flags"][0]["level"] == "hold"


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


def test_a_study_with_no_raw_data_on_the_scanner_is_complete_at_the_first_look(env, scanner):
    """The scanner makes DICOM from raw data, so no folder when the DICOM is in means there's no raw data."""
    from hyperwheel.organiser import Organiser
    from hyperwheel.rawpull import RawPuller
    svc = service(env, scanner)
    organiser = Organiser(env.store, env.site, env.routing, env.orthanc, clock=env.clock)
    puller = RawPuller(env.store, env.site, scanner.connect, clock=env.clock, free_bytes=lambda p: 10 ** 12)
    uid = phantom_study(env)
    svc.local_round(env.store, organiser, puller)
    svc.upload_round(env.uploader())
    env.store.set_cursor(HEARTBEAT, int(env.clock()))
    s = snap(env)
    assert env.store.study_by_uid(uid).state == "uploaded"
    assert s["headline"] == "Everything is on GRIP" and s["safe_to_switch_off"]
    assert s["studies"][0]["raw"]["text"] == "No raw data"


def test_a_scanner_that_cant_be_read_doesnt_count_as_no_raw_data(env, scanner):
    from hyperwheel.organiser import Organiser
    from hyperwheel.rawpull import RawPuller

    def unreachable():
        raise OSError("no route to host")

    svc = service(env, scanner)
    organiser = Organiser(env.store, env.site, env.routing, env.orthanc, clock=env.clock)
    puller = RawPuller(env.store, env.site, unreachable, clock=env.clock, free_bytes=lambda p: 10 ** 12)
    phantom_study(env)
    svc.local_round(env.store, organiser, puller)
    env.store.set_cursor(HEARTBEAT, int(env.clock()))
    s = snap(env)
    assert s["studies"][0]["raw"]["text"] == "Raw data still to come" and not s["on_pi_complete"]


def test_the_upload_monitor_also_listens_on_the_pi_itself():
    assert dashboard.addresses("192.168.99.1") == ["192.168.99.1", "127.0.0.1"]
    for covers_it in ("127.0.0.1", "127.0.1.1", "localhost", "0.0.0.0", ""):
        assert dashboard.addresses(covers_it) == [covers_it]  # never two listeners on one port


def test_an_address_the_pi_doesnt_have_yet_is_bound_on_linux(monkeypatch):
    """Python's socket module names IP_FREEBIND only from 3.14. Trixie has 3.13, so it's set by its number."""
    from hyperwheel import reach

    class Sock:
        def __init__(self):
            self.options = []

        def setsockopt(self, *args):
            self.options.append(args)

    sock = Sock()
    monkeypatch.setattr(reach.sys, "platform", "linux")
    reach.allow_unassigned_address(sock)
    assert sock.options == [(socket.IPPROTO_IP, 15, 1)]
    monkeypatch.setattr(reach.sys, "platform", "darwin")
    sock = Sock()
    reach.allow_unassigned_address(sock)
    assert sock.options == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only (CI)")
def test_the_hotspot_address_binds_before_the_hotspot_is_up():
    from http.server import BaseHTTPRequestHandler

    from hyperwheel import viewer
    for server_class in (dashboard.Server, viewer.Server):
        server = server_class(("192.168.99.1", 0), BaseHTTPRequestHandler)  # not an address this machine has
        server.server_close()


def test_the_pis_own_bookmark_and_start_page_open_the_upload_monitor_on_127_0_0_1():
    import re
    from pathlib import Path
    files = Path(__file__).resolve().parents[1] / "image/files"
    bookmarks = (files / "usr/local/share/hyperwheel/chromium-bookmarks.html").read_text()
    urls = {name: url for url, name in re.findall(r'<DT><A HREF="([^"]+)">([^<]+)</A>', bookmarks)}
    assert urls == {"Scanner": "https://10.42.0.1:8080/", "Upload Monitor": "http://127.0.0.1:5000/",
                    "Orthanc Explorer 2": "http://127.0.0.1:8442/ui/app/"}
    policy = json.loads((files / "etc/chromium/policies/managed/hyperwheel.json").read_text())
    assert policy["HomepageLocation"] == "http://127.0.0.1:5000/"
    assert policy["RestoreOnStartup"] == 4 and policy["RestoreOnStartupURLs"] == ["http://127.0.0.1:5000/"]
    assert policy["BrowserSignin"] == 0 and policy["SyncDisabled"] is True


def test_a_dataset_is_shown_by_its_client_id(env):
    from hyperwheel.config import Routing
    named = replace(env.routing.projects[PROJECT], client_id="unity/site-a-sandbox")
    env.routing = Routing(projects={PROJECT: named})
    env.store.add_study("1.2.8", state="ready", project=PROJECT, raw_checked_at=env.clock())
    env.store.add_study("1.2.9", state="receiving", operators_name="site_a/sandbox")
    s = snap(env)
    assert [c["project"] for c in s["studies"]] == ["unity/site-a-sandbox", "unity/site-a-sandbox"]
    assert s["keys"][0]["project"] == "unity/site-a-sandbox"


def test_a_session_sent_again_and_still_arriving_keeps_the_scanner_on(env):
    env.store.set_cursor(HEARTBEAT, int(env.clock()))
    env.store.add_study("1.2.70", state="unrouted", stable_at=None, operators_name="site_a/sandbox")
    s = snap(env)
    assert not s["on_pi_complete"] and s["headline"] == "Receiving images from the scanner" and s["studies"] == []


def test_a_session_sent_again_that_never_finished_stops_counting_as_receiving_after_an_hour(env):
    from hyperwheel.organiser import STUCK_S
    env.store.set_cursor(HEARTBEAT, int(env.clock()))
    env.store.add_study("1.2.71", state="unrouted", stable_at=None, operators_name="site_a/sandbox")
    env.advance(STUCK_S + 1)
    env.store.set_cursor(HEARTBEAT, int(env.clock()))
    assert snap(env)["on_pi_complete"]
