"""``hyperwheel datasets list | add | change | remove``, against the local fake GRIP."""

from __future__ import annotations

import os
import stat
from dataclasses import replace

import pytest
from conftest import PROJECT

from hyperwheel import cli
from hyperwheel.config import GripSettings, load_routing
from hyperwheel.datasetscmd import Console, check_with_grip, main, safe_name
from hyperwheel.grip.errors import GripError
from hyperwheel.installer import SUDO_RULE
from hyperwheel.keys import Credentials
from hyperwheel.sitesetup import render_routing_toml

OLD_SECRET = "s3cret-value-not-for-logs"
NEW_SECRET = "new-secret-not-for-logs-9f2"
NEW_CLIENT = "prisma-kenya"


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


class Pi:
    """The env's Pi with its routing file: one dataset from before GRIP's address was site-wide."""

    def __init__(self, env):
        self.env = env
        self.site = replace(env.site, grip=GripSettings(env.fake.base_url, env.fake.token_url))
        self.routing_path = env.tmp / "routing.toml"
        self.routing_path.write_text(render_routing_toml({PROJECT: {
            "logins": ["sandbox"], "api_base_url": env.fake.base_url, "token_url": env.fake.token_url}}))
        self.restarts, self.service = [], []
        env.fake.clients[NEW_CLIENT] = NEW_SECRET

    def run(self, action, typed, *, check=check_with_grip, root=True, active=False, restart=None):
        return main(self.site, [action], routing_path=self.routing_path, console=typed.console(), keys=self.env.keys,
                    check=check, restart=restart or (lambda: self.restarts.append(1) or "Hyperwheel was restarted."),
                    is_root=lambda: root, service_active=lambda: active,
                    service_stop=lambda: self.service.append("stop"),
                    service_start=lambda: self.service.append("start"))

    def routing(self):
        return load_routing(self.routing_path, test_mode=True, grip=self.site.grip)


@pytest.fixture
def pi(env):
    return Pi(env)


# ---- add ----


def test_add_checks_the_key_with_grip_then_saves_the_dataset_and_its_key_and_restarts(pi, env):
    tokens = env.fake.token_requests
    typed = Scripted(answers=[NEW_CLIENT, "Site_B/Study_One", "phantom", ""], hidden=[NEW_SECRET + "\n"])
    assert pi.run("add", typed) == 0, typed.text
    dataset = pi.routing().projects[NEW_CLIENT]
    assert dataset.logins == ("study_one", "phantom") and dataset.client_id == NEW_CLIENT
    assert dataset.api_base_url == env.fake.base_url  # the site's GRIP address: none of its own
    assert "api_base_url" not in pi.routing_path.read_text().split(f"[projects.{NEW_CLIENT}]")[1]
    assert env.keys.read(NEW_CLIENT) == Credentials(NEW_CLIENT, NEW_SECRET)
    assert stat.S_IMODE(os.stat(env.keys.path(NEW_CLIENT)).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(pi.routing_path).st_mode) == 0o644
    assert env.fake.token_requests == tokens + 1 and pi.restarts == [1]
    assert f"Added {NEW_CLIENT}, for scans from study_one, phantom." in typed.text
    assert NEW_SECRET not in typed.text and ";" not in typed.text
    assert PROJECT in pi.routing().projects  # the dataset already there is kept as it was
    assert not any("grip" in p.lower() or "address" in p.lower() for p in typed.prompts)  # never asked


def test_a_login_already_used_says_which_dataset_has_it_and_stops(pi, env):
    before, tokens = pi.routing_path.read_text(), env.fake.token_requests
    typed = Scripted(answers=[NEW_CLIENT, "sandbox"], hidden=[NEW_SECRET])
    assert pi.run("add", typed) == 1
    assert f"The login sandbox is already used by the dataset {PROJECT}." in typed.text
    assert pi.routing_path.read_text() == before and env.fake.token_requests == tokens
    assert not env.keys.path(NEW_CLIENT).exists() and typed.hidden == [NEW_SECRET]  # stopped before the secret


def test_a_client_id_already_on_the_pi_is_not_added_twice(pi):
    pi.run("add", Scripted(answers=[NEW_CLIENT, "one", ""], hidden=[NEW_SECRET]))
    typed = Scripted(answers=[NEW_CLIENT])
    assert pi.run("add", typed) == 1 and "already a dataset on this Pi" in typed.text
    assert "hyperwheel datasets change" in typed.text


def test_a_key_grip_refuses_adds_nothing(pi, env):
    before = pi.routing_path.read_text()
    typed = Scripted(answers=[NEW_CLIENT, "one", ""], hidden=["wrong-secret"])
    assert pi.run("add", typed) == 1 and pi.restarts == []
    assert "GRIP refused this key (HTTP 401). Nothing was saved." in typed.text
    assert pi.routing_path.read_text() == before and not env.keys.path(NEW_CLIENT).exists()
    assert "wrong-secret" not in typed.text


