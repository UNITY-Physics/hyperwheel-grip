"""``hyperwheel setup``: set up a newly flashed Pi for its site. Run as root.

The admin runs it once, over the Raspberry Pi Connect shell, on a Pi flashed
from the Hyperwheel image. Until the Pi is set up, the data-manager account may
run it without a password (there's no admin password to ask for yet), and it
refuses to run once the Pi is set up. The steps:

1. **The site file.** The admin pastes the site's file (a template is in
   ``config/examples/site-file.toml``): the site name, time zone, Wi-Fi
   country, update channel, phantom Patient ID, and which scanner logins go to
   which GRIP project. It is checked and shown back to confirm, then written as
   ``site.toml`` and ``routing.toml``. Site settings already on the Pi are never
   overwritten.
2. **Time zone and Wi-Fi country**, from the site file, so times are local and
   the hotspot uses the channels allowed there.
3. **The scanner cable.** Every scanner is 10.42.0.1 and the Pi is 10.42.0.88
   on its cable. Setup adds that network connection (from the next restart)
   and says whether the scanner answers.
4. **The hotspot** ``Hyperwheel``. Its password comes from the copy the admin
   put on the card (``/boot/firmware/hyperwheel/hotspot-password``). Setup
   moves it into the site settings, root-only, and deletes the copy. Only if
   there is none does it ask.
5. **The scanner's raw-data password**, tried on the scanner before it's
   saved, as ``hyperwheel scanner password`` does. It can be skipped and set
   later.
6. **The datamanager password**, generated (``passwords``), shown once, and
   set only once it has been typed back. It is never written to a file or a
   log, only to the system's password hashes.
7. **The admin account**, in the sudo group. Its password isn't set here: it
   is the same on every Pi and arrives as a hash through the signed update
   channel.
8. **Password-free sudo removed** (Raspberry Pi OS's ``010_pi-nopasswd``) and
   datamanager taken out of the sudo group, but only once another account can
   still become root with a password. Then the Pi is marked set up, and the
   data-manager sudo rule loses its setup line.

Then Hyperwheel starts, and setup offers to restart the Pi. Every step can be
run again: stopping half way and running setup again carries on, and nothing
already done is redone without asking.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import tomllib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import passwords, reach, scannercmd
from .config import ConfigError, Routing, SiteConfig, is_time_zone_name, load_routing, load_site
from .installer import (DATA_MANAGER, SETUP_DONE, SETUP_PROGRESS, InstallError, Layout, is_set_up,
                        write_sudo_rule)
from .keyscmd import Console
from .rawpull import LoginRefused, ScannerError

ADMIN = "admin"
ADMIN_GROUP = "sudo"
SCANNER_HOST = "10.42.0.1"
PI_ON_CABLE = "10.42.0.88/24"
HOTSPOT_SSID = "Hyperwheel"
HOTSPOT_ADDRESS = "192.168.99.1/24"
HOTSPOT_CHANNEL = 6  # 2.4 GHz channels 1, 6 and 11 are allowed everywhere
BOOT_HOTSPOT = "/boot/firmware/hyperwheel/hotspot-password"
NM_DIR = "/etc/NetworkManager/system-connections"
HOTSPOT_FILE = "Hyperwheel.nmconnection"
CABLE_FILE = "Hyperwheel-scanner-cable.nmconnection"
END_OF_SITE_FILE = "# end of site file"
SITE_FILE_KEYS = {"schema_version", "site", "time_zone", "wifi_country", "update_channel", "phantom_patient_id",
                  "projects"}
CLEAR_SCREEN = "\x1b[H\x1b[2J\x1b[3J"  # also clears the scroll-back
STEPS = 8
_SITE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
# A sudoers line that lets an account run anything as root without a password.
_FULL_NOPASSWD = re.compile(r"^\s*\S+\s+ALL\s*=\s*\(\s*ALL(\s*:\s*ALL)?\s*\)\s*NOPASSWD\s*:\s*ALL\s*$")


class SetupError(Exception):
    """Setup stopped. The message says why."""


# ---- the site file ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class SiteFile:
    site: SiteConfig
    routing: Routing
    site_toml: str
    routing_toml: str


def _q(value: str) -> str:
    return json.dumps(value)  # a JSON string is a valid TOML basic string


def render_site_toml(data: dict) -> str:
    phantom = data.get("phantom_patient_id")
    return "\n".join([
        "# /etc/hyperwheel/site.toml: written by hyperwheel setup from the site file. Kept outside the",
        "# release, so updates don't touch it. Every setting not listed here has its default.",
        "schema_version = 1",
        "",
        f"site = {_q(data['site'])}",
        f"time_zone = {_q(data['time_zone'])}",
        f"wifi_country = {_q(data['wifi_country'])}",
        f"phantom_patient_id = {_q(phantom)}" if phantom is not None else "# phantom_patient_id: none",
        'staging_dir = "/var/lib/hyperwheel/stagingArea"',
        'state_db = "/var/lib/hyperwheel/state.db"',
        'keys_dir = "/etc/hyperwheel/keys"',
        "",
        "[scanner]",
        f'host = "{SCANNER_HOST}"',
        "",
        "[updates]",
        f"channel = {_q(data.get('update_channel', 'stable'))}",
        "",
    ])


def render_routing_toml(projects: dict) -> str:
    out = ["# /etc/hyperwheel/routing.toml: written by hyperwheel setup from the site file.",
           "# Which scanner logins go to which GRIP project. Each project's key is stored with",
           "# hyperwheel keys set, never here.",
           "schema_version = 1"]
    for name, entry in projects.items():
        out += ["", f"[projects.{name}]"]
        for key in ("logins", "api_base_url", "token_url", "patient_id_pattern"):
            if key in entry:
                out.append(f"{key} = {json.dumps(entry[key])}")
    return "\n".join(out) + "\n"


def parse_site_file(text: str, *, zoneinfo: Path) -> SiteFile:
    """Check a pasted site file. Raises SetupError with what's wrong, in plain words."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise SetupError(f"the site file isn't valid TOML ({e})") from None
    if data.get("schema_version") != 1:
        raise SetupError("the site file needs schema_version = 1 at the top")
    unknown = set(data) - SITE_FILE_KEYS
    if unknown:
        raise SetupError(f"the site file has settings setup doesn't know: {', '.join(sorted(unknown))}")
    site = data.get("site")
    if not isinstance(site, str) or not _SITE_NAME.fullmatch(site):
        raise SetupError("site must be a short name of letters, digits, '.', '-' and '_', such as \"site-a\"")
    tz = data.get("time_zone")
    if not is_time_zone_name(tz) or not (zoneinfo / tz).is_file():
        raise SetupError(f"time_zone {tz!r} isn't a time zone this Pi knows. Use a name such as \"Europe/London\"")
    country = data.get("wifi_country")
    if not isinstance(country, str) or country not in known_countries(zoneinfo):
        raise SetupError(f"wifi_country {country!r} isn't a two-letter country code, such as \"GB\" or \"KE\"")
    projects = data.get("projects")
    if not isinstance(projects, dict) or not projects:
        raise SetupError("the site file lists no projects. Add at least one [projects.<name>] with its logins")
    if any(not isinstance(entry, dict) for entry in projects.values()):
        raise SetupError("each project must be a [projects.<name>] table")
    site_toml, routing_toml = render_site_toml(data), render_routing_toml(projects)
    with tempfile.TemporaryDirectory() as tmp:
        site_path, routing_path = Path(tmp) / "site.toml", Path(tmp) / "routing.toml"
        site_path.write_text(site_toml)
        routing_path.write_text(routing_toml)
        try:
            config = load_site(site_path)
            routing = load_routing(routing_path)
        except ConfigError as e:
            message = str(e).replace(str(site_path), "the site file").replace(str(routing_path), "the site file")
            raise SetupError(message) from None
    return SiteFile(config, routing, site_toml, routing_toml)


