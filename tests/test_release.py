"""Checking a release before anything is installed: signature, signer, namespace, every file."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hyperwheel.release import (FLAGS, MANIFEST, NAMESPACE, SIGNATURE, Manifest, ReleaseError, ReleaseFile,
                                parse_manifest, sha256_file, verify_release)

REPO = Path(__file__).resolve().parents[1]


def keypair(tmp: Path, name: str) -> Path:
    key = tmp / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key)], check=True)
    return key


def signers_file(tmp: Path, *entries: tuple[str, Path], namespace: str = NAMESPACE) -> Path:
    path = tmp / "allowed_signers"
    path.write_text("".join(f'{who} namespaces="{namespace}" {key.with_suffix(".pub").read_text()}'
                            for who, key in entries))
    return path


def sign(key: Path, folder: Path, namespace: str = NAMESPACE) -> None:
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", namespace, str(folder / MANIFEST)],
                   check=True, capture_output=True)


def make_release(tmp: Path, key: Path, **overrides) -> Path:
    folder = tmp / "release"
    (folder / "debs").mkdir(parents=True)
    (folder / "wheels").mkdir()
    (folder / "debs" / "orthanc_1.12.7+dfsg-4_arm64.deb").write_bytes(b"deb" * 100)
    (folder / "wheels" / "hyperwheel-0.1.0-py3-none-any.whl").write_bytes(b"whl" * 100)
    files = tuple(ReleaseFile(str(p.relative_to(folder)), p.stat().st_size, sha256_file(p))
                  for p in sorted(folder.rglob("*")) if p.is_file())
    m = Manifest(version="0.1.0", channel="dev", built_at="2026-11-05T12:00:00Z", min_image=None,
                 flags={f: False for f in FLAGS}, components={"orthanc": "1.12.7+dfsg-4"}, files=files)
    text = m.to_json()
    if overrides:
        data = json.loads(text)
        data.update(overrides)
        text = json.dumps(data)
    (folder / MANIFEST).write_text(text)
    sign(key, folder)
    return folder


@pytest.fixture
def signer(tmp_path):
    return keypair(tmp_path, "signer")


@pytest.fixture
def signers(tmp_path, signer):
    return signers_file(tmp_path, ("signer", signer))


def test_good_release_is_accepted(tmp_path, signer, signers):
    folder = make_release(tmp_path, signer)
    manifest, signer = verify_release(folder, signers)
    assert signer == "signer" and manifest.version == "0.1.0" and len(manifest.files) == 2


def test_a_second_signer_is_also_accepted(tmp_path, signer):
    other = keypair(tmp_path, "second")
    signers = signers_file(tmp_path, ("signer", signer), ("second", other))
    folder = make_release(tmp_path, other)
    assert verify_release(folder, signers)[1] == "second"


def test_changed_file_is_refused(tmp_path, signer, signers):
    folder = make_release(tmp_path, signer)
    (folder / "debs" / "orthanc_1.12.7+dfsg-4_arm64.deb").write_bytes(b"DEB" * 100)  # same size, new content
    with pytest.raises(ReleaseError, match="doesn't match"):
        verify_release(folder, signers)


def test_missing_and_extra_files_are_refused(tmp_path, signer, signers):
    folder = make_release(tmp_path, signer)
    (folder / "wheels" / "extra.whl").write_bytes(b"x")
    with pytest.raises(ReleaseError, match="not in the manifest"):
        verify_release(folder, signers)
    (folder / "wheels" / "extra.whl").unlink()
    (folder / "wheels" / "hyperwheel-0.1.0-py3-none-any.whl").unlink()
    with pytest.raises(ReleaseError, match="missing"):
        verify_release(folder, signers)


def test_edited_manifest_is_refused(tmp_path, signer, signers):
    folder = make_release(tmp_path, signer)
    text = (folder / MANIFEST).read_text().replace('"dev"', '"stable"')
    (folder / MANIFEST).write_text(text)
    with pytest.raises(ReleaseError, match="does not match"):
        verify_release(folder, signers)


def test_unknown_signer_is_refused(tmp_path, signer, signers):
    stranger = keypair(tmp_path, "stranger")
    folder = make_release(tmp_path, stranger)
    with pytest.raises(ReleaseError, match="not by an allowed signer"):
        verify_release(folder, signers)


def test_signature_for_another_namespace_is_refused(tmp_path, signer, signers):
    folder = make_release(tmp_path, signer)
    (folder / SIGNATURE).unlink()
    sign(signer, folder, namespace="file")  # e.g. a signature made for something else
    with pytest.raises(ReleaseError):
        verify_release(folder, signers)


def test_missing_signature_is_refused(tmp_path, signer, signers):
    folder = make_release(tmp_path, signer)
    (folder / SIGNATURE).unlink()
    with pytest.raises(ReleaseError, match="missing"):
        verify_release(folder, signers)


@pytest.mark.parametrize("path", ["../etc/passwd", "/etc/passwd", "debs/../x", ".hidden", "debs/.x", "a//b", ""])
def test_unsafe_paths_in_a_manifest_are_refused(path):
    text = json.dumps({"format": 1, "name": "hyperwheel", "version": "0.1.0", "channel": "dev", "built_at": "",
                       "min_image": None, "flags": {f: False for f in FLAGS}, "components": {},
                       "files": [{"path": path, "size": 1, "sha256": "0" * 64}]})
    with pytest.raises(ReleaseError):
        parse_manifest(text)


@pytest.mark.parametrize("change", [{"version": "1.0"}, {"channel": "beta"}, {"format": 2},
                                    {"flags": {"manual_only": False}}, {"min_image": "05/11/2026"}, {"files": []}])
def test_malformed_manifests_are_refused(tmp_path, signer, signers, change):
    folder = make_release(tmp_path, signer, **change)
    with pytest.raises(ReleaseError):
        verify_release(folder, signers)


def test_the_committed_allowed_signers_file_is_valid():
    lines = [line for line in (REPO / "release" / "allowed_signers").read_text().splitlines()
             if line and not line.startswith("#")]
    assert lines and all(f'namespaces="{NAMESPACE}" ssh-ed25519 ' in line for line in lines)
    for line in lines:
        key = line.split(" ", 2)[2]
        out = subprocess.run(["ssh-keygen", "-l", "-f", "/dev/stdin"], input=key, capture_output=True, text=True)
        assert out.returncode == 0 and "ED25519" in out.stdout


def test_lock_files_agree():
    """Every runtime pin has a hashed arm64 wheel, and every pinned .deb has a checksum."""
    def pins(path):
        return [line.split()[0] for line in (REPO / path).read_text().splitlines()
                if line.strip() and not line.startswith("#")]
    assert sorted(pins("requirements/runtime.lock")) == sorted(pins("requirements/runtime-arm64.lock"))
    assert all("--hash=sha256:" in line for line in (REPO / "requirements/runtime-arm64.lock").read_text().splitlines()
               if line and not line.startswith("#"))
    listed = {p.replace("=", "_", 1) for p in pins("release/debs.list")}
    locked = {line.split()[1].rsplit("_", 1)[0] for line in (REPO / "release/debs.lock").read_text().splitlines()
              if line and not line.startswith("#")}
    assert listed == locked
