"""Telling "can't reach the scanner" apart from "no internet", and when operators may switch off (once all is on GRIP)."""

from __future__ import annotations

import socket
from dataclasses import replace

from hyperwheel import reach
from hyperwheel.dashboard import HEARTBEAT, snapshot
from hyperwheel.reach import SCANNER_NO_ANSWER, SCANNER_NO_LINK, SCANNER_OK, SCANNER_UNKNOWN_LINK
from hyperwheel.service import Service

GB = 1024 ** 3


def scanner_site(env):
    return replace(env.site, scanner=replace(env.site.scanner, host="10.42.0.1"))


def snap(env, free=50 * GB, site=None):
    env.store.set_cursor(HEARTBEAT, int(env.clock()))
    return snapshot(env.store, site or env.site, env.routing, now=env.clock(), free_bytes=lambda p: free)


def staged(env):
    """A study whose raw data has all been fetched: everything from the scanner is on the Pi."""
    study = env.stage_study(n_dicom=1, raw_sizes=())
    env.store.update_study(study.id, raw_checked_at=env.clock())
    return study


def texts(s):
    return [f["text"] for f in s["general"]]


def test_link_is_read_from_sysfs(tmp_path):
    (tmp_path / "eth0").mkdir()
    (tmp_path / "eth0" / "carrier").write_text("1\n")
    (tmp_path / "eth1").mkdir()
    (tmp_path / "eth1" / "carrier").write_text("0\n")
    assert reach.link_up("eth0", tmp_path) is True
    assert reach.link_up("eth1", tmp_path) is False
    assert reach.link_up("eth9", tmp_path) is None


def test_scanner_states(env):
    site = scanner_site(env)
    assert reach.scanner_state(env.site) is None  # no scanner set up
    assert reach.scanner_state(site, answers=lambda h, p: True, link=lambda i: False) == SCANNER_OK
    assert reach.scanner_state(site, answers=lambda h, p: False, link=lambda i: True) == SCANNER_NO_ANSWER
    assert reach.scanner_state(site, answers=lambda h, p: False, link=lambda i: False) == SCANNER_NO_LINK
    assert reach.scanner_state(site, answers=lambda h, p: False, link=lambda i: None) == SCANNER_UNKNOWN_LINK


def test_internet_probe_tries_each_grip_address(env):
    seen = []
    assert reach.internet_state(env.routing, answers=lambda h, p: seen.append((h, p)) or True) is True
    assert seen == [("127.0.0.1", int(env.fake.base_url.split(":")[2].split("/")[0]))]
    assert reach.internet_state(env.routing, answers=lambda h, p: False) is False


def test_tcp_probe_against_a_real_socket():
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        assert reach.tcp_answers("127.0.0.1", port, 1)
    assert not reach.tcp_answers("127.0.0.1", port, 1)


def test_scanner_not_answering_although_cable_connected(env):
    reach.record_scanner(env.store, SCANNER_NO_ANSWER, env.clock())
    reach.record_internet(env.store, False, env.clock())
    assert not any("scanner" in t for t in texts(snap(env)))  # a short blip isn't shown
    env.advance(reach.GRACE_S)
    reach.record_scanner(env.store, SCANNER_NO_ANSWER, env.clock())  # unchanged: keeps its start time
    s = snap(env)
    msgs = [f for f in s["general"] if "scanner" in f["text"]]
    assert len(msgs) == 1 and msgs[0]["level"] == "alert"
    assert "cable is connected" in msgs[0]["text"] and "isn't answering" in msgs[0]["text"]
    assert not any("not GRIP" in t for t in texts(s))  # one message, not a separate "no internet" one


def test_no_link_shows_at_once_as_scanner_off(env):
    reach.record_scanner(env.store, SCANNER_NO_LINK, env.clock())
    msgs = [f for f in snap(env)["general"] if "scanner" in f["text"]]
    assert msgs[0]["level"] == "flag" and "off or the cable is unplugged" in msgs[0]["text"]


def test_internet_down_while_scanner_answers(env):
    reach.record_scanner(env.store, SCANNER_OK, env.clock())
    reach.record_internet(env.store, False, env.clock())
    env.advance(reach.GRACE_S)
    t = texts(snap(env))
    assert any("can reach the scanner but not GRIP" in x for x in t)
    reach.record_internet(env.store, True, env.clock())
    assert not any("GRIP" in x and "reach" in x for x in texts(snap(env)))


