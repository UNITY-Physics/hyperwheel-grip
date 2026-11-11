"""``hyperwheel help``, unknown commands, and root commands run through sudo for the person typing."""

from __future__ import annotations

import re
import subprocess

import pytest

from hyperwheel import __main__ as entry
from hyperwheel import cli
from hyperwheel.installer import SUDO_RULE

DATA_MANAGER_HELP = """\
What you can do on this Pi:
  hyperwheel status             Is everything on GRIP, and is it safe to switch off?
  hyperwheel doctor             Check the cable, the scanner and the internet
  hyperwheel datasets list      The GRIP datasets, their scanner logins, and whether each key works
  hyperwheel datasets add       Add a GRIP dataset with its login and key (asks your password)
  hyperwheel datasets change    Change a dataset's logins, or replace its key (asks your password)
  hyperwheel datasets remove    Remove a dataset and its key (asks your password)
  hyperwheel scanner password   Set the scanner's raw-data password (asks your password)
  hyperwheel update status      Which version is running, and is an update waiting

Type them just as shown. Hyperwheel asks for your password when it needs it."""


def test_the_data_manager_sees_one_command_per_line_in_plain_words():
    assert cli.help_text(admin=False) == DATA_MANAGER_HELP
    assert "/" not in DATA_MANAGER_HELP and "sudo" not in DATA_MANAGER_HELP


def test_the_admin_sees_the_admin_commands_too_in_their_own_section():
    text = cli.help_text(admin=True)
    head, admin = text.split("\n\nAdmin commands:\n")
    squeeze = lambda lines: [" ".join(line.split()) for line in lines]  # noqa: E731
    assert squeeze(head.splitlines()) == squeeze(DATA_MANAGER_HELP.split("\n\n")[0].splitlines())
    assert [line.split("   ")[0].strip() for line in admin.splitlines()[:5]] == [
        "hyperwheel update now", "hyperwheel update hold", "hyperwheel update release-hold",
        "hyperwheel update channel <dev|pilot|stable>", "hyperwheel grip address"]
    starts = {re.match(r"  hyperwheel [a-z <|>-]+?\s{3,}", line).end() for line in text.splitlines()
              if line.startswith("  hyperwheel")}
    assert len(starts) == 1  # the descriptions line up in one column


def test_the_help_lists_exactly_what_the_sudo_rule_allows():
    allowed = set(re.findall(r"/usr/local/bin/hyperwheel ([a-z][a-z -]*[a-z])", SUDO_RULE))
    assert allowed == {words for words, _ in cli.DATA_MANAGER}


@pytest.mark.parametrize("argv", [[], ["help"], ["-h"], ["--help"]])
def test_no_words_or_help_shows_the_help(argv, capsys, monkeypatch):
    monkeypatch.setattr(cli, "is_admin", lambda: False)
    assert entry.main(argv) == 0
    assert capsys.readouterr().out.strip() == DATA_MANAGER_HELP


@pytest.mark.parametrize("argv, line", [
    (["frob"], "There's no command 'hyperwheel frob'."),
    (["datasets", "delete"], "There's no command 'hyperwheel datasets delete'."),
    (["datasets"], "'hyperwheel datasets' needs one more word: list, add, change or remove."),
    (["keys", "set"], "This is now: hyperwheel datasets change\nFor a new dataset: hyperwheel datasets add"),
    (["scanner"], "'hyperwheel scanner' needs one more word: password."),
    (["scanner", "key"], "There's no command 'hyperwheel scanner key'."),
    (["update", "now-please"], "There's no command 'hyperwheel update now-please'."),
    (["update"], "'hyperwheel update' needs one more word, e.g. status."),
])
def test_an_unknown_command_says_so_in_one_line_then_shows_the_help(argv, line, capsys, monkeypatch):
    monkeypatch.setattr(cli, "is_admin", lambda: False)
    assert entry.main(argv) == 2
    out = capsys.readouterr().out
    assert out.startswith(line + "\n\n") and out.strip().endswith(DATA_MANAGER_HELP.splitlines()[-1])


def test_which_commands_need_root():
    for argv in (["status"], ["doctor"], ["datasets", "list"], ["datasets", "add"], ["datasets", "remove"],
                 ["scanner", "password"], ["update", "now"],
                 ["update", "hold", "--reason", "x"], ["update", "release-hold"], ["update", "check"]):
        assert cli.needs_root(argv), argv
    for argv in (["update", "status"], ["run"], ["dashboard"], ["help"], [], ["--site", "x", "status"],
                 ["update", "--help"], ["datasets", "add", "-h"]):
        assert not cli.needs_root(argv), argv


