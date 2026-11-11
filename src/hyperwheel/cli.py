"""What people type: ``hyperwheel help``, and commands that need root run through sudo for them.

Data managers never type ``sudo``. When an ordinary account runs a command that
needs root, Hyperwheel runs itself again through sudo with exactly the same
words (``sudo /usr/local/bin/hyperwheel <words>``). The sudo rule
(``/etc/sudoers.d/hyperwheel``) still decides who may run what, so no account
gains a right it didn't have. An account that isn't allowed a command gets one
plain line, not sudo's message. ``sudo hyperwheel ...`` keeps working too.
"""

from __future__ import annotations

import grp
import os
import subprocess
import sys
from collections.abc import Callable, Sequence

COMMAND = "/usr/local/bin/hyperwheel"
SUDO = "/usr/bin/sudo"
ADMIN_GROUP = "sudo"

# What the data-manager account may run. The installer's sudo rule allows exactly these
# (a test checks the two lists match); "update status" also works without root.
DATA_MANAGER = (
    ("status", "Is everything on GRIP, and is it safe to switch off?"),
    ("doctor", "Check the cable, the scanner and the internet"),
    ("datasets list", "The GRIP datasets, their scanner logins, and whether each key works"),
    ("datasets add", "Add a GRIP dataset with its login and key (asks your password)"),
    ("datasets change", "Change a dataset's logins, or replace its key (asks your password)"),
    ("datasets remove", "Remove a dataset and its key (asks your password)"),
    ("scanner password", "Set the scanner's raw-data password (asks your password)"),
    ("update status", "Which version is running, and is an update waiting"),
)
# Shown only until the Pi is set up (the sudo rule allows it only until then, without a password).
SETUP = ("setup", "Set up this Pi for its site (only until it is set up)")
ADMIN = (
    ("update now", "Look for an update now, and install it once the Pi is idle"),
    ("update hold", "Keep this Pi on its version until the hold is released"),
    ("update release-hold", "Let this Pi take updates again"),
    ("update channel <dev|pilot|stable>", "Take releases from another channel"),
)

# run, dashboard, viewer, firewall and connect-watch are the services, and first-boot runs once on a newly flashed Pi
COMMANDS = {"help", "status", "doctor", "datasets", "keys", "scanner", "update", "setup", "run", "dashboard", "first-boot",
            "connect-watch", "viewer", "firewall"}
DATASETS_ACTIONS = {"list", "add", "change", "remove"}
# The commands of releases before 0.1.16, which sites may have in their notes.
OLD_KEYS = {"list": "hyperwheel datasets list", "set": "hyperwheel datasets change"}
SCANNER_ACTIONS = {"password"}
UPDATE_ACTIONS = {"check", "now", "hold", "release-hold", "status", "channel"}


def is_admin() -> bool:
    """Root, or an account in the admin (sudo) group."""
    if os.geteuid() == 0:
        return True
    try:
        gid = grp.getgrnam(ADMIN_GROUP).gr_gid
    except KeyError:
        return False
    return gid == os.getgid() or gid in os.getgroups()


def setup_pending(etc: str = "/etc/hyperwheel") -> bool:
    """Whether this Pi still waits for ``hyperwheel setup``."""
    from pathlib import Path

    from .installer import is_set_up
    return Path("/opt/hyperwheel/current").exists() and not is_set_up(Path(etc))


def help_text(admin: bool, setup: bool = False) -> str:
    mine = [SETUP, *DATA_MANAGER] if setup else list(DATA_MANAGER)
    rows = [*mine, *(ADMIN if admin else ())]
    width = max(len(f"hyperwheel {words}") for words, _ in rows) + 3

    def line(words: str, what: str) -> str:
        return f"  {f'hyperwheel {words}':<{width}}{what}"

    out = ["What you can do on this Pi:", *(line(w, t) for w, t in mine)]
    if admin:
        out += ["", "Admin commands:", *(line(w, t) for w, t in ADMIN)]
    out += ["", "Type them just as shown. Hyperwheel asks for your password when it needs it."]
    return "\n".join(out)


def problem(argv: Sequence[str]) -> str | None:
    """What's wrong with the words typed, in one line, or None if it's a command Hyperwheel knows.

    Words starting with ``-`` (options for testing) are left to the normal parser.
    """
    if not argv or argv[0].startswith("-"):
        return None
    command, rest = argv[0], argv[1:2]
    if rest and rest[0].startswith("-"):
        return None
    if command not in COMMANDS:
        return f"There's no command 'hyperwheel {command}'."
    if command == "keys":
        now = OLD_KEYS.get(argv[1] if rest else "", "hyperwheel datasets list")
        return f"This is now: {now}" + ("\nFor a new dataset: hyperwheel datasets add" if rest == ["set"] else "")
    if command == "datasets" and rest and rest[0] not in DATASETS_ACTIONS:
        return f"There's no command 'hyperwheel datasets {argv[1]}'."
    if command == "datasets" and not rest:
        return "'hyperwheel datasets' needs one more word: list, add, change or remove."
    if command == "scanner" and rest and rest[0] not in SCANNER_ACTIONS:
        return f"There's no command 'hyperwheel scanner {argv[1]}'."
    if command == "scanner" and not rest:
        return "'hyperwheel scanner' needs one more word: password."
    if command == "update" and rest and rest[0] not in UPDATE_ACTIONS:
        return f"There's no command 'hyperwheel update {argv[1]}'."
    if command == "update" and not rest:
        return "'hyperwheel update' needs one more word, e.g. status."
    return None


def needs_root(argv: Sequence[str]) -> bool:
    """Commands people type that read root-only files. ``run`` and ``dashboard`` are the services
    (started by systemd under their own account), and ``update status`` reads only public files."""
    if not argv or "-h" in argv or "--help" in argv:
        return False
    if argv[0] in ("status", "doctor", "datasets", "scanner", "setup", "first-boot", "connect-watch"):
        return True
    return argv[0] == "update" and argv[1:2] != ["status"]


def run_as_root(argv: Sequence[str], *, admin: Callable[[], bool] = is_admin,
                run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                execv: Callable[[str, list[str]], None] = os.execv,
                exists: Callable[[str], bool] = os.path.exists,
                say: Callable[[str], None] = print) -> int:
    """Run ``hyperwheel <argv>`` again through sudo. Only returns if it can't (or isn't allowed to)."""
    words = " ".join(argv)
    if not (exists(COMMAND) and exists(SUDO)):
        say(f"'hyperwheel {words}' needs administrator rights. Run it as root.")
        return 2
    if not admin():
        # sudo -n -l <command> says whether this account may run exactly these words, without asking
        # for a password (the data-manager rule has password-free entries, so listing needs none).
        allowed = run([SUDO, "-n", "-l", COMMAND, *argv], capture_output=True).returncode == 0
        if not allowed:
            say(f"Your account can't run 'hyperwheel {words}'. To see what it can run, type: hyperwheel help")
            return 2
    sys.stdout.flush()
    sys.stderr.flush()
    execv(SUDO, [SUDO, "-p", "Your password (%p): ", COMMAND, *argv])
    return 2  # only reached when execv is replaced in tests
