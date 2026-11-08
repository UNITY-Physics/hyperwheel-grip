"""``tools/image/add-hotspot-password.sh``: the private copy of an image, with the hotspot password on it.

The script runs on the admin's Mac (it uses hdiutil), so the round trip runs only on macOS.
Elsewhere only its syntax is checked.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "image" / "add-hotspot-password.sh"
on_a_mac = pytest.mark.skipif(platform.system() != "Darwin" or not shutil.which("hdiutil"),
                              reason="the script runs on a Mac (hdiutil)")


def test_the_script_is_valid_bash_and_never_puts_the_password_on_a_command_line():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    text = SCRIPT.read_text()
    uses = [line.strip() for line in text.splitlines() if "$password" in line or "$first" in line]
    # only shell built-ins ([, printf and a function in a subshell) ever see the password, never another program
    assert all(line.startswith(("printf '%s\\n' \"$password\"", "password=$first", "why=$(problem \"$first\")",
                                "if [ \"$first\" != \"$second\" ]", "[ -n \"$password\" ]")) for line in uses), uses


def run(*args, input_text=""):
    return subprocess.run(["bash", str(SCRIPT), *map(str, args)], input=input_text, capture_output=True, text=True)


def attach(image: Path, mountroot: Path, *extra: str) -> tuple[str, Path | None]:
    out = subprocess.run(["hdiutil", "attach", *extra, "-imagekey", "diskimage-class=CRawDiskImage", "-nobrowse",
                          "-mountroot", str(mountroot), str(image)], capture_output=True, text=True, check=True).stdout
    device = out.splitlines()[0].split()[0]
    boot = next((line.split("\t")[2].strip() for line in out.splitlines()
                 if len(line.split("\t")) >= 3 and ("FAT" in line.split("\t")[1] or "DOS" in line.split("\t")[1])),
                None)
    return device, Path(boot) if boot else None


@pytest.fixture
def image(tmp_path):
    """A small blank Raspberry Pi style image: a FAT32 boot partition with config.txt and cmdline.txt,
    and a second partition, zipped like the real one."""
    raw = tmp_path / "hyperwheel-test.img"
    subprocess.run(["mkfile", "-n", "96m", str(raw)], check=True)
    out = subprocess.run(["hdiutil", "attach", "-imagekey", "diskimage-class=CRawDiskImage", "-nomount", str(raw)],
                         capture_output=True, text=True, check=True).stdout
    device = out.splitlines()[0].split()[0]
    try:
        subprocess.run(["diskutil", "partitionDisk", device, "2", "MBR", "FAT32", "BOOTFS", "64M", "JHFS+", "rootfs",
                        "R"], capture_output=True, check=True)
        subprocess.run(["hdiutil", "detach", "-quiet", device], check=True)
        (tmp_path / "mnt").mkdir()
        device, boot = attach(raw, tmp_path / "mnt")
        (boot / "config.txt").write_text("arm_64bit=1\n")
        (boot / "cmdline.txt").write_text("console=tty1 rootwait\n")
    finally:
        subprocess.run(["hdiutil", "detach", "-quiet", device])
    zipped = tmp_path / "hyperwheel-test.img.zip"
    with zipfile.ZipFile(zipped, "w") as z:
        z.write(raw, raw.name)
    raw.unlink()
    return zipped


def boot_files(private: Path, tmp: Path) -> dict[str, bytes]:
    folder = tmp / "check"
    folder.mkdir()
    with zipfile.ZipFile(private) as z:
        z.extractall(folder)
    (folder / "mnt").mkdir()
    device, boot = attach(folder / "hyperwheel-test.img", folder / "mnt", "-readonly")
    try:
        return {p.relative_to(boot).as_posix(): p.read_bytes() for p in boot.rglob("*") if p.is_file()}
    finally:
        subprocess.run(["hdiutil", "detach", "-quiet", device])


@on_a_mac
def test_the_private_copy_has_the_password_and_nothing_else_from_the_mac(image, tmp_path):
    before = image.read_bytes()
    r = run(image, input_text="short\ntest-hotspot-pw-1\nanother-pw-22\ntest-hotspot-pw-1\ntest-hotspot-pw-1\n")
    assert r.returncode == 0, r.stderr
    assert "it must be 8 to 63 characters long" in r.stdout and "The two don't match." in r.stdout
    assert "test-hotspot-pw-1" not in r.stdout + r.stderr
    private = tmp_path / "hyperwheel-test-private.img.zip"
    assert private.stat().st_mode & 0o777 == 0o600
    assert image.read_bytes() == before
    files = boot_files(private, tmp_path)
    assert files == {"config.txt": b"arm_64bit=1\n", "cmdline.txt": b"console=tty1 rootwait\n",
                     "hyperwheel/hotspot-password": b"test-hotspot-pw-1\n"}
    assert str(tmp_path) not in subprocess.run(["hdiutil", "info"], capture_output=True, text=True).stdout


@on_a_mac
def test_it_refuses_a_private_copy_an_existing_output_and_three_unusable_passwords(image, tmp_path):
    assert run(image, input_text="test-hotspot-pw-1\ntest-hotspot-pw-1\n").returncode == 0
    private = tmp_path / "hyperwheel-test-private.img.zip"
    again = run(private, input_text="test-hotspot-pw-1\ntest-hotspot-pw-1\n")
    assert again.returncode == 1 and "already has a hotspot password" in again.stderr
    exists = run(image, input_text="")
    assert exists.returncode == 1 and "already exists" in exists.stderr
    private.unlink()
    bad = run(image, input_text=" leading-space\nok-password-1\nok-password-2\nnon-ascii-pwé\n")
    assert bad.returncode == 1 and "Nothing was written" in bad.stderr
    assert "can't start or end with a space" in bad.stdout and "symbols on a keyboard" in bad.stdout
    assert not private.exists()