def test_grip_not_answering_adds_nothing(pi, env):
    def busy(project, creds, timeout_s):
        raise GripError("token request: HTTP 503")
    before = pi.routing_path.read_text()
    typed = Scripted(answers=[NEW_CLIENT, "one", ""], hidden=[NEW_SECRET])
    assert pi.run("add", typed, check=busy) == 1
    assert "couldn't be reached" in typed.text and "hyperwheel doctor" in typed.text
    assert pi.routing_path.read_text() == before and not env.keys.path(NEW_CLIENT).exists()


def test_a_client_id_that_cant_be_a_folder_name_gets_a_safe_one_but_is_always_shown(pi, env):
    odd = "unity/prisma.kenya"
    env.fake.clients[odd] = NEW_SECRET
    typed = Scripted(answers=[odd, "one", ""], hidden=[NEW_SECRET])
    assert pi.run("add", typed) == 0, typed.text
    name = safe_name(odd, {PROJECT})
    assert name == "unity-prisma-kenya" and pi.routing().projects[name].label == odd
    assert env.keys.read(name).client_id == odd
    listed = Scripted()
    pi.run("list", listed)
    assert f"  {odd}" in listed.shown and name not in listed.text


def test_safe_names_never_clash():
    assert safe_name("prisma-kenya", set()) == "prisma-kenya"
    assert safe_name("prisma-kenya", {"PRISMA-Kenya"}) == "prisma-kenya-2"
    assert safe_name("a/b", {"a-b", "a-b-2"}) == "a-b-3"
    assert safe_name("../..", set()) == "dataset"
    assert safe_name("_x", set()) == "x"


def test_stopping_at_any_prompt_changes_nothing(pi, env):
    before = pi.routing_path.read_text()
    for typed in (Scripted(answers=[""]), Scripted(answers=[NEW_CLIENT, ""]),
                  Scripted(answers=[NEW_CLIENT, "one", ""], hidden=[""]), Scripted(answers=[NEW_CLIENT])):
        assert pi.run("add", typed) == 1 and "Stopped. Nothing was changed." in typed.text
    assert pi.routing_path.read_text() == before and pi.restarts == []


# ---- change ----


def test_change_logins_replaces_them_and_a_login_of_another_dataset_is_refused(pi):
    pi.run("add", Scripted(answers=[NEW_CLIENT, "one", ""], hidden=[NEW_SECRET]))
    typed = Scripted(answers=["2", "1", "one", ""])  # dataset 2, its logins, one login only... that's unchanged
    assert pi.run("change", typed) == 0 and "Those are its logins already" in typed.text
    typed = Scripted(answers=[NEW_CLIENT, "1", "two", "sandbox"])
    assert pi.run("change", typed) == 1 and f"already used by the dataset {PROJECT}" in typed.text
    assert pi.routing().projects[NEW_CLIENT].logins == ("one",)
    typed = Scripted(answers=[NEW_CLIENT, "1", "two", "three", ""])
    assert pi.run("change", typed) == 0
    assert pi.routing().projects[NEW_CLIENT].logins == ("two", "three")
    assert "Logins you leave out stop going to it" in typed.text


def test_change_key_keeps_the_client_id_and_refuses_another(pi, env):
    pi.run("add", Scripted(answers=[NEW_CLIENT, "one", ""], hidden=[NEW_SECRET]))
    env.fake.clients[NEW_CLIENT] = "rotated-secret-0000"
    typed = Scripted(answers=[NEW_CLIENT, "2", NEW_CLIENT], hidden=["rotated-secret-0000"])
    assert pi.run("change", typed) == 0, typed.text
    assert env.keys.read(NEW_CLIENT).client_secret == "rotated-secret-0000"
    typed = Scripted(answers=[NEW_CLIENT, "2", "someone-else"])
    assert pi.run("change", typed) == 1 and "That's another client ID" in typed.text
    assert "hyperwheel datasets add" in typed.text


def test_a_dataset_from_before_client_ids_is_named_after_its_key_once_replaced(pi, env):
    env.fake.clients["site-a-sandbox-client"] = OLD_SECRET
    typed = Scripted(answers=["1", "2", "site-a-sandbox-client"], hidden=[OLD_SECRET])
    assert pi.run("change", typed) == 0, typed.text
    dataset = pi.routing().projects[PROJECT]
    assert dataset.client_id == "site-a-sandbox-client" and dataset.api_base_url == env.fake.base_url


def test_a_refused_new_key_leaves_the_old_one(pi, env):
    typed = Scripted(answers=["1", "2", "site-a-sandbox-client"], hidden=["wrong"])
    assert pi.run("change", typed) == 1 and "The key already stored is unchanged." in typed.text
    assert env.keys.read(PROJECT).client_secret == OLD_SECRET


