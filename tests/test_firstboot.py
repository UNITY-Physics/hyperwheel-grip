"""The first start of a newly flashed Pi: Orthanc's login and the hotspot, with nobody there."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from hyperwheel import cli, firstboot
from hyperwheel.installer import UNITS, Installer, Layout
from hyperwheel.sitesetup import BOOT_HOTSPOT, HOTSPOT_FILE, NM_DIR, Setup, remove_boot_copy

PASSWORD = "test-hotspot-pw-1"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def pi(tmp_path):
    layout = Layout(tmp_path)
    layout.orthanc_conf.mkdir(parents=True)
    return layout


def boot(layout, *, password=PASSWORD):
    said = []
    code = firstboot.first_boot(layout, say=said.append,
                                installer=Installer(Path("."), Path("."), layout, chown=lambda *a: None, say=said.append),
                                setup=Setup(layout=layout, chown=lambda path: None, admin_password=lambda site: ""))
    return code, "\n".join(said)


def card(layout, text=PASSWORD + "\n", mac_files=True) -> Path:
    copy = layout.p(BOOT_HOTSPOT)
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text(text)
    if mac_files:
        (copy.parent / "._hotspot-password").write_bytes(b"\x00\x05\x16\x07")
        (copy.parent / ".DS_Store").write_bytes(b"x")
        (copy.parent.parent / "._hyperwheel").write_bytes(b"\x00\x05\x16\x07")
    return copy


def test_with_a_password_on_the_card_the_hotspot_is_ready_and_the_card_is_clean(pi):
    copy = card(pi)
    code, said = boot(pi)
    assert code == 0
    connection = pi.p(NM_DIR) / HOTSPOT_FILE
    assert f"psk={PASSWORD}\n" in connection.read_text() and mode(connection) == 0o600
    assert json.loads((pi.etc / "hotspot.json").read_text())["password"] == PASSWORD
    assert mode(pi.etc / "hotspot.json") == 0o600
    assert not copy.parent.exists() and not (copy.parent.parent / "._hyperwheel").exists()
    assert PASSWORD not in said


def test_every_pi_makes_its_own_orthanc_login_and_certificate(pi, tmp_path):
    code, said = boot(pi)
    assert code == 0 and "Orthanc Explorer 2's certificate: made for this Pi." in said
    assert mode(pi.p("/etc/hyperwheel/viewer/key.pem")) == 0o600
    login = json.loads((pi.etc / "orthanc.json").read_text())
    assert mode(pi.etc / "orthanc.json") == 0o600
    assert mode(pi.orthanc_conf / "users.json") == 0o640
    assert json.loads((pi.orthanc_conf / "users.json").read_text()) == {
        "RegisteredUsers": {login["username"]: login["password"]}}
    other = Layout(tmp_path / "other")
    other.orthanc_conf.mkdir(parents=True)
    assert boot(other)[0] == 0
    assert json.loads((other.etc / "orthanc.json").read_text())["password"] != login["password"]


def test_a_published_image_with_no_password_waits_for_setup(pi):
    code, said = boot(pi)
    assert code == 0 and "hyperwheel setup will ask" in said
    assert not (pi.etc / "hotspot.json").exists() and not (pi.p(NM_DIR) / HOTSPOT_FILE).exists()
    assert (pi.etc / firstboot.MARKER).exists()


def test_an_unusable_password_is_deleted_never_shown_and_setup_asks(pi):
    copy = card(pi, "short\n")
    code, said = boot(pi)
    assert code == 0 and "can't be used" in said and "short\n" not in said
    assert not copy.exists() and not copy.parent.exists() and not (pi.etc / "hotspot.json").exists()


def test_it_runs_once(pi):
    assert boot(pi)[0] == 0
    before = (pi.etc / "orthanc.json").read_text()
    copy = card(pi, mac_files=False)  # never read again, even if a password appears
    code, said = boot(pi)
    assert code == 0 and "already done" in said
    assert (pi.etc / "orthanc.json").read_text() == before and copy.exists()


def test_a_failure_leaves_no_marker_and_the_next_start_tries_again(pi, monkeypatch):
    copy = card(pi)

    def full(*a, **k):
        raise OSError("no space left")

    monkeypatch.setattr(Setup, "store_hotspot", full)
    code, said = boot(pi)
    assert code == 1 and "next start tries again" in said
    assert copy.exists() and not (pi.etc / firstboot.MARKER).exists()  # the password isn't lost
    monkeypatch.undo()
    assert boot(pi)[0] == 0 and (pi.etc / "hotspot.json").exists()


def test_removing_the_copy_leaves_other_files_alone(pi):
    copy = card(pi)
    (copy.parent / "something-else").write_text("x")
    assert remove_boot_copy(pi) is True
    assert not copy.exists() and not (copy.parent / "._hotspot-password").exists()
    assert (copy.parent / "something-else").exists()


def test_only_macs_files_left_are_cleaned_up_too(pi):
    copy = card(pi)
    copy.unlink()
    assert remove_boot_copy(pi) is False
    assert not copy.parent.exists()


def test_the_unit_runs_before_the_network_and_orthanc_and_only_once():
    unit = UNITS["hyperwheel-firstboot.service"]
    assert "Before=NetworkManager.service orthanc.service" in unit
    assert "ConditionPathExists=!/etc/hyperwheel/first-boot-done" in unit
    assert "RequiresMountsFor=/boot/firmware" in unit


def test_people_cant_run_it_through_sudo_and_it_isnt_in_help():
    assert cli.needs_root(["first-boot"])
    assert cli.problem(["first-boot"]) is None
    assert "first-boot" not in cli.help_text(True, True)
    from hyperwheel.installer import sudo_rule
    assert "first-boot" not in sudo_rule(False)
