"""The installer, against a scratch folder standing in for the Pi, with fake system commands."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest
from test_release import keypair, sign, signers_file

from hyperwheel.installer import InstallError, Installer, Layout
from hyperwheel.release import FLAGS, MANIFEST, Manifest, ReleaseFile, sha256_file

DEBS = {"orthanc_1.12.7+dfsg-4_arm64.deb": ("orthanc", "1.12.7+dfsg-4"),
        "libdcmtk19_3.6.9-5+deb13u3_arm64.deb": ("libdcmtk19", "3.6.9-5+deb13u3"),
        "libssl3t64_3.5.9-1~deb13u1+rpt1_arm64.deb": ("libssl3t64", "3.5.9-1~deb13u1+rpt1")}


def make_release(tmp: Path, key: Path, version: str = "0.1.0", oe2: bool = True) -> Path:
    folder = tmp / f"release-{version}"
    if folder.exists():
        return folder
    (folder / "debs").mkdir(parents=True)
    (folder / "wheels").mkdir()
    for name in DEBS:
        (folder / "debs" / name).write_bytes(name.encode() * 10)
    (folder / "wheels" / f"hyperwheel-{version}-py3-none-any.whl").write_bytes(b"wheel")
    if oe2:
        (folder / "oe2").mkdir()
        (folder / "oe2" / "libOrthancExplorer2.so").write_bytes(b"\x7fELF")
        (folder / "oe2" / "VERSION").write_text("1.14.1\n")
    files = tuple(ReleaseFile(p.relative_to(folder).as_posix(), p.stat().st_size, sha256_file(p))
                  for p in sorted(folder.rglob("*")) if p.is_file())
    m = Manifest(version=version, channel="dev", built_at="2026-11-06T12:00:00Z", min_image=None,
                 flags={f: False for f in FLAGS}, components={}, files=files)
    (folder / MANIFEST).write_text(m.to_json())
    sign(key, folder)
    return folder


class FakePi:
    """Records the system commands; knows which accounts and packages exist."""

    def __init__(self):
        self.commands: list[list[str]] = []
        self.accounts: dict[str, tuple[int, str]] = {"orthanc": (109, "/bin/bash")}
        self.packages: dict[str, str] = {}

    def run(self, cmd, env=None):
        self.commands.append(list(cmd))
        if cmd[0] == "dpkg-deb":
            package, version = DEBS[Path(cmd[2]).name]
            return f"Package: {package}\nVersion: {version}\n"
        if cmd[0] == "dpkg-query":
            if cmd[-1] not in self.packages:
                raise InstallError("not installed")
            return self.packages[cmd[-1]]
        if cmd[:2] == ["dpkg", "--compare-versions"]:
            if not cmd[2] > cmd[4]:  # plain string order: good enough for these made-up versions
                raise InstallError("not greater")
            return ""
        if cmd[0] == "openssl":
            Path(cmd[cmd.index("-keyout") + 1]).write_text("KEY")
            Path(cmd[cmd.index("-out") + 1]).write_text("CERT")
        if cmd[0] == "useradd":
            self.accounts[cmd[-1]] = (998, "/usr/sbin/nologin")
        if cmd[0] == "apt-get":
            for path in cmd:
                if path.endswith(".deb"):
                    package, version = DEBS[Path(path).name]
                    self.packages[package] = version
        if cmd[1:3] == ["-m", "venv"]:
            Path(cmd[3], "bin").mkdir(parents=True, exist_ok=True)
        if cmd[0].endswith("/pip"):
            Path(cmd[0]).parent.joinpath("hyperwheel").write_text("#!/bin/sh\n")
        return ""

    def getpwnam(self, name):
        if name not in self.accounts:
            raise KeyError(name)
        uid, shell = self.accounts[name]
        return pwd.struct_passwd((name, "x", uid, uid, "", "/", shell))

    def ran(self, program):
        return [c for c in self.commands if c[0].endswith(program)]


@pytest.fixture
def key(tmp_path):
    return keypair(tmp_path, "signer")


@pytest.fixture
def signers(tmp_path, key):
    return signers_file(tmp_path, ("release-signer-1", key))


@pytest.fixture
def pi(tmp_path):
    root = tmp_path / "pi"
    root.mkdir()
    return root, FakePi()


def installer(release, signers, pi, said=None, **kw):
    root, fake = pi
    return Installer(release, signers, Layout(root), run=fake.run, getpwnam=fake.getpwnam,
                     chown=lambda path, user, group: None, say=(said.append if said is not None else lambda s: None),
                     **kw)


GRIP_TOML = '[grip]\napi_base_url = "https://grip.example.org/api"\ntoken_url = "https://grip.example.org/api/get/token"\n'


def site_setup(tmp: Path) -> Path:
    setup = tmp / "setup"
    (setup / "keys").mkdir(parents=True, exist_ok=True)
    (setup / "site.toml").write_text('schema_version = 1\nsite = "site-a"\n[grip]\n'
                                     'api_base_url = "https://grip.example.org/api"\n'
                                     'token_url = "https://grip.example.org/api/get/token"\n')
    (setup / "routing.toml").write_text("schema_version = 1\n")
    (setup / "scanner.json").write_text('{"password": "scanner-pw-not-for-logs"}')
    (setup / "keys" / "SiteA-Sandbox.json").write_text('{"client_id": "c", "client_secret": "key-not-for-logs"}')
    return setup


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_first_install_on_a_fresh_pi(tmp_path, key, signers, pi):
    root, fake = pi
    said = []
    installer(make_release(tmp_path, key), signers, pi, said).install()
    assert fake.ran("useradd")[0][-1] == "hyperwheel" and "--system" in fake.ran("useradd")[0]
    apt = fake.ran("apt-get")[0]
    assert sum(a.endswith(".deb") for a in apt) == 3  # a first release: its OS updates too
    # apt sees no repository, or it would prefer a repository's copy of the same version and need the network
    assert {"Dir::Etc::sourcelist=/dev/null", "Dir::Etc::sourceparts=/dev/null"} <= set(apt)
    assert any(a.startswith("Dir::State::lists=") and "/var/lib/apt" not in a for a in apt)
    assert any(a.startswith("Dir::Cache=") and "/var/cache/apt" not in a for a in apt)
    assert "--no-download" not in apt  # apt stops with an internal error when it's combined with no sources
    rel = root / "opt/hyperwheel/releases/0.1.0"
    assert (rel / "release" / "manifest.json").exists() and mode(rel / "release" / "debs") == 0o755
    assert os.readlink(root / "opt/hyperwheel/current") == "releases/0.1.0"
    assert not (root / "opt/hyperwheel/previous").exists()
    pip = fake.ran("pip")[0]
    assert "--no-index" in pip and "hyperwheel==0.1.0" in pip
    settings = json.loads((rel / "orthanc.json").read_text())
    assert settings["Plugins"] == ["/opt/hyperwheel/releases/0.1.0/release/oe2/libOrthancExplorer2.so"]
    assert settings["OrthancExplorerEnabled"] is False and settings["DicomAet"] == "HYPERWHEEL"
    assert settings["RemoteAccessAllowed"] is False and settings["AuthenticationEnabled"] is True
    login = root / "etc/hyperwheel/orthanc.json"
    assert mode(login) == 0o600 and json.loads(login.read_text())["username"] == "hyperwheel"
    users = json.loads((root / "etc/hyperwheel/orthanc.d/users.json").read_text())["RegisteredUsers"]
    assert users == {"hyperwheel": json.loads(login.read_text())["password"]}
    assert os.readlink(root / "etc/hyperwheel/orthanc.d/hyperwheel.json") == "/opt/hyperwheel/current/orthanc.json"
    unit = (root / "etc/systemd/system/hyperwheel.service").read_text()
    assert "User=hyperwheel" in unit and "LoadCredential=keys:/etc/hyperwheel/keys" in unit
    assert "/etc/hyperwheel/orthanc.d" in (root / "etc/systemd/system/orthanc.service.d/hyperwheel.conf").read_text()
    assert ["systemctl", "restart", "orthanc"] in fake.commands
    assert not [c for c in fake.commands if "hyperwheel.service" in c]  # not set up: not started
    assert any("isn't set up yet" in s for s in said)


def test_site_setup_adds_only_what_is_missing_and_never_prints_secrets(tmp_path, key, signers, pi):
    root, fake = pi
    etc = root / "etc/hyperwheel"
    etc.mkdir(parents=True)
    own = "schema_version = 1\nsite = \"already-here\"\n[grip]\napi_base_url = \"https://grip.example.org/api\"\ntoken_url = \"https://grip.example.org/api/get/token\"\n"
    (etc / "site.toml").write_text(own)  # the site's own
    said = []
    installer(make_release(tmp_path, key), signers, pi, said, site_setup=site_setup(tmp_path)).install()
    assert (etc / "site.toml").read_text() == own
    assert (etc / "routing.toml").exists() and mode(etc / "scanner.json") == 0o600
    assert mode(etc / "keys" / "SiteA-Sandbox.json") == 0o600 and mode(etc / "keys") == 0o700
    text = "\n".join(said)
    assert "site.toml" in text and "kept" in text
    for secret in ("scanner-pw-not-for-logs", "key-not-for-logs", json.loads((etc / "orthanc.json").read_text())["password"]):
        assert secret not in text
    assert ["systemctl", "restart", "hyperwheel.service", "hyperwheel-dashboard.service"] in fake.commands


def test_update_checks_are_switched_on_and_the_release_signers_kept_for_the_updater(tmp_path, key, signers, pi):
    root, fake = pi
    installer(make_release(tmp_path, key), signers, pi).install()
    units = root / "etc/systemd/system"
    assert "ExecStart=/opt/hyperwheel/current/venv/bin/hyperwheel update check" in \
        (units / "hyperwheel-update.service").read_text()
    assert "OnUnitInactiveSec=6h" in (units / "hyperwheel-update.timer").read_text()
    services = ["hyperwheel-update.timer", "hyperwheel-connect.service", "hyperwheel-viewer.service"]
    assert ["systemctl", "enable", *services] in fake.commands
    assert ["systemctl", "restart", *services] in fake.commands  # running ones take the new code
    assert os.readlink(root / "usr/local/bin/hyperwheel") == "/opt/hyperwheel/current/venv/bin/hyperwheel"
    trusted = root / "etc/hyperwheel/release_allowed_signers"
    assert trusted.read_bytes() == signers.read_bytes() and mode(trusted) == 0o644
    (tmp_path / "other").mkdir()
    other = signers_file(tmp_path / "other", ("someone-else", key))
    installer(make_release(tmp_path, key, "0.2.0"), other, pi).install()
    assert trusted.read_bytes() == signers.read_bytes()  # an update never replaces who may sign releases


def test_the_kept_list_names_every_site_file_including_keys(tmp_path, key, signers, pi):
    root, _ = pi
    installer(make_release(tmp_path, key, "0.1.0"), signers, pi, site_setup=site_setup(tmp_path)).install()
    said = []
    installer(make_release(tmp_path, key, "0.2.0"), signers, pi, said, site_setup=site_setup(tmp_path)).install()
    kept = next(s for s in said if s.startswith("Site settings kept as they are:"))
    for name in ("site.toml", "routing.toml", "scanner.json", "orthanc.json", "keys/SiteA-Sandbox.json"):
        assert name in kept
    assert not any(s.startswith("Site settings added") for s in said)
    assert "key-not-for-logs" not in "\n".join(said)


def test_installing_the_same_release_again_changes_nothing(tmp_path, key, signers, pi):
    root, fake = pi
    release = make_release(tmp_path, key)
    installer(release, signers, pi, site_setup=site_setup(tmp_path)).install()
    login_before = (root / "etc/hyperwheel/orthanc.json").read_bytes()
    fake.commands.clear()
    said = []
    installer(release, signers, pi, said, site_setup=site_setup(tmp_path)).install()
    assert not fake.ran("useradd") and not fake.ran("apt-get") and not fake.ran("pip")
    assert (root / "etc/hyperwheel/orthanc.json").read_bytes() == login_before
    assert any("already" in s for s in said)


def test_update_keeps_previous_and_every_site_file(tmp_path, key, signers, pi):
    root, fake = pi
    installer(make_release(tmp_path, key, "0.1.0"), signers, pi, site_setup=site_setup(tmp_path)).install()
    etc = root / "etc/hyperwheel"
    before = {p.relative_to(etc): p.read_bytes() for p in etc.rglob("*") if p.is_file() and "orthanc.d" not in p.parts}
    installer(make_release(tmp_path, key, "0.2.0"), signers, pi, site_setup=site_setup(tmp_path)).install()
    after = {p.relative_to(etc): p.read_bytes() for p in etc.rglob("*") if p.is_file() and "orthanc.d" not in p.parts}
    assert after == before  # settings, keys, scanner login and Orthanc login untouched
    assert os.readlink(root / "opt/hyperwheel/current") == "releases/0.2.0"
    assert os.readlink(root / "opt/hyperwheel/previous") == "releases/0.1.0"
    assert (root / "opt/hyperwheel/releases/0.1.0").is_dir()


def test_an_existing_login_account_called_hyperwheel_is_never_taken_over(tmp_path, key, signers, pi):
    root, fake = pi
    fake.accounts["hyperwheel"] = (1000, "/bin/bash")  # as on the dev Pi
    with pytest.raises(InstallError, match="won't be taken over"):
        installer(make_release(tmp_path, key), signers, pi).install()
    assert not fake.ran("apt-get") and not (root / "opt").exists()
    installer(make_release(tmp_path, key), signers, pi, service_user="hwsvc").install()
    assert fake.ran("useradd")[0][-1] == "hwsvc"
    assert "User=hwsvc" in (root / "etc/systemd/system/hyperwheel.service").read_text()


def test_a_tampered_release_is_refused_before_anything_changes(tmp_path, key, signers, pi):
    root, fake = pi
    release = make_release(tmp_path, key)
    (release / "debs" / "orthanc_1.12.7+dfsg-4_arm64.deb").write_bytes(b"tampered")
    with pytest.raises(InstallError, match="can't be used"):
        installer(release, signers, pi).install()
    assert fake.commands == [] and not (root / "opt").exists() and not (root / "etc").exists()


def test_release_without_oe2_loads_no_plugins(tmp_path, key, signers, pi):
    root, _ = pi
    installer(make_release(tmp_path, key, oe2=False), signers, pi).install()
    assert json.loads((root / "opt/hyperwheel/releases/0.1.0/orthanc.json").read_text())["Plugins"] == []


def test_install_script_runs_the_installer_from_the_release_wheel():
    script = (Path(__file__).resolve().parents[1] / "tools" / "release" / "install.sh").read_text()
    assert "PYTHONPATH=\"$whl\"" in script and "-m hyperwheel.installer" in script
    assert subprocess.run(["sh", "-n", str(Path(__file__).resolve().parents[1] / "tools/release/install.sh")]).returncode == 0


def test_the_data_manager_sudo_rule_is_checked_by_visudo_then_put_in_place(tmp_path, key, signers, pi):
    from hyperwheel.installer import SUDO_RULE
    root, fake = pi
    installer(make_release(tmp_path, key), signers, pi, site_setup=site_setup(tmp_path)).install()
    rule = root / "etc/sudoers.d/hyperwheel"
    assert rule.read_text() == SUDO_RULE and mode(rule) == 0o440
    check = fake.ran("visudo")[0]
    assert check[:4] == ["visudo", "-c", "-q", "-f"] and check[4].endswith("/etc/sudoers.d/.hyperwheel.new")
    assert not (root / "etc/sudoers.d/.hyperwheel.new").exists()
    lines = [line for line in SUDO_RULE.splitlines() if line.startswith("datamanager")]
    assert lines == ["datamanager ALL = (root) NOPASSWD: HYPERWHEEL_LOOK", "datamanager ALL = (root) HYPERWHEEL_KEYS"]
    assert "ALL = (root) ALL" not in SUDO_RULE and "*" not in SUDO_RULE
    # the commands that change something ask for the data manager's password
    asks = SUDO_RULE.split("Cmnd_Alias HYPERWHEEL_KEYS = ")[1].split("\n" + "datamanager")[0]
    assert " ".join(asks.replace("\\", "").split()) == (
        "/usr/local/bin/hyperwheel datasets add, /usr/local/bin/hyperwheel datasets change, "
        "/usr/local/bin/hyperwheel datasets remove, /usr/local/bin/hyperwheel scanner password, "
        "/usr/local/bin/hyperwheel grip address")


def test_a_sudo_rule_visudo_rejects_is_never_put_in_place(tmp_path, key, signers, pi):
    root, fake = pi
    real_run = fake.run

    def run(cmd, env=None):
        if cmd[0] == "visudo":
            raise InstallError("visudo failed (exit 1): syntax error")
        return real_run(cmd, env)
    fake.run = run
    with pytest.raises(InstallError, match="sudo was left as it was"):
        installer(make_release(tmp_path, key), signers, pi).install()
    assert not (root / "etc/sudoers.d/hyperwheel").exists()
    assert not (root / "etc/sudoers.d/.hyperwheel.new").exists()
    assert not (root / "opt/hyperwheel/current").exists()  # stopped before the switch


SETUP_LINE = "datamanager ALL = (root) NOPASSWD: /usr/local/bin/hyperwheel setup"


def test_a_pi_that_is_not_set_up_lets_the_data_manager_run_setup_without_a_password(tmp_path, key, signers, pi):
    root, _ = pi
    installer(make_release(tmp_path, key), signers, pi).install()
    rule = (root / "etc/sudoers.d/hyperwheel").read_text()
    assert SETUP_LINE in rule.splitlines()
    # half way through setup (site settings written, not finished), it still may
    (root / "etc/hyperwheel/setup.json").write_text("{}")
    (root / "etc/hyperwheel/site.toml").write_text(GRIP_TOML)
    installer(make_release(tmp_path, key, "0.2.0"), signers, pi).install()
    assert SETUP_LINE in (root / "etc/sudoers.d/hyperwheel").read_text().splitlines()
    # once setup has finished, an update leaves the line out
    (root / "etc/hyperwheel/setup-done").write_text("")
    installer(make_release(tmp_path, key, "0.3.0"), signers, pi).install()
    assert SETUP_LINE not in (root / "etc/sudoers.d/hyperwheel").read_text()


def test_a_pi_set_up_before_setup_existed_never_gets_the_setup_line(tmp_path, key, signers, pi):
    root, _ = pi
    installer(make_release(tmp_path, key), signers, pi, site_setup=site_setup(tmp_path)).install()
    assert SETUP_LINE not in (root / "etc/sudoers.d/hyperwheel").read_text()


def test_image_mode_starts_nothing_and_leaves_the_login_to_first_boot(tmp_path, key, signers, pi):
    root, fake = pi
    said = []
    installer(make_release(tmp_path, key), signers, pi, said, image=True, start=False).install()
    assert fake.ran("apt-get") and fake.ran("pip")  # the same install
    systemctl = fake.ran("systemctl")
    assert systemctl == [["systemctl", "enable", "hyperwheel-firstboot.service", "hyperwheel-update.timer",
                          "hyperwheel-connect.service", "hyperwheel-dashboard.service", "hyperwheel-viewer.service",
                          "hyperwheel-firewall.service"]]
    assert not (root / "etc/hyperwheel/viewer").exists()  # each Pi makes its own certificate at first boot
    assert not (root / "etc/hyperwheel/orthanc.json").exists()
    assert not (root / "etc/hyperwheel/orthanc.d/users.json").exists()
    assert (root / "etc/hyperwheel/orthanc.d/hyperwheel.json").is_symlink()
    assert (root / "etc/hyperwheel/release_allowed_signers").exists()
    assert (root / "etc/systemd/system/hyperwheel-firstboot.service").exists()
    assert "hyperwheel setup" in (root / "etc/sudoers.d/hyperwheel").read_text()  # not set up yet


def test_a_normal_install_never_enables_first_boot(tmp_path, key, signers, pi):
    root, fake = pi
    installer(make_release(tmp_path, key), signers, pi, []).install()
    assert not any("hyperwheel-firstboot.service" in c for c in fake.ran("systemctl"))
    assert (root / "etc/hyperwheel/orthanc.json").exists()


def test_the_upload_monitor_writes_nothing_and_an_old_pasted_connect_key_is_deleted(tmp_path, key, signers, pi):
    root, fake = pi
    var = root / "var/lib/hyperwheel"
    (var / "connect" / "inbox").mkdir(parents=True)  # left by a release before 0.1.15
    (var / "connect" / "inbox" / "key").write_text("rpoak_test0123456789")
    (var / "connect" / "state.json").write_text("{}")
    installer(make_release(tmp_path, key), signers, pi, []).install()
    assert mode(var / "connect") == 0o755 and not (var / "connect" / "inbox").exists()
    assert (var / "connect" / "state.json").exists()
    unit = (root / "etc/systemd/system/hyperwheel-dashboard.service").read_text()
    assert "ReadWritePaths" not in unit and "ProtectSystem=strict" in unit
    # The Pi isn't set up, so a running not-set-up page is restarted to show this release's page (no key box).
    assert ["systemctl", "try-restart", "hyperwheel-dashboard.service"] in fake.ran("systemctl")


def test_each_pi_makes_its_own_viewer_certificate_once(tmp_path, key, signers, pi):
    root, fake = pi
    installer(make_release(tmp_path, key), signers, pi, []).install()
    viewer = root / "etc/hyperwheel/viewer"
    assert mode(viewer / "key.pem") == 0o600 and mode(viewer / "cert.pem") == 0o644
    assert not (viewer / ".new").exists()
    openssl = fake.ran("openssl")[0]
    assert "subjectAltName=IP:192.168.99.1,IP:127.0.0.1" in openssl and "ec_paramgen_curve:prime256v1" in openssl
    installer(make_release(tmp_path, key, version="0.2.0"), signers, pi, []).install()
    assert len(fake.ran("openssl")) == 1  # an update never replaces it


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_the_real_certificate_is_for_the_hotspot_and_the_pi_itself(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import ec

    from hyperwheel.installer import run_command
    layout = Layout(tmp_path)
    made = Installer(Path("."), Path("."), layout, run=run_command, chown=lambda *a: None).viewer_certificate()
    assert made
    cert = x509.load_pem_x509_certificate((tmp_path / "etc/hyperwheel/viewer/cert.pem").read_bytes())
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert sorted(str(a) for a in names.get_values_for_type(x509.IPAddress)) == ["127.0.0.1", "192.168.99.1"]
    assert isinstance(cert.public_key(), ec.EllipticCurvePublicKey) and cert.public_key().curve.name == "secp256r1"
    assert (cert.not_valid_after_utc - cert.not_valid_before_utc).days >= 3649
    assert not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    import ssl
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(tmp_path / "etc/hyperwheel/viewer/cert.pem",
                                                            tmp_path / "etc/hyperwheel/viewer/key.pem")


def test_ssh_settings_go_in_only_if_sshd_accepts_them(tmp_path, key, signers, pi):
    root, fake = pi
    drop_in = root / "etc/ssh/sshd_config.d/05-hyperwheel.conf"
    drop_in.parent.mkdir(parents=True)
    installer(make_release(tmp_path, key), signers, pi, []).install()
    assert "PerSourcePenalties" in drop_in.read_text() and mode(drop_in) == 0o644
    assert fake.ran("sshd") == [["sshd", "-t"]] and ["systemctl", "reload", "ssh"] in fake.commands
    assert ["systemctl", "reload-or-restart", "hyperwheel-firewall.service"] in fake.commands

    drop_in.write_text("# an older version\n")
    real_run = fake.run

    def sshd_refuses(cmd, env=None):
        if cmd[0] == "sshd":
            raise InstallError("sshd failed (exit 255): bad option")
        return real_run(cmd, env)

    said = []
    Installer(make_release(tmp_path, key, version="0.2.0"), signers, Layout(root), run=sshd_refuses,
              getpwnam=fake.getpwnam, chown=lambda *a: None, say=said.append).install()
    assert drop_in.read_text() == "# an older version\n"  # put back as it was
    assert any("SSH settings: not changed" in s for s in said)


def test_a_pi_with_no_ssh_server_is_left_alone(tmp_path, key, signers, pi):
    root, fake = pi
    installer(make_release(tmp_path, key), signers, pi, []).install()
    assert not fake.ran("sshd") and not (root / "etc/ssh").exists()


def test_an_update_leaves_the_os_updates_for_after_it_is_accepted_and_an_image_installs_them(tmp_path, key, signers, pi):
    root, fake = pi
    said = []
    fake.packages["libssl3t64"] = "3.5.7-1~deb13u3+rpt1"
    (root / "opt/hyperwheel/releases/0.0.9").mkdir(parents=True)
    (root / "opt/hyperwheel/current").symlink_to("releases/0.0.9")  # an update: the Pi has a release already
    installer(make_release(tmp_path, key), signers, pi, said).install()
    debs = [a for a in fake.ran("apt-get")[0] if a.endswith(".deb")]
    assert len(debs) == 2 and not any("libssl3t64" in d for d in debs)
    assert fake.packages["libssl3t64"] == "3.5.7-1~deb13u3+rpt1"
    assert any("wait until this release has been accepted" in s for s in said)


def test_an_image_build_installs_the_os_updates_straight_away(tmp_path, key, signers, pi):
    root, fake = pi
    fake.packages["libssl3t64"] = "3.5.7-1~deb13u3+rpt1"
    installer(make_release(tmp_path, key), signers, pi, [], image=True).install()
    assert fake.packages["libssl3t64"] == "3.5.9-1~deb13u1+rpt1"


def test_an_os_package_is_never_downgraded():
    from hyperwheel.installer import plan_debs
    fake = FakePi()
    folder = Path("/release/debs")
    fake.packages.update({"libssl3t64": "3.6.0-1", "orthanc": "1.12.8"})
    import types
    names = list(DEBS)
    others, os_debs = plan_debs(types.SimpleNamespace(glob=lambda pattern: [folder / n for n in names]), fake.run)
    assert os_debs == []  # 3.6.0 is newer than the release's 3.5.9
    assert str(folder / "orthanc_1.12.7+dfsg-4_arm64.deb") in others  # other packages follow the release, as before


def test_a_pis_first_release_installs_the_os_updates_straight_away(tmp_path, key, signers, pi):
    root, fake = pi
    fake.packages["libssl3t64"] = "3.5.7-1~deb13u3+rpt1"
    installer(make_release(tmp_path, key), signers, pi, []).install()  # no release on the Pi yet
    assert fake.packages["libssl3t64"] == "3.5.9-1~deb13u1+rpt1"


def _old_release_with_grip_built_in(root: Path, version: str = "0.1.16") -> None:
    """What a release before 0.1.17 looks like where the GRIP address is concerned (made-up address)."""
    config = root / f"opt/hyperwheel/releases/{version}/venv/lib/python3.13/site-packages/hyperwheel/config.py"
    config.parent.mkdir(parents=True)
    config.write_text('GRIP_API_BASE_URL = "https://grip-old.example.org/api"\n'
                      'GRIP_TOKEN_URL = "https://grip-old.example.org/api/get/token"\n'
                      'raise SystemExit("never run")\n')


def test_an_update_keeps_the_grip_address_the_pi_used_in_its_site_settings(tmp_path, key, signers, pi):
    root, fake = pi
    installer(make_release(tmp_path, key, "0.1.16"), signers, pi, site_setup=site_setup(tmp_path)).install()
    _old_release_with_grip_built_in(root)
    site = root / "etc/hyperwheel/site.toml"
    old = 'schema_version = 1\nsite = "site-a"  # a comment kept\n\n[hotspot]\nrouting = false\n'
    site.write_text(old)  # set up before the address was a site setting
    said = []
    installer(make_release(tmp_path, key, "0.1.17"), signers, pi, said).install()
    text = site.read_text()
    assert text.startswith(old) and mode(site) == 0o644  # nothing else in the file changes
    added = tomllib.loads(text)
    assert added["grip"] == {"api_base_url": "https://grip-old.example.org/api",
                             "token_url": "https://grip-old.example.org/api/get/token"}
    assert added["hotspot"] == {"routing": False}
    assert "grip-old.example.org" not in "\n".join(said)  # site settings are never printed
    assert os.readlink(root / "opt/hyperwheel/current") == "releases/0.1.17"
    installer(make_release(tmp_path, key, "0.1.18"), signers, pi).install()
    assert site.read_text() == text  # added once, then kept as it is


def test_with_no_grip_address_to_keep_the_install_stops_before_anything_changes(tmp_path, key, signers, pi):
    root, fake = pi
    installer(make_release(tmp_path, key, "0.1.15"), signers, pi, site_setup=site_setup(tmp_path)).install()
    site = root / "etc/hyperwheel/site.toml"
    site.write_text('schema_version = 1\nsite = "site-a"\n')  # and 0.1.15 had no address built in
    ran = len(fake.commands)
    with pytest.raises(InstallError, match=r"no \[grip\] section"):
        installer(make_release(tmp_path, key, "0.1.17"), signers, pi).install()
    assert len(fake.commands) == ran and not (root / "opt/hyperwheel/releases/0.1.17").exists()
    assert os.readlink(root / "opt/hyperwheel/current") == "releases/0.1.15"
    assert site.read_text() == 'schema_version = 1\nsite = "site-a"\n'