# ---- remove ----


def test_remove_asks_for_its_name_then_deletes_the_dataset_and_key(pi, env):
    typed = Scripted(answers=["1", "wrong name"])
    assert pi.run("remove", typed) == 1 and PROJECT in pi.routing().projects
    assert "Scans from sandbox will then be deleted on this Pi, not uploaded." in typed.text
    typed = Scripted(answers=["1", PROJECT])
    assert pi.run("remove", typed, active=True) == 0, typed.text
    assert pi.routing().projects == {} and not env.keys.path(PROJECT).exists()
    assert pi.service == ["stop", "start"] and pi.restarts == [1]


def test_remove_refuses_while_files_wait_unless_they_may_be_deleted(pi, env):
    study = env.stage_study(n_dicom=2, raw_sizes=(1000,))
    done = env.store.add_study("1.2.9", state="uploaded", project=PROJECT, stable_at=env.clock())
    paths = [f.path for f in env.store.files(study.id)]
    typed = Scripted(answers=["1", PROJECT, "no"])
    assert pi.run("remove", typed) == 1
    assert "3 files from 1 session still wait to upload" in typed.text and "never reach GRIP" in typed.text
    assert PROJECT in pi.routing().projects and all(os.path.exists(p) for p in paths)
    typed = Scripted(answers=["1", PROJECT, "delete them"])
    assert pi.run("remove", typed) == 0, typed.text
    assert not any(os.path.exists(p) for p in paths)
    assert not (env.site.staging_dir / PROJECT).exists()
    after = env.store.study(study.id)
    assert after.state == "unrouted" and after.project is None and after.stable_at is not None
    assert env.store.files(study.id) == [] and env.store.open_flags(study_id=study.id) == []
    assert env.store.study(done.id).state == "archived"  # on GRIP already: no longer watched for raw data
    assert "1 waiting session was deleted on this Pi" in typed.text


def test_a_dropped_study_is_then_deleted_from_orthanc_like_one_with_no_dataset(pi, env):
    from hyperwheel.organiser import Organiser
    study = env.stage_study(n_dicom=1, raw_sizes=())
    pi.run("remove", Scripted(answers=["1", PROJECT, "delete them"]))
    routing = pi.routing()
    Organiser(env.store, env.site, routing, env.orthanc, clock=env.clock).housekeeping()
    assert env.store.study(study.id).state == "purged" and study.orthanc_id in env.orthanc.deleted_studies


# ---- list, root, the old command ----


def test_list_shows_each_dataset_its_logins_and_its_key_but_never_the_secret(pi, env):
    pi.run("add", Scripted(answers=[NEW_CLIENT, "one", "two", ""], hidden=[NEW_SECRET]))
    env.store.set_key_status(PROJECT, "rejected", "HTTP 401")
    env.store.set_key_status(NEW_CLIENT, "working")
    typed = Scripted()
    assert pi.run("list", typed) == 0
    assert typed.shown == [
        "GRIP datasets on this Pi:",
        f"  {PROJECT}",
        "    Scanner login: sandbox",
        "    Key: refused by GRIP (wrong, revoked or disabled). Uploads for this dataset are paused "
        "(client ID ending ient)",
        f"  {NEW_CLIENT}",
        "    Scanner logins: one, two",
        "    Key: working",
        "Scans from any other login are deleted on this Pi, never uploaded.",
    ]
    assert OLD_SECRET not in typed.text and NEW_SECRET not in typed.text


def test_list_with_no_datasets_says_how_to_add_one(pi):
    pi.routing_path.write_text(render_routing_toml({}))
    typed = Scripted()
    assert pi.run("list", typed) == 0 and "To add one, type: hyperwheel datasets add" in typed.text


def test_every_action_needs_root(pi):
    for action in ("list", "add", "change", "remove"):
        typed = Scripted()
        assert pi.run(action, typed, root=False) == 2 and typed.prompts == []
        assert f"'hyperwheel datasets {action}' needs administrator rights" in typed.text


def test_the_data_manager_may_run_them_list_without_a_password():
    look, asks = SUDO_RULE.split("Cmnd_Alias HYPERWHEEL_KEYS")
    assert "/usr/local/bin/hyperwheel datasets list" in look
    for action in ("add", "change", "remove"):
        assert f"/usr/local/bin/hyperwheel datasets {action}" in asks
    assert "hyperwheel keys" not in SUDO_RULE


def test_the_old_keys_command_says_what_it_is_now():
    assert cli.problem(["keys", "set"]) == "This is now: hyperwheel datasets change"
    assert cli.problem(["keys", "list"]) == "This is now: hyperwheel datasets list"
    assert cli.problem(["datasets"]).startswith("'hyperwheel datasets' needs one more word")
    assert cli.problem(["datasets", "delete"]) == "There's no command 'hyperwheel datasets delete'."
