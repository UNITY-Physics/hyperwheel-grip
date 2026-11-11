"""``hyperwheel setup`` on a scratch Pi: the system's files under a folder, its commands faked."""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path

import pytest
from test_datasetscmd import Scripted

from hyperwheel import passwords, reach
from hyperwheel.adminpw import AdminError
from hyperwheel.config import GripSettings, load_routing, load_site
from hyperwheel.installer import Layout, sudo_rule
from hyperwheel.rawpull import LoginRefused
from hyperwheel.sitesetup import (BOOT_HOTSPOT, CABLE_FILE, CLEAR_SCREEN, HOTSPOT_FILE, NM_DIR, Accounts, Setup,
                                  SetupError, hostname_for_site, parse_site_file)

TEMPLATE = Path(__file__).resolve().parents[1] / "config" / "examples" / "site-file.toml"
EXAMPLE_GRIP = GripSettings("https://grip.example.org/api", "https://grip.example.org/api/get/token")  # made up
HOTSPOT_PW = "hotspot-pw-not-for-logs"
SCANNER_PW = "scanner-pw-not-for-logs"
DM_PW = "borak-kefim-tumos"
NOPASSWD = ("# Raspberry Pi OS: the first user runs anything as root without a password\n"
            "firstuser ALL=(ALL) NOPASSWD: ALL\n")


class FakePi:
    """The account files and the commands setup runs, on files under ``root``."""

    def __init__(self, root: Path, *, other_admin: bool = True, wifi: str = "wlan0:disconnected:"):
        self.root, self.ran, self.chpasswd, self.wifi = root, [], [], wifi
        etc = root / "etc"
        (etc / "hyperwheel").mkdir(parents=True)
        (etc / "sudoers.d").mkdir()
        first = "firstuser" if other_admin else "datamanager"
        (etc / "passwd").write_text(f"root:x:0:0:root:/root:/bin/bash\n{first}:x:1000:1000::/home/{first}:/bin/bash\n")
        (etc / "group").write_text(f"root:x:0:\nsudo:x:27:{first}\n{first}:x:1000:\n")
        (etc / "shadow").write_text(f"root:*:20000::::::\n{first}:$y$j9T$firstuserhash:20000::::::\n")
        (etc / "sudoers.d" / "010_pi-nopasswd").write_text(NOPASSWD.replace("firstuser", first))
        (etc / "sudoers.d" / "hyperwheel").write_text(sudo_rule(False))
        zi = root / "usr/share/zoneinfo"
        (zi / "Europe").mkdir(parents=True)
        (zi / "Europe" / "London").write_text("TZif")
        (zi / "Africa").mkdir()
        (zi / "Africa" / "Nairobi").write_text("TZif")
        (zi / "iso3166.tab").write_text("# ISO 3166\nGB\tBritain (UK)\nKE\tKenya\n")
        copy = root / BOOT_HOTSPOT.lstrip("/")
        copy.parent.mkdir(parents=True)
        copy.write_text(HOTSPOT_PW + "\n")

    def _edit(self, name, fn):
        path = self.root / "etc" / name
        path.write_text("".join(fn(line) + "\n" for line in path.read_text().splitlines()))

    def run(self, cmd, input=None):
        self.ran.append(list(cmd))
        etc = self.root / "etc"
        if cmd[0] == "install":
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
        if cmd[0] == "useradd":
            name, groups = cmd[-1], (cmd[cmd.index("--groups") + 1].split(",") if "--groups" in cmd else [])
            uid = 1001 + len((etc / "passwd").read_text().splitlines())
            with open(etc / "passwd", "a") as f:
                f.write(f"{name}:x:{uid}:{uid}::/home/{name}:/bin/bash\n")
            with open(etc / "group", "a") as f:
                f.write(f"{name}:x:{uid}:\n")
            with open(etc / "shadow", "a") as f:
                f.write(f"{name}:!:20000::::::\n")
            for g in groups:
                self._add(g, name)
        elif cmd[0] == "usermod":
            self._add(cmd[cmd.index("--groups") + 1], cmd[-1])
        elif cmd[0] == "gpasswd":
            user, group = cmd[2], cmd[3]
            self._edit("group", lambda line: line if not line.startswith(group + ":") else
                       ":".join(line.split(":")[:3] + [",".join(m for m in line.split(":")[3].split(",")
                                                               if m and m != user)]))
        elif cmd[0] == "chpasswd":
            user, password = input.rstrip("\n").split(":", 1)
            self.chpasswd.append((user, password))
            self._edit("shadow", lambda line: line if not line.startswith(user + ":") else
                       f"{user}:$y$j9T$newhash:" + ":".join(line.split(":")[2:]))
        elif cmd[:2] == ["nmcli", "-t"]:
            return self.wifi + "\n"
        return ""

    def _add(self, group, user):
        def add(line):
            parts = line.split(":")
            if parts[0] != group:
                return line
            members = [m for m in parts[3].split(",") if m]
            return ":".join(parts[:3] + [",".join([*members, user])])
        self._edit("group", add)

    def ran_any(self, *words):
        return [c for c in self.ran if c[:len(words)] == list(words)]


