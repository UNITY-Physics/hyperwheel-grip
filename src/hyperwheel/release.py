"""Releases: what's in one, and checking it before anything is installed.

A release is a folder (shipped as a tar) holding ``manifest.json``, its OpenSSH
signature ``manifest.json.sig``, and the files the manifest lists, each with its
size and SHA-256:

    manifest.json        format, version, channel, minimum image, flags, components, files
    manifest.json.sig    ssh-keygen -Y sign -n hyperwheel-release (by an allowed signer)
    debs/                Orthanc and its dependencies, pinned .deb files
    wheels/              Hyperwheel and every Python package, pinned wheels (cp313, arm64)
    oe2/                 Orthanc Explorer 2, built for arm64 (when included)

A release is used only if the signature is good, by a key in the allowed-signers
file, under the ``hyperwheel-release`` namespace, and every listed file is there
with the right size and checksum, with nothing else in the folder. The check
needs no secret: it uses the public keys and OpenSSH's ``ssh-keygen -Y``.

    python -m hyperwheel.release verify <folder> [--signers release/allowed_signers]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

FORMAT = 1
NAMESPACE = "hyperwheel-release"
MANIFEST = "manifest.json"
SIGNATURE = "manifest.json.sig"
FLAGS = ("orthanc_db_change", "kernel_or_firmware", "manual_only")
CHANNELS = ("dev", "pilot", "stable")
_VERSION = re.compile(r"\d+\.\d+\.\d+")
_PATH_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_BLOCK = 1024 * 1024


class ReleaseError(Exception):
    """The release can't be used. The message says why; it is safe to log."""


@dataclass(frozen=True)
class ReleaseFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Manifest:
    version: str
    channel: str
    built_at: str
    min_image: str | None
    flags: dict[str, bool]
    components: dict[str, str]
    files: tuple[ReleaseFile, ...] = field(default_factory=tuple)

    def to_json(self) -> str:
        return json.dumps({
            "format": FORMAT, "name": "hyperwheel", "version": self.version, "channel": self.channel,
            "built_at": self.built_at, "min_image": self.min_image, "flags": self.flags,
            "components": self.components,
            "files": [{"path": f.path, "size": f.size, "sha256": f.sha256} for f in self.files],
        }, indent=2, sort_keys=True) + "\n"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(_BLOCK):
            digest.update(block)
    return digest.hexdigest()


def check_path(path: str) -> str:
    """A file path inside a release: relative, plain names, no '..', no hidden files."""
    p = PurePosixPath(path)
    if (p.is_absolute() or not p.parts or str(p) != path
            or any(not _PATH_PART.fullmatch(part) for part in p.parts)):
        raise ReleaseError(f"unexpected file name in the manifest: {path!r}")
    if p.name in (MANIFEST, SIGNATURE):
        raise ReleaseError(f"the manifest can't list {p.name}")
    return str(p)


def parse_manifest(text: str) -> Manifest:
    try:
        data = json.loads(text)
    except ValueError:
        raise ReleaseError("manifest.json is not valid JSON") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT or data.get("name") != "hyperwheel":
        raise ReleaseError(f"manifest.json is not a Hyperwheel release manifest of format {FORMAT}")
    version, channel = data.get("version"), data.get("channel")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ReleaseError("the manifest's version must look like 1.2.3")
    if channel not in CHANNELS:
        raise ReleaseError(f"the manifest's channel must be one of {', '.join(CHANNELS)}")
    flags = data.get("flags")
    if not isinstance(flags, dict) or set(flags) != set(FLAGS) or not all(isinstance(v, bool) for v in flags.values()):
        raise ReleaseError(f"the manifest's flags must be exactly {', '.join(FLAGS)}, each true or false")
    components = data.get("components")
    if not isinstance(components, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                   for k, v in components.items()):
        raise ReleaseError("the manifest's components must map names to versions")
    min_image = data.get("min_image")
    if min_image is not None and not (isinstance(min_image, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", min_image)):
        raise ReleaseError("the manifest's min_image must be a date (YYYY-MM-DD) or null")
    files, seen = [], set()
    for entry in data.get("files") or []:
        if not isinstance(entry, dict):
            raise ReleaseError("a manifest file entry is not an object")
        path = check_path(entry.get("path") if isinstance(entry.get("path"), str) else "")
        size, sha = entry.get("size"), entry.get("sha256")
        if not isinstance(size, int) or size < 0 or not isinstance(sha, str) or not _SHA256.fullmatch(sha):
            raise ReleaseError(f"the manifest entry for {path} needs a size and a SHA-256")
        if path in seen:
            raise ReleaseError(f"the manifest lists {path} twice")
        seen.add(path)
        files.append(ReleaseFile(path, size, sha))
    if not files:
        raise ReleaseError("the manifest lists no files")
    built_at = data.get("built_at") if isinstance(data.get("built_at"), str) else ""
    return Manifest(version, channel, built_at, min_image, dict(flags), dict(components), tuple(files))


def verify_signature(data: Path, signature: Path, allowed_signers: Path, namespace: str = NAMESPACE) -> str:
    """Check an OpenSSH signature over a file; returns the signer's name. Raises ReleaseError."""
    for p in (data, signature, allowed_signers):
        if not p.is_file():
            raise ReleaseError(f"{p.name} is missing")
    found = subprocess.run(["ssh-keygen", "-Y", "find-principals", "-s", str(signature), "-f", str(allowed_signers)],
                           capture_output=True, text=True, timeout=30)
    principals = [line.strip() for line in found.stdout.splitlines() if line.strip()]
    if found.returncode != 0 or not principals:
        raise ReleaseError("the signature is not by an allowed signer")
    with open(data, "rb") as fh:
        checked = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(allowed_signers), "-I", principals[0],
                                  "-n", namespace, "-s", str(signature)], stdin=fh, capture_output=True, timeout=30)
    if checked.returncode != 0:
        raise ReleaseError(f"the signature does not match {data.name} (or is not signed as {namespace})")
    return principals[0]


