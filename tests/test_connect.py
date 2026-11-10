"""Raspberry Pi Connect: the key box on a newly flashed Pi's page, the watcher that signs it in, and its state."""

from __future__ import annotations

import json
import logging
import stat
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from hyperwheel import cli, connect
from hyperwheel.connect import OK, PROBLEM, Watcher
from hyperwheel.dashboard import make_not_set_up_server, not_set_up_status
from hyperwheel.installer import DATA_MANAGER, UNITS, Layout

KEY = "rpoak_test0123456789abcdef"
UID = 1001


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def folder(tmp_path) -> Path:
    (tmp_path / "connect" / "inbox").mkdir(parents=True)
    return tmp_path / "connect"


# ---- the key box ------------------------------------------------------------------------------------------


def test_an_organisation_key_is_kept_root_readable_only(folder):
    assert connect.accept_key(f"  {KEY}\n", folder) is None
    key = folder / "inbox" / "key"
    assert key.read_text() == KEY and mode(key) == 0o600


@pytest.mark.parametrize("text", ["", "hello", "rpoak_", "rpoak_short", f"{KEY} extra", f"{KEY}\n{KEY}",
                                  "rporg_abcdefghijkl"])
def test_anything_else_is_refused_and_not_kept(folder, text):
    assert "starts with rpoak_" in connect.accept_key(text, folder)
    assert not (folder / "inbox" / "key").exists()


def test_a_personal_key_is_refused_so_a_pi_cant_join_someones_own_account(folder):
    assert "personal key" in connect.accept_key("rpuak_test0123456789abcdef", folder)
    assert not (folder / "inbox" / "key").exists()


# ---- the page ---------------------------------------------------------------------------------------------


