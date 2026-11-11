"""``hyperwheel grip address``: GRIP's address on a Pi that is set up, checked with every key before it's saved."""

from __future__ import annotations

import os
import stat
from dataclasses import replace

import pytest
from conftest import PROJECT
from fakegrip import FakeGrip
from test_datasetscmd import OLD_SECRET, Scripted

from hyperwheel.config import GripSettings, load_routing, load_site
from hyperwheel.datasetscmd import check_with_grip
from hyperwheel.gripcmd import answers_like_grip, main, with_address
from hyperwheel.keys import Credentials
from hyperwheel.sitesetup import render_routing_toml

OTHER = "site-a-other"


def site_toml(grip: GripSettings, tmp) -> str:
    return f"""\
# Site settings: a comment that stays
schema_version = 1
site = "test"
staging_dir = "{tmp / 'stagingArea'}"
state_db = "{tmp / 'state.db'}"
keys_dir = "{tmp / 'keys'}"
test_mode = true

[upload]
timeout_s = 5

[grip]
# GRIP's address for this site's region.
api_base_url = "{grip.api_base_url}"
token_url = "{grip.token_url}"   # trailing words go too

[hotspot]
routing = true
"""


class Pi:
    """The env's Pi with its site.toml and routing file, and GRIP's new address (a second fake GRIP)."""

    def __init__(self, env, new):
        self.env, self.new = env, new
        self.site_path = env.tmp / "site.toml"
        self.site_path.write_text(site_toml(env.site.grip, env.tmp))
        os.chmod(self.site_path, 0o644)
        self.site = load_site(self.site_path)
        self.routing_path = env.tmp / "routing.toml"
        # A dataset from before GRIP's address was site-wide: it names the site's address itself.
        self.routing_path.write_text(render_routing_toml({PROJECT: {
            "logins": ["sandbox"], "api_base_url": env.fake.base_url, "token_url": env.fake.token_url}}))
        self.restarts = []
        self.answered = []

    def run(self, typed, *, root=True, answers=None):
        def answer(url, timeout):
            self.answered.append(url)
            return answers_like_grip(url, timeout) if answers is None else answers
        return main(self.site, ["address"], site_path=self.site_path, routing_path=self.routing_path,
                    answers=answer, console=typed.console(), keys=self.env.keys, check=check_with_grip,
                    restart=lambda: self.restarts.append(1) or "Hyperwheel was restarted.", is_root=lambda: root,
                    update_lock=self.env.tmp / "update" / ".lock", check_lock=self.env.tmp / "update" / ".check",
                    current=self.env.tmp / "current", clock=self.env.clock)

    def files(self):
        return self.site_path.read_bytes(), self.routing_path.read_bytes()


@pytest.fixture
def new():
    server = FakeGrip(clients={"site-a-sandbox-client": OLD_SECRET}).start()
    yield server
    server.stop()


@pytest.fixture
def pi(env, new):
    return Pi(env, new)


def test_the_new_address_is_saved_only_after_grip_there_gave_every_dataset_a_token(pi, env, new):
    before = pi.site_path.read_text()
    typed = Scripted(answers=[new.base_url + "/", ""])
    assert pi.run(typed) == 0, typed.text
    assert new.token_requests == 1  # the dataset's own key, at the new address
    assert not pi.answered  # a key was checked, so no made-up one is needed

    after = pi.site_path.read_text()
    site = load_site(pi.site_path)
    assert site.grip == GripSettings(new.base_url, new.token_url)
    assert replace(site, grip=pi.site.grip) == pi.site  # nothing else changed
    changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert changed == [(f'api_base_url = "{env.fake.base_url}"', f'api_base_url = "{new.base_url}"'),
                       (f'token_url = "{env.fake.token_url}"   # trailing words go too',
                        f'token_url = "{new.token_url}"')]
    assert "# GRIP's address for this site's region." in after and stat.S_IMODE(pi.site_path.stat().st_mode) == 0o644
    # The dataset that named the old site address follows the new one.
    project = load_routing(pi.routing_path, test_mode=True, grip=site.grip).projects[PROJECT]
    assert (project.api_base_url, project.token_url) == (new.base_url, new.token_url)
    assert pi.restarts == [1]
    assert f"now {new.base_url}" in typed.text
    assert OLD_SECRET not in typed.text and not list(env.tmp.glob(".*.new"))


def test_a_token_address_of_its_own_can_be_given(pi, env, new):
    typed = Scripted(answers=[new.base_url, new.base_url + "/get/token"])
    assert pi.run(typed) == 0, typed.text
    assert load_site(pi.site_path).grip.token_url == new.token_url


