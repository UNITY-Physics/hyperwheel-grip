"""Channel files: which release a channel runs, signed like a release, and the tools that publish them."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_release import keypair, make_release, signers_file

from hyperwheel.release import (CHANNEL_FILE, CHANNEL_SIGNATURE, NAMESPACE, Channel, ReleaseError, parse_channel,
                                version_key, verify_channel)

REPO = Path(__file__).resolve().parents[1]
TAR_SHA = "a" * 64
MANIFEST_SHA = "b" * 64


def channel(name="dev", version="0.1.0", **kw) -> Channel:
    return Channel(name, version, f"hyperwheel-{version}-arm64.tar", 1000, TAR_SHA, MANIFEST_SHA,
                   "2026-11-06T15:00:00Z", **kw)


def signed(tmp: Path, key: Path, ch: Channel, folder: str = "ch") -> Path:
    out = tmp / folder
    out.mkdir()
    (out / CHANNEL_FILE).write_text(ch.to_json())
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(out / CHANNEL_FILE)],
                   check=True, capture_output=True)
    return out


@pytest.fixture
def key(tmp_path):
    return keypair(tmp_path, "signer")


@pytest.fixture
def signers(tmp_path, key):
    return signers_file(tmp_path, ("release-signer-1", key))


def test_a_signed_channel_file_is_accepted(tmp_path, key, signers):
    ch, who = verify_channel(signed(tmp_path, key, channel("pilot", "0.2.0")), signers, expected="pilot")
    assert (ch.channel, ch.version, who) == ("pilot", "0.2.0", "release-signer-1")


def test_another_channels_file_is_refused(tmp_path, key, signers):
    with pytest.raises(ReleaseError, match="dev channel's file, not stable's"):
        verify_channel(signed(tmp_path, key, channel("dev")), signers, expected="stable")


def test_a_changed_channel_file_is_refused(tmp_path, key, signers):
    folder = signed(tmp_path, key, channel("stable", "0.1.0"))
    data = json.loads((folder / CHANNEL_FILE).read_text())
    data.update(version="0.9.0", tar="hyperwheel-0.9.0-arm64.tar")
    (folder / CHANNEL_FILE).write_text(json.dumps(data))
    with pytest.raises(ReleaseError):
        verify_channel(folder, signers, expected="stable")


def test_a_channel_file_signed_by_an_unknown_key_is_refused(tmp_path, signers):
    with pytest.raises(ReleaseError):
        verify_channel(signed(tmp_path, keypair(tmp_path, "stranger"), channel()), signers)


@pytest.mark.parametrize("change, message", [
    ({"tar": "hyperwheel-0.2.0-arm64.tar"}, "tar name"),
    ({"tar": "../evil.tar"}, "tar name"),
    ({"channel": "nightly"}, "must be one of"),
    ({"version": "1.0"}, "not a release version"),
    ({"tar_sha256": "x"}, "tar_sha256"),
    ({"tar_size": 0}, "size"),
    ({"name": "hyperwheel-release"}, "not a Hyperwheel channel file"),
])
def test_bad_channel_files_are_refused(change, message):
    data = json.loads(channel().to_json())
    data.update(change)
    with pytest.raises(ReleaseError, match=message):
        parse_channel(json.dumps(data))


def test_a_signed_manifest_cannot_pass_as_a_channel_file(tmp_path, key, signers):
    release = make_release(tmp_path, key)
    folder = tmp_path / "fake-channel"
    folder.mkdir()
    (folder / CHANNEL_FILE).write_bytes((release / "manifest.json").read_bytes())
    (folder / CHANNEL_SIGNATURE).write_bytes((release / "manifest.json.sig").read_bytes())
    with pytest.raises(ReleaseError, match="not a Hyperwheel channel file"):
        verify_channel(folder, signers)


def test_versions_compare_as_numbers():
    assert version_key("0.1.10") > version_key("0.1.9") > version_key("0.0.99")
    with pytest.raises(ReleaseError):
        version_key("0.1.2-rc1")


def tool(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=REPO, capture_output=True, text=True)


def packed_release(tmp: Path, key: Path, signers: Path) -> Path:
    folder = make_release(tmp, key).rename(tmp / "hyperwheel-0.1.0")
    r = tool("tools/release/build_release.py", "pack", str(folder), "--signers", str(signers))
    assert r.returncode == 0, r.stderr
    return tmp / "hyperwheel-0.1.0-arm64.tar"


def test_publish_writes_a_channel_file_for_a_checked_release(tmp_path, key, signers):
    tar = packed_release(tmp_path, key, signers)
    r = tool("tools/release/publish.py", "--signers", str(signers), "channel-file", "--channel", "pilot",
             str(tar), "--out", str(tmp_path / "channels"))
    assert r.returncode == 0, r.stderr
    written = tmp_path / "channels" / "pilot"
    ch = parse_channel((written / CHANNEL_FILE).read_text())
    assert (ch.channel, ch.version, ch.tar, ch.tar_size) == ("pilot", "0.1.0", tar.name, tar.stat().st_size)
    assert not (written / CHANNEL_SIGNATURE).exists() and "ssh-keygen -Y sign" in r.stdout
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(written / CHANNEL_FILE)],
                   check=True, capture_output=True)
    r = tool("tools/release/publish.py", "--signers", str(signers), "channel", str(written), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "release upload channel-pilot" in r.stdout and "--clobber" in r.stdout


def test_publish_refuses_a_release_that_does_not_verify(tmp_path, key, signers):
    tar = packed_release(tmp_path, key, signers)
    (tmp_path / "other").mkdir()
    stranger = signers_file(tmp_path / "other", ("someone-else", keypair(tmp_path, "stranger")))
    for args in (["release", str(tar), "--dry-run"], ["channel-file", "--channel", "dev", str(tar)]):
        r = tool("tools/release/publish.py", "--signers", str(stranger), *args)
        assert r.returncode == 1 and r.stderr.startswith("Not published:")


def test_publish_release_dry_run_names_the_tar_manifest_and_signature(tmp_path, key, signers):
    tar = packed_release(tmp_path, key, signers)
    r = tool("tools/release/publish.py", "--signers", str(signers), "release", str(tar), "--dry-run")
    assert r.returncode == 0, r.stderr
    line = next(l for l in r.stdout.splitlines() if l.startswith("would run: gh release create v0.1.0"))
    assert tar.name in line and "manifest.json " in line and "manifest.json.sig" in line
