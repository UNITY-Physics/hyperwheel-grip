"""Publish releases and channel files on GitHub Releases. Runs on the release signer's Mac.

    .venv/bin/python tools/release/publish.py release dist/releases/hyperwheel-<v>-arm64.tar [--dry-run]
    .venv/bin/python tools/release/publish.py channel-file --channel dev dist/releases/hyperwheel-<v>-arm64.tar
    ssh-keygen -Y sign -f ~/.ssh/hyperwheel-release -n hyperwheel-release dist/channels/dev/channel.json
    .venv/bin/python tools/release/publish.py channel dist/channels/dev [--dry-run]
    .venv/bin/python tools/release/publish.py admin-file
    ssh-keygen -Y sign -f ~/.ssh/hyperwheel-release -n hyperwheel-release dist/admin/admin.json
    .venv/bin/python tools/release/publish.py admin dist/admin [--dry-run]

release:      checks the signed tar the way a Pi will (signature, every file), then creates the
              GitHub release v<version> with the tar, its manifest and the manifest's signature.
channel-file: writes dist/channels/<channel>/channel.json for that release, to be signed.
channel:      checks the signed channel file, and that the release it names is published with the
              same tar checksum, then puts it on the GitHub release channel-<name> (replacing the
              channel's previous file). That is the promotion: Pis on that channel move to it.
admin-file:   makes a new admin password (the same on every Pi): shows readable ones until you
              take one, asks you to type it back, then writes only its hash to dist/admin/admin.json,
              to be signed. The password is shown once and never written anywhere.
admin:        checks the signed admin file and puts it on the GitHub release admin-password
              (replacing the one before). Every Pi takes it at its next update check.

Uses the gh CLI as whoever is logged in; no secrets are read or written here.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from hyperwheel import passwords  # noqa: E402
from hyperwheel.release import (ADMIN_FILE, ADMIN_SIGNATURE, ADMIN_TAG, CHANNEL_FILE,  # noqa: E402
                                CHANNEL_SIGNATURE, CHANNELS, MANIFEST, SIGNATURE, AdminFile, Channel, ReleaseError,
                                parse_admin, sha256_file, verify_admin, verify_channel, verify_release)

GITHUB = "UNITY-Physics/hyperwheel-grip"
SIGNERS = REPO / "release" / "allowed_signers"


def fail(message: str) -> int:
    print(f"Not published: {message}", file=sys.stderr)
    return 1


def gh(args: list[str], dry_run: bool) -> str:
    cmd = ["gh", *args, "-R", GITHUB]
    if dry_run:
        print("would run:", " ".join(cmd))
        return ""
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def unpack(tar_path: Path, into: Path) -> Path:
    with tarfile.open(tar_path) as tar:
        names = tar.getnames()
        tops = {n.split("/", 1)[0] for n in names}
        if len(tops) != 1 or any(n.startswith("/") or ".." in n.split("/") for n in names):
            raise ReleaseError("unexpected layout in the release tar")
        tar.extractall(into, filter="data")
    return into / tops.pop()


def checked_tar(tar_path: Path, signers: Path):
    """The release's manifest and signer, after checking the whole tar as a Pi will."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = unpack(tar_path, Path(tmp))
        manifest, signer = verify_release(folder, signers)
        manifest_sha = sha256_file(folder / MANIFEST)
        if tar_path.name != f"hyperwheel-{manifest.version}-arm64.tar":
            raise ReleaseError(f"the tar is called {tar_path.name}, not hyperwheel-{manifest.version}-arm64.tar")
        return manifest, signer, manifest_sha, folder.name