@pytest.fixture
def pi(tmp_path):
    return FakePi(tmp_path / "pi")


# The example site file with two datasets in it: one with an address of its own (as on Pis set up before GRIP's
# address was site-wide), one using the site's.
WITH_DATASETS = TEMPLATE.read_text().replace("# end of site file", """[projects.SiteA-StudyOne]
logins = ["study_one"]
api_base_url = "https://own.example.org/api"
token_url = "https://own.example.org/api/get/token"

[projects.SiteA-Sandbox]
client_id = "site-a-sandbox"
logins = ["sandbox", "phantom"]

# end of site file""")


def template_lines():
    return WITH_DATASETS.splitlines()


def no_admin_file(site):
    raise AdminError("couldn't download admin.json (404)")


def make(pi, typed, *, scanner=reach.SCANNER_OK, try_password=None, new_password=lambda: DM_PW,
         admin_password=no_admin_file):
    tried = []

    def default_try(site, password):
        tried.append(password)
        return 3
    setup = Setup(layout=Layout(pi.root), console=typed.console(), run=pi.run,
                  scanner_state=lambda site: scanner, try_password=try_password or default_try,
                  unreachable=lambda site: "the scanner cable has no link", new_password=new_password,
                  chown=lambda path: None, clock=lambda: 1_800_000_000.0, admin_password=admin_password)
    return setup, tried


def every_file_text(root: Path) -> str:
    return "\n".join(p.read_text(errors="replace") for p in root.rglob("*") if p.is_file())


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ---- the whole setup --------------------------------------------------------------------------------


def test_a_fresh_pi_is_set_up_from_the_pasted_site_file(pi):
    typed = Scripted(answers=[*template_lines(), "yes", DM_PW, "no"], hidden=[SCANNER_PW])
    setup, tried = make(pi, typed)
    assert setup.main() == 0, typed.text
    etc = pi.root / "etc/hyperwheel"

    site = load_site(etc / "site.toml")
    routing = load_routing(etc / "routing.toml", grip=EXAMPLE_GRIP)
    assert (site.site, site.time_zone, site.wifi_country, site.updates.channel) == \
        ("site-a", "Europe/London", "GB", "stable")
    assert site.scanner.host == "10.42.0.1"
    assert routing.project_for_login("study_one").name == "SiteA-StudyOne"
    assert routing.project_for_login("phantom").name == "SiteA-Sandbox"

    assert pi.ran_any("raspi-config", "nonint", "do_hostname", "hyperwheel-site-a")
    assert pi.ran_any("timedatectl", "set-timezone", "Europe/London")
    assert pi.ran_any("raspi-config", "nonint", "do_wifi_country", "GB")

    nm = pi.root / NM_DIR.lstrip("/")
    hotspot = (nm / HOTSPOT_FILE).read_text()
    assert "ssid=Hyperwheel\n" in hotspot and "channel=6\n" in hotspot and "band=bg\n" in hotspot
    assert f"psk={HOTSPOT_PW}\n" in hotspot and "address1=192.168.99.1/24\n" in hotspot
    assert "address1=10.42.0.88/24,10.42.0.1\n" in (nm / CABLE_FILE).read_text()
    assert mode(nm / HOTSPOT_FILE) == 0o600 and mode(nm / CABLE_FILE) == 0o600
    assert json.loads((etc / "hotspot.json").read_text()) == {"ssid": "Hyperwheel", "password": HOTSPOT_PW}
    assert mode(etc / "hotspot.json") == 0o600
    assert not (pi.root / BOOT_HOTSPOT.lstrip("/")).exists()
    assert pi.ran_any("nmcli", "connection", "up", "Hyperwheel")

    assert tried == [SCANNER_PW]
    assert json.loads((etc / "scanner.json").read_text()) == {"password": SCANNER_PW}

    assert pi.chpasswd == [("datamanager", DM_PW)]
    accounts = Accounts.read(Layout(pi.root))
    assert accounts.exists("datamanager") and not accounts.in_group("datamanager", "sudo")
    assert accounts.exists("admin") and accounts.in_group("admin", "sudo") and not accounts.has_password("admin")

    assert not (pi.root / "etc/sudoers.d/010_pi-nopasswd").exists()
    assert (etc / "setup-done").exists()
    assert (pi.root / "etc/sudoers.d/hyperwheel").read_text() == sudo_rule(True)
    assert pi.ran_any("systemctl", "enable", "hyperwheel.service", "hyperwheel-dashboard.service")
    assert pi.ran_any("systemctl", "restart", "hyperwheel.service", "hyperwheel-dashboard.service")
    assert not pi.ran_any("systemctl", "reboot")

    # no password is written anywhere, except the hotspot's and the scanner's own root-only files
    files = every_file_text(pi.root)
    assert DM_PW not in files
    assert files.count(SCANNER_PW) == 1 and files.count(HOTSPOT_PW) == 2
    # and only the datamanager password is ever shown, once with the screen cleared after
    assert HOTSPOT_PW not in typed.text and SCANNER_PW not in typed.text
    shown = [i for i, line in enumerate(typed.shown) if DM_PW in line]
    assert len(shown) == 1 and CLEAR_SCREEN in typed.shown[shown[0]:]