def known_countries(zoneinfo: Path) -> set[str]:
    """Two-letter country codes from the time zone data (iso3166.tab)."""
    try:
        lines = (zoneinfo / "iso3166.tab").read_text().splitlines()
    except OSError:
        return set()
    return {line.split("\t", 1)[0] for line in lines if re.match(r"[A-Z]{2}\t", line)}


def summary(site: SiteConfig, routing: Routing, now: float | None = None) -> list[str]:
    local = ""
    if site.time_zone:
        try:
            from zoneinfo import ZoneInfo
            local = f" (it's {datetime.fromtimestamp(now or time.time(), ZoneInfo(site.time_zone)):%H:%M} there now)"
        except Exception:  # noqa: BLE001 - only for the summary
            local = ""
    out = [f"  Site: {site.site}",
           f"  Time zone: {site.time_zone or 'not set'}{local}",
           f"  Wi-Fi country: {site.wifi_country or 'not set'}",
           f"  Updates: {site.updates.channel}",
           f"  Phantom Patient ID: {site.phantom_patient_id or 'none'}",
           "  Projects:"]
    for p in routing.projects.values():
        out.append(f"    {p.name}: login{'s' if len(p.logins) != 1 else ''} {', '.join(p.logins)}, "
                   f"at {_host(p.api_base_url)}")
    return out


