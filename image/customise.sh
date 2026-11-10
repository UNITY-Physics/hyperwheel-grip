#!/usr/bin/env bash
# What makes the official image a Hyperwheel image. Run by tools/image/build-image.sh, as root, on the image's
# root file system mounted at $1, after the release is installed in it (install.sh --image).
#
#     image/customise.sh <root> <image-version> <admin-password-folder> [<commit>]
#
# Nothing here is different from one Pi to the next: whatever must differ (Orthanc's login, the certificate,
# SSH host keys, the hotspot password, every password but admin's) is made on each Pi, at first boot or by
# hyperwheel setup.

set -euo pipefail
root=${1:?root}
version=${2:?image version}
admin=${3:?admin password folder}
commit=${4:-unknown}
here=$(cd "$(dirname "$0")" && pwd)
in_image() { chroot "$root" "$@"; }
say() { printf '  %s\n' "$*"; }

echo "== Accounts"
# The official image's placeholder user and its first-boot wizard go. datamanager takes uid 1000, the one
# Raspberry Pi OS's tools take as the desktop's user.
# Their password-free sudo goes too (the wizard's, and pi's 010_pi-nopasswd).
for f in "$root"/etc/sudoers.d/*; do
  if grep -qE '^[[:space:]]*(pi|rpi-first-boot-wizard)[[:space:]]' "$f"; then rm "$f"; fi
done
in_image userdel --remove rpi-first-boot-wizard 2>/dev/null || true
in_image userdel --remove pi
# systemd-journal: data managers read the Pi's logs (journalctl), which hold no patient data.
in_image useradd --create-home --shell /bin/bash --user-group --uid 1000 \
  --groups audio,video,plugdev,users,input,render,netdev,gpio,i2c,spi,systemd-journal \
  --comment "Data manager" datamanager
in_image useradd --create-home --shell /bin/bash --user-group --groups sudo --comment "Hyperwheel admin" admin
say "datamanager (desktop, locked until setup gives it a password) and admin (in sudo)"
# Connect's user services run from every boot, with nobody logged in.
mkdir -p "$root/var/lib/systemd/linger"
touch "$root/var/lib/systemd/linger/datamanager"

echo "== The desktop"
sed -i 's/^autologin-user=.*/autologin-user=datamanager/' "$root/etc/lightdm/lightdm.conf"
rm -f "$root/etc/xdg/autostart/piwiz.desktop"
install -m 755 "$here/files/etc/chromium.d/hyperwheel" "$root/etc/chromium.d/hyperwheel"
install -d -m 755 "$root/etc/chromium/policies/managed"
install -m 644 "$here/files/etc/chromium/policies/managed/hyperwheel.json" "$root/etc/chromium/policies/managed/"
# Chromium's first start: Raspberry Pi OS's preferences (from rpi-chromium-mods) add its own bookmarks and open its
# welcome page. They're diverted, so a package update puts its copy beside ours and never over it, and ours start
# with the three Hyperwheel bookmarks on the bar and the Upload Monitor.
install -m 644 -D "$here/files/usr/local/share/hyperwheel/chromium-bookmarks.html" \
  "$root/usr/local/share/hyperwheel/chromium-bookmarks.html"
for f in initial_preferences master_preferences; do
  in_image dpkg-divert --quiet --local --rename --divert "/etc/chromium/$f.rpi" --add "/etc/chromium/$f"
done
python3 - "$root/etc/chromium/initial_preferences.rpi" > "$root/etc/chromium/initial_preferences" <<'PY'
import json, sys
prefs = json.load(open(sys.argv[1]))
prefs["distribution"]["import_bookmarks_from_file"] = "/usr/local/share/hyperwheel/chromium-bookmarks.html"
prefs["first_run_tabs"] = ["http://127.0.0.1:5000/"]
print(json.dumps(prefs, indent=2))
PY
cp "$root/etc/chromium/initial_preferences" "$root/etc/chromium/master_preferences"
chmod 644 "$root/etc/chromium/initial_preferences" "$root/etc/chromium/master_preferences"
say "datamanager logged in automatically, no keyring prompt"
say "Chromium: the Hyperwheel bookmarks on the bar, the Upload Monitor as its start page, no sign-in prompt"