@pytest.mark.parametrize("site, name", [
    ("kcl-test", "hyperwheel-kcl-test"),
    ("Site_A.2", "hyperwheel-site-a-2"),
    ("a--b__c", "hyperwheel-a-b-c"),
    ("x" * 64, "hyperwheel-" + "x" * 52),
    ("a" * 51 + "-b", "hyperwheel-" + "a" * 51),
])
def test_the_pis_name_comes_from_the_site(site, name):
    assert hostname_for_site(site) == name
    assert len(name) <= 63 and re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", name)


def test_the_example_site_file_in_the_repo_is_accepted(pi):
    parsed = parse_site_file(TEMPLATE.read_text(), zoneinfo=pi.root / "usr/share/zoneinfo")
    assert parsed.site.site == "site-a" and parsed.routing.projects == {}
    assert parsed.site.grip == EXAMPLE_GRIP  # made up: GRIP's addresses are never in the repository
    parsed = parse_site_file(WITH_DATASETS, zoneinfo=pi.root / "usr/share/zoneinfo")
    assert set(parsed.routing.projects) == {"SiteA-StudyOne", "SiteA-Sandbox"}
    own, shared = parsed.routing.projects["SiteA-StudyOne"], parsed.routing.projects["SiteA-Sandbox"]
    assert own.api_base_url == "https://own.example.org/api" and shared.api_base_url == EXAMPLE_GRIP.api_base_url
    assert shared.label == "site-a-sandbox" and own.label == "SiteA-StudyOne"


def test_a_site_file_must_give_grips_address(pi):
    zoneinfo = pi.root / "usr/share/zoneinfo"
    parsed = parse_site_file(TEMPLATE.read_text(), zoneinfo=zoneinfo)
    assert '[grip]\napi_base_url = "https://grip.example.org/api"' in parsed.site_toml.replace(
        "# GRIP's address for this site's region, from the site file. Never in the release.\n", "")
    grip = '[grip]\napi_base_url = "https://grip.example.org/api"\ntoken_url = "https://grip.example.org/api/get/token"\n'
    assert grip in TEMPLATE.read_text()
    for text in (TEMPLATE.read_text().replace(grip, ""),
                 TEMPLATE.read_text().replace(grip, '[grip]\napi_base_url = "https://grip.example.org/api"\n'),
                 TEMPLATE.read_text().replace(grip, 'grip = "https://grip.example.org/api"\n')):
        with pytest.raises(SetupError, match=r"needs a \[grip\] section"):
            parse_site_file(text, zoneinfo=zoneinfo)


def test_setup_says_restart_and_restarts_only_on_yes(pi):
    typed = Scripted(answers=[*template_lines(), "yes", DM_PW, "yes"], hidden=[""])
    setup, _ = make(pi, typed)
    assert setup.main() == 0
    assert pi.ran[-1] == ["systemctl", "reboot"]


# ---- administrator rights -----------------------------------------------------------------------------