def verify_release(folder: Path, allowed_signers: Path) -> tuple[Manifest, str]:
    """Check a release folder completely. Returns the manifest and who signed it."""
    folder = Path(folder)
    signer = verify_signature(folder / MANIFEST, folder / SIGNATURE, allowed_signers)
    manifest = parse_manifest((folder / MANIFEST).read_text())
    listed = {f.path for f in manifest.files}
    present = {str(p.relative_to(folder).as_posix()) for p in folder.rglob("*")
               if p.is_file() or p.is_symlink()} - {MANIFEST, SIGNATURE}
    if present - listed:
        raise ReleaseError(f"files not in the manifest: {', '.join(sorted(present - listed)[:5])}")
    for f in manifest.files:
        p = folder / f.path
        if p.is_symlink() or not p.is_file():
            raise ReleaseError(f"{f.path} is missing")
        if p.stat().st_size != f.size or sha256_file(p) != f.sha256:
            raise ReleaseError(f"{f.path} doesn't match the manifest")
    return manifest, signer


# ---- channel files --------------------------------------------------------------------
#
# A channel file says which release a channel (dev, pilot, stable) should run. It is
# signed like a manifest (same namespace, same allowed signers) and published as
# ``channel.json`` + ``channel.json.sig`` on the GitHub release tagged
# ``channel-<name>``, so each channel has a fixed download address. Promoting a
# release means signing a new channel file.

CHANNEL_FILE = "channel.json"
CHANNEL_SIGNATURE = "channel.json.sig"
_TAR = re.compile(r"hyperwheel-(\d+\.\d+\.\d+)-arm64\.tar")


@dataclass(frozen=True)
class Channel:
    channel: str
    version: str
    tar: str
    tar_size: int
    tar_sha256: str
    manifest_sha256: str
    published_at: str

    def to_json(self) -> str:
        return json.dumps({"format": FORMAT, "name": "hyperwheel-channel", "channel": self.channel,
                           "version": self.version, "tar": self.tar, "tar_size": self.tar_size,
                           "tar_sha256": self.tar_sha256, "manifest_sha256": self.manifest_sha256,
                           "published_at": self.published_at}, indent=2, sort_keys=True) + "\n"


def version_key(version: str) -> tuple[int, int, int]:
    if not _VERSION.fullmatch(version):
        raise ReleaseError(f"not a release version: {version!r}")
    a, b, c = version.split(".")
    return int(a), int(b), int(c)


def parse_channel(text: str) -> Channel:
    try:
        data = json.loads(text)
    except ValueError:
        raise ReleaseError("the channel file is not valid JSON") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT or data.get("name") != "hyperwheel-channel":
        raise ReleaseError(f"not a Hyperwheel channel file of format {FORMAT}")
    if data.get("channel") not in CHANNELS:
        raise ReleaseError(f"the channel must be one of {', '.join(CHANNELS)}")
    version = data.get("version")
    version_key(version if isinstance(version, str) else "")
    tar = data.get("tar")
    m = _TAR.fullmatch(tar) if isinstance(tar, str) else None
    if not m or m.group(1) != version:
        raise ReleaseError("the channel file's tar name doesn't match its version")
    size = data.get("tar_size")
    if not isinstance(size, int) or size <= 0:
        raise ReleaseError("the channel file needs the tar's size")
    for key in ("tar_sha256", "manifest_sha256"):
        if not isinstance(data.get(key), str) or not _SHA256.fullmatch(data[key]):
            raise ReleaseError(f"the channel file needs {key}")
    published = data.get("published_at") if isinstance(data.get("published_at"), str) else ""
    return Channel(data["channel"], version, tar, size, data["tar_sha256"], data["manifest_sha256"], published)


