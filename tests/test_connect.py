"""Raspberry Pi Connect: the sign-in link on a newly flashed Pi's page, the watcher that keeps it ready, and its state."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from hyperwheel import cli, connect
from hyperwheel.connect import OK, PROBLEM, Watcher
from hyperwheel.dashboard import make_not_set_up_server, not_set_up_status
from hyperwheel.installer import DATA_MANAGER, UNITS, Layout

UID = 1001
GID = 2002  # not the uid, so the test sees which one is used


@pytest.fixture
def folder(tmp_path) -> Path:
    (tmp_path / "connect").mkdir(parents=True)
    return tmp_path / "connect"


# ---- the page ---------------------------------------------------------------------------------------------


@pytest.fixture
def page(folder):
    server = make_not_set_up_server(bind="127.0.0.1", port=0, folder=folder, now=lambda: 1000.0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_the_page_has_the_link_and_no_key_box(page, folder):
    with urllib.request.urlopen(page + "/") as r:
        html = r.read().decode()
        assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert "This Pi isn't set up yet" in html
    assert "Send this link to your Hyperwheel admin, and only to them" in html and 'id="link-card"' in html
    assert "<form" not in html and "<input" not in html and "key" not in html.lower()
    with urllib.request.urlopen(page + "/setup.js") as r:
        script = r.read().decode()
    assert "innerHTML" not in script and "POST" not in script and "key" not in script.lower()
    (folder / "state.json").write_text(json.dumps({"signed_in": False, "checked_at": 1000.0,
                                                   "link": "https://connect.raspberrypi.com/x"}))
    with urllib.request.urlopen(page + "/api/status") as r:
        assert json.loads(r.read()) == {"signed_in": False, "link": "https://connect.raspberrypi.com/x"}


def test_the_page_takes_nothing(page, folder):
    request = urllib.request.Request(page + "/api/connect-key", data=b'{"key": "rpoak_test0123456789"}',
                                     method="POST", headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(request)
    assert e.value.code >= 400
    assert not list(folder.iterdir())


def test_status_shows_the_link_until_the_pi_is_signed_in():
    # A state file from an older release may still say how a pasted key went: that's ignored.
    state = {"signed_in": False, "checked_at": 1000.0, "key": "refused", "link": "https://connect.raspberrypi.com/x"}
    assert not_set_up_status(state, 1000.0) == {"signed_in": False, "link": state["link"]}
    assert not_set_up_status({**state, "signed_in": True}, 1000.0)["link"] is None
    assert not_set_up_status(None, 1000.0)["signed_in"] is False


# ---- the state in words -----------------------------------------------------------------------------------


def test_describe():
    now = 5000.0
    assert connect.describe({"signed_in": True, "checked_at": now}, now, set_up=True) == (OK, "signed in")
    assert connect.describe({"signed_in": False, "checked_at": now}, now, set_up=True)[0] == PROBLEM
    assert connect.describe({"signed_in": False, "checked_at": now}, now, set_up=False)[1] == "not signed in yet"
    assert "15 minutes" in connect.describe({"signed_in": True, "checked_at": 0}, now, set_up=True)[1]
    assert connect.describe({"signed_in": None, "checked_at": now}, now, set_up=True)[1] == "its state can't be read"
    assert connect.describe(None, now, set_up=True)[1] == "not checked yet"


# ---- the watcher ------------------------------------------------------------------------------------------


class FakeSession:
    def __init__(self, cmd):
        self.cmd, self.link = cmd, "https://connect.raspberrypi.com/verify/ABCD-EFGH"
        self.stopped, self.alive = False, True

    def running(self):
        return self.alive

    def stop(self):
        self.stopped, self.alive = True, False


class Pi:
    """Connect on a new Pi: whether it's signed in, and what a sign-in does."""

    def __init__(self, root: Path):
        self.layout = Layout(root)
        self.now = 1000.0
        self.ran: list[list[str]] = []
        self.signed = False
        self.sessions: list[FakeSession] = []
        for folder in ("var/lib/hyperwheel/connect", f"run/user/{UID}", f"home/{DATA_MANAGER}", "etc/hyperwheel"):
            (root / folder).mkdir(parents=True)
        self.watcher = Watcher(layout=self.layout, run=self.run, spawn=self.spawn, clock=lambda: self.now, uid_of=lambda a: UID if a == DATA_MANAGER else None,
                               gid_of=lambda a: GID if a == DATA_MANAGER else None)

    def run(self, cmd, timeout=60.0):
        self.ran.append(list(cmd))
        if "status" in cmd:
            return 0, f"Signed in: {'yes' if self.signed else 'no'}\nScreen sharing: allowed\n"
        if cmd[-1] == "on":
            link = self.layout.p(f"/home/{DATA_MANAGER}") / connect.ENABLED_LINK
            link.parent.mkdir(parents=True, exist_ok=True)
            link.write_text("")
        return 0, ""

    def spawn(self, cmd):
        self.sessions.append(FakeSession(cmd))
        return self.sessions[-1]

    @property
    def state(self) -> dict:
        return connect.read_state(self.layout.p(connect.FOLDER))

    def tick(self, seconds=connect.CHECK_EVERY_S):
        self.now += seconds
        self.watcher.round()