def test_password_free_sudo_stays_until_another_account_can_become_root_with_a_password(tmp_path):
    pi = FakePi(tmp_path / "pi", other_admin=False)  # the image: datamanager is the only account, admin has none
    typed = Scripted(answers=[*template_lines(), "yes", DM_PW, "no"], hidden=[""])
    setup, _ = make(pi, typed)
    assert setup.main() == 3
    assert (pi.root / "etc/sudoers.d/010_pi-nopasswd").exists()
    assert Accounts.read(Layout(pi.root)).in_group("datamanager", "sudo")
    assert not (pi.root / "etc/hyperwheel/setup-done").exists()
    assert (pi.root / "etc/sudoers.d/hyperwheel").read_text() == sudo_rule(False)
    assert "Not yet: no account other than datamanager can become root with a password" in typed.text
    assert "except for administrator rights" in typed.text


def test_a_sudoers_file_with_other_rules_is_kept_and_named(pi):
    mixed = pi.root / "etc/sudoers.d/020_site"
    mixed.write_text("firstuser ALL=(ALL) NOPASSWD: ALL\nfirstuser ALL=(root) /usr/bin/apt\n")
    (pi.root / "etc/sudoers.d/README.txt").write_text("firstuser ALL=(ALL) NOPASSWD: ALL\n")  # sudo skips it
    typed = Scripted(answers=[*template_lines(), "yes", DM_PW, "no"], hidden=[""])
    setup, _ = make(pi, typed)
    assert setup.main() == 0
    assert mixed.exists() and "Kept 020_site" in typed.text
    assert (pi.root / "etc/sudoers.d/README.txt").exists()


# ---- running it again -------------------------------------------------------------------------------------


def test_it_refuses_once_the_pi_is_set_up(pi):
    (pi.root / "etc/hyperwheel/setup-done").write_text("")
    typed = Scripted()
    setup, _ = make(pi, typed)
    assert setup.main() == 1 and pi.ran == []
    assert "already set up" in typed.text


def test_it_refuses_on_a_pi_set_up_by_hand_before_setup_existed(pi):
    (pi.root / "etc/hyperwheel/site.toml").write_text("")
    typed = Scripted()
    setup, _ = make(pi, typed)
    assert setup.main() == 1 and pi.ran == []
    assert "set up before hyperwheel setup existed" in typed.text


def test_run_again_after_a_stop_it_carries_on_and_redoes_nothing(pi):
    # first run: stops at the type-back (the person couldn't type it back)
    typed = Scripted(answers=[*template_lines(), "yes", *["wrong"] * 5], hidden=[SCANNER_PW])
    setup, _ = make(pi, typed)
    with pytest.raises(SetupError, match="wasn't typed back, so it wasn't set"):
        setup.main()
    assert pi.chpasswd == []
    site_before = (pi.root / "etc/hyperwheel/site.toml").read_bytes()
    # second run: the site file, hotspot and scanner password are kept, the password is made now
    typed = Scripted(answers=["dafog-hupen-kisor", "no"], hidden=[""])
    setup, tried = make(pi, typed, new_password=lambda: "dafog-hupen-kisor")
    assert setup.main() == 0, typed.text
    assert "already on this Pi, and are kept as they are" in typed.text
    assert "The hotspot Hyperwheel is already set up." in typed.text
    assert "Kept as it is." in typed.text and tried == []
    assert (pi.root / "etc/hyperwheel/site.toml").read_bytes() == site_before
    assert pi.chpasswd == [("datamanager", "dafog-hupen-kisor")]


def test_a_datamanager_password_set_before_is_kept_unless_asked(pi):
    typed = Scripted(answers=[*template_lines(), "yes", DM_PW, "no"], hidden=[""])
    setup, _ = make(pi, typed)
    assert setup.main() == 0
    (pi.root / "etc/hyperwheel/setup-done").unlink()  # as if it had stopped after step 6
    typed = Scripted(answers=["", "no"], hidden=[""])
    setup, _ = make(pi, typed)
    assert setup.main() == 0
    assert pi.chpasswd == [("datamanager", DM_PW)]  # not set again
    assert any("set by an earlier run of setup" in prompt for prompt in typed.prompts)


