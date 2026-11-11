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
NEW_CLIENT = "site-b-study-one"


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
        # Raw data off unless a test is about it, so the "not checked on the scanner yet" question stays out of the way.
        self.site = replace(env.site, grip=GripSettings(env.fake.base_url, env.fake.token_url),
                            rrdf=replace(env.site.rrdf, enabled=False))
        self.routing_path = env.tmp / "routing.toml"
        self.routing_path.write_text(render_routing_toml({PROJECT: {
            "logins": ["sandbox"], "api_base_url": env.fake.base_url, "token_url": env.fake.token_url}}))
        self.restarts, self.service = [], []
        env.fake.clients[NEW_CLIENT] = NEW_SECRET

    def run(self, action, typed, *, check=check_with_grip, root=True, active=True, restart=None):
        return main(self.site, [action], routing_path=self.routing_path, console=typed.console(), keys=self.env.keys,
                    check=check, restart=restart or (lambda: self.restarts.append(1) or "Hyperwheel was restarted."),
                    is_root=lambda: root, service_active=lambda: active,
                    service_stop=lambda: self.service.append("stop"),
                    service_start=lambda: self.service.append("start"),
                    restart_dashboard=lambda: self.service.append("dashboard"),
                    update_lock=self.env.tmp / "update" / ".lock", check_lock=self.env.tmp / "update" / ".check",
                    current=self.env.tmp / "current", clock=self.env.clock)

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
    odd = "unity/site-b.study-one"
    env.fake.clients[odd] = NEW_SECRET
    typed = Scripted(answers=[odd, "one", ""], hidden=[NEW_SECRET])
    assert pi.run("add", typed) == 0, typed.text
    name = safe_name(odd, {PROJECT})
    assert name == "unity-site-b-study-one" and pi.routing().projects[name].label == odd
    assert env.keys.read(name).client_id == odd
    listed = Scripted()
    pi.run("list", listed)
    assert f"  {odd}" in listed.shown and name not in listed.text


def test_safe_names_never_clash():
    assert safe_name("site-b-study-one", set()) == "site-b-study-one"
    assert safe_name("site-b-study-one", {"SITE-B-Study-One"}) == "site-b-study-one-2"
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


def test_change_key_with_the_same_client_id_replaces_the_secret(pi, env):
    pi.run("add", Scripted(answers=[NEW_CLIENT, "one", ""], hidden=[NEW_SECRET]))
    env.fake.clients[NEW_CLIENT] = "rotated-secret-0000"
    typed = Scripted(answers=[NEW_CLIENT, "2", NEW_CLIENT], hidden=["rotated-secret-0000"])
    assert pi.run("change", typed) == 0, typed.text
    assert env.keys.read(NEW_CLIENT).client_secret == "rotated-secret-0000"
    assert "type yes" not in "".join(typed.prompts)  # the same client ID: nothing to ask


