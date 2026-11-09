#!/usr/bin/env bash
# Load Hyperwheel's firewall into a stand-in Pi and try what each side can reach. Linux, as root, with nft, ip
# and python3. No real network is touched: everything runs in network namespaces made here.
#
#     sudo tools/firewall/test-firewall.sh
#
#   phone   (192.168.99.50)  --wlan0--  pi  --eth0--  (10.42.0.1)  scanner
#                                   192.168.99.1  10.42.0.88
#
# NetworkManager's own NAT for the hotspot is stood in for by one masquerade rule, as on a Pi.

set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
export PYTHONPATH="$repo/src"
failures=0
ns() { ip netns exec "$@"; }

cleanup() {
  set +e
  for n in hw-phone hw-pi hw-scanner; do
    ip netns pids "$n" 2>/dev/null | xargs -r kill 2>/dev/null
    ip netns del "$n" 2>/dev/null
  done
}
trap cleanup EXIT
cleanup

for n in hw-phone hw-pi hw-scanner; do ip netns add "$n"; ns "$n" ip link set lo up; done
ip link add wlan0 netns hw-pi type veth peer name eth0 netns hw-phone
ip link add eth0 netns hw-pi type veth peer name eth0 netns hw-scanner
ns hw-pi ip addr add 192.168.99.1/24 dev wlan0
ns hw-pi ip addr add 10.42.0.88/24 dev eth0
ns hw-phone ip addr add 192.168.99.50/24 dev eth0
ns hw-scanner ip addr add 10.42.0.1/24 dev eth0
ns hw-scanner ip addr add 10.42.0.77/24 dev eth0   # another machine on the scanner cable
for n in hw-phone hw-pi hw-scanner; do ns "$n" ip link set eth0 up; done
ns hw-pi ip link set wlan0 up
ns hw-phone ip route add default via 192.168.99.1
ns hw-pi sh -c 'echo 1 > /proc/sys/net/ipv4/ip_forward'
ns hw-pi nft -f - <<'NM'
table ip nm-shared-stand-in {
    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;
        ip saddr 192.168.99.0/24 oifname "eth0" masquerade
    }
}
NM

# Something listening on every port that matters, in each place.
listen() {
  ns "$1" python3 -c '
import socket, sys, threading
def serve(port):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port)); s.listen(64)
    while True:
        c, _ = s.accept(); c.close()
for p in sys.argv[1:]:
    threading.Thread(target=serve, args=(int(p),), daemon=True).start()
threading.Event().wait()
' "${@:2}" &
}
listen hw-pi 22 5000 8443 8042 4242 9999
listen hw-scanner 8080 443
listen hw-phone 7000
sleep 1

can() {  # can <from> <host> <port>: 0 if a connection is made within 2 s
  ns "$1" python3 -c '
import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=2).close()
except OSError:
    sys.exit(1)
' "$2" "$3"
}
expect() {  # expect yes|no <what> <from> <host> <port>
  local want=$1 what=$2
  if can "$3" "$4" "$5"; then got=yes; else got=no; fi
  if [ "$got" = "$want" ]; then echo "  ok    $what"; else echo "  WRONG $what (expected $want, got $got)"; failures=$((failures + 1)); fi
}