def _host(url: str) -> str:
    from urllib.parse import urlsplit
    return urlsplit(url).hostname or url


# ---- network files ---------------------------------------------------------------------------------


def hotspot_password_problem(password: str) -> str | None:
    if not 8 <= len(password) <= 63:
        return "it must be 8 to 63 characters long"
    if not all(" " <= ch <= "~" for ch in password):
        return "it may use only letters, digits, spaces and the symbols on a keyboard"
    if password != password.strip():
        return "it can't start or end with a space"
    return None


def hotspot_connection(password: str, connection_id: str | None = None) -> str:
    psk = password.replace("\\", "\\\\")  # NetworkManager's file format escapes backslashes
    return f"""\
[connection]
id={HOTSPOT_SSID}
uuid={connection_id or uuid.uuid4()}
type=wifi
interface-name=wlan0
autoconnect=true
autoconnect-priority=100

[wifi]
mode=ap
ssid={HOTSPOT_SSID}
band=bg
channel={HOTSPOT_CHANNEL}

[wifi-security]
key-mgmt=wpa-psk
proto=rsn
pairwise=ccmp
group=ccmp
psk={psk}

[ipv4]
method=shared
address1={HOTSPOT_ADDRESS}

[ipv6]
method=disabled
"""


def cable_connection(connection_id: str | None = None) -> str:
    return f"""\
[connection]
id=Hyperwheel scanner cable
uuid={connection_id or uuid.uuid4()}
type=ethernet
interface-name=eth0
autoconnect=true
autoconnect-priority=100

[ethernet]

[ipv4]
method=manual
address1={PI_ON_CABLE},{SCANNER_HOST}
dns={SCANNER_HOST}

[ipv6]
method=disabled
"""


