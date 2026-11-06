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
# RRDF names end in the acquisition start time, scanner local time:
# e.g. flair_iso_rrdf_20261027_134807.h5 or T2map_CALIPR_UNITY_v1100_beta1.user_rrdf_20261027_141317.h5
_RRDF_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}_(\d{8}_\d{6})\.h5")
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
    """An RRDF file name that is safe as a file name and ends in a real date and time."""
    return isinstance(name, str) and rrdf_timestamp(name) is not None


def rrdf_timestamp(name: str) -> datetime | None:
    """The acquisition time in an RRDF file name, or None if the name has none."""
    m = _RRDF_NAME.fullmatch(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), _TIMESTAMP)
    except ValueError:
        return None


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
