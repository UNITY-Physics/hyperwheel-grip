"""Checks for every value that ends up in a local path or a request URL.

Tag values from the scanner are never used as they are: a leading space in a
Patient ID once broke the staging path. Anything used as a folder or file name
is checked here first, and anything put in a URL path goes through
``quote_path``.
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote

_UID = re.compile(r"[0-9]+(\.[0-9]+)*")
_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_LOGIN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
# The scanner keeps each exam's raw files in RRDF/rrdf_<StudyDate>_<StudyTime>/.
_RRDF_FOLDER = re.compile(r"rrdf_(\d{8}_\d{6})")
_TIMESTAMP = "%Y%m%d_%H%M%S"


def is_uid(value: object) -> bool:
    """A DICOM UID that is safe to use as a folder or file name: digits and dots, at most 64 characters."""
    return isinstance(value, str) and len(value) <= 64 and _UID.fullmatch(value) is not None


def is_project_name(value: object) -> bool:
    """A routed project name, used as the top folder of the staging area."""
    return isinstance(value, str) and _PROJECT.fullmatch(value) is not None


def is_login(value: object) -> bool:
    """A routing login as written in the routing file (lower case)."""
    return isinstance(value, str) and _LOGIN.fullmatch(value) is not None


def login_from_operators_name(operators_name: object) -> str | None:
    """The part of OperatorsName after the last '/', lower case, as used for routing.

    Returns None when there is no '/' or nothing after it; such a study has
    no routing entry and is never uploaded.
    """
    if not isinstance(operators_name, str) or "/" not in operators_name:
        return None
    login = operators_name.rsplit("/", 1)[1].strip().lower()
    return login or None


def is_rrdf_name(name: object) -> bool:
    """A raw file name the Pi fetches and GRIP accepts, unchanged: a .h5 file whose name isn't empty or only
    spaces, has no '/', '\\' or '..', and is at most 255 characters (and no control characters, which no file
    name of the scanner's has). Any other name stays on the scanner and is flagged."""
    # Also kept on the scanner: a name with a space at either end (the scanner's listing can't show it as it is) or
    # one that isn't valid text (the listing shows it changed), so the Pi could never fetch it by that name.
    return (isinstance(name, str) and name.endswith(".h5") and len(name.encode("utf-8", "replace")) <= 255
            and name == name.strip() and "\ufffd" not in name
            and "/" not in name and "\\" not in name and ".." not in name
            and not any(ord(c) < 32 or ord(c) == 127 for c in name))


def rrdf_folder_time(folder: str) -> datetime | None:
    """The study start time in a scanner RRDF folder name, or None."""
    m = _RRDF_FOLDER.fullmatch(folder)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), _TIMESTAMP)
    except ValueError:
        return None


def quote_path(value: str) -> str:
    """URL-encode one path segment (every reserved character, including '/')."""
    return quote(value, safe="")
