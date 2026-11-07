"""A new scanner SSH key is accepted and remembered, with a notice for a day; never a stop."""

from __future__ import annotations

import base64
import shutil
import socket
import sqlite3
import subprocess
import threading

import paramiko
import pytest
from test_dashboard import snap

from hyperwheel import __main__ as entry
from hyperwheel import hostkeys
from hyperwheel.dashboard import HEARTBEAT
from hyperwheel.hostkeys import HostKeyBook, fingerprint, new_key_notices, read_known_hosts
from hyperwheel.rawpull import LoginRefused, ScannerError, SshScanner
from hyperwheel.state import Store

HOST = "[10.42.0.1]:25125"
KEY_A, KEY_B, KEY_C = b"scanner-a key bytes", b"scanner-b key bytes", b"scanner-a after upgrade"


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "state.db", clock=clock)
    yield s
    s.close()


def notices(store, clock):
    return new_key_notices(store, clock())


def test_the_first_scanner_is_quiet_and_a_second_one_gets_a_notice_for_a_day(store, clock):
    book = HostKeyBook(store, clock=clock)
    book.seen(HOST, "ssh-ed25519", KEY_A)
    assert notices(store, clock) == []  # the Pi's first scanner: nothing to tell
    clock.now += 60
    book.seen(HOST, "ssh-ed25519", KEY_B)  # the site's other scanner, on the same address
    [text] = notices(store, clock)
    assert fingerprint(KEY_B)[:19] in text and "tell support" in text
    clock.now += 23 * 3600
    assert len(notices(store, clock)) == 1
    clock.now += 3600
    assert notices(store, clock) == []  # gone after 24 hours; the key stays remembered
    assert [k.fingerprint for k in store.host_keys()] == [fingerprint(KEY_A), fingerprint(KEY_B)]


def test_moving_back_and_forth_between_known_scanners_is_quiet(store, clock):
    book = HostKeyBook(store, clock=clock)
    book.seen(HOST, "ssh-ed25519", KEY_A)
    book.seen(HOST, "ssh-ed25519", KEY_B)
    clock.now += 2 * 86400
    for key in (KEY_A, KEY_B, KEY_A, KEY_B):
        book.seen(HOST, "ssh-ed25519", key)
        assert notices(store, clock) == []
    assert len(store.host_keys()) == 2


def test_a_scanner_upgrade_is_a_new_key_with_a_notice(store, clock):
    book = HostKeyBook(store, clock=clock)
    book.seen(HOST, "ssh-ed25519", KEY_A)
    clock.now += 30 * 86400
    book.seen(HOST, "ssh-ed25519", KEY_C)
    assert len(notices(store, clock)) == 1


def test_last_seen_is_written_at_most_hourly_but_a_new_address_is_written_at_once(store, clock):
    book = HostKeyBook(store, clock=clock)
    book.seen(HOST, "ssh-ed25519", KEY_A)
    first = clock.now
    clock.now += 600
    book.seen(HOST, "ssh-ed25519", KEY_A)
    assert store.host_keys()[0].last_seen == first
    book.seen("[10.42.0.2]:25125", "ssh-ed25519", KEY_A)
    k = store.host_keys()[0]
    assert (k.host, k.last_seen) == ("[10.42.0.2]:25125", clock.now)
    clock.now += 3601
    book.seen("[10.42.0.2]:25125", "ssh-ed25519", KEY_A)
    assert store.host_keys()[0].last_seen == clock.now


def known_hosts_line(key: bytes, host: str = HOST, key_type: str = "ssh-ed25519") -> str:
    return f"{host} {key_type} {base64.b64encode(key).decode()}\n"


def test_keys_in_the_known_hosts_file_count_as_seen(tmp_path, store, clock):
    path = tmp_path / "scanner_known_hosts"
    path.write_text("# 10.42.0.1:25125 SSH-2.0-OpenSSH_8.9p1\n" + known_hosts_line(KEY_A)
                    + "@revoked * ssh-rsa AAAA\nbroken line\n" + f"{HOST} ssh-rsa not-base64!\n")
    book = HostKeyBook(store, path, clock=clock)
    assert [k.fingerprint for k in store.host_keys()] == [fingerprint(KEY_A)]
    book.seen(HOST, "ssh-ed25519", KEY_A)
    book.seen(HOST, "ssh-ed25519", KEY_B)  # not the first key any more: a notice
    assert len(notices(store, clock)) == 1


def test_the_known_hosts_file_is_read_only_while_the_list_is_empty(tmp_path, store, clock):
    path = tmp_path / "scanner_known_hosts"
    path.write_text(known_hosts_line(KEY_A))
    HostKeyBook(store, path, clock=clock)
    path.write_text(known_hosts_line(KEY_B))
    HostKeyBook(store, path, clock=clock)  # e.g. the next service start
    assert [k.fingerprint for k in store.host_keys()] == [fingerprint(KEY_A)]
    assert read_known_hosts(tmp_path / "missing") == []


def test_the_fingerprint_is_the_one_openssh_shows(tmp_path):
    if not shutil.which("ssh-keygen"):
        pytest.skip("no ssh-keygen")
    key = paramiko.ECDSAKey.generate()
    path = tmp_path / "kh"
    path.write_text(f"{HOST} {key.get_name()} {key.get_base64()}\n")
    shown = subprocess.run(["ssh-keygen", "-l", "-f", str(path)], capture_output=True, text=True, check=True).stdout
    assert fingerprint(key.asbytes()) == shown.split()[1]
    assert read_known_hosts(path)[0][2] == shown.split()[1]


