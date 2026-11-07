"""No command ever shows a traceback: one plain line on screen, the details in the system log."""

from __future__ import annotations

import sqlite3

import pytest

from hyperwheel import __main__ as entry
from hyperwheel import unexpected

DATA_MANAGER_END = "Run hyperwheel doctor, and if that doesn't help, send this message to your Hyperwheel admin."


@pytest.fixture
def logged(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(unexpected, "log_details", lines.extend)
    return lines


def broken_site(tmp_path):
    site = tmp_path / "site.toml"
    site.write_text('schema_version = 1\nsite = "site-a"\n'
                    f'staging_dir = "{tmp_path}/staging"\n'
                    f'state_db = "{tmp_path}/not-there/state.db"\n'  # a folder that doesn't exist
                    f'keys_dir = "{tmp_path}/keys"\n')
    routing = tmp_path / "routing.toml"
    routing.write_text("schema_version = 1\n")
    return ["--site", str(site), "--routing", str(routing)]


def test_a_state_database_that_cannot_be_opened_gives_one_plain_line_and_no_traceback(tmp_path, capsys, logged,
                                                                                     monkeypatch):
    monkeypatch.setattr(unexpected, "person_is_admin", lambda: False)
    assert entry.main([*broken_site(tmp_path), "status"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.splitlines() == ["'hyperwheel status' stopped: the state database can't be used (unable to open "
                                f"database file). {DATA_MANAGER_END}"]
    assert "Traceback" not in err and "journalctl" not in err
    # the details are in the system log, for the maintainer
    assert logged[0] == "'hyperwheel status' stopped: OperationalError: unable to open database file"
    assert any(line.startswith("Traceback") for line in logged)
    assert any("state.py" in line for line in logged)


def test_the_admin_is_told_only_where_the_details_are(tmp_path, capsys, logged, monkeypatch):
    monkeypatch.setattr(unexpected, "person_is_admin", lambda: True)
    assert entry.main([*broken_site(tmp_path), "status"]) == 1
    err = capsys.readouterr().err
    assert err.splitlines() == ["'hyperwheel status' stopped: the state database can't be used (unable to open "
                                "database file). Details: journalctl -t hyperwheel"]
    assert "Hyperwheel admin" not in err and "doctor" not in err
    for words in ("doctor", "keys list"):
        line = unexpected.user_line(words, ValueError("x"), admin=True)
        assert line.endswith(" Details: journalctl -t hyperwheel") and "Hyperwheel admin" not in line


def run(body, argv=("status",), admin=False):
    said, logged = [], []
    code = unexpected.guarded(body, list(argv), say=said.append, log=logged.extend, admin=lambda: admin)
    return code, said, logged


def test_any_unexpected_error_is_one_line_naming_the_command():
    def boom(argv):
        raise ValueError("bad value\nover two lines")
    code, said, logged = run(boom, ["keys", "list"])
    assert code == 1
    assert said == ["'hyperwheel keys list' stopped: an unexpected problem (ValueError: bad value over two "
                    f"lines). {DATA_MANAGER_END}"]
    assert logged[0] == "'hyperwheel keys list' stopped: ValueError: bad value over two lines"


def test_a_file_that_cannot_be_opened_says_so():
    def denied(argv):
        raise PermissionError(13, "Permission denied", "/var/lib/hyperwheel/state.db")
    code, said, _ = run(denied, ["doctor"])
    # no "run hyperwheel doctor" when doctor is what stopped
    assert said == ["'hyperwheel doctor' stopped: a file it needs can't be opened (Permission denied). "
                    "Send this message to your Hyperwheel admin."]


def test_ctrl_c_says_stopped():
    def interrupted(argv):
        raise KeyboardInterrupt
    assert run(interrupted) == (130, ["Stopped."], [])


def test_normal_exits_pass_through_untouched():
    assert run(lambda argv: 0) == (0, [], [])
    assert run(lambda argv: 2) == (2, [], [])

    def exits(argv):
        raise SystemExit(2)  # argparse's own usage errors
    with pytest.raises(SystemExit):
        run(exits)


def test_the_options_are_left_out_of_the_command_shown():
    assert unexpected.command_words(["--site", "/x/site.toml", "--routing", "/x/r.toml", "status"]) == "status"
    assert unexpected.command_words(["--site=/x", "keys", "set"]) == "keys set"
    assert unexpected.command_words(["update", "hold", "--reason", "x"]) == "update hold --reason x"


def test_a_data_manager_under_sudo_is_not_sent_to_the_system_log(monkeypatch):
    monkeypatch.setattr(unexpected.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "datamanager")
    import grp
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda name: pwd.struct_passwd((name, "x", 1001, 1001, "", "/", "/bin/sh")))
    monkeypatch.setattr(grp, "getgrnam", lambda name: grp.struct_group(("sudo", "x", 27, ["admin"])))
    assert unexpected.person_is_admin() is False
    monkeypatch.setenv("SUDO_USER", "admin")
    assert unexpected.person_is_admin() is True


def test_the_system_log_failing_never_hides_the_line(monkeypatch):
    import syslog

    def broken(*a, **k):
        raise OSError("no /dev/log")
    monkeypatch.setattr(syslog, "openlog", broken)
    unexpected.log_details(["x"])  # doesn't raise


def test_sqlite_errors_name_the_state_database():
    assert unexpected.what_went_wrong(sqlite3.DatabaseError("file is not a database")) == \
        "the state database can't be used (file is not a database)"
