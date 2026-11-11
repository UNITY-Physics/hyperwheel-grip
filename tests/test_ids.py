from datetime import datetime

import pytest

from hyperwheel import ids


@pytest.mark.parametrize("value,ok", [
    ("1.2.840.113619.2.55.3.604688119", True),
    ("1" * 64, True),
    ("1" * 65, False),
    ("", False),
    (" 1.2.3", False),
    ("1.2.3 ", False),
    ("1..2", False),
    ("1.2.", False),
    ("../1.2", False),
    ("1.2/3", False),
    (None, False),
])
def test_uid(value, ok):
    assert ids.is_uid(value) is ok


@pytest.mark.parametrize("value,login", [
    ("site_a/sandbox", "sandbox"),
    ("site/sub/STUDYX ", "studyx"),
    ("admin", None),
    ("site/", None),
    ("", None),
    (None, None),
])
def test_login_from_operators_name(value, login):
    assert ids.login_from_operators_name(value) == login


def test_raw_names_are_any_h5_file_grip_would_accept():
    for name in ("flair_iso_rrdf_20261027_134807.h5",
                 "T2map_CALIPR_UNITY_v1100_beta1.user_rrdf_20261027_141317.h5",
                 "0T1SPGRFA30_4x224scale.user_rrdf_20261007_110757.h5",
                 "no_date_or_time.h5", "a b.h5", ".hidden.h5", "x" * 252 + ".h5"):
        assert ids.is_rrdf_name(name), name
    for name in ("", "   ", "x.txt", "../x.h5", "x..y.h5", "x/y.h5", "x\\y.h5", "x" * 253 + ".h5", "a\nb.h5", None):
        assert not ids.is_rrdf_name(name), name


def test_rrdf_folder_time():
    assert ids.rrdf_folder_time("rrdf_20261027_132802") == datetime(2026, 10, 27, 13, 28, 2)
    assert ids.rrdf_folder_time("rrdf_20261027_132802/") is None
    assert ids.rrdf_folder_time("other") is None


def test_quote_path_encodes_every_reserved_character():
    assert ids.quote_path("a b/c?d#e%") == "a%20b%2Fc%3Fd%23e%25"
    assert ids.quote_path("x.user_rrdf_20261027_141317.h5") == "x.user_rrdf_20261027_141317.h5"
