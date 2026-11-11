from pathlib import Path

import pytest

from hyperwheel.config import GRIP_API_BASE_URL, GRIP_TOKEN_URL, ConfigError, GripSettings, load_routing, load_site

EXAMPLES = Path(__file__).parent.parent / "config" / "examples"


def write(tmp_path, text, name="f.toml"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_examples_load():
    site = load_site(EXAMPLES / "site.toml")
    assert site.upload.chunk_bytes == 8 * 1024 * 1024
    assert site.phantom_patient_id is None and site.test_mode is False
    assert site.grip == GripSettings(GRIP_API_BASE_URL, GRIP_TOKEN_URL)  # the release's GRIP address
    routing = load_routing(EXAMPLES / "routing.toml", grip=site.grip)
    dataset = routing.project_for_login("example")
    assert dataset.name == "site-a-study-one" and dataset.label == "site-a-study-one"
    assert dataset.api_base_url == GRIP_API_BASE_URL and dataset.token_url == GRIP_TOKEN_URL
    assert routing.project_for_login("clinical") is None


def test_site_settings(tmp_path):
    site = load_site(write(tmp_path, """
schema_version = 1
site = "site-b"
staging_dir = "/s"
state_db = "/d"
keys_dir = "/k"
phantom_patient_id = "PH-0001"
[upload]
timeout_s = 30
"""))
    assert site.phantom_patient_id == "PH-0001"
    assert site.upload.timeout_s == 30.0
    assert site.updates.channel == "stable"  # every Pi but the dev Pi and the pilot site
    assert site.updates.url == "https://github.com/UNITY-Physics/hyperwheel-grip/releases/download"


@pytest.mark.parametrize("text,message", [
    ('schema_version = 2\nsite="a"\nstaging_dir="/s"\nstate_db="/d"\nkeys_dir="/k"', "schema_version"),
    ('schema_version = 1\nstaging_dir="/s"\nstate_db="/d"\nkeys_dir="/k"', "'site' is required"),
    ('schema_version = 1\nsite="a"\nstaging_dir="/s"\nstate_db="/d"\nkeys_dir="/k"\nstaging="/x"', "unknown"),
    ('schema_version = 1\nsite="a"\nstaging_dir="/s"\nstate_db="/d"\nkeys_dir="/k"\n[upload]\nchunk_bytes = 70000000',
     "chunk_bytes"),
    ('schema_version = 1\nsite="a"\nstaging_dir="/s"\nstate_db="/d"\nkeys_dir="/k"\n[upload]\ntimeout_s = "60"',
     "expected float"),
    ('schema_version = 1\nsite="a"\nstaging_dir="/s"\nstate_db="/d"\nkeys_dir="/k"\n[rrdf]\nmargin = 60', "unknown"),
    ('schema_version = 1\nsite="a"\nstaging_dir="/s"\nstate_db="/d"\nkeys_dir="/k"\nphantom_patient_id = " "',
     "phantom"),
    ('schema_version = 1\nsite="a"\nstaging_dir="/s"\nstate_db="/d"\nkeys_dir="/k"\n[updates]\nchannel = "beta"',
     "channel must be dev, pilot or stable"),
    ('schema_version = 1\nsite="a"\nstaging_dir="/s"\nstate_db="/d"\nkeys_dir="/k"\n[updates]\n'
     'url = "http://releases.example"', r"\[updates\].url: must be https"),
])
def test_bad_site_settings(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_site(write(tmp_path, text))


ROUTING = """
schema_version = 1
[projects.StudyX-SiteB]
logins = ["studyx"]
api_base_url = "{api}"
token_url = "{token}"
"""


def test_routing_requires_https_unless_local_test_mode(tmp_path):
    good = ROUTING.format(api="https://grip.example.org/api", token="https://grip.example.org/api/get/token")
    assert load_routing(write(tmp_path, good)).projects["StudyX-SiteB"].logins == ("studyx",)

    local = ROUTING.format(api="http://127.0.0.1:8080/api", token="http://127.0.0.1:8080/api/get/token")
    with pytest.raises(ConfigError, match="https"):
        load_routing(write(tmp_path, local))
    assert load_routing(write(tmp_path, local), test_mode=True)

    remote_http = ROUTING.format(api="http://grip.example.org/api", token="https://grip.example.org/t")
    with pytest.raises(ConfigError, match="https"):
        load_routing(write(tmp_path, remote_http), test_mode=True)


@pytest.mark.parametrize("text,message", [
    ('schema_version = 1\n[projects."a/b"]\nlogins=["x"]\napi_base_url="https://h"\ntoken_url="https://h"', "names"),
    ('schema_version = 1\n[projects.A]\nlogins=["X"]\napi_base_url="https://h"\ntoken_url="https://h"', "lower case"),
    ('schema_version = 1\n[projects.A]\nlogins=[]\napi_base_url="https://h"\ntoken_url="https://h"', "at least one"),
    ('schema_version = 1\n[projects.A]\nlogins=["x"]\napi_base_url="https://h"\ntoken_url="https://h"\n'
     '[projects.B]\nlogins=["x"]\napi_base_url="https://h"\ntoken_url="https://h"', "already routed"),
    ('schema_version = 1\n[projects.A]\nlogins=["x"]\napi_base_url="https://h"\ntoken_url="https://h"\nkey="s"',
     "unknown"),
    ('schema_version = 1\n[projects.A]\nlogins=["x"]\napi_base_url="https://h"', "both api_base_url and token_url"),
    ('schema_version = 1\n[projects.A]\nclient_id="c"\nlogins=["x"]\n[projects.B]\nclient_id="c"\nlogins=["y"]',
     "listed twice"),
    ('schema_version = 1\n[projects.A]\nclient_id="two words"\nlogins=["x"]', "client ID"),
])
def test_bad_routing(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_routing(write(tmp_path, text))


def test_a_dataset_uses_the_sites_grip_address_unless_it_has_its_own(tmp_path):
    text = ('schema_version = 1\n[projects.a]\nclient_id="a"\nlogins=["x"]\n'
            '[projects.B]\nlogins=["y"]\napi_base_url="https://own.example.org/api"\n'
            'token_url="https://own.example.org/api/get/token"\n')
    site_grip = GripSettings("https://grip.example.org/api", "https://grip.example.org/api/get/token")
    routing = load_routing(write(tmp_path, text), grip=site_grip)
    assert routing.projects["a"].api_base_url == "https://grip.example.org/api"
    assert routing.projects["a"].token_url == "https://grip.example.org/api/get/token"
    assert routing.projects["B"].api_base_url == "https://own.example.org/api"
    assert load_routing(write(tmp_path, text)).projects["a"].api_base_url == GRIP_API_BASE_URL  # the default
    assert routing.label("a") == "a" and routing.label("gone") == "gone" and routing.label(None) == ""


def test_a_site_may_override_grips_address_with_both_settings(tmp_path):
    base = ('schema_version = 1\nsite = "s"\nstaging_dir = "/s"\nstate_db = "/s.db"\nkeys_dir = "/k"\n')
    site = load_site(write(tmp_path, base + '[grip]\napi_base_url = "https://g.example.org/api"\n'
                                            'token_url = "https://g.example.org/token"\n'))
    assert site.grip == GripSettings("https://g.example.org/api", "https://g.example.org/token")
    with pytest.raises(ConfigError, match="give both"):
        load_site(write(tmp_path, base + '[grip]\napi_base_url = "https://g.example.org/api"\n'))
    with pytest.raises(ConfigError, match="https"):
        load_site(write(tmp_path, base + '[grip]\napi_base_url = "http://g.example.org/api"\n'
                                         'token_url = "https://g.example.org/token"\n'))
    with pytest.raises(ConfigError, match="unknown"):
        load_site(write(tmp_path, base + '[grip]\nurl = "https://g.example.org"\n'))
    assert load_site(write(tmp_path, base)).grip == GripSettings()


def test_an_old_margin_minutes_setting_is_ignored(tmp_path):
    base = 'schema_version = 1\nsite = "s"\nstaging_dir = "/s"\nstate_db = "/s.db"\nkeys_dir = "/k"\n'
    site = load_site(write(tmp_path, base + "[rrdf]\nenabled = true\nmargin_minutes = 60\nwatch_days = 7\n"))
    assert site.rrdf.enabled and site.rrdf.watch_days == 7 and not hasattr(site.rrdf, "margin_minutes")
    with pytest.raises(ConfigError, match="unknown"):
        load_site(write(tmp_path, base + "[rrdf]\nmargin_minuets = 60\n"))  # a real typo is still an error
