"""The admin password: made on the signer's Mac, published as a signed hash, set on every Pi."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_keyscmd import Scripted
from test_release import keypair, signers_file
from test_sitesetup import DM_PW, FakePi, make, template_lines

from hyperwheel import passwords
from hyperwheel.adminpw import AdminError, AdminPassword
from hyperwheel.installer import Layout, sudo_rule
from hyperwheel.release import ADMIN_FILE, ADMIN_SIGNATURE, NAMESPACE, AdminFile, ReleaseError, parse_admin
from hyperwheel.sitesetup import finish_waiting_setup

BASE = "https://releases.example.org/download"
REPO = Path(__file__).resolve().parents[1]
FAST = 1000  # rounds, for tests


def hashed(password: str) -> str:
    return passwords.sha512_crypt(password, rounds=FAST)


# ---- the hash ----------------------------------------------------------------------------------------


def test_sha512_crypt_matches_the_specification():
    # From the SHA-crypt specification's test vectors (the salt is cut to 16 characters).
    assert passwords.sha512_crypt("Hello world!", "saltstringsaltstring", 10000) == \
        ("$6$rounds=10000$saltstringsaltst$OW1/O6BYHV6BcXZu8QVeXbDWra3Oeqh0sbHbbMCVNSnCM/UrjmM0Dp8vOuZeHBy/"
         "YTBmSK6H9qs/y3RnOaw5v.")


def test_sha512_crypt_matches_this_systems_crypt_where_it_has_one():
    probe = subprocess.run(["perl", "-e", 'print crypt("x", q{$6$rounds=1000$abcdefgh$})'], capture_output=True,
                           text=True)
    if probe.returncode != 0 or not probe.stdout.startswith("$6$"):
        pytest.skip("no SHA-512 crypt here (macOS)")
    for password in ("borak-kefim-tumos", "a" * 70, "x"):
        ours = passwords.sha512_crypt(password, "Zx9./abcdefgh123", 5000)
        theirs = subprocess.run(["perl", "-e", f'print crypt($ARGV[0], q{{$6$rounds=5000$Zx9./abcdefgh123$}})',
                                 password], capture_output=True, text=True).stdout
        assert ours == theirs


def test_a_hash_checks_out_only_against_its_own_password():
    h = hashed("borak-kefim-tumos")
    assert passwords.HASH_PATTERN.fullmatch(h) and h.startswith("$6$rounds=1000$")
    assert passwords.matches("borak-kefim-tumos", h) and not passwords.matches("borak-kefim-tumoz", h)
    assert hashed("borak-kefim-tumos") != h  # a new salt every time
    assert passwords.ROUNDS == 1_000_000


# ---- the signed file -----------------------------------------------------------------------------------


class Channel:
    """A pretend download site holding the admin password file."""

    def __init__(self, tmp: Path):
        self.tmp, self.key = tmp, keypair(tmp, "signer")
        self.signers = signers_file(tmp, ("release-signer-1", self.key))
        self.site: dict[str, Path] = {}
        self.n = 0

    def publish(self, serial: int, hash_: str, *, key=None, text=None):
        self.n += 1
        folder = self.tmp / f"admin-{self.n}"
        folder.mkdir()
        (folder / ADMIN_FILE).write_text(text or AdminFile(serial, hash_, "2026-11-08T10:00:00Z").to_json())
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key or self.key), "-n", NAMESPACE,
                        str(folder / ADMIN_FILE)], check=True, capture_output=True)
        self.site = {f"{BASE}/admin-password/{n}": folder / n for n in (ADMIN_FILE, ADMIN_SIGNATURE)}

    def fetch(self, url, dest, max_bytes):
        if url not in self.site:
            raise AdminError(f"couldn't download {dest.name} (404)")
        shutil.copy(self.site[url], dest)


@pytest.fixture
def pi(tmp_path):
    p = FakePi(tmp_path / "pi")
    p.run(["useradd", "--create-home", "--groups", "sudo", "admin"])
    p.channel = Channel(tmp_path)
    shutil.copy(p.channel.signers, p.root / "etc/hyperwheel/release_allowed_signers")
    p.set = []

    def set_hash(user, h):
        p.set.append((user, h))
        p._edit("shadow", lambda line: line if not line.startswith(user + ":") else
                f"{user}:{h}:" + ":".join(line.split(":")[2:]))
    p.admin = AdminPassword(BASE, Layout(p.root), fetch=p.channel.fetch, set_hash=set_hash,
                            clock=lambda: 1_800_000_000.0)
    return p


def shadow_hash(pi, user="admin"):
    return next(line.split(":")[1] for line in (pi.root / "etc/shadow").read_text().splitlines()
                if line.startswith(user + ":"))


def test_a_published_hash_is_set_for_admin_once(pi):
    h = hashed("borak-kefim-tumos")
    pi.channel.publish(1, h)
    assert pi.admin.check() == "admin password: set (version 1, signed by release-signer-1)"
    assert pi.set == [("admin", h)] and shadow_hash(pi) == h
    assert pi.admin.check() == "admin password: up to date (version 1)"
    assert pi.set == [("admin", h)]
    assert not (pi.root / "var/lib/hyperwheel-update/downloads/admin").exists()


def test_a_newer_file_replaces_the_password_and_an_older_one_is_never_taken(pi):
    first, second = hashed("borak-kefim-tumos"), hashed("dafog-hupen-kisor")
    pi.channel.publish(1, first)
    pi.admin.check()
    pi.channel.publish(2, second)
    pi.admin.check()
    assert shadow_hash(pi) == second
    pi.channel.publish(1, first)  # the old file, signed and all, put back
    with pytest.raises(AdminError, match=r"version 1\) is older than the one this Pi took \(version 2\)"):
        pi.admin.check()
    assert shadow_hash(pi) == second


def test_a_file_signed_by_anyone_else_or_changed_is_refused(pi, tmp_path):
    h = hashed("borak-kefim-tumos")
    pi.channel.publish(1, h, key=keypair(tmp_path, "stranger"))
    with pytest.raises(AdminError, match="not by an allowed signer"):
        pi.admin.check()
    pi.channel.publish(1, h)
    pi.channel.site[f"{BASE}/admin-password/{ADMIN_FILE}"].write_text(AdminFile(1, hashed("x"), "").to_json())
    with pytest.raises(AdminError, match="signature does not match"):
        pi.admin.check()
    assert pi.set == [] and shadow_hash(pi) == "!"


def test_another_signed_file_cannot_pass_for_the_admin_file(pi):
    pi.channel.publish(1, "", text=json.dumps({"format": 1, "name": "hyperwheel-channel", "channel": "dev"}))
    with pytest.raises(AdminError, match="not a Hyperwheel admin password file"):
        pi.admin.check()


@pytest.mark.parametrize("change, says", [
    ({"user": "root"}, "is for 'root', not admin"),
    ({"serial": 0}, "serial of 1 or more"),
    ({"hash": "$5$rounds=5000$abcdefgh$" + "a" * 43}, "isn't a SHA-512 crypt hash"),
    ({"hash": "$6$abcdefgh$" + "a" * 86}, "isn't a SHA-512 crypt hash with rounds"),
])
def test_the_file_must_be_for_admin_with_a_sha512_crypt_hash(change, says):
    data = json.loads(AdminFile(1, hashed("x"), "").to_json()) | change
    with pytest.raises(ReleaseError, match=says):
        parse_admin(json.dumps(data))


def test_a_pi_with_no_admin_account_changes_nothing(tmp_path):
    p = FakePi(tmp_path / "pi")
    ch = Channel(tmp_path)
    shutil.copy(ch.signers, p.root / "etc/hyperwheel/release_allowed_signers")
    ch.publish(1, hashed("x"))
    admin = AdminPassword(BASE, Layout(p.root), fetch=ch.fetch, set_hash=lambda *a: pytest.fail("set"))
    assert admin.check() == "admin password: this Pi has no admin account"


# ---- setup finishes ------------------------------------------------------------------------------------


def test_setup_fetches_the_admin_password_and_finishes_while_still_connected(tmp_path):
    p = FakePi(tmp_path / "pi", other_admin=False)  # the image: no account but datamanager can become root
    ch = Channel(tmp_path)
    shutil.copy(ch.signers, p.root / "etc/hyperwheel/release_allowed_signers")
    ch.publish(1, hashed("borak-kefim-tumos"))

    def fetch_now(site):
        def set_hash(user, h):
            p._edit("shadow", lambda line: line if not line.startswith(user + ":") else
                    f"{user}:{h}:" + ":".join(line.split(":")[2:]))
        return AdminPassword(BASE, Layout(p.root), fetch=ch.fetch, set_hash=set_hash).check()
    typed = Scripted(answers=[*template_lines(), "yes", DM_PW, "no"], hidden=[""])
    setup, _ = make(p, typed, admin_password=fetch_now)
    assert setup.main() == 0, typed.text
    assert "admin password: set (version 1, signed by release-signer-1)" in typed.text
    assert "Administrator rights: admin (with a password). Password-free sudo is gone." in typed.text
    assert not (p.root / "etc/sudoers.d/010_pi-nopasswd").exists()
    assert (p.root / "etc/hyperwheel/setup-done").exists()


def test_without_the_admin_password_setup_waits_and_the_updater_finishes_it_later(tmp_path):
    p = FakePi(tmp_path / "pi", other_admin=False)
    typed = Scripted(answers=[*template_lines(), "yes", DM_PW, "no"], hidden=[""])
    setup, _ = make(p, typed)  # the admin password file can't be fetched
    assert setup.main() == 3
    assert "It couldn't be fetched now: couldn't download admin.json (404)." in typed.text
    assert "The Pi finishes this step by itself once admin's password arrives" in typed.text
    layout = Layout(p.root)
    assert not finish_waiting_setup(layout, run=p.run, say=lambda s: None)  # still no admin password
    assert (p.root / "etc/sudoers.d/010_pi-nopasswd").exists()

    p._edit("shadow", lambda line: line if not line.startswith("admin:") else
            "admin:" + hashed("x") + ":" + ":".join(line.split(":")[2:]))
    said = []
    assert finish_waiting_setup(layout, run=p.run, say=said.append)
    assert not (p.root / "etc/sudoers.d/010_pi-nopasswd").exists()
    assert (p.root / "etc/hyperwheel/setup-done").exists()
    assert (p.root / "etc/sudoers.d/hyperwheel").read_text() == sudo_rule(True)
    assert any("Password-free sudo is gone" in s for s in said)
    assert not finish_waiting_setup(layout, run=p.run, say=said.append)  # done once


def test_the_updater_never_finishes_a_setup_that_stopped_before_the_admin_account(tmp_path):
    p = FakePi(tmp_path / "pi")
    (p.root / "etc/hyperwheel/setup.json").write_text(json.dumps({"started_at": "x", "site_file_at": "x"}))
    assert not finish_waiting_setup(Layout(p.root), run=p.run, say=lambda s: None)
    assert (p.root / "etc/sudoers.d/010_pi-nopasswd").exists() and p.ran == []


# ---- the Mac command ----------------------------------------------------------------------------------


def publish_tool():
    spec = importlib.util.spec_from_file_location("publish_tool", REPO / "tools/release/publish.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_the_mac_command_shows_passwords_until_one_is_taken_then_needs_it_typed_back(tmp_path, monkeypatch):
    tool = publish_tool()
    made = iter(["borak-kefim-tumos", "dafog-hupen-kisor"])
    monkeypatch.setattr(tool.passwords, "readable", lambda: next(made))
    monkeypatch.setattr(tool.passwords, "ROUNDS", FAST)
    answers = iter(["", "yes", "dafog-hupen-kiso", "Dafog-Hupen-Kisor"])
    shown = []
    code = tool.cmd_admin_file(Args(out=tmp_path / "admin"), ask=lambda prompt: next(answers), say=shown.append,
                               published=lambda: AdminFile(4, hashed("old"), ""))
    assert code == 0
    written = parse_admin((tmp_path / "admin" / ADMIN_FILE).read_text())
    assert written.serial == 5 and passwords.matches("dafog-hupen-kisor", written.hash)
    assert "dafog-hupen-kisor" not in (tmp_path / "admin" / ADMIN_FILE).read_text()
    assert tool.CLEAR_SCREEN in shown and "ssh-keygen -Y sign" in "\n".join(shown)
    assert sum("dafog-hupen-kisor" in s for s in shown) == 2  # shown, and once more after a wrong type-back


def test_the_mac_command_writes_nothing_if_the_password_is_not_typed_back(tmp_path, monkeypatch):
    tool = publish_tool()
    monkeypatch.setattr(tool.passwords, "readable", lambda: "borak-kefim-tumos")
    answers = iter(["yes", *["wrong"] * 5])
    code = tool.cmd_admin_file(Args(out=tmp_path / "admin"), ask=lambda prompt: next(answers), say=lambda s: None,
                               published=lambda: None)
    assert code == 1 and not (tmp_path / "admin").exists()


def test_publishing_refuses_a_file_no_newer_than_the_published_one(tmp_path):
    tool = publish_tool()
    ch = Channel(tmp_path)
    ch.publish(3, hashed("x"))
    folder = ch.site[f"{BASE}/admin-password/{ADMIN_FILE}"].parent
    code = tool.cmd_admin(Args(folder=folder, signers=ch.signers, dry_run=False),
                          published=lambda: AdminFile(3, hashed("y"), ""))
    assert code == 1