def test_a_key_the_new_address_refuses_changes_nothing(pi, env, new):
    new.clients["site-a-sandbox-client"] = "another-secret"
    before = pi.files()
    typed = Scripted(answers=[new.base_url, ""])
    assert pi.run(typed) == 1
    assert pi.files() == before and not pi.restarts
    assert "refused" in typed.text and "Nothing was changed" in typed.text and "hyperwheel datasets change" in typed.text


def test_an_address_that_doesnt_answer_changes_nothing(pi, env, new):
    url = new.base_url
    new.stop()
    before = pi.files()
    typed = Scripted(answers=[url, ""])
    assert pi.run(typed) == 1
    assert pi.files() == before and not pi.restarts
    assert "couldn't be reached" in typed.text


@pytest.mark.parametrize("typed_url", ["grip.example.org/api", "ftp://grip.example.org/api",
                                       "http://grip.example.org/api"])
def test_only_an_https_address_is_taken(pi, typed_url):
    before = pi.files()
    typed = Scripted(answers=[typed_url])
    assert pi.run(typed) == 1
    assert pi.files() == before and "isn't an address starting https://" in typed.text


def test_enter_or_the_same_address_keeps_it(pi, env):
    before = pi.files()
    for answers in ([""], [env.fake.base_url, ""]):
        typed = Scripted(answers=answers)
        assert pi.run(typed) == 0
        assert pi.files() == before and not pi.restarts
        assert "Nothing was changed" in typed.text
    assert env.fake.base_url in typed.text  # the address in use is shown first


def test_a_dataset_with_an_address_of_its_own_keeps_it_and_isnt_checked(pi, env, new):
    elsewhere = FakeGrip(clients={"other": "other-secret"}).start()
    try:
        pi.routing_path.write_text(render_routing_toml({
            PROJECT: {"logins": ["sandbox"]},
            OTHER: {"logins": ["other"], "api_base_url": elsewhere.base_url, "token_url": elsewhere.token_url}}))
        env.keys.write(OTHER, Credentials("other", "other-secret"))
        typed = Scripted(answers=[new.base_url, ""])
        assert pi.run(typed) == 0, typed.text
        assert elsewhere.token_requests == 0 and new.token_requests == 1
        routing = load_routing(pi.routing_path, test_mode=True, grip=load_site(pi.site_path).grip)
        assert routing.projects[OTHER].api_base_url == elsewhere.base_url
        assert routing.projects[PROJECT].api_base_url == new.base_url
        assert "keeps it" in typed.text
    finally:
        elsewhere.stop()


def test_with_no_key_to_check_the_new_address_must_answer_like_grip(pi, env, new):
    pi.routing_path.write_text(render_routing_toml({}))
    before = pi.files()
    typed = Scripted(answers=[new.base_url, ""])
    assert pi.run(typed, answers=False) == 1
    assert pi.files() == before and "doesn't answer like GRIP" in typed.text
    typed = Scripted(answers=[new.base_url, ""])
    assert pi.run(typed) == 0, typed.text
    assert pi.answered[-1] == new.token_url and load_site(pi.site_path).grip.api_base_url == new.base_url


def test_a_dataset_with_no_key_is_said_and_the_rest_are_checked(pi, env, new):
    env.keys.path(PROJECT).unlink()
    typed = Scripted(answers=[new.base_url, ""])
    assert pi.run(typed) == 0, typed.text
    assert "has no key stored" in typed.text and pi.answered == [new.token_url]


def test_answers_like_grip_only_for_a_token_address_that_refuses_a_made_up_key(new):
    assert answers_like_grip(new.token_url, 5)
    url = new.token_url
    new.stop()
    assert not answers_like_grip(url, 2)


def test_a_grip_section_not_written_the_usual_way_is_left_alone():
    grip = GripSettings("https://grip.example.org/api", "https://grip.example.org/api/get/token")
    from hyperwheel.datasetscmd import Stop
    for text in ('schema_version = 1\ngrip = { api_base_url = "x", token_url = "y" }\n',
                 '[grip]\napi_base_url = "x"\n',
                 '[grip]\napi_base_url = "x"\ntoken_url = "y"\n[grip]\napi_base_url = "x"\n'):
        with pytest.raises(Stop):
            with_address(text, grip)
    assert with_address('[grip]\r\napi_base_url = "x"\r\ntoken_url = "y"\r\n', grip) == (
        f'[grip]\r\napi_base_url = "{grip.api_base_url}"\r\ntoken_url = "{grip.token_url}"\r\n')


def test_only_root_runs_it(pi):
    typed = Scripted(answers=["https://grip.example.org/api"])
    assert pi.run(typed, root=False) == 2 and "administrator rights" in typed.text
