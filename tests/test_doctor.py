"""``hyperwheel doctor`` says plainly which connection is the problem."""

from __future__ import annotations

from dataclasses import replace

from hyperwheel import doctor
from hyperwheel.dashboard import HEARTBEAT
from hyperwheel.doctor import OK, PROBLEM, Probes


def run(env, *, scanner=True, link=True, grip=True, orthanc=True, beat=True):
    site = replace(env.site, scanner=replace(env.site.scanner, host="10.42.0.1"))
    if beat:
        env.store.set_cursor(HEARTBEAT, int(env.clock()))
    probes = Probes(link=lambda i: link, answers=lambda h, p: scanner if h == "10.42.0.1" else grip,
                    orthanc_answers=lambda url: orthanc, synced=lambda: True,
                    free_bytes=lambda p: 50 * 1024 ** 3, now=env.clock)
    results, code = doctor.run(site, env.routing, probes)
    return {what: (result, detail) for result, what, detail in results}, code


def test_all_well(env):
    r, code = run(env)
    assert code == 0
    assert r["Scanner"][0] == OK and r["GRIP over the internet"][0] == OK and r["Hyperwheel service"][0] == OK


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
