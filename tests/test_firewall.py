"""The firewall's rules and the routing switch. tools/firewall/test-firewall.sh tries them for real (CI, Linux)."""

from __future__ import annotations

import pytest

from hyperwheel import cli, firewall
from hyperwheel.installer import UNITS


def test_the_pi_itself_is_reached_only_on_what_people_use():
    text = firewall.rules(routing=True)
    assert "type filter hook input priority filter; policy drop;" in text
    assert 'iifname "wlan0" ip saddr 192.168.99.0/24 tcp dport { 22, 5000, 8443 } accept' in text
    assert 'iifname "eth0" ip saddr 10.42.0.0/24 tcp dport 22 accept' in text
    assert 'iifname "eth0" ip saddr 10.42.0.1 tcp dport 4242 accept' in text
    assert "8042" not in text  # Orthanc's REST API: the Pi only
    assert text.count("4242") == 1


def test_routing_is_a_switch_and_nothing_starts_from_the_scanner_side():
    on, off = firewall.rules(routing=True), firewall.rules(routing=False)
    assert 'iifname "wlan0" oifname "eth0" ip saddr 192.168.99.0/24 accept' in on
    assert 'oifname "eth0"' not in off and "routing: off" in off
    for text in (on, off):
        assert "type filter hook forward priority filter; policy drop;" in text
        assert 'iifname "eth0" oifname "wlan0"' not in text


def test_the_rules_replace_the_table_in_one_step():
    lines = firewall.rules(routing=True).splitlines()
    assert lines[1:4] == ["add table inet hyperwheel", "delete table inet hyperwheel", "table inet hyperwheel {"]


def site_toml(tmp_path, extra=""):
    path = tmp_path / "site.toml"
    path.write_text('schema_version = 1\nsite = "t"\nstaging_dir = "/x"\nstate_db = "/x.db"\nkeys_dir = "/k"\n' + extra)
    return path


def test_the_routing_setting(tmp_path):
    assert firewall.routing_setting(tmp_path / "missing.toml") is True  # before setup
    assert firewall.routing_setting(site_toml(tmp_path)) is True  # the default
    assert firewall.routing_setting(site_toml(tmp_path, "[hotspot]\nrouting = false\n")) is False


def test_a_site_toml_that_cant_be_read_keeps_routing_on_and_still_loads_a_firewall(tmp_path, capsys):
    path = site_toml(tmp_path, "[hotspot]\nrouting = 'no'\n")
    assert firewall.routing_setting(path) is True
    assert firewall.main(["--print"], site_toml=path) == 0
    assert "policy drop" in capsys.readouterr().out


def test_a_failed_load_is_reported(monkeypatch, tmp_path):
    def refuse(text):
        raise RuntimeError("Error: Could not process rule")
    monkeypatch.setattr(firewall, "load", refuse)
    assert firewall.main([], site_toml=tmp_path / "missing.toml") == 1


@pytest.mark.parametrize("words", [["--open"], ["--print", "x"]])
def test_only_print_is_an_option(words):
    assert firewall.main(words) == 2


def test_the_unit_runs_before_the_network_and_reloads_in_one_step():
    unit = UNITS["hyperwheel-firewall.service"]
    assert "Before=network-pre.target" in unit and "Wants=network-pre.target" in unit
    assert "DefaultDependencies=no" in unit and "RemainAfterExit=yes" in unit
    assert "ExecReload=/opt/hyperwheel/current/venv/bin/hyperwheel firewall" in unit
    assert cli.problem(["firewall"]) is None and "firewall" not in cli.help_text(True, False)


def test_ssh_slows_down_wrong_passwords_and_never_locks_out():
    assert "PerSourcePenalties authfail:10s min:10s max:60s" in firewall.SSHD_SETTINGS
    assert "MaxAuthTries 3" in firewall.SSHD_SETTINGS
    assert firewall.SSHD_DROP_IN.endswith("/05-hyperwheel.conf")  # read before cloud-init's 50- file
