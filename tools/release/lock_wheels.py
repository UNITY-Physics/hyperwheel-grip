"""Write requirements/runtime-arm64.lock: every pinned runtime package as the one wheel a Pi
uses (CPython 3.13, Linux arm64), with its SHA-256, so the release build can fetch them with
pip --require-hashes. Run after changing requirements/runtime.lock.

    .venv/bin/python tools/release/lock_wheels.py
"""
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLATFORM = ["--platform", "manylinux_2_28_aarch64", "--platform", "manylinux_2_26_aarch64",
            "--platform", "manylinux_2_17_aarch64", "--platform", "manylinux2014_aarch64",
            "--python-version", "3.13", "--implementation", "cp", "--only-binary=:all:", "--no-deps"]


def pins() -> list[str]:
    lines = (REPO / "requirements" / "runtime.lock").read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def main() -> int:
    out = ["# Every runtime package as the wheel a Pi uses (CPython 3.13, Linux arm64), with its SHA-256.",
           "# Written by tools/release/lock_wheels.py from runtime.lock; used by the release build."]
    for pin in pins():
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, "-m", "pip", "download", "-q", "-d", tmp, *PLATFORM, pin], check=True)
            wheels = list(Path(tmp).glob("*.whl"))
            if len(wheels) != 1:
                print(f"{pin}: expected one wheel, got {[w.name for w in wheels]}", file=sys.stderr)
                return 1
            digest = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
            out.append(f"{pin} --hash=sha256:{digest}  # {wheels[0].name}")
    (REPO / "requirements" / "runtime-arm64.lock").write_text("\n".join(out) + "\n")
    print(f"requirements/runtime-arm64.lock: {len(out) - 2} wheels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
