"""The Pi's firewall: one nftables table, ``inet hyperwheel``, loaded by ``hyperwheel-firewall.service``
before the network comes up. Run as root.

Into the Pi (everything else is dropped):

- **from the hotspot** (``wlan0``, 192.168.99.0/24): SSH, the Upload Monitor (5000), Orthanc Explorer 2 (8443),
  and the DHCP and DNS the hotspot needs. Never Orthanc's DICOM port or its REST API;
- **from the scanner cable** (``eth0``, 10.42.0.0/24): SSH, and DICOM (4242) from the scanner (10.42.0.1) only;
- ping, and replies to the Pi's own connections, from both.

Through the Pi: hotspot devices may reach the scanner and the internet beyond it, unless the site turns that
off (``[hotspot] routing = false`` in site.toml). Nothing from the scanner side may start a connection
towards the hotspot.

NetworkManager keeps its own tables for the hotspot (its NAT, DHCP and DNS). A packet must get through every
table, so this one has the last word without touching NetworkManager's.

New SSH connections are limited per address, and sshd slows down wrong passwords itself (``SSHD_SETTINGS``,
written by the installer). Never a lockout.

    hyperwheel firewall          load the rules for this site's settings
    hyperwheel firewall --print  show them (also without root)
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from .config import ConfigError, HotspotSettings, load_site

log = logging.getLogger("hyperwheel.firewall")

HOTSPOT, CABLE = "wlan0", "eth0"
HOTSPOT_NET, CABLE_NET, SCANNER = "192.168.99.0/24", "10.42.0.0/24", "10.42.0.1"
SSH, MONITOR, VIEWER, DICOM = 22, 5000, 8443, 4242
# New SSH connections from one address. A person never comes near this. A script guessing passwords does.
SSH_NEW_PER_MINUTE, SSH_BURST = 20, 30

# sshd's own slowdown for wrong passwords: an address that keeps failing waits a little longer each time, up
# to a minute. Only on connections that fail, so a person who knows the password isn't held up.
SSHD_SETTINGS = """\
# Written by Hyperwheel (each release). Changes made here are lost.
# Read before the other files here, so these win.
PermitRootLogin no
MaxAuthTries 3
LoginGraceTime 30
PerSourcePenalties authfail:10s min:10s max:60s
"""
SSHD_DROP_IN = "/etc/ssh/sshd_config.d/05-hyperwheel.conf"


def rules(routing: bool) -> str:
    """The table, replaced in one step (``nft -f``): there's never a moment with no firewall."""
    forward = (f'        iifname "{HOTSPOT}" oifname "{CABLE}" ip saddr {HOTSPOT_NET} accept\n' if routing else
               "        # Hotspot devices don't reach the scanner or the internet through this Pi (site setting).\n")
    return f"""\
# Hyperwheel's firewall (hyperwheel firewall). Hotspot routing: {"on" if routing else "off"}.
add table inet hyperwheel
delete table inet hyperwheel
table inet hyperwheel {{
    set ssh_new {{
        type ipv4_addr
        size 4096
        flags dynamic, timeout
        timeout 2m
    }}

    chain input {{
        type filter hook input priority filter; policy drop;
        ct state established,related accept
        ct state invalid drop
        iifname "lo" accept
        meta l4proto ipv6-icmp accept
        iifname {{ "{HOTSPOT}", "{CABLE}" }} icmp type echo-request accept

        iifname "{HOTSPOT}" udp dport {{ 53, 67 }} accept
        iifname "{HOTSPOT}" tcp dport 53 ip saddr {HOTSPOT_NET} accept
        iifname "{CABLE}" udp dport 68 accept

        tcp dport {SSH} ct state new add @ssh_new {{ ip saddr limit rate over {SSH_NEW_PER_MINUTE}/minute \
burst {SSH_BURST} packets }} drop
        iifname "{HOTSPOT}" ip saddr {HOTSPOT_NET} tcp dport {{ {SSH}, {MONITOR}, {VIEWER} }} accept
        iifname "{CABLE}" ip saddr {CABLE_NET} tcp dport {SSH} accept
        iifname "{CABLE}" ip saddr {SCANNER} tcp dport {DICOM} accept
    }}

    chain forward {{
        type filter hook forward priority filter; policy drop;
        ct state established,related accept
{forward}    }}
}}
"""


def routing_setting(site_toml: Path) -> bool:
    """The site's setting. Before setup, or if site.toml can't be read, routing stays on: the default."""
    if not site_toml.exists():
        return HotspotSettings().routing
    try:
        return load_site(site_toml).hotspot.routing
    except ConfigError as e:
        log.error("Firewall: site.toml can't be read (%s), so hotspot routing stays on.", e)
        return HotspotSettings().routing


def load(text: str) -> None:
    out = subprocess.run(["nft", "-f", "-"], input=text, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[-400:])


def main(args: list[str], *, site_toml: Path = Path("/etc/hyperwheel/site.toml")) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args not in ([], ["--print"]):
        print("Usage: hyperwheel firewall [--print]")
        return 2
    routing = routing_setting(site_toml)
    text = rules(routing)
    if args == ["--print"]:
        print(text, end="")
        return 0
    try:
        load(text)
    except (OSError, RuntimeError) as e:
        log.error("Firewall: the rules weren't loaded (%s).", e)
        return 1
    log.info("Firewall: loaded. Hotspot devices %s the scanner and the internet.",
             "reach" if routing else "don't reach")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
