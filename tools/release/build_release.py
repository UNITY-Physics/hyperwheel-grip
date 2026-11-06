"""Build, then (after the release signer signs it) pack a Hyperwheel release. Runs on the build Mac.

    .venv/bin/python tools/release/build_release.py build --debs <folder> [--oe2 <folder>] [--channel dev]
    ssh-keygen -Y sign -f ~/.ssh/hyperwheel-release -n hyperwheel-release dist/releases/hyperwheel-<v>/manifest.json
    .venv/bin/python tools/release/build_release.py pack dist/releases/hyperwheel-<v>

build: collects every pinned part into dist/releases/hyperwheel-<version>/ and writes its
       manifest. The version is the package version (pyproject.toml), X.Y.Z.
       - wheels/: Hyperwheel itself, and each package in requirements/runtime-arm64.lock,
         fetched with pip --require-hashes (CPython 3.13, Linux arm64);
       - debs/: the .deb files from tools/release/fetch_debs.sh, checked against release/debs.lock;
       - oe2/: Orthanc Explorer 2 for arm64, if --oe2 is given (from tools/oe2/build.sh).
pack:  checks the signature and every file (as a Pi will), then writes
       dist/releases/hyperwheel-<version>-arm64.tar, the same bytes every time.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from hyperwheel import __version__  # noqa: E402
from hyperwheel.release import FLAGS, CHANNELS, Manifest, ReleaseError, ReleaseFile, sha256_file, verify_release  # noqa: E402

PLATFORM = ["--platform", "manylinux_2_28_aarch64", "--platform", "manylinux_2_26_aarch64",
            "--platform", "manylinux_2_17_aarch64", "--platform", "manylinux2014_aarch64",
            "--python-version", "3.13", "--implementation", "cp", "--only-binary=:all:", "--no-deps"]


def fail(message: str) -> int:
    print(f"Release build stopped: {message}", file=sys.stderr)
    return 1


def package_version() -> str:
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or version != __version__:
        raise SystemExit(fail(f"the package version must be X.Y.Z and the same in pyproject.toml and "
                              f"hyperwheel/__init__.py (they are {version} and {__version__})"))
    return version


def debs_lock() -> dict[str, str]:
    lock = {}
    for line in (REPO / "release" / "debs.lock").read_text().splitlines():
        if line and not line.startswith("#"):
            sha, name = line.split()
            lock[name] = sha
    return lock


def build(args) -> int:
    version = package_version()
    folder = args.out / f"hyperwheel-{version}"
    if folder.exists():
        return fail(f"{folder} already exists; a release is never rebuilt in place")
    (folder / "wheels").mkdir(parents=True)
    (folder / "debs").mkdir()

    subprocess.run([sys.executable, "-m", "pip", "download", "-q", "--require-hashes", "-d", str(folder / "wheels"),
                    *PLATFORM, "-r", str(REPO / "requirements" / "runtime-arm64.lock")], check=True)
    subprocess.run([sys.executable, "-m", "pip", "wheel", "-q", "--no-deps", "-w", str(folder / "wheels"),
                    str(REPO)], check=True)
    if not list((folder / "wheels").glob(f"hyperwheel-{version}-*.whl")):
        return fail("the Hyperwheel wheel wasn't built")

    lock = debs_lock()
    found = {p.name: p for p in args.debs.glob("*.deb")}
    if set(found) != set(lock):
        return fail(f".deb files in {args.debs} don't match release/debs.lock "
                    f"(missing {sorted(set(lock) - set(found))}, extra {sorted(set(found) - set(lock))})")
    for name, path in sorted(found.items()):
        if sha256_file(path) != lock[name]:
            return fail(f"{name} doesn't match its checksum in release/debs.lock")
        shutil.copy2(path, folder / "debs" / name)

    components = {"hyperwheel": version, "python": "3.13 (cp313, linux arm64)"}
    for line in (REPO / "release" / "debs.list").read_text().splitlines():
        if line.startswith("orthanc="):
            components["orthanc"] = line.split("=", 1)[1]
    if args.oe2:
        oe2_version = (args.oe2 / "VERSION").read_text().strip()
        shutil.copytree(args.oe2, folder / "oe2")
        components["orthanc-explorer-2"] = oe2_version

    files = tuple(ReleaseFile(p.relative_to(folder).as_posix(), p.stat().st_size, sha256_file(p))
                  for p in sorted(folder.rglob("*")) if p.is_file())
    manifest = Manifest(version=version, channel=args.channel,
                        built_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        min_image=args.min_image, flags={f: f in (args.flag or []) for f in FLAGS},
                        components=components, files=files)
    (folder / "manifest.json").write_text(manifest.to_json())
    total = sum(f.size for f in files) / 1e6
    print(f"Built {folder} ({len(files)} files, {total:.1f} MB): {components}")
    print("Next, on the release signer's Mac (asks for the key's passphrase):")
    print(f"  ssh-keygen -Y sign -f ~/.ssh/hyperwheel-release -n hyperwheel-release {folder}/manifest.json")
    print(f"Then: .venv/bin/python tools/release/build_release.py pack {folder}")
    return 0


def pack(args) -> int:
    folder = args.folder.resolve()
    try:
        manifest, signer = verify_release(folder, args.signers)
    except ReleaseError as e:
        return fail(f"the release doesn't verify: {e}")
    built = int(datetime.strptime(manifest.built_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
    tar_path = folder.parent / f"{folder.name}-arm64.tar"
    names = ["manifest.json", "manifest.json.sig", *(f.path for f in manifest.files)]
    dirs = sorted({str(Path(n).parent) for n in names if "/" in n})
    with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tar:
        for name in [*dirs, *names]:
            info = tar.gettarinfo(str(folder / name), arcname=f"{folder.name}/{name}")
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = built
            info.mode = 0o755 if info.isdir() else 0o644
            if info.isdir():
                tar.addfile(info)
            else:
                with open(folder / name, "rb") as fh:
                    tar.addfile(info, fh)
    print(f"Release {manifest.version} signed by {signer}: {tar_path} ({tar_path.stat().st_size / 1e6:.1f} MB), "
          f"SHA-256 {sha256_file(tar_path)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--debs", type=Path, required=True, help="folder from tools/release/fetch_debs.sh")
    b.add_argument("--oe2", type=Path, help="folder from tools/oe2/build.sh")
    b.add_argument("--channel", choices=CHANNELS, default="dev")
    b.add_argument("--min-image", help="oldest image (YYYY-MM-DD) this release installs on")
    b.add_argument("--flag", action="append", choices=FLAGS)
    b.add_argument("--out", type=Path, default=REPO / "dist" / "releases")
    p = sub.add_parser("pack")
    p.add_argument("folder", type=Path)
    p.add_argument("--signers", type=Path, default=REPO / "release" / "allowed_signers",
                   help="allowed-signers file (only tests use another)")
    args = ap.parse_args()
    return build(args) if args.command == "build" else pack(args)


if __name__ == "__main__":
    sys.exit(main())