@pytest.fixture
def pi(tmp_path):
    return Pi(tmp_path)


def set_up(pi):
    (pi.layout.etc / "setup-done").write_text("")


def test_a_new_pi_turns_connect_on_with_nobody_logged_in_and_offers_the_link(pi):
    pi.tick()
    assert ["loginctl", "enable-linger", DATA_MANAGER] in pi.ran
    assert pi.state["signed_in"] is False and pi.state["link"] == pi.sessions[0].link
    assert pi.state["account"] == DATA_MANAGER
    on = [c for c in pi.ran if c[-1] == "on"]
    assert len(on) == 1 and on[0][:6] == ["setpriv", f"--reuid={UID}", f"--regid={GID}", "--init-groups", "env", "-i"]
    assert f"HOME=/home/{DATA_MANAGER}" in on[0] and on[0][-2:] == ["rpi-connect", "on"]
    # no login session, so nothing in the journal: every command, and the link's, goes through setpriv
    assert all(c[0] == "setpriv" for c in pi.ran if "rpi-connect" in c)
    assert pi.sessions[0].cmd[:6] == on[0][:6] and pi.sessions[0].cmd[-2:] == ["rpi-connect", "signin"]
    assert f"XDG_RUNTIME_DIR=/run/user/{UID}" in on[0]
    pi.tick()
    assert len([c for c in pi.ran if c[-1] == "on"]) == 1  # asked once
    assert len(pi.sessions) == 1  # the same link stays while it's running


def test_a_new_link_when_the_old_one_ends(pi):
    pi.tick()
    pi.sessions[0].alive = False
    pi.tick()
    assert len(pi.sessions) == 2


def test_the_link_goes_once_the_admin_has_opened_it(pi):
    pi.tick()
    assert pi.state["link"]
    pi.signed = True  # the admin opened the link
    pi.tick()
    assert pi.sessions[0].stopped and pi.state["signed_in"] is True and pi.state["link"] is None
    assert not any(w.startswith("--auth-key") for c in pi.ran + [s.cmd for s in pi.sessions] for w in c)


def test_once_set_up_it_only_reads_the_state(pi):
    set_up(pi)
    pi.tick()
    assert pi.state["signed_in"] is False and pi.state["link"] is None
    assert not pi.sessions and not any(c[0] == "loginctl" or c[-1] == "on" for c in pi.ran)
    assert all("status" in c for c in pi.ran)


def test_it_uses_the_account_that_has_connect_enabled(pi):
    other = pi.layout.p("/home/someone")
    (other / connect.ENABLED_LINK).parent.mkdir(parents=True)
    (other / connect.ENABLED_LINK).write_text("")
    pi.watcher.uid_of = lambda a: 1000 if a == "someone" else (UID if a == DATA_MANAGER else None)
    pi.watcher.gid_of = pi.watcher.uid_of
    assert pi.watcher.account() == "someone"
    link = pi.layout.p(f"/home/{DATA_MANAGER}") / connect.ENABLED_LINK
    link.parent.mkdir(parents=True)
    link.write_text("")
    assert pi.watcher.account() == DATA_MANAGER


def test_with_no_account_yet_it_says_it_cant_tell(pi):
    pi.watcher.uid_of = lambda a: None
    pi.tick()
    assert pi.state["account"] is None and pi.state["signed_in"] is None and not pi.sessions


def test_status_that_cant_be_read_is_not_signed_out(pi):
    set_up(pi)
    pi.watcher.run = lambda cmd, timeout=60.0: (1, "")
    pi.tick()
    assert pi.state["signed_in"] is None


def test_checks_are_rarer_once_signed_in(pi):
    pi.signed = True
    pi.tick()
    count = len(pi.ran)
    pi.tick(connect.CHECK_EVERY_S)
    assert len(pi.ran) == count
    pi.tick(connect.CHECK_EVERY_SIGNED_IN_S)
    assert len(pi.ran) > count


def test_the_link_is_read_from_connects_output():
    session = connect.LinkSession(["sh", "-c", "echo 'Complete sign in by visiting "
                                               "https://connect.raspberrypi.com/verify/ABCD-EFGH'; sleep 5"])
    for _ in range(100):
        if session.link:
            break
        threading.Event().wait(0.02)
    session.stop()
    assert session.link == "https://connect.raspberrypi.com/verify/ABCD-EFGH"


def test_the_service_starts_after_first_boot_and_isnt_a_command_for_people():
    assert "After=systemd-logind.service hyperwheel-firstboot.service" in UNITS["hyperwheel-connect.service"]
    assert cli.needs_root(["connect-watch"]) and "connect-watch" not in cli.help_text(True, True)