# ---- accounts --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Accounts:
    """Read from the system's account files (under the layout's root)."""
    users: dict[str, int]  # name -> primary group ID
    groups: dict[str, tuple[int, set[str]]]  # name -> (ID, members)
    hashes: dict[str, str]

    @classmethod
    def read(cls, layout: Layout) -> Accounts:
        def rows(name: str) -> list[list[str]]:
            try:
                return [line.split(":") for line in layout.p(f"/etc/{name}").read_text().splitlines()
                        if line and not line.startswith("#")]
            except FileNotFoundError:
                return []
        users = {r[0]: int(r[3]) for r in rows("passwd") if len(r) >= 4 and r[3].isdigit()}
        groups = {r[0]: (int(r[2]), {m for m in r[3].split(",") if m}) for r in rows("group")
                  if len(r) >= 4 and r[2].isdigit()}
        hashes = {r[0]: r[1] for r in rows("shadow") if len(r) >= 2}
        return cls(users, groups, hashes)

    def exists(self, user: str) -> bool:
        return user in self.users

    def in_group(self, user: str, group: str) -> bool:
        if group not in self.groups or user not in self.users:
            return False
        gid, members = self.groups[group]
        return user in members or self.users[user] == gid

    def has_password(self, user: str) -> bool:
        """A password someone can log in with: not empty, not locked ("!", "*")."""
        h = self.hashes.get(user, "")
        return bool(h) and not h.startswith(("!", "*")) and h != "x"

    def admins_with_password(self, besides: str) -> list[str]:
        return sorted(u for u in self.users if u != besides and self.in_group(u, ADMIN_GROUP)
                      and self.has_password(u))


# ---- running it ------------------------------------------------------------------------------------


def run_command(cmd: Sequence[str], input: str | None = None) -> str:
    """Run a system command. What goes to its stdin (a password) is never in the error message."""
    out = subprocess.run(list(cmd), input=input, capture_output=True, text=True)
    if out.returncode != 0:
        raise SetupError(f"{' '.join(cmd[:2])} didn't work (exit {out.returncode}): {out.stderr.strip()[-300:]}")
    return out.stdout