check_pi() {
  expect yes "phone: SSH on the Pi"                  hw-phone 192.168.99.1 22
  expect yes "phone: the Upload Monitor"             hw-phone 192.168.99.1 5000
  expect yes "phone: Orthanc Explorer 2"             hw-phone 192.168.99.1 8443
  expect no  "phone: Orthanc's REST API"             hw-phone 192.168.99.1 8042
  expect no  "phone: Orthanc's DICOM port"           hw-phone 192.168.99.1 4242
  expect no  "phone: anything else on the Pi"        hw-phone 192.168.99.1 9999
  expect no  "phone: DICOM on the Pi's cable address" hw-phone 10.42.0.88 4242
  expect yes "scanner: DICOM to the Pi"              hw-scanner 10.42.0.88 4242
  expect yes "scanner cable: SSH on the Pi"          hw-scanner 10.42.0.88 22
  expect no  "scanner cable: the Upload Monitor"     hw-scanner 10.42.0.88 5000
  expect no  "scanner cable: Orthanc Explorer 2"     hw-scanner 10.42.0.88 8443
  expect no  "scanner cable: Orthanc's REST API"     hw-scanner 10.42.0.88 8042
  expect no  "scanner: a connection to the phone"    hw-scanner 192.168.99.50 7000
  expect yes "the Pi: the scanner's web interface"   hw-pi 10.42.0.1 8080
  expect yes "the Pi: itself"                        hw-pi 127.0.0.1 8042
  if ns hw-phone ping -c1 -W2 192.168.99.1 >/dev/null; then echo "  ok    phone: ping the Pi"; else
    echo "  WRONG phone: ping the Pi"; failures=$((failures + 1)); fi
}

# DICOM from another address on the cable: the listener binds the scanner's second address.
dicom_from_other() {
  if ns hw-scanner python3 -c '
import socket, sys
s = socket.socket(); s.settimeout(2); s.bind(("10.42.0.77", 0))
try:
    s.connect(("10.42.0.88", 4242))
except OSError:
    sys.exit(1)
'; then echo "  WRONG another address on the cable: DICOM to the Pi"; failures=$((failures + 1))
  else echo "  ok    another address on the cable: DICOM to the Pi"; fi
}

echo "== No firewall yet: the test must see what's open, or it proves nothing"
expect yes "phone: Orthanc's REST API (open without the firewall)" hw-phone 192.168.99.1 8042
expect yes "phone: the scanner, through the Pi"                    hw-phone 10.42.0.1 8080

echo "== Hotspot routing on (the default)"
python3 -m hyperwheel.firewall --print | ns hw-pi nft -f -
check_pi
dicom_from_other
expect yes "phone: the scanner's web interface, through the Pi" hw-phone 10.42.0.1 8080
expect yes "phone: beyond the scanner, through the Pi"          hw-phone 10.42.0.1 443

echo "== Loaded again: replaced in one step, not added twice"
python3 -m hyperwheel.firewall --print | ns hw-pi nft -f -
[ "$(ns hw-pi nft list tables | grep -c 'inet hyperwheel')" = 1 ] && echo "  ok    one table" \
  || { echo "  WRONG more than one table"; failures=$((failures + 1)); }

echo "== Hotspot routing off (site setting)"
site=$(mktemp -d)
cat > "$site/site.toml" <<'TOML'
schema_version = 1
site = "test"
staging_dir = "/tmp/x"
state_db = "/tmp/x.db"
keys_dir = "/tmp/keys"
[hotspot]
routing = false
TOML
python3 -c "import sys; from pathlib import Path; from hyperwheel import firewall
sys.exit(firewall.main(['--print'], site_toml=Path('$site/site.toml')))" | ns hw-pi nft -f -
rm -r "$site"
check_pi
expect no "phone: the scanner's web interface, through the Pi" hw-phone 10.42.0.1 8080
expect no "phone: beyond the scanner, through the Pi"          hw-phone 10.42.0.1 443

echo "== New SSH connections from one address, quickly"
made=0
for _ in $(seq 1 45); do if can hw-phone 192.168.99.1 22; then made=$((made + 1)); fi; done
if [ "$made" -ge 25 ] && [ "$made" -lt 45 ]; then echo "  ok    $made of 45 let through, the rest held back"
else echo "  WRONG $made of 45 let through"; failures=$((failures + 1)); fi
expect yes "scanner cable: SSH, from another address meanwhile" hw-scanner 10.42.0.88 22

echo
if [ "$failures" -eq 0 ]; then echo "The firewall test passed."; else echo "The firewall test failed: $failures wrong."; exit 1; fi
