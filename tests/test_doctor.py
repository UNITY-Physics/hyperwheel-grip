"""``hyperwheel doctor`` says plainly which connection is the problem."""

from __future__ import annotations

import socket
import time
from dataclasses import replace

from hyperwheel import doctor, reach
from hyperwheel.dashboard import HEARTBEAT
from hyperwheel.doctor import NOTE, OK, PROBLEM, Probes


def run(env, *, scanner=True, link=True, grip=True, orthanc=True, beat=True, lookup=lambda name: 0.01,
        connect_state=None, viewer=True):
    site = replace(env.site, scanner=replace(env.site.scanner, host="10.42.0.1"))
    if beat:
        env.store.set_cursor(HEARTBEAT, int(env.clock()))
    probes = Probes(link=lambda i: link, answers=lambda h, p: scanner if h == "10.42.0.1" else grip, lookup=lookup,
                    connect_state=lambda: connect_state, viewer_answers=lambda: viewer,
                    firewall_on=lambda: True,
                    etc=site.state_db.parent,
                    orthanc_answers=lambda url: orthanc, synced=lambda: True,
                    free_bytes=lambda p: 50 * 1024 ** 3, now=env.clock)
    results, code = doctor.run(site, env.routing, probes)
    return {what: (result, detail) for result, what, detail in results}, code


def test_all_well(env):
    r, code = run(env)
    assert code == 0
    assert r["Scanner"][0] == OK and r["GRIP over the internet"][0] == OK and r["Hyperwheel service"][0] == OK


def test_name_lookups_quick(env):
    r, code = run(env, lookup=lambda name: 0.2)
    assert code == 0 and r["Name lookups"] == (OK, "quick (usual names 0.2 s, a new name 0.2 s)")


def test_slow_name_lookups_are_a_note_showing_which_kind_is_slow(env):
    r, code = run(env, lookup=lambda name: 24.0 if name.startswith("hyperwheel-check-") else 0.1)
    assert code == 0 and r["Name lookups"][0] == NOTE
    assert "slow (usual names 0.1 s, a new name 24.0 s)" in r["Name lookups"][1]
    assert "hyperwheel-check-" not in r["Name lookups"][1]


def test_no_answer_to_a_name_lookup_is_a_problem_and_stops_at_the_first(env):
    asked = []

    def lookup(name):
        asked.append(name)

    r, code = run(env, lookup=lookup)
    assert code == 1 and r["Name lookups"][0] == PROBLEM and "no answer within 30 s" in r["Name lookups"][1]
    assert len(asked) == 1


def test_name_lookups_blame_the_scanner_when_it_is_down(env):
    r, _ = run(env, scanner=False, link=False, grip=False, lookup=lambda name: None)
    assert r["Name lookups"][0] == NOTE and "can't reach the scanner" in r["Name lookups"][1]


def test_lookup_names_are_each_grip_host_and_github_and_never_an_address(env):
    names = reach.lookup_names(env.routing)
    assert "github.com" in names and names == sorted(set(names))
    assert not any(n.replace(".", "").isdigit() for n in names)


def test_lookup_counts_no_such_name_as_an_answer_and_a_hang_as_none(monkeypatch):
    def no_such_name(*a, **k):
        raise socket.gaierror(socket.EAI_NONAME, "no such name")

    monkeypatch.setattr(socket, "getaddrinfo", no_such_name)
    assert reach.lookup_seconds("x.example.org") is not None

    def no_server(*a, **k):
        raise socket.gaierror(socket.EAI_AGAIN, "try again")

    monkeypatch.setattr(socket, "getaddrinfo", no_server)
    assert reach.lookup_seconds("x.example.org") is None

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: time.sleep(1))
    assert reach.lookup_seconds("x.example.org", timeout_s=0.01) is None


def test_viewer_not_answering(env):
    r, code = run(env, viewer=False)
    assert code == 1 and r["Orthanc Explorer 2"] == (PROBLEM, "not answering. Uploads aren't affected.")