def test_change_key_with_another_client_id_asks_once_and_keeps_the_waiting_files(pi, env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    paths = [f.path for f in env.store.files(study.id)]
    env.fake.clients["site-a-sandbox-new"] = "new-pair-secret"
    typed = Scripted(answers=["1", "2", "site-a-sandbox-new", "no"])
    assert pi.run("change", typed) == 1
    assert "The dataset's client ID changes from site-a-sandbox-client to site-a-sandbox-new." in typed.text
    assert env.keys.read(PROJECT).client_id == "site-a-sandbox-client"
    typed = Scripted(answers=["1", "2", "site-a-sandbox-new", "yes"], hidden=["new-pair-secret"])
    assert pi.run("change", typed) == 0, typed.text
    assert env.keys.read(PROJECT) == Credentials("site-a-sandbox-new", "new-pair-secret")
    dataset = pi.routing().projects[PROJECT]
    assert dataset.label == "site-a-sandbox-new" and dataset.logins == ("sandbox",)
    assert env.store.study(study.id).project == PROJECT and all(os.path.exists(x) for x in paths)  # still waiting
    assert "new-pair-secret" not in typed.text


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
    assert "Scans from sandbox will then be deleted on this Pi, not uploaded, and raw data" in typed.text
    typed = Scripted(answers=["1", PROJECT])
    assert pi.run("remove", typed, active=True) == 0, typed.text
    assert pi.routing().projects == {} and not env.keys.path(PROJECT).exists()
    assert pi.service == ["stop", "start", "dashboard"] and pi.restarts == []


def test_remove_refuses_while_files_wait_unless_they_may_be_deleted(pi, env):
    study = env.stage_study(n_dicom=2, raw_sizes=(1000,))
    done = env.store.add_study("1.2.9", state="uploaded", project=PROJECT, stable_at=env.clock())
    paths = [f.path for f in env.store.files(study.id)]
    typed = Scripted(answers=["1", PROJECT, "no"])
    assert pi.run("remove", typed) == 1
    assert "1 session of SiteA-Sandbox is not all on GRIP yet (3 files on this Pi not uploaded)" in typed.text
    assert "never reaches GRIP" in typed.text
    assert PROJECT in pi.routing().projects and all(os.path.exists(p) for p in paths)
    typed = Scripted(answers=["1", PROJECT, "delete them"])
    assert pi.run("remove", typed) == 0, typed.text
    assert not any(os.path.exists(p) for p in paths)
    assert not (env.site.staging_dir / PROJECT).exists()
    after = env.store.study(study.id)
    assert after.state == "unrouted" and after.project is None and after.stable_at is not None
    assert env.store.files(study.id) == [] and env.store.open_flags(study_id=study.id) == []
    assert env.store.study(done.id).state == "archived"  # on GRIP already: no longer watched for raw data
    assert "1 session was deleted on this Pi" in typed.text


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
    assert cli.problem(["keys", "set"]) == ("This is now: hyperwheel datasets change\n"
                                            "For a new dataset: hyperwheel datasets add")
    assert cli.problem(["keys", "list"]) == "This is now: hyperwheel datasets list"
    assert cli.problem(["datasets"]).startswith("'hyperwheel datasets' needs one more word")
    assert cli.problem(["datasets", "delete"]) == "There's no command 'hyperwheel datasets delete'."


# ---- remove: what counts as waiting, and what's never touched ----


def test_refused_files_count_as_not_on_grip_and_need_the_second_confirmation(pi, env):
    study = env.stage_study(n_dicom=2, raw_sizes=())
    for f in env.store.files(study.id):
        env.store.move_file(f.id, "sending")
        env.store.move_file(f.id, "refused")
    typed = Scripted(answers=["1", PROJECT, ""])
    assert pi.run("remove", typed) == 1 and "(2 files on this Pi not uploaded)" in typed.text
    assert PROJECT in pi.routing().projects and all(os.path.exists(f.path) for f in env.store.files(study.id))


def test_remove_waits_while_images_are_still_arriving_for_the_dataset(pi, env):
    # A: a session already on GRIP that went back to receiving (one more image arrived)
    study = env.stage_study(n_dicom=1, raw_sizes=())
    env.store.move_study(study.id, "uploading")
    env.store.move_study(study.id, "receiving")
    before = pi.routing_path.read_text()
    typed = Scripted(answers=["1", PROJECT, "delete them"])
    assert pi.run("remove", typed, active=True) == 1
    assert "Images are still arriving from the scanner for SiteA-Sandbox. Nothing was changed." in typed.text
    assert pi.routing_path.read_text() == before and env.store.study(study.id).state == "receiving"
    assert pi.service == [] and typed.prompts[-1].startswith("Which dataset")  # nothing stopped, nothing asked
    # B: a new scan from one of its logins, not routed yet
    env.store.move_study(study.id, "stable")
    env.store.move_study(study.id, "checking")
    env.store.move_study(study.id, "ready")
    env.store.add_study("1.2.11", state="receiving", operators_name="site_a/Sandbox")
    typed = Scripted(answers=["1", PROJECT, "delete them"])
    assert pi.run("remove", typed) == 1 and "Images are still arriving" in typed.text


def test_remove_stops_if_more_arrived_while_the_person_answered(pi, env):
    study = env.stage_study(n_dicom=1, raw_sizes=())

    def more_arrives():
        env.add_dicom(env.store.study(study.id), "1.2.3.4.5")
        pi.service.append("stop")
    typed = Scripted(answers=["1", PROJECT, "delete them"])
    code = main(pi.site, ["remove"], routing_path=pi.routing_path, console=typed.console(), keys=env.keys,
                is_root=lambda: True, service_active=lambda: True, service_stop=more_arrives,
                service_start=lambda: pi.service.append("start"), restart_dashboard=lambda: None)
    assert code == 1 and "More arrived for SiteA-Sandbox while you answered. Nothing was changed." in typed.text
    assert PROJECT in pi.routing().projects and len(env.store.files(study.id)) == 2
    assert pi.service == ["stop", "start"]


def test_remove_stopped_part_way_keeps_the_dataset_and_running_it_again_finishes(pi, env, monkeypatch):
    from hyperwheel import datasetscmd
    first = env.stage_study(n_dicom=1, raw_sizes=())
    second = env.stage_study(n_dicom=1, raw_sizes=())
    real, calls = datasetscmd.Datasets._drop_one, []

    def fails_second(self, store, study, name):
        calls.append(study.id)
        if len(calls) == 2:
            raise OSError("disk went away")
        return real(self, store, study, name)
    monkeypatch.setattr(datasetscmd.Datasets, "_drop_one", fails_second)
    typed = Scripted(answers=["1", PROJECT, "delete them"])
    assert pi.run("remove", typed) == 1
    assert "stopped part-way (OSError). The dataset is still on this Pi" in typed.text
    assert "To finish, type: hyperwheel datasets remove" in typed.text
    assert PROJECT in pi.routing().projects and env.keys.path(PROJECT).exists()
    monkeypatch.setattr(datasetscmd.Datasets, "_drop_one", real)
    typed = Scripted(answers=["1", PROJECT, "delete them"])
    assert pi.run("remove", typed) == 0, typed.text
    assert pi.routing().projects == {} and not env.keys.path(PROJECT).exists()
    assert {env.store.study(s.id).state for s in (first, second)} == {"unrouted"}


def test_a_service_that_cant_be_stopped_changes_nothing(pi, env):
    import subprocess
    env.stage_study(n_dicom=1, raw_sizes=())

    def cant():
        raise subprocess.CalledProcessError(1, "systemctl")
    typed = Scripted(answers=["1", PROJECT, "delete them"])
    code = main(pi.site, ["remove"], routing_path=pi.routing_path, console=typed.console(), keys=env.keys,
                is_root=lambda: True, service_active=lambda: True, service_stop=cant,
                service_start=lambda: pi.service.append("start"), restart_dashboard=lambda: None)
    assert code == 1 and "Hyperwheel couldn't be stopped to clear the dataset, so nothing was changed." in typed.text
    assert PROJECT in pi.routing().projects


def test_a_session_on_grip_with_a_refused_file_left_is_counted_too(pi, env):
    study = env.stage_study(n_dicom=2, raw_sizes=())
    first, second = env.store.files(study.id)
    env.store.move_file(first.id, "sending")
    env.store.move_file(first.id, "confirmed")
    env.store.move_file(second.id, "sending")
    env.store.move_file(second.id, "refused")
    env.store.move_study(study.id, "uploading")
    env.store.move_study(study.id, "uploaded")
    typed = Scripted(answers=["1", PROJECT, "delete them"])
    assert pi.run("remove", typed) == 0 and "(1 file on this Pi not uploaded)" in typed.text
    assert not os.path.exists(second.path) and env.store.study(study.id).state == "unrouted"


def test_the_count_and_the_deleting_happen_with_the_upload_service_stopped(pi, env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    path = env.store.files(study.id)[0].path
    seen = []
    run = main(pi.site, ["remove"], routing_path=pi.routing_path,
               console=Scripted(answers=["1", PROJECT, "delete them"]).console(), keys=env.keys,
               is_root=lambda: True, service_active=lambda: True,
               service_stop=lambda: seen.append(("stop", os.path.exists(path))),
               service_start=lambda: seen.append(("start", os.path.exists(path))), restart_dashboard=lambda: None)
    assert run == 0 and seen == [("stop", True), ("start", False)]


def test_a_store_that_cant_be_read_stops_remove_and_changes_nothing(pi, env):
    env.stage_study(n_dicom=1, raw_sizes=())
    env.store.close()
    env.site.state_db.write_bytes(b"not a database")
    for extra in ("-wal", "-shm"):
        env.site.state_db.with_name(env.site.state_db.name + extra).unlink(missing_ok=True)
    before = pi.routing_path.read_text()
    typed = Scripted(answers=["1", PROJECT, "delete them"])
    assert pi.run("remove", typed, active=True) == 1
    assert "can't tell what's still waiting to upload. Nothing was changed." in typed.text
    assert pi.routing_path.read_text() == before and env.keys.path(PROJECT).exists()
    assert pi.service == []  # found before anything was stopped
    from hyperwheel.state import Store
    env.site.state_db.unlink()
    env.store = Store(env.site.state_db, clock=env.clock)  # for the fixture's close


def test_a_path_outside_the_datasets_own_folder_is_never_deleted(pi, env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    elsewhere = env.tmp / "not-staging.txt"
    elsewhere.write_text("keep me")
    env.store.add_file(kind="dicom", name="x.dcm", state="staged", study_id=study.id, path=str(elsewhere),
                       sop_uid="9.9.9")
    assert pi.run("remove", Scripted(answers=["1", PROJECT, "delete them"])) == 0
    assert elsewhere.read_text() == "keep me"


# ---- client IDs and names ----


def test_a_client_id_already_in_an_older_datasets_key_is_not_added_again(pi, env):
    typed = Scripted(answers=["site-a-sandbox-client"])
    assert pi.run("add", typed) == 1 and "already a dataset on this Pi" in typed.text


def test_replacing_an_older_datasets_key_with_another_datasets_client_id_changes_nothing(pi, env):
    pi.run("add", Scripted(answers=[NEW_CLIENT, "one", ""], hidden=[NEW_SECRET]))
    typed = Scripted(answers=[PROJECT, "2", NEW_CLIENT], hidden=[NEW_SECRET])
    assert pi.run("change", typed) == 1 and f"That client ID is the dataset {NEW_CLIENT}'s." in typed.text
    assert env.keys.read(PROJECT).client_id == "site-a-sandbox-client"


def test_a_new_dataset_never_takes_the_folder_name_of_a_removed_ones_sessions(pi, env):
    env.store.add_study("1.2.10", state="ready", project=NEW_CLIENT)  # left by a dataset removed earlier
    assert pi.run("add", Scripted(answers=[NEW_CLIENT, "one", ""], hidden=[NEW_SECRET])) == 0
    assert set(pi.routing().projects) == {PROJECT, f"{NEW_CLIENT}-2"}
    assert pi.routing().projects[f"{NEW_CLIENT}-2"].label == NEW_CLIENT


def test_an_own_grip_address_that_is_the_sites_is_dropped_so_it_follows_the_site(pi, env):
    assert "api_base_url" in pi.routing_path.read_text()
    pi.run("add", Scripted(answers=[NEW_CLIENT, "one", ""], hidden=[NEW_SECRET]))
    assert "api_base_url" not in pi.routing_path.read_text()
    assert pi.routing().projects[PROJECT].api_base_url == env.fake.base_url


def test_messages_never_show_the_dataset_settings_path(pi):
    pi.routing_path.write_text("not = [valid")
    typed = Scripted()
    assert pi.run("list", typed) == 1 and str(pi.routing_path) not in typed.text
    assert "dataset settings can't be read" in typed.text


# ---- the key and the routing file change together or not at all ----


def _routing_write_fails(monkeypatch):
    from hyperwheel import datasetscmd
    real = datasetscmd.os.replace

    def failing(src, dst):
        if str(dst).endswith("routing.toml"):
            raise OSError(28, "No space left on device")
        return real(src, dst)
    monkeypatch.setattr(datasetscmd.os, "replace", failing)


def test_add_takes_the_new_key_away_again_if_the_routing_file_cant_be_saved(pi, env, monkeypatch):
    before = pi.routing_path.read_text()
    _routing_write_fails(monkeypatch)
    typed = Scripted(answers=[NEW_CLIENT, "one", ""], hidden=[NEW_SECRET])
    assert pi.run("add", typed) == 1 and "couldn't be saved" in typed.text
    assert pi.routing_path.read_text() == before and not env.keys.path(NEW_CLIENT).exists()


def test_replacing_an_older_datasets_key_checks_the_routing_file_before_the_key_is_saved(pi, env, monkeypatch):
    from hyperwheel import datasetscmd
    real_load = datasetscmd.load_routing

    def failing(path, **kw):
        if str(path).endswith(".new"):
            raise datasetscmd.ConfigError("made to fail")
        return real_load(path, **kw)
    monkeypatch.setattr(datasetscmd, "load_routing", failing)
    env.fake.clients["site-a-sandbox-client"] = "rotated-0000"
    typed = Scripted(answers=["1", "2", "site-a-sandbox-client"], hidden=["rotated-0000"])
    assert pi.run("change", typed) == 1 and "couldn't be saved" in typed.text
    assert env.keys.read(PROJECT).client_secret == OLD_SECRET and typed.hidden == ["rotated-0000"]


def test_replacing_an_older_datasets_key_puts_the_old_key_back_if_the_routing_file_cant_be_saved(pi, env,
                                                                                                monkeypatch):
    env.fake.clients["site-a-sandbox-client"] = "rotated-0000"
    _routing_write_fails(monkeypatch)
    typed = Scripted(answers=["1", "2", "site-a-sandbox-client"], hidden=["rotated-0000"])
    assert pi.run("change", typed) == 1 and "couldn't be saved" in typed.text
    assert env.keys.read(PROJECT).client_secret == OLD_SECRET
    assert pi.routing().projects[PROJECT].client_id is None


# ---- raw data on the scanner, logins changed while arriving, the service and the updater ----


def _main(pi, env, typed, **kw):
    defaults = dict(routing_path=pi.routing_path, console=typed.console(), keys=env.keys, is_root=lambda: True,
                    service_active=lambda: True, service_stop=lambda: pi.service.append("stop"),
                    service_start=lambda: pi.service.append("start"), restart_dashboard=lambda: None,
                    update_lock=env.tmp / "update" / ".lock", check_lock=env.tmp / "update" / ".check",
                    current=env.tmp / "current", clock=env.clock)
    return main(pi.site, ["remove"], **{**defaults, **kw})


def test_raw_data_still_on_the_scanner_is_said_and_needs_its_own_yes(pi, env):
    env.stage_study(n_dicom=0, raw_sizes=())
    # as the raw-data puller lists it: in the session's scanner folder, with no session until it's fetched
    env.store.add_file(kind="rrdf", name="fse_rrdf_20261027_134000.h5", state="seen", study_id=None,
                       scanner_folder="rrdf_20261027_132802")
    env.store.add_file(kind="rrdf", name="other_rrdf_20261028_090000.h5", state="seen", study_id=None,
                       scanner_folder="rrdf_20261028_090000")  # another session's: not counted
    typed = Scripted(answers=["1", PROJECT, ""])
    assert pi.run("remove", typed) == 1
    assert "1 raw file of its sessions is still on the scanner, not fetched yet." in typed.text
    assert PROJECT in pi.routing().projects
    typed = Scripted(answers=["1", PROJECT, "go on"])
    assert pi.run("remove", typed) == 0 and pi.routing().projects == {}, typed.text


def test_more_arriving_once_the_service_is_stopped_also_stops_remove(pi, env):
    env.stage_study(n_dicom=1, raw_sizes=())

    def arrives():
        env.store.add_study("1.2.12", state="receiving", operators_name="site_a/sandbox")
    typed = Scripted(answers=["1", PROJECT, "delete them"])
    assert _main(pi, env, typed, service_stop=arrives) == 1 and "More arrived" in typed.text
    assert PROJECT in pi.routing().projects


def test_hang_ups_are_ignored_while_it_clears(pi, env):
    import signal
    seen = []
    typed = Scripted(answers=["1", PROJECT])
    signals = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM, signal.SIGTSTP)
    assert _main(pi, env, typed, service_stop=lambda: seen.append([signal.getsignal(s) for s in signals])) == 0
    assert seen == [[signal.SIG_IGN] * 4] and signal.getsignal(signal.SIGINT) is not signal.SIG_IGN


def test_a_service_that_wont_start_again_is_said_plainly(pi, env):
    import subprocess

    def wont():
        raise subprocess.CalledProcessError(1, "systemctl")
    typed = Scripted(answers=["1", PROJECT])
    assert _main(pi, env, typed, service_start=wont) == 0
    assert "Hyperwheel couldn't be started again. Restart the Pi, or ask your Hyperwheel admin." in typed.text


def test_nothing_changes_while_an_update_is_being_installed(pi, env):
    import fcntl
    lock = env.tmp / "update" / ".lock"
    lock.parent.mkdir()
    before = pi.routing_path.read_text()
    with open(lock, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        typed = Scripted(answers=["1", PROJECT])
        assert _main(pi, env, typed) == 1 and "An update is being installed just now" in typed.text and "Nothing was changed." in typed.text
        assert pi.service == []  # not stopped
        typed = Scripted(answers=[NEW_CLIENT, "one", ""], hidden=[NEW_SECRET])
        assert pi.run("add", typed) == 1 and "An update is being installed" in typed.text
        assert not env.keys.path(NEW_CLIENT).exists()
        typed = Scripted(answers=["1", "1", "sandbox", "extra", ""])
        assert pi.run("change", typed) == 1 and "An update is being installed" in typed.text
        assert pi.routing_path.read_text() == before
        listed = Scripted()
        assert pi.run("list", listed) == 0  # looking is fine
    assert _main(pi, env, Scripted(answers=["1", PROJECT])) == 0


def test_the_datasets_commands_use_the_updaters_install_lock():
    from hyperwheel import datasetscmd, installer, organiser, updater
    assert datasetscmd.UPDATE_LOCK == updater.Paths().installing
    assert datasetscmd.CHECK_LOCK == updater.Paths().lock
    assert datasetscmd.CURRENT == installer.Layout().current
    assert datasetscmd.STUCK_S == organiser.STUCK_S


def test_a_session_with_nothing_new_for_an_hour_doesnt_block_remove(pi, env):
    stuck = env.store.add_study("1.2.60", state="receiving", operators_name="site_a/sandbox")
    typed = Scripted(answers=["1", PROJECT])
    assert pi.run("remove", typed) == 1 and "Images are still arriving" in typed.text
    env.store._db.execute("UPDATE studies SET updated_at = updated_at - 7200 WHERE id = ?", (stuck.id,))
    assert pi.run("remove", Scripted(answers=["1", PROJECT])) == 0


def test_a_session_whose_files_are_all_on_grip_is_kept_as_a_record_not_called_unuploaded(pi, env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    f = env.store.files(study.id)[0]
    env.store.move_file(f.id, "sending")
    env.store.move_file(f.id, "confirmed")
    env.store.move_study(study.id, "uploading")
    typed = Scripted(answers=["1", PROJECT])
    assert pi.run("remove", typed) == 0 and "delete them" not in "".join(typed.prompts), typed.text
    assert env.store.study(study.id).state == "unrouted" and not os.path.exists(f.path)  # on GRIP: cleared


def test_a_session_on_grip_of_a_removed_dataset_goes_through_when_sent_again_after_it_is_added_back(pi, env):
    from hyperwheel.organiser import Organiser
    from conftest import make_dicom
    from pydicom.uid import generate_uid
    done = env.store.add_study("1.2.40", state="uploaded", project=PROJECT, stable_at=env.clock(),
                               operators_name="site_a/sandbox")
    assert pi.run("remove", Scripted(answers=["1", PROJECT])) == 0
    assert env.store.study(done.id).state == "archived" and env.store.study(done.id).project is None
    env.fake.clients["site-a-sandbox-client"] = OLD_SECRET
    assert pi.run("add", Scripted(answers=["site-a-sandbox-client", "sandbox", ""], hidden=[OLD_SECRET])) == 0
    series = generate_uid()
    for i in range(2):
        _, data = make_dicom("1.2.40", series, acq=f"13341{i}")
        env.orthanc.receive(data)
    env.orthanc.stable("1.2.40")
    Organiser(env.store, env.site, pi.routing(), env.orthanc, clock=env.clock).follow()
    again = env.store.study(done.id)
    assert again.project == "site-a-sandbox-client" and again.state == "ready"


def test_dropping_a_login_waits_while_its_images_arrive(pi, env):
    pi.run("add", Scripted(answers=[NEW_CLIENT, "one", "two", ""], hidden=[NEW_SECRET]))
    env.store.add_study("1.2.13", state="receiving", operators_name="site_b/two")
    typed = Scripted(answers=[NEW_CLIENT, "1", "one", ""])
    assert pi.run("change", typed) == 1 and "Images are still arriving from the scanner for two." in typed.text
    assert pi.routing().projects[NEW_CLIENT].logins == ("one", "two")
    typed = Scripted(answers=[NEW_CLIENT, "1", "two", ""])  # dropping "one": what arrives from "two" doesn't matter
    assert pi.run("change", typed) == 0 and "New scans from one are deleted on this Pi" in typed.text


def test_every_session_of_a_removed_dataset_loses_its_name_so_it_goes_through_once_added_back(pi, env):
    from conftest import make_dicom
    from pydicom.uid import generate_uid
    from hyperwheel.organiser import Organiser
    old = env.store.add_study("1.2.41", state="archived", project=PROJECT, stable_at=env.clock(),
                              operators_name="site_a/sandbox")  # past the watch period before the removal
    gone = env.store.add_study("1.2.42", state="purged", project=PROJECT, stable_at=env.clock())
    assert pi.run("remove", Scripted(answers=["1", PROJECT])) == 0
    assert env.store.study(old.id).project is None and env.store.study(gone.id).project is None
    env.fake.clients["site-a-sandbox-client"] = OLD_SECRET
    assert pi.run("add", Scripted(answers=["site-a-sandbox-client", "sandbox", ""], hidden=[OLD_SECRET])) == 0
    _, data = make_dicom("1.2.41", generate_uid(), acq="133410")
    env.orthanc.receive(data)
    env.orthanc.stable("1.2.41")
    Organiser(env.store, env.site, pi.routing(), env.orthanc, clock=env.clock).follow()
    again = env.store.study(old.id)
    assert again.state == "ready" and again.project == "site-a-sandbox-client"


def test_a_purged_session_being_sent_again_from_its_login_counts_as_arriving(pi, env):
    from conftest import make_dicom
    from pydicom.uid import generate_uid
    from hyperwheel.organiser import Organiser
    env.store.add_study("1.2.43", state="purged", stable_at=env.clock())  # its details were wiped
    _, data = make_dicom("1.2.43", generate_uid(), acq="133410")
    env.orthanc.receive(data)
    Organiser(env.store, env.site, pi.routing(), env.orthanc, clock=env.clock).follow()
    typed = Scripted(answers=["1", PROJECT])
    assert pi.run("remove", typed) == 1 and "Images are still arriving" in typed.text


def test_a_linked_dataset_folder_is_never_followed(pi, env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    real = env.site.staging_dir / PROJECT
    elsewhere = env.tmp / "elsewhere"
    real.rename(elsewhere)
    real.symlink_to(elsewhere)  # stagingArea/<dataset> now points somewhere else
    kept = list(elsewhere.rglob("*.dcm"))
    assert kept and pi.run("remove", Scripted(answers=["1", PROJECT, "delete them"])) == 0
    assert all(p.exists() for p in kept) and env.store.files(study.id) == []


def test_a_shown_name_that_is_a_number_picks_that_dataset_not_that_position(pi, env):
    env.fake.clients["2"] = NEW_SECRET
    assert pi.run("add", Scripted(answers=["2", "one", ""], hidden=[NEW_SECRET])) == 0, "added"
    typed = Scripted(answers=["2", "2"])  # dataset "2" is the second in the list, and also named 2
    env.fake.clients["1"] = NEW_SECRET
    pi.run("add", Scripted(answers=["1", "two", ""], hidden=[NEW_SECRET]))
    typed = Scripted(answers=["1", "1"])  # "1" is the third dataset's name, not the first position
    assert pi.run("remove", typed) == 0 and "Removed 1." in typed.text
    assert PROJECT in pi.routing().projects


def test_an_update_check_running_just_after_a_release_switch_counts_as_installing(pi, env):
    import fcntl
    check = env.tmp / "update" / ".check"
    check.parent.mkdir()
    check.write_text("")
    (env.tmp / "release").mkdir()
    (env.tmp / "current").symlink_to(env.tmp / "release")  # switched just now
    with open(check, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)  # the earlier release's updater, still checking what it installed
        typed = Scripted(answers=["1", PROJECT])
        assert _main(pi, env, typed) == 1 and "An update is being installed just now" in typed.text
        old = __import__("time").time() - 3600
        os.utime(env.tmp / "current", (old, old), follow_symlinks=False)  # an hour ago: it's waiting, not installing
        assert _main(pi, env, Scripted(answers=["1", PROJECT])) == 0


def test_saving_a_new_dataset_cant_be_interrupted_part_way(pi, env, monkeypatch):
    import signal
    seen = []
    real = env.keys.write
    monkeypatch.setattr(env.keys, "write", lambda *a: seen.append(signal.getsignal(signal.SIGINT)) or real(*a))
    assert pi.run("add", Scripted(answers=[NEW_CLIENT, "one", ""], hidden=[NEW_SECRET])) == 0
    assert seen == [signal.SIG_IGN] and signal.getsignal(signal.SIGINT) is not signal.SIG_IGN


def test_a_held_session_with_everything_staged_on_grip_still_needs_delete_them(pi, env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    f = env.store.files(study.id)[0]
    env.store.move_file(f.id, "sending")
    env.store.move_file(f.id, "confirmed")
    env.store.move_study(study.id, "uploading")
    env.store.move_study(study.id, "uploaded")
    env.store.move_study(study.id, "receiving")
    env.store.move_study(study.id, "stable")
    env.store.move_study(study.id, "checking")
    env.store.move_study(study.id, "held")  # images from another login came in, never staged
    typed = Scripted(answers=["1", PROJECT, ""])
    assert pi.run("remove", typed) == 1
    assert "(0 files on this Pi not uploaded, and some held or still receiving" in typed.text
    assert PROJECT in pi.routing().projects and env.store.study(study.id).state == "held"


def test_unassigned_raw_files_of_its_sessions_are_said_and_kept(pi, env):
    env.store.add_study("1.2.50", state="uploaded", project=PROJECT, study_date="20261027", study_time="132802",
                        stable_at=env.clock())
    kept = env.store.add_file(kind="rrdf", name="x_rrdf_20261027_134000.h5", state="unassigned", study_id=None,
                              scanner_folder="rrdf_20261027_132802")
    typed = Scripted(answers=["1", PROJECT])
    assert pi.run("remove", typed) == 0
    assert "1 raw file of its sessions is on this Pi, held because the Pi couldn't tell which session" in typed.text
    assert env.store.file(kept.id).state == "unassigned"


def test_a_session_back_to_receiving_long_ago_still_needs_delete_them(pi, env):
    study = env.stage_study(n_dicom=1, raw_sizes=())
    f = env.store.files(study.id)[0]
    env.store.move_file(f.id, "sending")
    env.store.move_file(f.id, "confirmed")
    env.store.move_study(study.id, "uploading")
    env.store.move_study(study.id, "uploaded")
    env.store.move_study(study.id, "receiving")  # an image came, and Orthanc never said it was complete
    env.store._db.execute("UPDATE studies SET updated_at = updated_at - 7200 WHERE id = ?", (study.id,))
    typed = Scripted(answers=["1", PROJECT, ""])
    assert pi.run("remove", typed) == 1 and "some held or still receiving" in typed.text
    assert PROJECT in pi.routing().projects


def test_remove_refuses_while_hyperwheel_isnt_running(pi, env):
    typed = Scripted(answers=["1", PROJECT])
    assert pi.run("remove", typed, active=False) == 1
    assert "Hyperwheel isn't running, so this Pi may have scans it hasn't looked at yet." in typed.text
    assert PROJECT in pi.routing().projects and typed.prompts == ["Which dataset do you want to remove? Type its "
                                                                  "number (or press Enter to stop): "]


def test_the_key_goes_only_after_the_routing_entry(pi, env, monkeypatch):
    from hyperwheel import datasetscmd
    real = datasetscmd.os.replace

    def failing(src, dst):
        if str(dst).endswith("routing.toml"):
            raise OSError(28, "No space left on device")
        return real(src, dst)
    monkeypatch.setattr(datasetscmd.os, "replace", failing)
    typed = Scripted(answers=["1", PROJECT])
    assert pi.run("remove", typed) == 1 and "To finish, type: hyperwheel datasets remove" in typed.text
    assert env.keys.path(PROJECT).exists() and PROJECT in pi.routing().projects


def test_changing_logins_cant_be_interrupted_part_way(pi, env, monkeypatch):
    import signal
    from hyperwheel import datasetscmd
    seen, real = [], datasetscmd.Datasets._write

    def probe(self, projects, **kw):
        seen.append(signal.getsignal(signal.SIGINT))
        return real(self, projects, **kw)
    monkeypatch.setattr(datasetscmd.Datasets, "_write", probe)
    assert pi.run("change", Scripted(answers=["1", "1", "sandbox", "extra", ""])) == 0
    assert seen == [signal.SIG_IGN]


def test_raw_data_not_checked_on_the_scanner_yet_needs_go_on(pi, env):
    pi.site = replace(pi.site, rrdf=replace(pi.site.rrdf, enabled=True))
    env.store.add_study("1.2.80", state="uploaded", project=PROJECT, stable_at=env.clock())  # raw never checked
    typed = Scripted(answers=["1", PROJECT, ""])
    assert pi.run("remove", typed) == 1
    assert "The Pi hasn't been able to check the scanner for raw data of 1 of its sessions yet." in typed.text
    checked = env.store.add_study("1.2.81", state="uploaded", project=PROJECT, stable_at=env.clock(),
                                  raw_checked_at=env.clock())
    env.store.update_study(env.store.study_by_uid("1.2.80").id, raw_checked_at=env.clock())
    assert pi.run("remove", Scripted(answers=["1", PROJECT])) == 0, "all checked: nothing to ask"
    assert env.store.study(checked.id).state == "archived"


def test_a_failing_raw_data_fetch_also_needs_go_on(pi, env):
    pi.site = replace(pi.site, rrdf=replace(pi.site.rrdf, enabled=True))
    env.store.add_study("1.2.82", state="uploaded", project=PROJECT, stable_at=env.clock(), raw_checked_at=env.clock())
    env.store.raise_flag("RAW_LOGIN_REFUSED", "The scanner refused the raw-data login.", level="alert")
    typed = Scripted(answers=["1", PROJECT, ""])
    assert pi.run("remove", typed) == 1 and "check the scanner for raw data of 1" in typed.text