class Sudo:
    def __init__(self, allowed: bool):
        self.allowed, self.checked, self.execd, self.said = allowed, [], [], []

    def run(self, cmd, **kw):
        self.checked.append(cmd)
        return subprocess.CompletedProcess(cmd, 0 if self.allowed else 1, b"", b"")

    def execv(self, path, args):
        self.execd.append((path, args))

    def go(self, argv, *, admin=False, exists=True):
        return cli.run_as_root(argv, admin=lambda: admin, run=self.run, execv=self.execv,
                               exists=lambda p: exists, say=self.said.append)


def test_an_allowed_command_runs_again_through_sudo_with_exactly_the_same_words():
    sudo = Sudo(allowed=True)
    sudo.go(["datasets", "add"])
    assert sudo.checked == [["/usr/bin/sudo", "-n", "-l", "/usr/local/bin/hyperwheel", "datasets", "add"]]
    assert sudo.execd == [("/usr/bin/sudo", ["/usr/bin/sudo", "-p", "Your password (%p): ",
                                             "/usr/local/bin/hyperwheel", "datasets", "add"])]
    assert sudo.said == []


def test_a_command_the_account_may_not_run_gets_one_plain_line_and_sudo_is_not_run():
    sudo = Sudo(allowed=False)
    assert sudo.go(["update", "now"]) == 2
    assert sudo.execd == []
    assert sudo.said == ["Your account can't run 'hyperwheel update now'. To see what it can run, type: "
                         "hyperwheel help"]


def test_the_admin_goes_straight_to_sudo_which_asks_for_the_admin_password():
    sudo = Sudo(allowed=False)  # listing would need the admin's password, so it isn't asked
    sudo.go(["update", "hold", "--reason", "testing"], admin=True)
    assert sudo.checked == []
    assert sudo.execd[0][1][-5:] == ["/usr/local/bin/hyperwheel", "update", "hold", "--reason", "testing"]


def test_without_the_installed_command_it_says_root_is_needed():
    sudo = Sudo(allowed=True)
    assert sudo.go(["status"], exists=False) == 2
    assert sudo.execd == [] and sudo.said == ["'hyperwheel status' needs administrator rights. Run it as root."]


def test_an_ordinary_account_typing_a_root_command_goes_through_run_as_root(monkeypatch):
    seen = []
    monkeypatch.setattr(entry.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(cli, "run_as_root", lambda argv: seen.append(list(argv)) or 2)
    assert entry.main(["datasets", "list"]) == 2 and seen == [["datasets", "list"]]


def test_root_runs_the_command_itself(monkeypatch):
    monkeypatch.setattr(entry.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "run_as_root", lambda argv: pytest.fail("ran sudo as root"))
    assert entry.main(["--site", "/nonexistent/site.toml", "status"]) == 2  # config missing, but no sudo


def test_update_status_works_without_root(monkeypatch, capsys):
    monkeypatch.setattr(entry.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(cli, "run_as_root", lambda argv: pytest.fail("update status needs no root"))
    from hyperwheel import updater
    monkeypatch.setattr(updater, "main", lambda argv: print("Release: 0.1.9") or 0)
    assert entry.main(["update", "status"]) == 0 and "Release: 0.1.9" in capsys.readouterr().out


def test_until_the_pi_is_set_up_the_data_manager_also_sees_setup(capsys, monkeypatch):
    from hyperwheel.installer import sudo_rule
    text = cli.help_text(admin=False, setup=True)
    assert text.splitlines()[1].split("   ")[0].strip() == "hyperwheel setup"
    allowed = set(re.findall(r"/usr/local/bin/hyperwheel ([a-z][a-z -]*[a-z])", sudo_rule(False)))
    assert allowed == {words for words, _ in cli.DATA_MANAGER} | {"setup"}
    monkeypatch.setattr(cli, "is_admin", lambda: False)
    monkeypatch.setattr(cli, "setup_pending", lambda: True)
    assert entry.main(["help"]) == 0
    assert "hyperwheel setup" in capsys.readouterr().out


def test_setup_needs_root_and_on_a_set_up_pi_is_not_in_the_help(tmp_path):
    assert cli.needs_root(["setup"]) and cli.problem(["setup"]) is None
    assert "hyperwheel setup" not in cli.help_text(admin=False)
    (tmp_path / "setup-done").write_text("")
    assert not cli.setup_pending(str(tmp_path))
