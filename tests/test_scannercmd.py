"""``hyperwheel scanner password``: tried on the scanner before it's saved; never shown."""

from __future__ import annotations

import json
import stat
from dataclasses import replace

import pytest
from test_hostkeys import Scanner
from test_keyscmd import Scripted

from hyperwheel import scannercmd
from hyperwheel.config import ScannerSettings, SiteConfig
from hyperwheel.rawpull import LoginRefused, ScannerError
from hyperwheel.scannercmd import ENTITLEMENT, save, set_password, try_on_scanner

OLD = "old-scanner-pw-not-for-logs"
NEW = "new-scanner-pw-not-for-logs"


@pytest.fixture
def site(tmp_path):
    return SiteConfig(site="test", staging_dir=tmp_path / "staging", state_db=tmp_path / "state.db",
                      keys_dir=tmp_path / "keys",
                      scanner=ScannerSettings(host="10.42.0.1", known_hosts=str(tmp_path / "kh"), timeout_s=5))


@pytest.fixture
def stored(tmp_path):
    path = tmp_path / "scanner.json"
    path.write_text(json.dumps({"password": OLD, "note": "kept"}))
    path.chmod(0o600)
    return path


def run(site, path, typed, *, test=None, restarts=None, root=True, unreachable=None):
    restarts = restarts if restarts is not None else []
    tried = []

    def default_test(site, password):
        tried.append(password)
        return 4
    code = set_password(site, typed.console(), path=path, test=test or default_test,
                        unreachable=unreachable or (lambda site: "the scanner doesn't answer"),
                        restart=lambda: restarts.append(1) or "Hyperwheel was restarted to use it.",
                        is_root=lambda: root)
    return code, restarts, tried


def secret_free(typed):
    assert NEW not in typed.text and OLD not in typed.text


def test_a_password_the_scanner_accepts_is_saved_root_only_and_the_service_restarted(site, stored):
    typed = Scripted(hidden=[NEW])
    code, restarts, tried = run(site, stored, typed)
    assert code == 0, typed.text
    assert tried == [NEW] and restarts == [1]
    assert json.loads(stored.read_text()) == {"password": NEW, "note": "kept"}
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert "Scanner: 10.42.0.1, login rrdf" in typed.text
    assert "The scanner accepted it (4 raw-data folders found)." in typed.text and "Saved." in typed.text
    assert not list(stored.parent.glob(".scanner.json.new"))
    secret_free(typed)


def test_a_refused_password_is_never_saved_and_mentions_the_entitlement(site, stored):
    typed = Scripted(hidden=[NEW])

    def refused(site, password):
        raise LoginRefused("the scanner refused the raw-data login")
    code, restarts, _ = run(site, stored, typed, test=refused)
    assert code == 1 and restarts == []
    assert json.loads(stored.read_text())["password"] == OLD
    assert ("The scanner refused this password. Nothing was saved. The password already on this Pi is "
            "unchanged.") in typed.shown
    assert typed.shown[-1] == "Check the password and try again. " + ENTITLEMENT
    assert ENTITLEMENT == ("If you're sure the password is right, the scanner may be missing the RRDF "
                           "entitlement. Tell your Hyperwheel admin.")
    secret_free(typed)


def test_a_scanner_that_cannot_be_reached_saves_nothing_and_says_why(site, stored):
    typed = Scripted(hidden=[NEW])

    def down(site, password):
        raise ScannerError("SSH to the scanner failed (TimeoutError)")
    code, restarts, _ = run(site, stored, typed, test=down,
                            unreachable=lambda site: "the scanner cable has no link: check the cable")
    assert code == 1 and restarts == []
    assert json.loads(stored.read_text())["password"] == OLD
    assert ("The password couldn't be tried: the scanner cable has no link: check the cable. Nothing was saved. "
            "The password already on this Pi is unchanged.") in typed.shown


