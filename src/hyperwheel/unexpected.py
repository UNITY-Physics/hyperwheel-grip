"""No command ever shows a Python traceback.

An unexpected error is turned into one plain line on screen, saying which
command stopped, what went wrong and what the person can do. The full
traceback goes to the system log (tag ``hyperwheel``), where the admin
reads it with ``journalctl -t hyperwheel``; only the admin account is pointed
there, since a data manager can't read the system log.
"""

from __future__ import annotations

import grp
import os
import pwd
import sqlite3
import sys
import traceback
from collections.abc import Callable, Sequence

from . import cli

SYSLOG_TAG = "hyperwheel"
_OPTIONS_WITH_A_VALUE = ("--site", "--routing")


def command_words(argv: Sequence[str]) -> str:
    """The command as typed, without the leading options (which only tests use)."""
    words = list(argv)
    while words and words[0].startswith("-"):
        option = words.pop(0)
        if option in _OPTIONS_WITH_A_VALUE and "=" not in option and words:
            words.pop(0)
    return " ".join(words) or "hyperwheel"


def what_went_wrong(e: BaseException) -> str:
    detail = " ".join(str(e).split())[:160]
    if isinstance(e, sqlite3.Error):
        return f"the state database can't be used ({detail or type(e).__name__})"
    if isinstance(e, PermissionError):
        return f"a file it needs can't be opened ({e.strerror or detail})"
    if isinstance(e, OSError):
        return f"a file or connection didn't work ({e.strerror or detail or type(e).__name__})"
    return f"an unexpected problem ({type(e).__name__}{': ' + detail if detail else ''})"


def person_is_admin() -> bool:
    """Whether the person typing is the admin account. Under sudo (a data manager's command run
    again as root, see ``cli``), that's the account that ran sudo, not root."""
    user = os.environ.get("SUDO_USER") if os.geteuid() == 0 else None
    if not user or user == "root":
        return cli.is_admin()
    try:
        entry = pwd.getpwnam(user)
        group = grp.getgrnam(cli.ADMIN_GROUP)
    except KeyError:
        return False
    return entry.pw_gid == group.gr_gid or user in group.gr_mem


def user_line(words: str, e: BaseException, admin: bool) -> str:
    line = f"'hyperwheel {words}' stopped: {what_went_wrong(e)}."
    if admin:  # the admin is who the others are sent to; they get where the details are
        return line + " Details: journalctl -t hyperwheel"
    if words.split()[:1] == ["doctor"]:
        return line + " Send this message to your Hyperwheel admin."
    return line + " Run hyperwheel doctor, and if that doesn't help, send this message to your Hyperwheel admin."


def log_details(lines: list[str]) -> None:
    """Write to the system log. Never fails: if there's no system log, the details are lost, not the line."""
    try:
        import syslog
        syslog.openlog(SYSLOG_TAG, syslog.LOG_PID, syslog.LOG_USER)
        for line in lines:
            syslog.syslog(syslog.LOG_ERR, line)
        syslog.closelog()
    except Exception:  # noqa: BLE001
        pass


def guarded(body: Callable[[list[str]], int], argv: list[str], *,
            say: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
            log: Callable[[list[str]], None] | None = None,
            admin: Callable[[], bool] | None = None) -> int:
    """Run a command; an unexpected error becomes one plain line (exit 1), Ctrl-C "Stopped." (exit 130)."""
    try:
        return body(argv)
    except KeyboardInterrupt:
        say("Stopped.")
        return 130
    except Exception as e:  # noqa: BLE001 - everything, so no one ever sees a traceback
        words = command_words(argv)
        details = [f"'hyperwheel {words}' stopped: {type(e).__name__}: {' '.join(str(e).split())[:400]}"]
        details += [line for chunk in traceback.format_exception(e) for line in chunk.rstrip("\n").split("\n")]
        (log or log_details)(details)
        try:
            is_admin = (admin or person_is_admin)()
        except Exception:  # noqa: BLE001
            is_admin = False
        say(user_line(words, e, is_admin))
        return 1
