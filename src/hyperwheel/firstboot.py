"""The first start of a newly flashed Pi, before the network and Orthanc start. Run as root by
``hyperwheel-firstboot.service``, which only an image enables.

1. Orthanc's login. Every Pi has its own, so it can't be in the image.
2. The hotspot's password, from the private copy of the image: moved into the site settings
   (root-only) and into the hotspot's NetworkManager connection, then deleted from the boot
   partition, which every account on the Pi can read. NetworkManager starts afterwards and brings
   the hotspot up by itself, so the site can reach the Pi before anyone has logged in. A published
   image has no password on it: then the hotspot waits, and ``hyperwheel setup`` asks for one.
3. A marker, so this never runs again. If a step fails, there's no marker and the next start tries again.

It asks nothing and is safe to run twice. The password is never printed or logged.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .installer import Installer, InstallError, Layout
from .sitesetup import BOOT_HOTSPOT, Setup, SetupError, hotspot_password_problem, remove_boot_copy

MARKER = "first-boot-done"


def read_card_password(copy: Path) -> str:
    raw = copy.read_text(errors="replace")
    password = raw[:-1] if raw.endswith("\n") else raw
    return password[:-1] if password.endswith("\r") else password


def hotspot_from_card(setup: Setup, say: Callable[[str], None]) -> None:
    copy = setup.layout.p(BOOT_HOTSPOT)
    if copy.is_file():
        password = read_card_password(copy)
        problem = hotspot_password_problem(password)
        if problem is None:
            setup.store_hotspot(password)
            say("Hotspot: set up from the password on the card.")
        else:
            say(f"The hotspot password on the card can't be used: {problem}. hyperwheel setup will ask for one.")
    else:
        say("No hotspot password on the card. hyperwheel setup will ask for one.")
    remove_boot_copy(setup.layout)
    os.sync()  # the password is gone from the card even if the power goes now


def first_boot(layout: Layout, *, say: Callable[[str], None] = print, setup: Setup | None = None,
               installer: Installer | None = None, clock: Callable[[], datetime] = datetime.now) -> int:
    marker = layout.etc / MARKER
    if marker.exists():
        say("First boot was already done.")
        return 0
    installer = installer or Installer(Path("."), Path("."), layout, say=say)
    setup = setup or Setup(layout=layout, admin_password=lambda site: "")
    try:
        if installer.orthanc_login():
            say("Orthanc's login: made for this Pi.")
        hotspot_from_card(setup, say)
    except (InstallError, SetupError, OSError) as e:
        say(f"First boot stopped, and the next start tries again: {e}")
        return 1
    setup.write(marker, clock().isoformat(timespec="seconds") + "\n", 0o644)
    say("First boot done.")
    return 0


def main(args: list[str], *, is_root: Callable[[], bool] = lambda: os.geteuid() == 0) -> int:
    if args:
        print("Usage: hyperwheel first-boot")
        return 2
    if not is_root():
        print("First boot runs as root, from its service.")
        return 1
    return first_boot(Layout())
