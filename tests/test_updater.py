"""The updater: channel file, download and checks, waiting until idle, install, health and key checks, rollback."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from test_release import keypair, sign, signers_file

from hyperwheel import updater as up
from hyperwheel.installer import Layout
from hyperwheel.release import FLAGS, MANIFEST, NAMESPACE, Channel, Manifest, ReleaseFile, sha256_file

BASE = "https://releases.example/download"


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakePi(up.Pi):
    def __init__(self, clock):
        self.clock = clock
        self.busy_until = 0.0
        self.new_images = [False]  # whether Orthanc got a new image, poll by poll
        self.worked: set[str] = set()
        self.keys: dict[str, dict[str, str]] = {}  # version -> project -> status after a start
        self.health: dict[str, str | None] = {}
        self.started: dict[str, float] = {}
        self.current = lambda: None

    def busy(self, quiet_s):
        return "receiving" if self.clock() < self.busy_until else None

    def orthanc_new_images(self, since):
        if since is None:
            return 5, False
        return 5, self.new_images.pop(0) if len(self.new_images) > 1 else self.new_images[0]

    def working_keys(self):
        return set(self.worked)

    def key_results(self, since):
        version = self.current()
        return dict(self.keys.get(version, {})) if self.started.get(version, -1) >= since else {}

    def health_problem(self, since):
        return self.health.get(self.current())


class Pi4:
    """A scratch Pi: releases on disk, a fake download site, a fake install.sh."""

    def __init__(self, tmp: Path, key: Path, signers: Path):
        self.tmp, self.key = tmp, key
        self.layout = Layout(tmp / "pi")
        self.paths = up.Paths(self.layout)
        self.paths.layout.etc.mkdir(parents=True)
        shutil.copy(signers, self.paths.signers)
        self.site: dict[str, Path] = {}  # url -> file
        self.clock = Clock()
        self.pi = FakePi(self.clock)
        self.pi.current = self.current
        self.installs: list[str] = []
        self.install_exit: dict[str, int] = {}
        self.stops_before_switch: set[str] = set()
        self.layout.releases.mkdir(parents=True)

    def current(self):
        c = self.layout.current
        return Path(os.readlink(c)).name if c.is_symlink() else None

    def have(self, version):
        """A release already installed (as by hand)."""
        (self.layout.releases / version / "release").mkdir(parents=True)
        if self.layout.current.is_symlink():
            self._link(self.layout.previous, os.readlink(self.layout.current))
        self._link(self.layout.current, f"releases/{version}")

    @staticmethod
    def _link(link, target):
        if link.is_symlink():
            link.unlink()
        link.symlink_to(target)

    def run(self, cmd):
        script = Path(cmd[1])
        version = script.parent.name.removeprefix("hyperwheel-") if script.parent.name != "release" \
            else script.parent.parent.name
        self.installs.append(version)
        if version in self.stops_before_switch:
            return 1
        (self.layout.releases / version / "release").mkdir(parents=True, exist_ok=True)
        if self.current() != version:
            self._link(self.layout.previous, os.readlink(self.layout.current))
            self._link(self.layout.current, f"releases/{version}")
        self.pi.started[version] = self.clock()
        return self.install_exit.get(version, 0)

    def fetch(self, url, dest, max_bytes):
        if url not in self.site:
            raise up.UpdateError(f"couldn't download {dest.name} (404)")
        shutil.copy(self.site[url], dest)

    def publish(self, version, *, channel="dev", flags=(), key=None, tamper=None, channel_key=None):
        """Build, sign and pack a release, and publish it with its channel file."""
        folder = self.tmp / "build" / f"hyperwheel-{version}"
        if folder.exists():
            shutil.rmtree(folder)
        (folder / "wheels").mkdir(parents=True)
        (folder / "wheels" / f"hyperwheel-{version}-py3-none-any.whl").write_bytes(b"wheel " + version.encode())
        (folder / "install.sh").write_text("#!/bin/sh\n")
        files = tuple(ReleaseFile(p.relative_to(folder).as_posix(), p.stat().st_size, sha256_file(p))
                      for p in sorted(folder.rglob("*")) if p.is_file())
        m = Manifest(version=version, channel=channel, built_at="2026-11-06T12:00:00Z", min_image=None,
                     flags={f: f in flags for f in FLAGS}, components={}, files=files)
        (folder / MANIFEST).write_text(m.to_json())
        sign(key or self.key, folder)
        if tamper:
            tamper(folder)
        tar = self.tmp / "build" / f"hyperwheel-{version}-arm64.tar"
        with tarfile.open(tar, "w") as t:
            t.add(folder, arcname=folder.name)
        ch = Channel(channel, version, tar.name, tar.stat().st_size, sha256_file(tar),
                     sha256_file(folder / MANIFEST), "2026-11-06T12:00:00Z")
        chdir = self.tmp / "build" / f"channel-{channel}"
        chdir.mkdir(exist_ok=True)
        (chdir / "channel.json").write_text(ch.to_json())
        (chdir / "channel.json.sig").unlink(missing_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(channel_key or self.key), "-n", NAMESPACE,
                        str(chdir / "channel.json")], check=True, capture_output=True)
        self.site[f"{BASE}/v{version}/{tar.name}"] = tar
        self.site[f"{BASE}/channel-{channel}/channel.json"] = chdir / "channel.json"
        self.site[f"{BASE}/channel-{channel}/channel.json.sig"] = chdir / "channel.json.sig"
        return tar

    def updater(self, channel="dev"):
        return up.Updater(self.paths, channel, BASE, self.pi, fetch=self.fetch, run=self.run, clock=self.clock,
                          sleep=self.clock.sleep)


@pytest.fixture
def key(tmp_path):
    return keypair(tmp_path, "signer")


@pytest.fixture
def pi(tmp_path, key):
    p = Pi4(tmp_path, key, signers_file(tmp_path, ("release-signer-1", key)))
    p.have("0.1.2")
    return p


def test_a_newer_release_is_installed_once_the_pi_is_idle(pi):
    pi.publish("0.1.3")
    pi.pi.busy_until = pi.clock() + 3600  # an exam is coming in
    result = pi.updater().check()
    assert result == "updated: 0.1.2 -> 0.1.3"
    assert pi.current() == "0.1.3" and os.readlink(pi.layout.previous) == "releases/0.1.2"
    # after the exam, plus 30 quiet minutes (counted from the last poll that saw it busy)
    assert pi.pi.started["0.1.3"] >= 1_800_000_000 + 3600 - up.POLL_S + up.QUIET_S
    assert list(pi.paths.downloads.iterdir()) == [pi.paths.downloads / "channel"]
    status = json.loads(pi.paths.status.read_text())
    assert status["current"] == "0.1.3" and status["result"] == result


def test_only_current_and_previous_releases_are_kept(pi):
    for v in ("0.1.3", "0.1.4"):
        pi.publish(v)
        assert pi.updater().check().startswith("updated")
    assert sorted(p.name for p in pi.layout.releases.iterdir()) == ["0.1.3", "0.1.4"]


def test_old_releases_are_removed_oldest_first(pi, caplog):
    for v in ("0.1.10", "0.1.9", "0.1.11"):
        pi.have(v)
    pi.publish("0.1.12")
    with caplog.at_level("INFO"):
        pi.updater().check()
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("Removed")] == \
        ["Removed old release 0.1.2.", "Removed old release 0.1.9.", "Removed old release 0.1.10."]


def test_nothing_happens_when_the_channel_names_the_current_or_an_older_version(pi):
    pi.publish("0.1.2")
    assert pi.updater().check().startswith("up to date: 0.1.2")
    pi.publish("0.1.1")
    assert pi.updater().check().startswith("up to date: 0.1.2")
    assert pi.installs == [] and pi.current() == "0.1.2"


def test_a_held_pi_downloads_nothing(pi):
    pi.publish("0.1.3")
    assert up.main(["hold", "--reason", "site visit"], pi.paths) == 0
    assert pi.updater().check() == "on hold at 0.1.2: site visit"
    assert not pi.paths.downloads.exists() or not any(pi.paths.downloads.iterdir())
    assert up.main(["release-hold"], pi.paths) == 0
    assert pi.updater().check() == "updated: 0.1.2 -> 0.1.3"


def test_another_channels_file_is_refused(pi):
    pi.publish("0.1.3", channel="dev")
    pi.site[f"{BASE}/channel-stable/channel.json"] = pi.site[f"{BASE}/channel-dev/channel.json"]
    pi.site[f"{BASE}/channel-stable/channel.json.sig"] = pi.site[f"{BASE}/channel-dev/channel.json.sig"]
    result = pi.updater("stable").check()
    assert "dev channel's file, not stable's" in result and pi.installs == []


def test_a_channel_file_from_an_unknown_signer_is_refused(pi, tmp_path):
    pi.publish("0.1.3", channel_key=keypair(tmp_path, "stranger"))
    assert pi.updater().check().startswith("no update: the dev channel file can't be used") and pi.installs == []


def test_a_release_signed_by_an_unknown_key_is_refused(pi, tmp_path):
    pi.publish("0.1.3", key=keypair(tmp_path, "stranger"))
    assert pi.updater().check().startswith("no update: release 0.1.3 can't be used") and pi.installs == []


def test_a_release_with_a_changed_file_is_refused(pi):
    pi.publish("0.1.3", tamper=lambda f: (f / "install.sh").write_text("#!/bin/sh\nrm -rf /\n"))
    assert pi.updater().check().startswith("no update: release 0.1.3 can't be used") and pi.installs == []


def test_a_tar_that_does_not_match_the_channel_file_is_refused(pi):
    tar = pi.publish("0.1.3")
    with open(tar, "ab") as fh:
        fh.write(b"\0" * 512)
    result = pi.updater().check()
    assert "checksum" in result and pi.installs == []


def test_a_failed_health_check_rolls_back_and_the_version_is_not_tried_again(pi):
    pi.publish("0.1.3")
    pi.pi.health["0.1.3"] = "Hyperwheel's service didn't start"
    result = pi.updater().check()
    assert result == ("rolled back to 0.1.2: 0.1.3 failed (Hyperwheel's service didn't start within 3 minutes) "
                      "and won't be tried again")
    assert pi.installs == ["0.1.3", "0.1.2"] and pi.current() == "0.1.2"
    # 0.1.2 had no previous release before: the failed one doesn't become it, and is removed
    assert not pi.layout.previous.is_symlink() and not (pi.layout.releases / "0.1.3").exists()
    assert pi.updater().check().startswith("staying on 0.1.2: 0.1.3 failed here before")
    pi.publish("0.1.4")
    assert pi.updater().check() == "updated: 0.1.2 -> 0.1.4"


def test_after_a_rollback_previous_is_the_release_it_was_before(pi):
    pi.have("0.1.3")
    pi.publish("0.1.4")
    pi.pi.health["0.1.4"] = "Hyperwheel's service didn't start"
    assert pi.updater().check().startswith("rolled back to 0.1.3")
    assert os.readlink(pi.layout.previous) == "releases/0.1.2"
    assert sorted(f.name for f in pi.layout.releases.iterdir()) == ["0.1.2", "0.1.3"]
    assert [f.name for f in pi.paths.downloads.iterdir()] == ["channel"]


def test_an_alert_if_the_old_release_is_not_healthy_after_the_rollback_either(pi):
    pi.publish("0.1.3")
    pi.pi.health = {"0.1.3": "Hyperwheel's service didn't start", "0.1.2": "Orthanc didn't answer"}
    result = pi.updater().check()
    assert result.startswith("ALERT: 0.1.3 failed") and "0.1.2 is back on but isn't healthy either" in result
    assert pi.current() == "0.1.2" and "0.1.3" in json.loads(pi.paths.failed.read_text())


def test_the_passing_checks_are_logged(pi, caplog):
    pi.publish("0.1.3")
    pi.pi.worked = {"SiteA-Sandbox", "SiteA-Other"}
    pi.pi.keys = {"0.1.3": {"SiteA-Sandbox": "working"}}
    with caplog.at_level("INFO"):
        pi.updater().check()
    lines = [r.getMessage() for r in caplog.records]
    assert any(line.startswith("Health check passed") for line in lines)
    assert "Key check: SiteA-Sandbox working." in lines
    assert "Key check: SiteA-Other not checked yet (GRIP not reached)." in lines


def test_a_key_that_stops_working_with_the_new_release_rolls_back(pi):
    pi.publish("0.1.3")
    pi.pi.worked = {"SiteA-Sandbox", "SiteA-Other"}
    pi.pi.keys = {"0.1.3": {"SiteA-Sandbox": "rejected", "SiteA-Other": "working"},
                  "0.1.2": {"SiteA-Sandbox": "working", "SiteA-Other": "working"}}
    result = pi.updater().check()
    assert result.startswith("rolled back to 0.1.2: 0.1.3 failed (the key for SiteA-Sandbox worked before")
    assert "0.1.3" in json.loads(pi.paths.failed.read_text())


def test_a_key_revoked_meanwhile_is_not_blamed_on_the_release(pi):
    pi.publish("0.1.3")
    pi.pi.worked = {"SiteA-Sandbox"}
    pi.pi.keys = {"0.1.3": {"SiteA-Sandbox": "rejected"}, "0.1.2": {"SiteA-Sandbox": "rejected"}}
    result = pi.updater().check()
    assert result.startswith("updated: 0.1.2 -> 0.1.3. The key for SiteA-Sandbox doesn't work with either release")
    assert pi.installs == ["0.1.3", "0.1.2", "0.1.3"] and pi.current() == "0.1.3"
    assert os.readlink(pi.layout.previous) == "releases/0.1.2" and not pi.paths.failed.exists()


def test_a_revoked_key_and_a_release_that_then_fails_to_start_ends_on_the_old_release(pi):
    pi.publish("0.1.3")
    pi.pi.worked = {"SiteA-Sandbox"}
    pi.pi.keys = {"0.1.3": {"SiteA-Sandbox": "rejected"}, "0.1.2": {"SiteA-Sandbox": "rejected"}}
    healthy_once = iter([None])
    pi.pi.health_problem = lambda since: next(healthy_once, "Hyperwheel's service didn't start") \
        if pi.current() == "0.1.3" else None
    result = pi.updater().check()
    assert result.startswith("rolled back to 0.1.2") and "failed to start again" in result
    assert pi.installs == ["0.1.3", "0.1.2", "0.1.3", "0.1.2"] and pi.current() == "0.1.2"


def test_grip_not_answering_is_not_a_reason_to_roll_back(pi):
    pi.publish("0.1.3")
    pi.pi.worked = {"SiteA-Sandbox"}
    pi.pi.keys = {}  # no key check result at all: offline
    assert pi.updater().check() == "updated: 0.1.2 -> 0.1.3"


def test_orthanc_receiving_counts_as_busy_even_if_the_service_is_down(pi):
    pi.publish("0.1.3")
    pi.pi.new_images = [True, True, True, False]  # images arriving for the first three polls
    start = pi.clock()
    assert pi.updater().check() == "updated: 0.1.2 -> 0.1.3"
    assert pi.pi.started["0.1.3"] - start == 2 * up.POLL_S + up.QUIET_S  # quiet from the last poll that saw one


def test_a_pi_that_is_never_idle_waits_for_the_next_check_and_keeps_the_download(pi):
    pi.publish("0.1.3")
    pi.pi.busy_until = pi.clock() + 24 * 3600
    result = pi.updater().check()
    assert "Waiting for the Pi to be idle" in result and pi.installs == []
    assert (pi.paths.downloads / "hyperwheel-0.1.3-arm64.tar").exists()
    pi.pi.busy_until = 0
    downloads = []
    pi.fetch, real = (lambda url, dest, n: (downloads.append(url), real(url, dest, n))), pi.fetch
    assert pi.updater().check() == "updated: 0.1.2 -> 0.1.3"
    assert not any(u.endswith(".tar") for u in downloads)  # the checked tar was kept, not fetched again


def test_flagged_releases_need_update_now_and_database_changes_are_never_automatic(pi):
    pi.publish("0.1.3", flags=("manual_only",))
    assert "apply it with 'hyperwheel update now'" in pi.updater().check() and pi.installs == []
    start = pi.clock()
    assert pi.updater().check(now=True) == "updated: 0.1.2 -> 0.1.3"
    assert pi.pi.started["0.1.3"] - start < up.QUIET_S  # update now: one quiet minute, not thirty
    pi.publish("0.1.4", flags=("orthanc_db_change",))
    assert "must be installed by hand" in pi.updater().check(now=True) and pi.current() == "0.1.3"


def test_an_install_that_stops_before_the_switch_changes_nothing(pi):
    pi.publish("0.1.3")
    pi.stops_before_switch.add("0.1.3")
    result = pi.updater().check()
    assert result == "staying on 0.1.2: installing 0.1.3 stopped before the switch (exit 1)"
    assert pi.installs == ["0.1.3"] and pi.current() == "0.1.2"


def test_the_install_uses_the_release_signers_and_the_service_account(pi):
    pi.layout.units.mkdir(parents=True)
    (pi.layout.units / "hyperwheel.service").write_text("[Service]\nUser=hwsvc\n")
    calls = []
    run = pi.run
    pi.run = lambda cmd: (calls.append(cmd), run(cmd))[1]
    pi.publish("0.1.3")
    pi.updater().check()
    assert calls[0][0] == "sh" and calls[0][1].endswith("hyperwheel-0.1.3/install.sh")
    assert calls[0][2:] == ["--signers", str(pi.paths.signers), "--service-user", "hwsvc"]


def test_only_https_downloads_except_to_this_machine(tmp_path):
    with pytest.raises(up.UpdateError, match="refusing"):
        up.download("http://releases.example/x", tmp_path / "x", 10)
    with pytest.raises(up.UpdateError, match="refusing"):
        up.download("file:///etc/passwd", tmp_path / "x", 10)


def test_status_shows_the_last_check_and_a_hold(pi, capsys):
    pi.publish("0.1.3")
    pi.updater().check()
    up.main(["hold"], pi.paths)
    up.main(["status"], pi.paths)
    out = capsys.readouterr().out
    assert "Release: 0.1.3 (on hold)" in out and "updated: 0.1.2 -> 0.1.3" in out


def test_every_check_updates_the_last_check_time(pi):
    pi.publish("0.1.3")
    pi.updater().check()
    first = json.loads(pi.paths.status.read_text())["checked_at"]
    pi.clock.sleep(5)
    assert pi.updater().check().startswith("up to date")
    assert json.loads(pi.paths.status.read_text())["checked_at"] != first


def test_update_now_prints_the_result_in_plain_words(pi, capsys, monkeypatch):
    monkeypatch.setattr(up, "start_service", lambda: pi.updater()._status(
        "rolled back to 0.1.2: 0.1.3 failed (Hyperwheel's service didn't start within 3 minutes) "
        "and won't be tried again"))
    assert up.main(["now"], pi.paths) == 1
    out = capsys.readouterr().out
    assert "rolled back to 0.1.2" in out and "systemctl" not in out


def test_update_now_says_so_if_the_check_left_no_result(pi, capsys, monkeypatch):
    monkeypatch.setattr(up, "start_service", lambda: None)
    assert up.main(["now"], pi.paths) == 1
    assert "stopped without a result" in capsys.readouterr().out


def test_the_upload_monitor_and_the_doctor_show_a_hold(pi):
    assert up.hold_text(pi.paths) is None
    up.main(["hold", "--reason", "site visit"], pi.paths)
    assert "Updates are on hold since" in up.hold_text(pi.paths) and "site visit" in up.hold_text(pi.paths)
    assert up.summary(pi.paths).startswith("Updates are on hold") and "No update check has run yet" in \
        up.summary(pi.paths)


def test_the_update_command_works_without_site_settings():
    r = subprocess.run([sys.executable, "-m", "hyperwheel", "update", "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "release-hold" in r.stdout