# ---- the site file ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("change, says", [
    (("Europe/London", "Europe/Atlantis"), "isn't a time zone this Pi knows"),
    (('"GB"', '"UK"'), "isn't a two-letter country code"),
    (('StudyOne]\nlogins = ["study_one"]', 'StudyOne]\nlogins = ["Study One"]'), "must be lower case letters"),
    (('update_channel = "stable"', 'update_channel = "beta"'), "channel must be dev, pilot or stable"),
    (("https://grip.example.org/api\"\ntoken", "http://grip.example.org/api\"\ntoken"), "must be https://"),
    (("schema_version = 1", "schema_version = 1\nsite_name = \"x\""), "settings setup doesn't know: site_name"),
])
def test_a_wrong_site_file_is_explained_and_another_can_be_pasted(pi, change, says):
    wrong = WITH_DATASETS.replace(*change, 1)
    assert wrong != WITH_DATASETS
    typed = Scripted(answers=[*wrong.splitlines(), *template_lines(), "yes", DM_PW, "no"], hidden=[""])
    setup, _ = make(pi, typed)
    assert setup.main() == 0, typed.text
    assert any(line.startswith("That site file can't be used: ") and says in line for line in typed.shown), typed.text
    assert "/tmp" not in typed.text and "site.toml:" not in typed.text


def test_an_answer_other_than_yes_is_not_used_and_a_site_file_needs_no_datasets(pi):
    no_datasets = TEMPLATE.read_text().splitlines()  # the example lists no datasets
    typed = Scripted(answers=[*template_lines(), "no", *no_datasets, "yes", DM_PW, "no"], hidden=[""])
    setup, _ = make(pi, typed)
    assert setup.main() == 0
    assert "Not used." in typed.text and "  Datasets: none yet (added after setup)" in typed.shown
    assert "[projects." not in (pi.root / "etc/hyperwheel/routing.toml").read_text()
    assert "Next, the data manager adds each GRIP dataset and its key. Type: hyperwheel datasets add" in typed.shown


def test_one_site_file_already_there_is_never_overwritten(pi):
    (pi.root / "etc/hyperwheel/setup.json").write_text("{}")
    (pi.root / "etc/hyperwheel/routing.toml").write_text("kept")
    typed = Scripted(answers=template_lines())
    setup, _ = make(pi, typed)
    with pytest.raises(SetupError, match="only routing.toml is on this Pi"):
        setup.main()
    assert (pi.root / "etc/hyperwheel/routing.toml").read_text() == "kept"


# ---- the hotspot and the scanner ---------------------------------------------------------------------------


def test_with_no_hotspot_password_on_the_card_it_asks_twice_hidden(pi):
    (pi.root / BOOT_HOTSPOT.lstrip("/")).unlink()
    typed = Scripted(answers=[*template_lines(), "yes", DM_PW, "no"],
                     hidden=["short", "typed-hotspot-pw", "another-one", "typed-hotspot-pw", "typed-hotspot-pw", ""])
    setup, _ = make(pi, typed)
    assert setup.main() == 0, typed.text
    assert "It must be 8 to 63 characters long" not in typed.text and "it must be 8 to 63" in typed.text
    assert "The two don't match." in typed.text
    stored = json.loads((pi.root / "etc/hyperwheel/hotspot.json").read_text())
    assert stored["password"] == "typed-hotspot-pw" and "typed-hotspot-pw" not in typed.text


def test_the_hotspot_waits_for_a_restart_when_wifi_is_in_use(tmp_path):
    pi = FakePi(tmp_path / "pi", wifi="wlan0:connected:Hospital-Guest")
    typed = Scripted(answers=[*template_lines(), "yes", DM_PW, "no"], hidden=[""])
    setup, _ = make(pi, typed)
    assert setup.main() == 0
    assert not pi.ran_any("nmcli", "connection", "up")
    assert "Wi-Fi is in use for Hospital-Guest now, so the hotspot starts at the next restart." in typed.text


def test_a_refused_scanner_password_is_never_saved_and_can_be_skipped(pi):
    def refuse(site, password):
        raise LoginRefused("refused")
    typed = Scripted(answers=[*template_lines(), "yes", DM_PW, "no"], hidden=["wrong-scanner-pw", ""])
    setup, _ = make(pi, typed, try_password=refuse)
    assert setup.main() == 0
    assert not (pi.root / "etc/hyperwheel/scanner.json").exists()
    assert "may be missing the RRDF entitlement" in typed.text
    assert "Skipped. Set it later with: hyperwheel scanner password" in typed.text
    assert "wrong-scanner-pw" not in typed.text