def cmd_release(args) -> int:
    try:
        manifest, signer, _, _ = checked_tar(args.tar, args.signers)
    except ReleaseError as e:
        return fail(str(e))
    with tempfile.TemporaryDirectory() as tmp:
        folder = unpack(args.tar, Path(tmp))
        notes = (f"Hyperwheel {manifest.version}, signed by {signer}. Components: "
                 + ", ".join(f"{k} {v}" for k, v in sorted(manifest.components.items()))
                 + ". A Pi takes it only once its channel file names it.")
        gh(["release", "create", f"v{manifest.version}", str(args.tar), str(folder / MANIFEST),
            str(folder / SIGNATURE), "--title", f"Hyperwheel {manifest.version}", "--notes", notes,
            "--target", args.target, "--latest=false"], args.dry_run)
    print(f"Release v{manifest.version} published (signed by {signer})." if not args.dry_run else "Dry run done.")
    return 0


def cmd_channel_file(args) -> int:
    try:
        manifest, signer, manifest_sha, _ = checked_tar(args.tar, args.signers)
    except ReleaseError as e:
        return fail(str(e))
    out = args.out / args.channel
    out.mkdir(parents=True, exist_ok=True)
    channel = Channel(args.channel, manifest.version, args.tar.name, args.tar.stat().st_size, sha256_file(args.tar),
                      manifest_sha, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    (out / CHANNEL_FILE).write_text(channel.to_json())
    (out / CHANNEL_SIGNATURE).unlink(missing_ok=True)
    print(f"Wrote {out / CHANNEL_FILE}: {args.channel} -> {manifest.version}. Next, sign it:")
    print(f"  ssh-keygen -Y sign -f ~/.ssh/hyperwheel-release -n hyperwheel-release {out / CHANNEL_FILE}")
    print(f"Then: .venv/bin/python tools/release/publish.py channel {out}")
    return 0


def cmd_channel(args) -> int:
    try:
        channel, signer = verify_channel(args.folder, args.signers)
    except ReleaseError as e:
        return fail(str(e))
    if not args.dry_run:
        try:
            assets = json.loads(gh(["release", "view", f"v{channel.version}", "--json", "assets"], False))["assets"]
        except subprocess.CalledProcessError:
            return fail(f"release v{channel.version} isn't published yet; publish it first")
        tar = next((a for a in assets if a["name"] == channel.tar), None)
        if tar is None or tar.get("size") != channel.tar_size:
            return fail(f"release v{channel.version} has no {channel.tar} of {channel.tar_size} bytes")
        digest = tar.get("digest")
        if not digest:  # older gh: no checksum in the listing, so fetch the tar and hash it
            with tempfile.TemporaryDirectory() as tmp:
                gh(["release", "download", f"v{channel.version}", "-p", channel.tar, "-D", tmp], False)
                digest = f"sha256:{sha256_file(Path(tmp) / channel.tar)}"
        if digest != f"sha256:{channel.tar_sha256}":
            return fail(f"the published {channel.tar} doesn't match the channel file's checksum")
        exists = subprocess.run(["gh", "release", "view", f"channel-{channel.channel}", "-R", GITHUB],
                                capture_output=True).returncode == 0
    else:
        exists = True
    if not exists:
        gh(["release", "create", f"channel-{channel.channel}", "--title", f"Channel: {channel.channel}",
            "--notes", f"The signed channel file for {channel.channel}. Pis on this channel take the release it names.",
            "--target", "main", "--latest=false"], args.dry_run)
    gh(["release", "upload", f"channel-{channel.channel}", str(args.folder / CHANNEL_FILE),
        str(args.folder / CHANNEL_SIGNATURE), "--clobber"], args.dry_run)
    print(f"Channel {channel.channel} now names {channel.version} (signed by {signer})."
          if not args.dry_run else "Dry run done.")
    return 0


CLEAR_SCREEN = "\x1b[H\x1b[2J\x1b[3J"


def published_admin() -> AdminFile | None:
    """The admin file now on GitHub (its signature isn't checked here: only its serial is used)."""
    found = subprocess.run(["gh", "release", "download", ADMIN_TAG, "-p", ADMIN_FILE, "-O", "-", "-R", GITHUB],
                           capture_output=True, text=True)
    if found.returncode != 0:
        return None
    try:
        return parse_admin(found.stdout)
    except ReleaseError:
        return None


def cmd_admin_file(args, ask=input, say=print, published=published_admin) -> int:
    say("A new admin password, the same on every Pi. Only its hash leaves this Mac.")
    while True:
        password = passwords.readable()
        say("")
        say(f"    {password}")
        say("")
        if ask("Use this one? Type yes to use it, or press Enter for another: ").strip().lower() == "yes":
            break
    say("Save it in your password manager now. It's shown only this once.")
    for _ in range(5):
        if passwords.same(ask("Type it back to check you have it: "), password):
            break
        say(f"That isn't it. Here it is again:    {password}")
    else:
        return fail("the password wasn't typed back, so nothing was written")
    say(CLEAR_SCREEN)
    say("Typed back correctly. The screen was cleared so the password isn't left on it.")
    say(f"Hashing it ({passwords.ROUNDS:,} rounds of SHA-512) ...")
    hashed = passwords.sha512_crypt(password, rounds=passwords.ROUNDS)
    if not passwords.matches(password, hashed):
        return fail("the hash didn't check out against the password")
    before = published()
    serial = before.serial + 1 if before else 1
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    admin = AdminFile(serial, hashed, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    (out / ADMIN_FILE).write_text(admin.to_json())
    (out / ADMIN_SIGNATURE).unlink(missing_ok=True)
    say(f"Wrote {out / ADMIN_FILE}: version {serial} of the admin password (the hash only). Next, sign it:")
    say(f"  ssh-keygen -Y sign -f ~/.ssh/hyperwheel-release -n hyperwheel-release {out / ADMIN_FILE}")
    say(f"Then: .venv/bin/python tools/release/publish.py admin {out}")
    return 0


def cmd_admin(args, published=published_admin) -> int:
    try:
        admin, signer = verify_admin(args.folder, args.signers)
    except ReleaseError as e:
        return fail(str(e))
    before = None if args.dry_run else published()
    if before and admin.serial <= before.serial:
        return fail(f"this is version {admin.serial}, and version {before.serial} is already published. Make a new "
                    "one with: publish.py admin-file")
    if not args.dry_run:
        exists = subprocess.run(["gh", "release", "view", ADMIN_TAG, "-R", GITHUB], capture_output=True).returncode == 0
    else:
        exists = True
    if not exists:
        gh(["release", "create", ADMIN_TAG, "--title", "Admin password (hash only)",
            "--notes", "The signed hash of the admin password, the same on every Pi. Every Pi takes it at its next "
                       "update check.", "--target", "main", "--latest=false"], args.dry_run)
    gh(["release", "upload", ADMIN_TAG, str(args.folder / ADMIN_FILE), str(args.folder / ADMIN_SIGNATURE),
        "--clobber"], args.dry_run)
    print(f"Admin password version {admin.serial} published (signed by {signer}). Every Pi takes it at its next "
          "update check." if not args.dry_run else "Dry run done.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signers", type=Path, default=SIGNERS, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("release")
    r.add_argument("tar", type=Path)
    r.add_argument("--target", default="main", help="commit or branch the release tag points at")
    r.add_argument("--dry-run", action="store_true")
    f = sub.add_parser("channel-file")
    f.add_argument("tar", type=Path)
    f.add_argument("--channel", choices=CHANNELS, required=True)
    f.add_argument("--out", type=Path, default=REPO / "dist" / "channels")
    c = sub.add_parser("channel")
    c.add_argument("folder", type=Path)
    c.add_argument("--dry-run", action="store_true")
    af = sub.add_parser("admin-file")
    af.add_argument("--out", type=Path, default=REPO / "dist" / "admin")
    a = sub.add_parser("admin")
    a.add_argument("folder", type=Path)
    a.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    return {"release": cmd_release, "channel-file": cmd_channel_file, "channel": cmd_channel,
            "admin-file": cmd_admin_file, "admin": cmd_admin}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
