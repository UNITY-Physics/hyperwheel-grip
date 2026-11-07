"""``hyperwheel keys list`` and ``hyperwheel keys set``, against the local fake GRIP."""

from __future__ import annotations

import os
import stat
from dataclasses import replace

from conftest import PROJECT

from hyperwheel.config import Project, Routing
from hyperwheel.grip.errors import GripError
from hyperwheel.keys import Credentials
from hyperwheel.keyscmd import Console, check_with_grip, list_keys, set_key

OLD_SECRET = "s3cret-value-not-for-logs"
NEW_SECRET = "new-secret-not-for-logs-9f2"


class Scripted:
    """Plays a data manager at the keyboard: visible answers, hidden answers, and what was shown."""

    def __init__(self, answers=(), hidden=()):
        self.answers, self.hidden, self.shown, self.prompts = list(answers), list(hidden), [], []

    def ask(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def ask_hidden(self, prompt):
        self.prompts.append(prompt)
        if not self.hidden:
            raise EOFError
        return self.hidden.pop(0)

    def console(self):
        return Console(ask=self.ask, ask_hidden=self.ask_hidden, say=self.shown.append)

    @property
    def text(self):
        return "\n".join(self.shown)


def run_set(env, typed, *, check=check_with_grip, restarts=None, root=True):
    restarts = restarts if restarts is not None else []
    code = set_key(env.site, env.routing, typed.console(), keys=env.keys, check=check,
                   restart=lambda: restarts.append(1) or "Hyperwheel was restarted to use it.",
                   is_root=lambda: root)
    return code, restarts


def test_a_new_key_is_checked_with_grip_then_saved_root_only_and_the_service_restarted(env):
    env.fake.clients["site-a-new-client"] = NEW_SECRET
    typed = Scripted(answers=["1", "site-a-new-client"], hidden=[NEW_SECRET + "\n"])
    tokens = env.fake.token_requests
    code, restarts = run_set(env, typed)
    assert code == 0, typed.text
    assert env.keys.read(PROJECT) == Credentials("site-a-new-client", NEW_SECRET)
    assert stat.S_IMODE(os.stat(env.keys.path(PROJECT)).st_mode) == 0o600
    assert env.fake.token_requests == tokens + 1 and restarts == [1]
    assert "GRIP accepted the key" in typed.text and f"Saved for {PROJECT}" in typed.text
    assert NEW_SECRET not in typed.text and OLD_SECRET not in typed.text


def test_a_key_grip_refuses_is_not_saved_and_the_old_key_stays(env):
    typed = Scripted(answers=["1", "site-a-sandbox-client"], hidden=["wrong-secret"])
    code, restarts = run_set(env, typed)
    assert code == 1 and restarts == []
    assert env.keys.read(PROJECT).client_secret == OLD_SECRET
    assert "GRIP refused this key (HTTP 401). Nothing was saved." in typed.text
    assert "The key already stored for SiteA-Sandbox is unchanged." in typed.text
    assert "wrong-secret" not in typed.text


def test_nothing_is_saved_when_grip_cannot_be_reached(env):
    unreachable = replace(env.routing.projects[PROJECT], token_url="http://127.0.0.1:9/token")
    env.routing = Routing(projects={PROJECT: unreachable})
    typed = Scripted(answers=["1", "site-a-new-client"], hidden=[NEW_SECRET])
    code, restarts = run_set(env, typed)
    assert code == 1 and restarts == []
    assert env.keys.read(PROJECT).client_secret == OLD_SECRET
    assert "GRIP couldn't be reached to check the key" in typed.text
    assert "hyperwheel doctor" in typed.text and "sudo" not in typed.text


def test_grip_busy_is_not_taken_as_a_refusal(env):
    def busy(project, creds, timeout_s):
        raise GripError("token request: HTTP 503")
    typed = Scripted(answers=["1", "site-a-new-client"], hidden=[NEW_SECRET])
    code, _ = run_set(env, typed, check=busy)
    assert code == 1 and "couldn't be reached" in typed.text and "HTTP 503" in typed.text
    assert env.keys.read(PROJECT).client_secret == OLD_SECRET


def test_the_same_key_again_is_checked_but_nothing_restarts(env):
    typed = Scripted(answers=["1", "site-a-sandbox-client"], hidden=[OLD_SECRET])
    code, restarts = run_set(env, typed)
    assert code == 0 and restarts == [] and "already stored" in typed.text


def test_stopping_at_any_prompt_saves_nothing(env):
    for typed in (Scripted(answers=[""]), Scripted(answers=["1", ""]), Scripted(answers=["1", "c"], hidden=[""]),
                  Scripted(answers=["1"])):  # Enter on the project, the client ID, the secret; Ctrl-D
        code, restarts = run_set(env, typed)
        assert code == 1 and restarts == [] and "Nothing was saved" in typed.text
    assert env.keys.read(PROJECT).client_secret == OLD_SECRET


def test_the_project_can_be_picked_by_name_and_wrong_answers_are_explained(env):
    other = Project("SiteA-Other", ("other",), env.fake.base_url, env.fake.token_url)
    env.routing = Routing(projects={**env.routing.projects, other.name: other})
    env.fake.clients["other-client"] = NEW_SECRET
    typed = Scripted(answers=["7", "../keys", "SiteA-Other", "other-client"], hidden=[NEW_SECRET])
    code, _ = run_set(env, typed)
    assert code == 0, typed.text
    assert "There's no project '7'" in typed.text and "There's no project '../keys'" in typed.text
    assert env.keys.read("SiteA-Other").client_id == "other-client"
    assert "2. SiteA-Other  (no key stored)" in typed.text


def test_a_secret_with_a_space_or_line_break_inside_is_refused_before_grip_is_asked(env):
    tokens = env.fake.token_requests
    typed = Scripted(answers=["1", "site-a-new-client"], hidden=["two words", "a\nb"])
    code, _ = run_set(env, typed)
    assert code == 1 and env.fake.token_requests == tokens
    assert typed.text.count("doesn't look like a secret") == 2 and "two words" not in typed.text


def test_the_secret_is_asked_hidden(env):
    typed = Scripted(answers=["1", "site-a-sandbox-client"], hidden=[OLD_SECRET])
    run_set(env, typed)
    assert typed.prompts[-1].startswith("Client secret")
    assert len(typed.hidden) == 0  # the secret came through the hidden prompt, not the visible one


def test_a_failed_restart_is_reported_and_the_key_is_kept(env):
    env.fake.clients["site-a-new-client"] = NEW_SECRET
    typed = Scripted(answers=["1", "site-a-new-client"], hidden=[NEW_SECRET])

    def broken():
        raise RuntimeError("systemctl restart ended with exit 1")
    code = set_key(env.site, env.routing, typed.console(), keys=env.keys, restart=broken, is_root=lambda: True)
    assert code == 1 and env.keys.read(PROJECT).client_id == "site-a-new-client"
    assert "couldn't be restarted" in typed.text and "Ask the maintainer" in typed.text


def test_both_commands_need_root(env):
    typed = Scripted()
    assert run_set(env, typed, root=False)[0] == 2 and "'hyperwheel keys set' needs administrator rights" in typed.text
    typed = Scripted()
    assert list_keys(env.site, env.routing, typed.console(), keys=env.keys, is_root=lambda: False) == 2
    assert "'hyperwheel keys list' needs administrator rights" in typed.text and typed.prompts == []


def test_list_shows_each_project_and_its_status_but_never_the_secret(env):
    other = Project("SiteA-Other", ("other",), env.fake.base_url, env.fake.token_url)
    env.routing = Routing(projects={**env.routing.projects, other.name: other})
    env.store.set_key_status(PROJECT, "working")
    typed = Scripted()
    assert list_keys(env.site, env.routing, typed.console(), keys=env.keys, is_root=lambda: True) == 0
    assert typed.shown == ["GRIP keys on this Pi:",
                           f"  {PROJECT}: key stored (client ID ending ient), working",
                           "  SiteA-Other: no key stored"]
    assert OLD_SECRET not in typed.text and "site-a-sandbox-client" not in typed.text


def test_list_shows_a_refused_key_plainly(env):
    env.store.set_key_status(PROJECT, "rejected", "GRIP refused the key: HTTP 401")
    typed = Scripted()
    list_keys(env.site, env.routing, typed.console(), keys=env.keys, is_root=lambda: True)
    assert "refused by GRIP (wrong, revoked or disabled): uploads for this project are paused" in typed.text