@dataclass
class Setup:
    layout: Layout = field(default_factory=Layout)
    console: Console = field(default_factory=Console)
    run: Callable[..., str] = run_command
    scanner_state: Callable[[SiteConfig], int | None] = reach.scanner_state
    try_password: Callable[[SiteConfig, str], int] = scannercmd.try_on_scanner
    unreachable: Callable[[SiteConfig], str] = scannercmd.why_unreachable
    new_password: Callable[[], str] = passwords.readable
    chown: Callable[[Path], None] = field(default=lambda path: os.chown(path, 0, 0))
    clock: Callable[[], float] = time.time
    zoneinfo: Path | None = None

    def __post_init__(self):
        if self.zoneinfo is None:
            self.zoneinfo = self.layout.p("/usr/share/zoneinfo")

    @property
    def etc(self) -> Path:
        return self.layout.etc

    def say(self, text: str = "") -> None:
        self.console.say(text)

    def step(self, n: int, title: str) -> None:
        self.say("")
        self.say(f"Step {n} of {STEPS}: {title}")

    # ---- the whole setup ----

    def main(self) -> int:
        if is_set_up(self.etc):
            if (self.etc / SETUP_DONE).exists():
                self.say("This Pi is already set up, so setup won't change it. To store a GRIP key, type: "
                         "hyperwheel keys set")
            else:
                self.say("This Pi was set up before hyperwheel setup existed, so setup won't change it.")
            return 1
        self.say("Hyperwheel setup. It sets up this Pi for one site, in 8 steps. Nothing already on the Pi is "
                 "overwritten. If it stops, run hyperwheel setup again and it carries on.")
        self.progress("started_at", keep=True)
        site, routing = self.site_settings()
        self.time_and_country(site)
        self.scanner_cable(site)
        self.hotspot()
        self.scanner_password(site)
        self.datamanager()
        self.admin_account()
        done = self.lock_down()
        self.start_hyperwheel()
        return self.finish(site, done)

    # ---- progress (no secrets in it) ----

    def progress(self, what: str, *, keep: bool = False) -> None:
        path = self.etc / SETUP_PROGRESS
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        if keep and what in data:
            return
        data[what] = datetime.fromtimestamp(self.clock()).isoformat(timespec="seconds")
        self.write(path, json.dumps(data, indent=2) + "\n", 0o644)

    def done_before(self, what: str) -> bool:
        try:
            return what in json.loads((self.etc / SETUP_PROGRESS).read_text())
        except (OSError, ValueError):
            return False

    def write(self, path: Path, text: str, mode: int) -> None:
        """Write a file in one step (a power cut leaves the old one or the new one), owned by root."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.new")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, mode)
            self.chown(tmp)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    # ---- 1. the site file ----

    def site_settings(self) -> tuple[SiteConfig, Routing]:
        self.step(1, "the site file")
        site_path, routing_path = self.etc / "site.toml", self.etc / "routing.toml"
        if site_path.exists() and routing_path.exists():
            try:
                site = load_site(site_path)
                routing = load_routing(routing_path, test_mode=site.test_mode)
            except ConfigError as e:
                raise SetupError(f"the site settings already on this Pi can't be read ({e})") from None
            self.say("The site settings are already on this Pi, and are kept as they are:")
            for line in summary(site, routing, self.clock()):
                self.say(line)
            return site, routing
        if site_path.exists() or routing_path.exists():
            have = "site.toml" if site_path.exists() else "routing.toml"
            raise SetupError(f"only {have} is on this Pi, not both. Setup won't overwrite it. Remove it, or add "
                             "the other, then run hyperwheel setup again")
        for _ in range(5):
            self.say("Paste the site file now. It ends with the line '# end of site file'.")
            text = self.read_site_file()
            try:
                parsed = parse_site_file(text, zoneinfo=self.zoneinfo)
            except SetupError as e:
                self.say(f"That site file can't be used: {e}.")
                continue
            self.say("")
            self.say("This is the site file:")
            for line in summary(parsed.site, parsed.routing, self.clock()):
                self.say(line)
            if self.console.ask("Is it right? Type yes to use it: ").strip().lower() != "yes":
                self.say("Not used.")
                continue
            self.write(routing_path, parsed.routing_toml, 0o644)
            self.write(site_path, parsed.site_toml, 0o644)
            self.progress("site_file_at")
            self.say("Site settings saved.")
            return parsed.site, parsed.routing
        raise SetupError("no site file was accepted")

    def read_site_file(self) -> str:
        lines = []
        while True:
            try:
                line = self.console.ask("")
            except EOFError:
                break
            if line.strip() == END_OF_SITE_FILE:
                break
            lines.append(line)
        return "\n".join(lines) + "\n"

    # ---- 2. time zone and Wi-Fi country ----

    def time_and_country(self, site: SiteConfig) -> None:
        self.step(2, "time zone and Wi-Fi country")
        if site.time_zone:
            self.run(["timedatectl", "set-timezone", site.time_zone])
            self.say(f"Time zone: {site.time_zone}.")
        else:
            self.say("The site settings have no time zone, so it's left as it is.")
        if site.wifi_country:
            self.run(["raspi-config", "nonint", "do_wifi_country", site.wifi_country])
            self.say(f"Wi-Fi country: {site.wifi_country}.")
        else:
            self.say("The site settings have no Wi-Fi country, so it's left as it is.")

    # ---- 3. the scanner cable ----

    def scanner_cable(self, site: SiteConfig) -> None:
        self.step(3, "the scanner cable")
        path = self.layout.p(NM_DIR) / CABLE_FILE
        if path.exists():
            self.say(f"The scanner cable connection is already there ({PI_ON_CABLE.split('/')[0]} on the cable).")
        else:
            self.write(path, cable_connection(), 0o600)
            self.run(["nmcli", "connection", "reload"])
            self.say(f"On the scanner cable this Pi is {PI_ON_CABLE.split('/')[0]} and the scanner "
                     f"{SCANNER_HOST}, from the next restart.")
        state = self.scanner_state(site)
        if state == reach.SCANNER_OK:
            self.say(f"The scanner answers at {SCANNER_HOST}.")
        else:
            self.say(f"The scanner doesn't answer at {SCANNER_HOST} now: {self.unreachable(site)}. "
                     "Setup carries on.")

    # ---- 4. the hotspot ----

    def hotspot(self) -> None:
        self.step(4, f"the hotspot ({HOTSPOT_SSID})")
        stored, connection = self.etc / "hotspot.json", self.layout.p(NM_DIR) / HOTSPOT_FILE
        copy = self.layout.p(BOOT_HOTSPOT)
        if stored.exists() and connection.exists():
            self.say(f"The hotspot {HOTSPOT_SSID} is already set up.")
            self.remove_copy(copy)
            return
        password = self.hotspot_password(copy)
        self.write(stored, json.dumps({"ssid": HOTSPOT_SSID, "password": password}) + "\n", 0o600)
        self.write(connection, hotspot_connection(password), 0o600)
        self.remove_copy(copy)
        self.run(["nmcli", "connection", "reload"])
        self.progress("hotspot_at")
        self.say(f"Hotspot {HOTSPOT_SSID}: set up, on 2.4 GHz channel {HOTSPOT_CHANNEL}, with the Upload Monitor at "
                 f"http://{HOTSPOT_ADDRESS.split('/')[0]}:5000")
        self.start_hotspot()

    def hotspot_password(self, copy: Path) -> str:
        if copy.is_file():
            raw = copy.read_text(errors="replace")
            password = raw[:-1] if raw.endswith("\n") else raw
            password = password[:-1] if password.endswith("\r") else password
            problem = hotspot_password_problem(password)
            if problem is None:
                self.say("The hotspot password came with the image. It's moved into the site settings now.")
                return password
            self.say(f"The hotspot password that came with the image can't be used: {problem}.")
        else:
            self.say("No hotspot password came with the image, so type it here.")
        for _ in range(3):
            first = self.console.ask_hidden("Hotspot password (8 to 63 characters, hidden as you type): ")
            problem = hotspot_password_problem(first)
            if problem:
                self.say(f"  That can't be used: {problem}.")
                continue
            if self.console.ask_hidden("The same again: ") != first:
                self.say("  The two don't match.")
                continue
            return first
        raise SetupError("no hotspot password was set")

    def remove_copy(self, copy: Path) -> None:
        if copy.is_file() or copy.is_symlink():
            copy.unlink()
            try:
                copy.parent.rmdir()
            except OSError:
                pass
            self.say("The copy of the hotspot password on the card's boot folder was deleted.")

    def start_hotspot(self) -> None:
        """Start it now unless wlan0 is in use for something else (that would cut that connection)."""
        try:
            devices = self.run(["nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "device", "status"])
        except SetupError:
            devices = ""
        for line in devices.splitlines():
            parts = line.split(":")
            if parts[0] == "wlan0" and len(parts) >= 3 and parts[1] == "connected" and parts[2] != HOTSPOT_SSID:
                self.say(f"Wi-Fi is in use for {parts[2]} now, so the hotspot starts at the next restart.")
                return
        try:
            self.run(["nmcli", "connection", "up", HOTSPOT_SSID])
            self.say("The hotspot is on.")
        except SetupError:
            self.say("The hotspot didn't start yet. It starts at the next restart.")

    # ---- 5. the scanner's raw-data password ----

    def scanner_password(self, site: SiteConfig) -> None:
        self.step(5, "the scanner's raw-data password")
        path = self.etc / "scanner.json"
        stored = scannercmd._stored(path)
        if stored is not None:
            self.say("A raw-data password is already on this Pi. Press Enter to keep it, or paste a new one.")
        else:
            self.say("Paste the scanner's raw-data password, or press Enter to skip it and set it later with: "
                     "hyperwheel scanner password")
        for _ in range(3):
            try:
                password = scannercmd._ask(self.console)
            except (scannercmd.Cancelled, EOFError):
                self.say("Kept as it is." if stored is not None else "Skipped. Set it later with: "
                         "hyperwheel scanner password")
                return
            if password == stored:
                self.say("That's the password already on this Pi.")
                return
            self.say("Trying it on the scanner ...")
            try:
                folders = self.try_password(site, password)
            except LoginRefused:
                self.say("The scanner refused this password. Nothing was saved. " + scannercmd.ENTITLEMENT)
                self.say("Paste it again, or press Enter to skip.")
                continue
            except (ScannerError, OSError):
                self.say(f"The password couldn't be tried: {self.unreachable(site)}. Nothing was saved. "
                         "Set it later with: hyperwheel scanner password")
                return
            scannercmd.save(path, password)
            self.progress("scanner_password_at")
            self.say(f"The scanner accepted it ({folders} raw-data folder{'s' if folders != 1 else ''} found). "
                     "Saved.")
            return
        self.say("Skipped. Set it later with: hyperwheel scanner password")

    # ---- 6. the datamanager password ----

    def datamanager(self) -> None:
        self.step(6, f"the {DATA_MANAGER} account")
        accounts = Accounts.read(self.layout)
        if not accounts.exists(DATA_MANAGER):
            self.run(["useradd", "--create-home", "--shell", "/bin/bash", "--user-group", DATA_MANAGER])
            self.say(f"Account {DATA_MANAGER}: created.")
        if self.done_before("datamanager_password_at"):
            answer = self.console.ask(f"The {DATA_MANAGER} password was set by an earlier run of setup. Press Enter "
                                      "to keep it, or type new to make a new one: ").strip().lower()
            if answer != "new":
                self.say("Kept.")
                return
        password = self.new_password()
        self.say("")
        self.say(f"The {DATA_MANAGER} password for this Pi is:")
        self.say("")
        self.say(f"    {password}")
        self.say("")
        self.say("Save it in your password manager now. It's shown only this once, and it isn't kept anywhere on "
                 "the Pi except as the system's password hash.")
        for _ in range(5):
            typed = self.console.ask("Type it back to check you have it: ")
            if passwords.same(typed, password):
                break
            self.say(f"That isn't it. Here it is again:    {password}")
        else:
            raise SetupError(f"the {DATA_MANAGER} password wasn't typed back, so it wasn't set")
        self.run(["chpasswd"], input=f"{DATA_MANAGER}:{password}\n")
        self.progress("datamanager_password_at")
        self.say(CLEAR_SCREEN)
        self.say(f"The {DATA_MANAGER} password is set. The screen was cleared so it isn't left on it.")

    # ---- 7. the admin account ----

    def admin_account(self) -> None:
        self.step(7, f"the {ADMIN} account")
        accounts = Accounts.read(self.layout)
        if not accounts.exists(ADMIN):
            self.run(["useradd", "--create-home", "--shell", "/bin/bash", "--user-group", "--groups", ADMIN_GROUP,
                      "--comment", "Hyperwheel admin", ADMIN])
            self.say(f"Account {ADMIN}: created, in the {ADMIN_GROUP} group.")
        elif not accounts.in_group(ADMIN, ADMIN_GROUP):
            self.run(["usermod", "--append", "--groups", ADMIN_GROUP, ADMIN])
            self.say(f"Account {ADMIN}: added to the {ADMIN_GROUP} group.")
        else:
            self.say(f"Account {ADMIN}: already there.")
        if Accounts.read(self.layout).has_password(ADMIN):
            self.say(f"{ADMIN} has its password.")
        else:
            self.say(f"{ADMIN} has no password yet. It's the same on every Pi and arrives through the signed "
                     "update channel.")

    # ---- 8. password-free sudo removed ----

    def lock_down(self) -> bool:
        self.step(8, "administrator rights")
        accounts = Accounts.read(self.layout)
        others = accounts.admins_with_password(besides=DATA_MANAGER)
        if not others:
            self.say(f"Not yet: no account other than {DATA_MANAGER} can become root with a password ({ADMIN} has no "
                     f"password yet). So {DATA_MANAGER} keeps its administrator rights for now, and password-free "
                     f"sudo stays. Run hyperwheel setup again once {ADMIN} has its password.")
            return False
        if accounts.in_group(DATA_MANAGER, ADMIN_GROUP):
            self.run(["gpasswd", "--delete", DATA_MANAGER, ADMIN_GROUP])
            self.say(f"{DATA_MANAGER}: taken out of the {ADMIN_GROUP} group. It can run only the Hyperwheel commands "
                     "in hyperwheel help.")
        self.remove_password_free_sudo()
        self.write(self.etc / SETUP_DONE, datetime.fromtimestamp(self.clock()).isoformat(timespec="seconds") + "\n",
                   0o644)
        try:
            write_sudo_rule(self.layout, True, self.run)
        except InstallError as e:
            raise SetupError(str(e)) from None
        self.say(f"Administrator rights: {', '.join(others)} (with a password). Password-free sudo is gone.")
        return True

    def remove_password_free_sudo(self) -> None:
        folder = self.layout.p("/etc/sudoers.d")
        for path in sorted(folder.iterdir()) if folder.is_dir() else []:
            if path.name == "hyperwheel" or "." in path.name or path.name.endswith("~") or not path.is_file():
                continue  # sudo itself skips names with a dot or ending in ~
            rules = [line for line in path.read_text().splitlines() if line.strip() and not line.lstrip()
                     .startswith("#")]
            if not any("NOPASSWD" in line for line in rules):
                continue
            if all(_FULL_NOPASSWD.match(line) for line in rules):
                path.unlink()
                self.say(f"Removed {path.name} (it let an account run anything as root without a password).")
            else:
                self.say(f"Kept {path.name}: it has password-free rules among others. Check it by hand.")
        main = self.layout.p("/etc/sudoers")
        if main.is_file() and any("NOPASSWD" in line and not line.lstrip().startswith("#")
                                  for line in main.read_text().splitlines()):
            self.say("Note: /etc/sudoers itself has a password-free rule. Check it by hand.")

    # ---- the end ----

    def start_hyperwheel(self) -> None:
        self.say("")
        self.run(["systemctl", "enable", "--now", "hyperwheel.service", "hyperwheel-dashboard.service"])
        self.say("Hyperwheel and the Upload Monitor: started.")

    def finish(self, site: SiteConfig, done: bool) -> int:
        self.say("")
        if done:
            self.say(f"This Pi is set up for {site.site}.")
        else:
            self.say(f"This Pi is set up for {site.site}, except for administrator rights (step 8).")
        self.say("Next, the data manager stores each project's GRIP key. Type: hyperwheel keys set")
        self.say("The scanner cable address and the Wi-Fi country take effect when the Pi restarts.")
        if self.console.ask("Restart the Pi now? Type yes to restart: ").strip().lower() == "yes":
            self.say("Restarting.")
            self.run(["systemctl", "reboot"])
        else:
            self.say("Not restarted. Restart it before the first scan.")
        return 0 if done else 3


def main(args: list[str], *, is_root: Callable[[], bool] = lambda: os.geteuid() == 0) -> int:
    if args:
        print("Usage: hyperwheel setup")
        return 2
    if not is_root():
        print("'hyperwheel setup' needs administrator rights. Run it as root.")
        return 2
    setup = Setup()
    try:
        return setup.main()
    except SetupError as e:
        setup.say("")
        setup.say(f"Setup stopped: {e}. What was done so far stays done. Run hyperwheel setup again to carry on.")
        return 1
    except (KeyboardInterrupt, EOFError):
        setup.say("")
        setup.say("Setup stopped. What was done so far stays done. Run hyperwheel setup again to carry on.")
        return 1