@pytest.fixture
def page(folder, tmp_path):
    site_toml = tmp_path / "site.toml"
    server = make_not_set_up_server(bind="127.0.0.1", port=0, folder=folder, site_toml=site_toml, now=lambda: 1000.0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", site_toml
    server.shutdown()
    server.server_close()


def post(base, body, content_type="application/json"):
    request = urllib.request.Request(base + "/api/connect-key", data=body, method="POST",
                                     headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(request) as r:
            return r.status, json.loads(r.read())["message"]
    except urllib.error.HTTPError as e:
        data = e.read()
        return e.code, json.loads(data)["message"] if e.code != 404 else ""


def test_the_page_has_the_box_the_link_and_nothing_else(page):
    base, _ = page
    with urllib.request.urlopen(base + "/") as r:
        html = r.read().decode()
        assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert "This Pi isn't set up yet" in html and "Send this link to your Hyperwheel admin." in html
    with urllib.request.urlopen(base + "/setup.js") as r:
        assert "innerHTML" not in r.read().decode()


def test_pasting_a_key_keeps_it_and_never_sends_it_back(page, folder):
    base, _ = page
    status, message = post(base, json.dumps({"key": KEY}).encode())
    assert status == 200 and KEY not in message
    assert (folder / "inbox" / "key").read_text() == KEY
    (folder / "state.json").write_text(json.dumps({"signed_in": False, "checked_at": 1000.0, "key": "trying"}))
    with urllib.request.urlopen(base + "/api/status") as r:
        assert KEY not in r.read().decode()


def test_a_bad_key_is_explained(page, folder):
    base, _ = page
    status, message = post(base, json.dumps({"key": "rpuak_test0123456789"}).encode())
    assert status == 400 and "personal key" in message and not (folder / "inbox" / "key").exists()
    for body in (b"not json", b'{"nokey": 1}', json.dumps({"key": ["x"]}).encode(), b"x" * 2000):
        assert post(base, body)[0] == 400
    assert not (folder / "inbox" / "key").exists()


def test_only_json_is_taken_so_another_web_page_cant_send_a_key(page, folder):
    base, _ = page
    assert post(base, f"key={KEY}".encode(), "application/x-www-form-urlencoded")[0] == 404
    assert not (folder / "inbox" / "key").exists()


def test_the_box_stops_once_the_pi_has_site_settings(page, folder):
    base, site_toml = page
    site_toml.write_text("")
    assert post(base, json.dumps({"key": KEY}).encode())[0] == 403
    assert not (folder / "inbox" / "key").exists()


def test_status_shows_the_link_until_the_pi_is_signed_in():
    state = {"signed_in": False, "checked_at": 1000.0, "key": "none", "link": "https://connect.raspberrypi.com/x"}
    assert not_set_up_status(state, 1000.0) == {"signed_in": False, "link": state["link"], "key_refused": False,
                                                "key_trying": False}
    assert not_set_up_status({**state, "key": "refused"}, 1000.0)["key_refused"]
    assert not_set_up_status({**state, "key": "trying"}, 1000.0)["key_trying"]
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
        self.sign_in_code = 0
        self.key_seen: str | None = None
        self.is_online = True
        self.sessions: list[FakeSession] = []
        for folder in ("var/lib/hyperwheel/connect/inbox", f"run/user/{UID}", f"home/{DATA_MANAGER}", "etc/hyperwheel"):
            (root / folder).mkdir(parents=True)
        self.watcher = Watcher(layout=self.layout, run=self.run, spawn=self.spawn, online=lambda: self.is_online,
                               clock=lambda: self.now, uid_of=lambda a: UID if a == DATA_MANAGER else None)

    def run(self, cmd, timeout=60.0):
        self.ran.append(list(cmd))
        if "status" in cmd:
            return 0, f"Signed in: {'yes' if self.signed else 'no'}\nScreen sharing: allowed\n"
        for word in cmd:
            if word.startswith("--auth-key=@"):
                self.key_seen = Path(word.split("@", 1)[1]).read_text()
                if self.sign_in_code == 0:
                    self.signed = True
                return self.sign_in_code, ""
        if cmd[-1] == "on":
            link = self.layout.p(f"/home/{DATA_MANAGER}") / connect.ENABLED_LINK
            link.parent.mkdir(parents=True, exist_ok=True)
            link.write_text("")
        return 0, ""

    def spawn(self, cmd):
        self.sessions.append(FakeSession(cmd))
        return self.sessions[-1]

    def paste(self, key=KEY):
        assert connect.accept_key(key, self.layout.p(connect.FOLDER)) is None

    @property
    def key_file(self) -> Path:
        return self.layout.p(connect.FOLDER) / "inbox" / "key"

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
    assert len(on) == 1 and on[0][:4] == ["setpriv", f"--reuid={UID}", f"--regid={UID}", "--init-groups"]
    assert f"HOME=/home/{DATA_MANAGER}" in on[0] and on[0][-2:] == ["rpi-connect", "on"]
    assert not any(c[0] == "runuser" for c in pi.ran)  # no login session, so nothing in the journal
    assert f"XDG_RUNTIME_DIR=/run/user/{UID}" in on[0]
    pi.tick()
    assert len([c for c in pi.ran if c[-1] == "on"]) == 1  # asked once
    assert len(pi.sessions) == 1  # the same link stays while it's running


def test_a_new_link_when_the_old_one_ends(pi):
    pi.tick()
    pi.sessions[0].alive = False
    pi.tick()
    assert len(pi.sessions) == 2


def test_a_pasted_key_signs_in_and_is_deleted_and_never_on_a_command_line(pi):
    pi.tick()
    pi.paste()
    pi.tick(connect.TICK_S)
    assert pi.key_seen == KEY and pi.signed and not pi.key_file.exists()
    assert pi.sessions[0].stopped  # the link is gone
    assert pi.state["signed_in"] is True and pi.state["link"] is None and pi.state["key"] == "none"
    assert not any(KEY in word for cmd in pi.ran for word in cmd)
    assert not list((pi.layout.p(f"/run/user/{UID}")).iterdir())  # the temporary copy is gone
    assert KEY not in json.dumps(pi.state)


def test_with_no_internet_the_key_is_kept_and_tried_again(pi):
    pi.sign_in_code, pi.is_online = 1, False
    pi.paste()
    for _ in range(5):
        pi.tick()
        assert pi.state["key"] == "trying"
    assert pi.key_file.exists()
    pi.sign_in_code, pi.is_online = 0, True
    pi.tick()
    assert pi.signed and not pi.key_file.exists() and pi.state["signed_in"] is True


def test_the_key_isnt_tried_every_few_seconds(pi):
    pi.sign_in_code, pi.is_online = 1, False
    pi.paste()
    pi.tick()
    tried = len([c for c in pi.ran if any("--auth-key" in w for w in c)])
    pi.tick(connect.TICK_S)
    pi.tick(connect.TICK_S)
    assert len([c for c in pi.ran if any("--auth-key" in w for w in c)]) == tried


def test_a_key_refused_with_the_internet_working_is_given_up_after_a_few_tries(pi, caplog):
    pi.sign_in_code = 1
    pi.paste()
    with caplog.at_level(logging.INFO):
        for _ in range(connect.REFUSALS_BEFORE_GIVING_UP - 1):
            pi.tick()
            assert pi.state["key"] == "trying" and pi.key_file.exists()
        pi.tick()
    assert pi.state["key"] == "refused" and not pi.key_file.exists()
    assert KEY not in caplog.text
    pi.tick()  # the link is back, and the message stays until a new key
    assert pi.state["key"] == "refused" and pi.state["link"]
    pi.sign_in_code = 0
    pi.paste("rpoak_another0123456789")
    pi.tick()
    assert pi.state["signed_in"] is True and pi.state["key"] == "none"


def test_once_set_up_it_only_reads_the_state(pi):
    set_up(pi)
    pi.tick()
    assert pi.state["signed_in"] is False and pi.state["link"] is None
    assert not pi.sessions and not any(c[0] == "loginctl" or c[-1] == "on" for c in pi.ran)
    assert all("status" in c for c in pi.ran)
    (pi.key_file).write_text(KEY)  # nothing signs a set-up Pi in
    pi.tick()
    assert pi.key_seen is None


def test_it_uses_the_account_that_has_connect_enabled(pi):
    other = pi.layout.p("/home/someone")
    (other / connect.ENABLED_LINK).parent.mkdir(parents=True)
    (other / connect.ENABLED_LINK).write_text("")
    pi.watcher.uid_of = lambda a: 1000 if a == "someone" else (UID if a == DATA_MANAGER else None)
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
