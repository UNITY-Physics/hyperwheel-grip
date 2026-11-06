"""The scanner's listing and SCP download, without a scanner.

Listing samples are in the format a Swoop scanner's ``ls`` produces.
"""

from __future__ import annotations

import pytest

from hyperwheel.rawpull import ScannerError, ScpReader, parse_ls_files, parse_ls_folders

LS_FOLDERS = "rrdf_20261020_131331/\nrrdf_20261027_132802/\nnotes.txt\n"
LS_FILES = """total 604592
-rw-rw-rw- 1 1000 1000 182405024 Oct 27 14:21 T2map_CALIPR_UNITY_v1100_beta1.user_rrdf_20261027_141317.h5
-rw-rw-rw- 1 1000 1000  17432224 Oct 27 13:35 dwi_xyz_v1_b0_rrdf_20261027_133429.h5
drwxr-xr-x 2 1000 1000      4096 Oct 27 13:35 sub
"""


def test_listings():
    assert parse_ls_folders(LS_FOLDERS) == ["rrdf_20261020_131331", "rrdf_20261027_132802"]
    assert parse_ls_files(LS_FILES) == [
        ("T2map_CALIPR_UNITY_v1100_beta1.user_rrdf_20261027_141317.h5", 182405024),
        ("dwi_xyz_v1_b0_rrdf_20261027_133429.h5", 17432224),
    ]


class Chan:
    """Plays the scanner's side of ``scp -f``."""

    def __init__(self, answer: bytes, chunk: int = 7):
        self.answer, self.chunk, self.sent, self.closed = answer, chunk, b"", False

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        data, self.answer = self.answer[:min(n, self.chunk)], self.answer[min(n, self.chunk):]
        return data

    def close(self):
        self.closed = True


NAME = "flair_iso_rrdf_20261027_134807.h5"
DATA = bytes(range(256)) * 3


def read_all(reader):
    out = b""
    with reader:
        while block := reader.read(100):
            out += block
    return out


def test_scp_download():
    chan = Chan(f"C0644 {len(DATA)} {NAME}\n".encode() + DATA + b"\0")
    reader = ScpReader(chan, NAME)
    assert reader.size == len(DATA)
    assert read_all(reader) == DATA
    assert chan.sent == b"\0\0\0" and chan.closed


def test_scp_error_from_scanner():
    with pytest.raises(ScannerError, match="refused"):
        ScpReader(Chan(b"\x01scp: RRDF/x: No such file or directory\n"), NAME)


def test_scp_wrong_name_or_short_file():
    with pytest.raises(ScannerError, match="unexpected"):
        ScpReader(Chan(f"C0644 3 other_rrdf_20261027_134807.h5\n".encode() + b"abc\0"), NAME)
    reader = ScpReader(Chan(f"C0644 {len(DATA)} {NAME}\n".encode() + DATA[:100]), NAME)
    with pytest.raises(ScannerError, match="closed the connection early"):
        read_all(reader)


def test_scp_error_after_the_file():
    reader = ScpReader(Chan(f"C0644 3 {NAME}\n".encode() + b"abc\x01"), NAME)
    with pytest.raises(ScannerError, match="error after the file"):
        read_all(reader)