def verify_channel(folder: Path, allowed_signers: Path, expected: str | None = None) -> tuple[Channel, str]:
    """Check a downloaded channel file and its signature; returns it and who signed it.

    ``expected`` is the channel this Pi follows: a file for another channel is refused,
    so a dev channel file can never be passed off as the stable one.
    """
    folder = Path(folder)
    signer = verify_signature(folder / CHANNEL_FILE, folder / CHANNEL_SIGNATURE, allowed_signers)
    channel = parse_channel((folder / CHANNEL_FILE).read_text())
    if expected is not None and channel.channel != expected:
        raise ReleaseError(f"this is the {channel.channel} channel's file, not {expected}'s")
    return channel, signer


# ---- the admin password file ----------------------------------------------------------
#
# The admin password is the same on every Pi. Only its hash is published, in one file
# that every channel reads: ``admin.json`` + ``admin.json.sig`` on the GitHub release
# tagged ``admin-password``. It is signed like a manifest (same namespace, same allowed
# signers: each Pi's list allows the release key for that namespace only). Its "name"
# field, checked here, keeps a signed manifest or channel file from passing for it.
# Its serial only goes up: a Pi never takes a file older than the one it has.

ADMIN_FILE = "admin.json"
ADMIN_SIGNATURE = "admin.json.sig"
ADMIN_TAG = "admin-password"
ADMIN_USER = "admin"
_ADMIN_HASH = re.compile(r"\$6\$rounds=\d{4,9}\$[./0-9A-Za-z]{8,16}\$[./0-9A-Za-z]{86}")


@dataclass(frozen=True)
class AdminFile:
    serial: int
    hash: str
    published_at: str

    def to_json(self) -> str:
        return json.dumps({"format": FORMAT, "name": "hyperwheel-admin", "user": ADMIN_USER, "serial": self.serial,
                           "hash": self.hash, "published_at": self.published_at}, indent=2, sort_keys=True) + "\n"


def parse_admin(text: str) -> AdminFile:
    try:
        data = json.loads(text)
    except ValueError:
        raise ReleaseError("the admin password file is not valid JSON") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT or data.get("name") != "hyperwheel-admin":
        raise ReleaseError(f"not a Hyperwheel admin password file of format {FORMAT}")
    if data.get("user") != ADMIN_USER:
        raise ReleaseError(f"the admin password file is for {data.get('user')!r}, not {ADMIN_USER}")
    serial = data.get("serial")
    if not isinstance(serial, int) or isinstance(serial, bool) or serial < 1:
        raise ReleaseError("the admin password file needs a serial of 1 or more")
    hashed = data.get("hash")
    if not isinstance(hashed, str) or not _ADMIN_HASH.fullmatch(hashed):
        raise ReleaseError("the admin password file's hash isn't a SHA-512 crypt hash with rounds")
    published = data.get("published_at") if isinstance(data.get("published_at"), str) else ""
    return AdminFile(serial, hashed, published)


def verify_admin(folder: Path, allowed_signers: Path) -> tuple[AdminFile, str]:
    """Check a downloaded admin password file and its signature; returns it and who signed it."""
    folder = Path(folder)
    signer = verify_signature(folder / ADMIN_FILE, folder / ADMIN_SIGNATURE, allowed_signers)
    return parse_admin((folder / ADMIN_FILE).read_text()), signer


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hyperwheel.release")
    sub = ap.add_subparsers(dest="command", required=True)
    v = sub.add_parser("verify", help="check a release folder's signature and every file")
    v.add_argument("folder", type=Path)
    v.add_argument("--signers", type=Path, default=Path("/etc/hyperwheel/release_allowed_signers"))
    args = ap.parse_args(argv)
    try:
        manifest, signer = verify_release(args.folder, args.signers)
    except ReleaseError as e:
        print(f"Release NOT usable: {e}", file=sys.stderr)
        return 1
    print(f"Release {manifest.version} ({manifest.channel}) is good: signed by {signer}, "
          f"{len(manifest.files)} files checked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