def test_files_still_to_upload_keep_the_scanner_on_however_long_they_wait(env):
    staged(env)
    s = snap(env)
    assert s["headline"] == "Uploading to GRIP" and not s["safe_to_switch_off"] and s["on_pi_complete"]
    assert s["badge"] == "Please keep the scanner on"
    assert "keep the scanner on until everything is on GRIP" in s["subline"] and ";" not in s["subline"]
    env.advance(72 * 3600)  # no change after days: there's no switch-off after a wait any more
    assert snap(env)["badge"] == "Please keep the scanner on"
    assert snap(env, free=5 * GB)["badge"] == "Please keep the scanner on"


def test_safe_to_switch_off_only_once_everything_is_on_grip(env):
    s = snap(env)
    assert s["headline"] == "Everything is on GRIP" and s["safe_to_switch_off"]
    assert s["badge"] == "Everything is on GRIP: safe to switch off"


def test_nothing_waiting_is_safe(env):
    s = snap(env, free=5 * GB)
    assert s["headline"] == "Everything is on GRIP" and s["safe_to_switch_off"]


def test_waiting_to_upload_when_offline(env):
    staged(env)
    reach.record_scanner(env.store, SCANNER_NO_LINK, env.clock())
    s = snap(env)
    assert s["headline"] == "Waiting to upload to GRIP" and not s["safe_to_switch_off"]
    assert s["badge"] == "Please keep the scanner on"


def test_service_records_both(env):
    calls = []
    svc = Service(env.site, env.routing, orthanc=lambda: env.orthanc, keys=env.keys, clock=env.clock,
                  synced=lambda: False, scanner_probe=lambda site: calls.append("scanner") or SCANNER_NO_ANSWER,
                  internet_probe=lambda routing: calls.append("internet") or False)
    svc.upload_round(env.uploader())  # records even while the clock isn't set
    assert reach.read(env.store).internet is False

    class Idle:
        def follow(self): pass
        def pull_once(self): pass
        def housekeeping(self): pass

    svc.local_round(env.store, Idle(), Idle())
    r = reach.read(env.store)
    assert r.scanner == SCANNER_NO_ANSWER and r.scanner_since == env.clock()
    assert calls == ["internet", "scanner"]


def test_keep_the_scanner_on_shows_once(env):
    env.store.add_study("1.2.3", state="receiving", project=None, operators_name="site_a/sandbox")
    s = snap(env)
    assert s["headline"] == "Receiving images from the scanner" and s["subline"] == ""
    assert s["badge"] == "Please keep the scanner on"


def test_only_refused_or_held_files_left_is_safe_with_a_badge_of_its_own(env):
    study = env.store.add_study("1.2.90", state="uploading", project="SiteA-Sandbox", raw_checked_at=env.clock())
    f = env.store.add_file(kind="dicom", name="a.dcm", state="staged", study_id=study.id, sop_uid="1.2.90.1")
    env.store.move_file(f.id, "sending")
    env.store.move_file(f.id, "refused")
    s = snap(env)
    assert s["safe_to_switch_off"] and s["badge_style"] == "admin"
    assert s["badge"] == "Safe to switch off. Some files are not on GRIP. Ask your Hyperwheel admin if they stay."
    assert s["headline"] == "Not everything is on GRIP" and ";" not in s["subline"]
    held = env.store.add_study("1.2.91", state="held", project="SiteA-Sandbox")  # mixed logins
    env.store.add_file(kind="dicom", name="b.dcm", state="staged", study_id=held.id, sop_uid="1.2.91.1")
    s = snap(env)
    assert s["badge_style"] == "admin" and s["safe_to_switch_off"]  # its staged file waits for a person too
    other = env.store.add_study("1.2.92", state="ready", project="SiteA-Sandbox", raw_checked_at=env.clock())
    env.store.add_file(kind="dicom", name="c.dcm", state="staged", study_id=other.id, sop_uid="1.2.92.1")
    s = snap(env)  # a file that can still upload: keep the scanner on
    assert not s["safe_to_switch_off"] and s["badge"] == "Please keep the scanner on" and s["badge_style"] == "no"


def test_everything_on_grip_is_the_green_badge(env):
    s = snap(env)
    assert s["badge_style"] == "yes" and s["badge"] == "Everything is on GRIP: safe to switch off"


def test_a_site_file_that_still_sets_leave_on_after_hours_loads(tmp_path):
    from hyperwheel.config import load_site
    path = tmp_path / "site.toml"
    path.write_text('schema_version = 1\nsite = "s"\nstaging_dir = "/s"\nstate_db = "/s.db"\nkeys_dir = "/k"\n'
                    "[dashboard]\nleave_on_after_hours = 48\n"
                    '[grip]\napi_base_url = "https://grip.example.org/api"\ntoken_url = "https://grip.example.org/api/get/token"\n')
    assert load_site(path).site == "s"