def test_a_scanner_that_does_not_answer_is_said_and_setup_carries_on(pi):
    typed = Scripted(answers=[*template_lines(), "yes", DM_PW, "no"], hidden=[""])
    setup, _ = make(pi, typed, scanner=reach.SCANNER_NO_LINK)
    assert setup.main() == 0
    assert "The scanner doesn't answer at 10.42.0.1 now: the scanner cable has no link. Setup carries on." \
        in typed.shown


# ---- readable passwords ----------------------------------------------------------------------------------


def test_generated_passwords_are_three_readable_chunks_of_about_49_bits():
    made = {passwords.readable() for _ in range(2000)}
    assert len(made) == 2000
    assert all(passwords.PATTERN.fullmatch(p) for p in made)
    assert all(not set(p) & set("lcqwxy0123456789") for p in made)
    assert 49 <= passwords.bits() < 50
    assert passwords.PATTERN.fullmatch("borak-kefim-tumos")


def test_typing_back_ignores_case_and_spaces_around_the_dashes():
    assert passwords.same(" Borak - kefim-TUMOS ", "borak-kefim-tumos")
    assert not passwords.same("borak-kefim-tumo", "borak-kefim-tumos")
    assert not passwords.same("bórak-kefim-tumos", "borak-kefim-tumos")


def test_a_missing_system_command_stops_setup_with_one_plain_line():
    from hyperwheel.sitesetup import run_command
    with pytest.raises(SetupError, match="no-such-command-here isn't on this Pi"):
        run_command(["no-such-command-here", "x"])
    with pytest.raises(SetupError) as e:
        run_command(["sh", "-c", "exit 3"], input="datamanager:not-shown\n")
    assert "not-shown" not in str(e.value) and "exit 3" in str(e.value)


# ---- the screen lock ------------------------------------------------------------------------------------------


def test_the_screen_lock_starts_only_at_the_end_of_setup(pi):
    home = pi.root / "home" / "datamanager"
    home.mkdir(parents=True)
    autostart = home / ".config" / "labwc" / "autostart"
    typed = Scripted(answers=[*template_lines(), "yes", DM_PW, "no"], hidden=[SCANNER_PW])
    setup, _ = make(pi, typed)
    assert setup.main() == 0, typed.text
    assert "swayidle -w timeout 900 'swaylock -f" in autostart.read_text() and mode(autostart) == 0o644
    assert "The screen locks after 15 minutes idle" in typed.text
    # the folders belong to the account: root owning its .config would break the desktop
    assert pi.ran_any("install", "-d", "-o", "datamanager", "-g", "datamanager", str(home / ".config"))
    before = autostart.read_text()
    setup.screen_lock()
    assert autostart.read_text() == before  # never twice


def test_the_screen_lock_waits_while_setup_waits_for_the_admin_password(pi):
    home = pi.root / "home" / "datamanager"
    home.mkdir(parents=True)
    setup, _ = make(pi, Scripted(answers=[], hidden=[]))
    (pi.root / "etc" / "shadow").write_text("".join(
        line.split(":")[0] + ":!:20000:0:99999:7:::\n" for line in (pi.root / "etc" / "shadow").read_text().splitlines()))
    assert setup.lock_down() is False
    assert not (home / ".config").exists()


def test_macs_files_and_the_folder_go_with_the_copy(pi):
    copy = pi.root / BOOT_HOTSPOT.lstrip("/")
    (copy.parent / "._hotspot-password").write_bytes(b"x")
    typed = Scripted(answers=[*template_lines(), "yes", DM_PW, "no"], hidden=[SCANNER_PW])
    setup, _ = make(pi, typed)
    assert setup.main() == 0, typed.text
    assert not copy.parent.exists()


def test_hotspot_routing_is_on_unless_the_site_file_turns_it_off(pi):
    from hyperwheel.sitesetup import SetupError, parse_site_file
    zoneinfo = pi.root / "usr/share/zoneinfo"
    text = "\n".join(template_lines())
    on = parse_site_file(text, zoneinfo=zoneinfo)
    assert on.site.hotspot.routing is True and "routing = true" in on.site_toml
    off = parse_site_file(text.replace("schema_version = 1", "schema_version = 1\nhotspot_routing = false", 1),
                          zoneinfo=zoneinfo)
    assert off.site.hotspot.routing is False and "routing = false" in off.site_toml
    with pytest.raises(SetupError, match="hotspot_routing must be true or false"):
        parse_site_file(text.replace("schema_version = 1", 'schema_version = 1\nhotspot_routing = "no"', 1),
                        zoneinfo=zoneinfo)