def test_the_same_password_again_changes_nothing(site, stored):
    before = stored.stat().st_mtime_ns
    typed = Scripted(hidden=[OLD])
    code, restarts, _ = run(site, stored, typed)
    assert code == 0 and restarts == []
    assert stored.stat().st_mtime_ns == before
    assert "It's the password already on this Pi, so nothing needed to change." in typed.shown


def test_enter_stops_before_anything_is_tried(site, stored):
    for hidden in ([""], [], ["   "]):
        typed = Scripted(hidden=hidden)
        code, restarts, tried = run(site, stored, typed)
        assert code == 1 and tried == [] and restarts == []
        assert typed.shown[-1] == "Stopped. Nothing was saved; the password on this Pi is as it was."


def test_a_pasted_line_break_or_space_is_refused_before_trying(site, stored):
    typed = Scripted(hidden=[" " + NEW, NEW + "\n", NEW])
    code, _, tried = run(site, stored, typed)
    assert code == 0 and tried == [NEW]
    assert sum("Paste only the password." in line for line in typed.shown) == 2


def test_a_pi_with_no_password_yet_gets_one(site, tmp_path):
    path = tmp_path / "none" / "scanner.json"
    path.parent.mkdir()
    typed = Scripted(hidden=[NEW])
    code, restarts, _ = run(site, path, typed)
    assert code == 0 and restarts == [1]
    assert json.loads(path.read_text()) == {"password": NEW}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_only_root_and_only_with_a_scanner_address(site, stored):
    typed = Scripted(hidden=[NEW])
    assert run(site, stored, typed, root=False)[0] == 2
    assert typed.shown == ["'hyperwheel scanner password' needs administrator rights. Run it as root."]
    typed = Scripted(hidden=[NEW])
    assert run(replace(site, scanner=ScannerSettings(host="")), stored, typed)[0] == 1
    assert "no scanner address" in typed.text


def test_a_failed_restart_says_so(site, stored):
    typed = Scripted(hidden=[NEW])

    def broken():
        raise RuntimeError("systemctl restart ended with exit 1")
    code = set_password(site, typed.console(), path=stored, test=lambda s, p: 1, restart=broken,
                        is_root=lambda: True)
    assert code == 1 and json.loads(stored.read_text())["password"] == NEW
    assert "Hyperwheel couldn't be restarted" in typed.shown[-1]


def test_save_replaces_a_file_another_user_could_read_with_a_root_only_one(tmp_path):
    path = tmp_path / "scanner.json"
    path.write_text("not json")
    path.chmod(0o644)
    save(path, NEW)
    assert json.loads(path.read_text()) == {"password": NEW}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ---- against an SSH server ------------------------------------------------------------------------


@pytest.fixture
def scanner():
    s = Scanner()
    yield s
    s.close()


def test_the_try_logs_in_lists_the_raw_data_folder_and_records_no_key(site, scanner, tmp_path):
    local = replace(site, scanner=replace(site.scanner, host="127.0.0.1", port=scanner.port))
    scanner.serve_one()
    assert try_on_scanner(local, "scanner-pw") == 1
    scanner.serve_one()
    with pytest.raises(LoginRefused):
        try_on_scanner(local, "old-pw")
    assert not (tmp_path / "state.db").exists()  # the service records keys, not this command


def test_the_command_says_why_the_scanner_cannot_be_reached(site, monkeypatch):
    from hyperwheel import reach
    monkeypatch.setattr(reach, "scanner_state", lambda site: reach.SCANNER_NO_LINK)
    assert "no link" in scannercmd.why_unreachable(site)
    monkeypatch.setattr(reach, "scanner_state", lambda site: reach.SCANNER_NO_ANSWER)
    assert "doesn't answer" in scannercmd.why_unreachable(site)


def test_it_says_who_can_give_the_new_password(site, stored):
    typed = Scripted(hidden=[""])
    run(site, stored, typed)
    assert typed.shown[0] == ("Set the scanner's raw-data password on this Pi. It changes when the scanner's "
                              "software is upgraded. Your Hyperwheel admin can tell you the new one.")