def test_connect_signed_in_and_not_signed_in(env):
    (env.site.state_db.parent / "site.toml").write_text("")  # set up
    now = env.clock()
    r, code = run(env, connect_state={"signed_in": True, "checked_at": now})
    assert code == 0 and r["Raspberry Pi Connect"] == (OK, "signed in")
    r, code = run(env, connect_state={"signed_in": False, "checked_at": now})
    assert code == 1 and r["Raspberry Pi Connect"][0] == PROBLEM
    assert "Your Hyperwheel admin can't reach this Pi" in r["Raspberry Pi Connect"][1]
    r, code = run(env, connect_state=None)
    assert code == 0 and r["Raspberry Pi Connect"] == (NOTE, "not checked yet")


def test_scanner_not_answering_with_cable_connected(env):
    r, code = run(env, scanner=False, link=True, grip=False)
    assert code == 1
    assert r["Scanner"][0] == PROBLEM and "cable is connected" in r["Scanner"][1]
    assert "can't reach the scanner" in r["GRIP over the internet"][1]


def test_scanner_off_or_unplugged(env):
    r, _ = run(env, scanner=False, link=False, grip=False)
    assert "off or the cable is unplugged" in r["Scanner"][1]


def test_no_internet_while_scanner_answers(env):
    r, code = run(env, grip=False)
    assert code == 1 and r["Scanner"][0] == OK
    assert r["GRIP over the internet"][0] == PROBLEM and "although the scanner is" in r["GRIP over the internet"][1]


def test_service_and_orthanc_down(env):
    r, code = run(env, orthanc=False, beat=False)
    assert code == 1
    assert r["Hyperwheel service"][0] == PROBLEM and r["Orthanc (receives the scanner's DICOM)"][0] == PROBLEM


def test_no_patient_data_printed(env, capsys):
    env.stage_study(n_dicom=1, raw_sizes=())
    results, _ = run(env)
    doctor.print_report(env.site, [(res, what, detail) for what, (res, detail) in results.items()])
    out = capsys.readouterr().out
    from conftest import PATIENT_ID
    assert PATIENT_ID not in out and "Hyperwheel doctor" in out


def test_a_refused_raw_data_login_says_how_to_set_the_new_password(env):
    env.store.raise_flag("RAW_LOGIN_REFUSED", "The scanner refused the RRDF login, ... Set the new password "
                         "with: hyperwheel scanner password. ...", level="alert")
    r, code = run(env)
    assert code == 1
    assert r["Scanner"][0] == OK  # the scanner answers; only its RRDF login is refused
    assert r["RRDF from the scanner"][0] == PROBLEM
    assert "hyperwheel scanner password" in r["RRDF from the scanner"][1]


def test_it_shows_the_grip_address_the_pi_uses_and_checks_it_with_no_datasets_yet(env):
    from hyperwheel.config import GripSettings, Routing
    env.site = replace(env.site, grip=GripSettings("https://grip.example.org/api", "https://grip.example.org/t"))
    r, _ = run(env)
    # the env's one dataset still has its own address (a Pi set up before the address was site-wide)
    assert r["GRIP address"] == (NOTE, f"https://grip.example.org/api (and for some datasets: {env.fake.base_url})")
    env.routing = Routing(projects={})
    seen = []
    site = replace(env.site, scanner=replace(env.site.scanner, host="10.42.0.1"))
    probes = Probes(link=lambda i: True, answers=lambda h, p: seen.append((h, p)) or True, lookup=lambda n: 0.01,
                    connect_state=lambda: None, viewer_answers=lambda: True, firewall_on=lambda: True,
                    etc=site.state_db.parent, orthanc_answers=lambda url: True, synced=lambda: True,
                    free_bytes=lambda p: 50 * 1024 ** 3, now=env.clock)
    results, _ = doctor.run(site, env.routing, probes)
    r = {what: (result, detail) for result, what, detail in results}
    assert r["GRIP address"] == (NOTE, "https://grip.example.org/api")
    assert ("grip.example.org", 443) in seen and r["GRIP over the internet"][0] == OK
    assert r["GRIP datasets"] == (NOTE, "none yet. To add one, type: hyperwheel datasets add")