echo "== No other way in"
# Answering Yes to Raspberry Pi Imager's customisation by mistake changes nothing: cloud-init, the user
# renaming service and a first-run script on the kernel's command line are all off.
touch "$root/etc/cloud/cloud-init.disabled"
in_image systemctl mask --quiet userconfig.service
# Imager's first-run script is replaced, not masked: a masked one would leave its boot options in place and the
# Pi would never get past them. The replacement removes them and restarts.
install -m 755 "$here/files/usr/local/lib/hyperwheel/ignore-first-run.sh" -D \
  "$root/usr/local/lib/hyperwheel/ignore-first-run.sh"
install -m 644 -D "$here/files/etc/systemd/system/kernel-command-line.service.d/hyperwheel.conf" \
  "$root/etc/systemd/system/kernel-command-line.service.d/hyperwheel.conf"
rm -f "$root/etc/ssh/sshd_config.d/rename_user.conf"
in_image systemctl enable --quiet ssh.service
say "cloud-init and userconf off, Imager's first-run script replaced, SSH on"

echo "== Nothing changes by itself"
in_image systemctl mask --quiet apt-daily.timer apt-daily-upgrade.timer packagekit.service \
  packagekit-offline-update.service rpi-eeprom-update.service
say "no automatic package updates, no update notifier, no firmware updates outside a release"

echo "== The network"
install -m 755 "$here/files/etc/NetworkManager/dispatcher.d/99-hyperwheel-eee-off" \
  "$root/etc/NetworkManager/dispatcher.d/"
in_image /opt/hyperwheel/current/venv/bin/python -c '
import os
from hyperwheel.sitesetup import CABLE_FILE, NM_DIR, cable_connection
path = os.path.join(NM_DIR, CABLE_FILE)
with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
    f.write(cable_connection("72eefe84-a1ea-5c9a-8f30-1085ac89bed5"))  # the same on every Pi, like the rest
'
# Wi-Fi starts at first boot: the country is GB until setup sets the site's, and the hotspot uses channel 6,
# which every country allows.
cmdline="$root/boot/firmware/cmdline.txt"
sed -i -e 's/\s*cfg80211.ieee80211_regdom=\S*//' -e 's/$/ cfg80211.ieee80211_regdom=GB/' "$cmdline"
sed -i 's/^WirelessEnabled=.*/WirelessEnabled=true/' "$root/var/lib/NetworkManager/NetworkManager.state"
say "the scanner cable at 10.42.0.88, EEE off on it, Wi-Fi country GB"

echo "== The admin password, from its signed file"
install -d -m 700 "$root/tmp/hyperwheel-admin"
install -m 644 "$admin/admin.json" "$admin/admin.json.sig" "$root/tmp/hyperwheel-admin/"
in_image /opt/hyperwheel/current/venv/bin/python -c '
import shutil
from pathlib import Path
from hyperwheel.adminpw import AdminPassword

def fetch(url, dest, max_bytes):  # the files downloaded for this build, checked here as a Pi checks them
    shutil.copyfile(Path("/tmp/hyperwheel-admin") / url.rsplit("/", 1)[1], dest)

print("  " + AdminPassword("local", fetch=fetch).check())
'
rm -r "$root/tmp/hyperwheel-admin"

echo "== The image's version"
# The version first (what the updater compares), then the commit this image was built from.
printf '%s\nbuilt from commit %s\n' "$version" "$commit" > "$root/etc/hyperwheel-image"
chmod 644 "$root/etc/hyperwheel-image"
say "$version, from commit $commit"