# ---- the notice in hyperwheel status and the Upload Monitor ---------------------------------


def test_the_notice_shows_in_the_upload_monitor_without_asking_for_attention(env):
    env.store.set_cursor(HEARTBEAT, int(env.clock()))
    book = HostKeyBook(env.store, clock=env.clock)
    book.seen(HOST, "ssh-ed25519", KEY_A)
    assert [f for f in snap(env)["general"] if f["level"] == "notice"] == []
    book.seen(HOST, "ssh-ed25519", KEY_B)
    s = snap(env)
    [notice] = [f for f in s["general"] if f["level"] == "notice"]
    assert "a scanner it hadn't seen before" in notice["text"]
    assert s["attention"] is False and s["headline"] == "Everything is on GRIP"


def test_status_prints_the_notice(env, capsys, monkeypatch):
    book = HostKeyBook(env.store, clock=env.clock)
    book.seen(HOST, "ssh-ed25519", KEY_A)
    book.seen(HOST, "ssh-ed25519", KEY_B)
    monkeypatch.setattr(entry.os, "geteuid", lambda: 0)
    monkeypatch.setattr(entry.time, "time", env.clock)
    monkeypatch.setattr("hyperwheel.updater.hold_text", lambda paths: None)
    site, routing = env.tmp / "site.toml", env.tmp / "routing.toml"
    site.write_text(f'schema_version = 1\nsite = "site-a"\nstaging_dir = "{env.site.staging_dir}"\n'
                    f'state_db = "{env.site.state_db}"\nkeys_dir = "{env.site.keys_dir}"\n')
    routing.write_text("schema_version = 1\n")
    assert entry.main(["--site", str(site), "--routing", str(routing), "status"]) == 0
    out = capsys.readouterr().out
    assert "  [notice] At " in out and fingerprint(KEY_B)[:19] in out


# ---- a real SSH connection: two scanners on one address ---------------------------------------


class FakeScannerSsh(paramiko.ServerInterface):
    """Enough of the scanner's SSH server: password login for "rrdf", and ``ls`` of the RRDF folder."""

    def __init__(self, password: str):
        self.password, self.tried, self.done = password, [], threading.Event()

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        self.tried.append(username)
        ok = username == "rrdf" and password == self.password
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        # Paramiko sends the reply to this request after it returns, so the answer only ends with
        # end-of-file: closing the channel here could beat the reply and fail the client's exec.
        def answer():
            channel.sendall(b"rrdf_20261027_132802/\n")
            channel.send_exit_status(0)
            channel.shutdown_write()
        threading.Thread(target=answer, daemon=True).start()
        return True


class Scanner:
    """One listening address; which scanner answers (which host key) can be swapped, like moving the cable."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.key = paramiko.ECDSAKey.generate()
        self.server = None
        self.transports = []

    def serve_one(self, password="scanner-pw"):
        self.server = FakeScannerSsh(password)

        def run():
            conn, _ = self.sock.accept()
            t = paramiko.Transport(conn)
            self.transports.append(t)
            t.add_server_key(self.key)
            try:
                t.start_server(server=self.server)
            except (paramiko.SSHException, EOFError):
                pass
        threading.Thread(target=run, daemon=True).start()

    def close(self):
        for t in self.transports:
            t.close()
        self.sock.close()


@pytest.fixture
def scanner():
    s = Scanner()
    yield s
    s.close()


def connect(scanner, book, password="scanner-pw"):
    scanner.serve_one()
    return SshScanner("127.0.0.1", scanner.port, "rrdf", lambda: password, book.seen, timeout_s=5)


def test_two_scanners_on_one_address_both_connect_and_are_remembered(scanner, store, clock):
    book = HostKeyBook(store, clock=clock)
    first_key = scanner.key
    ssh = connect(scanner, book)
    assert ssh.list_folders() == ["rrdf_20261027_132802"]
    ssh.close()

    scanner.key = paramiko.ECDSAKey.generate()  # the Pi is moved to the other scanner
    ssh = connect(scanner, book)
    assert ssh.list_folders() == ["rrdf_20261027_132802"]
    ssh.close()
    assert len(notices(store, clock)) == 1

    scanner.key = first_key  # and back again: quiet
    clock.now += 2 * 86400
    ssh = connect(scanner, book)
    ssh.close()
    assert notices(store, clock) == []
    assert len(store.host_keys()) == 2
    assert {k.host for k in store.host_keys()} == {f"[127.0.0.1]:{scanner.port}"}


def test_a_key_that_cannot_be_recorded_stops_before_the_password_is_sent(scanner, store, clock):
    def busy(host, key_type, key_bytes):
        raise sqlite3.OperationalError("database is locked")
    scanner.serve_one()
    with pytest.raises(ScannerError) as e:
        SshScanner("127.0.0.1", scanner.port, "rrdf", lambda: "scanner-pw", busy, timeout_s=5)
    assert not isinstance(e.value, LoginRefused)
    assert scanner.server.tried == []  # the password never left the Pi


def test_a_wrong_password_is_still_a_refused_login(scanner, store, clock):
    with pytest.raises(LoginRefused):
        connect(scanner, HostKeyBook(store, clock=clock), password="old-pw")
    assert len(store.host_keys()) == 1  # the key was recorded before the login


def test_the_notice_text():
    k = hostkeys.HostKey("SHA256:abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG", "ssh-ed25519", HOST,
                         first_seen=0.0, last_seen=0.0)
    text = hostkeys.notice(k)
    assert "(scanner key SHA256:abcdefghijkl…)" in text
    assert text.endswith("If no one upgraded or swapped the scanner, tell support.")
