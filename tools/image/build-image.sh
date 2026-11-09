#!/usr/bin/env bash
# Build the Hyperwheel image: the pinned official Raspberry Pi OS image, with a signed release installed and
# image/customise.sh applied. Runs as root on an arm64 Linux machine (CI), so the image's own programs run
# directly in a chroot.
#
#     sudo tools/image/build-image.sh <release-folder> <signers-file> <admin-password-folder> <version> <out-folder>
#
# <release-folder>         an unpacked, signed release (hyperwheel-<v>/ with manifest.json and its signature)
# <signers-file>           who may sign releases: the repo's release/allowed_signers (CI tests add a throwaway key)
# <admin-password-folder>  admin.json and admin.json.sig from the admin-password release
# <version>                the image's version, a date such as 2026-11-20
#
# Writes hyperwheel-<version>.img.zip, its .sha256 and hyperwheel-<version>-packages.txt to <out-folder>.

set -euo pipefail
export LC_ALL=C
release=$(realpath "${1:?release folder}")
signers=$(realpath "${2:?signers file}")
admin=$(realpath "${3:?admin password folder}")
version=${4:?version}
out=$(realpath "${5:?out folder}")
[[ "$version" =~ ^20[0-9]{2}-[01][0-9]-[0-3][0-9]$ ]] || { echo "The version must be a date such as 2026-11-20." >&2; exit 2; }
repo=$(cd "$(dirname "$0")/../.." && pwd)
name="hyperwheel-$version"
mkdir -p "$out"

url=$(sed -n 's/^url=//p' "$repo/image/base.lock")
sha256=$(sed -n 's/^sha256=//p' "$repo/image/base.lock")
work=$(mktemp -d "${RUNNER_TEMP:-/tmp}/hyperwheel-image.XXXXXX")
mnt=$work/mnt
loop=""
cleanup() {
  set +e
  if [ -d "$mnt" ]; then
    fuser -k -m "$mnt" >/dev/null 2>&1
    umount -R "$mnt" 2>/dev/null
  fi
  [ -n "$loop" ] && losetup -d "$loop"
  rm -rf "$work"
}
trap cleanup EXIT
fail() { echo "Stopped: $*" >&2; exit 1; }
in_image() { chroot "$mnt" "$@"; }
offline() { unshare --net chroot "$mnt" "$@"; }

echo "== The base image"
curl -fsSL --retry 3 -o "$work/base.img.xz" "$url"
echo "$sha256  $work/base.img.xz" | sha256sum -c -
xz -d -T0 "$work/base.img.xz"
image=$work/$name.img
mv "$work/base.img" "$image"
# Room for the release (the Pi grows its file system to the whole card at first boot anyway).
truncate -s +2G "$image"
echo ", +" | sfdisk -q -N 2 "$image"
loop=$(losetup -P --show -f "$image")
e2fsck -fy "${loop}p2" >/dev/null || [ $? -le 1 ]
resize2fs -f "${loop}p2" >/dev/null

mkdir "$mnt"
mount "${loop}p2" "$mnt"
mount "${loop}p1" "$mnt/boot/firmware"
for fs in proc sys dev dev/pts; do mount --bind "/$fs" "$mnt/$fs"; done
printf '#!/bin/sh\nexit 101\n' > "$mnt/usr/sbin/policy-rc.d"  # nothing starts in the chroot
chmod 755 "$mnt/usr/sbin/policy-rc.d"
[ "$(cat "$mnt/etc/machine-id")" = uninitialized ] || fail "the base image's machine-id isn't 'uninitialized'"

echo "== The release, installed offline as a Pi would"
mkdir "$mnt/tmp/hyperwheel-release"
mount --bind "$release" "$mnt/tmp/hyperwheel-release" -o ro
install -m 644 "$signers" "$mnt/tmp/allowed_signers"
offline /bin/sh /tmp/hyperwheel-release/install.sh --signers /tmp/allowed_signers --image
umount "$mnt/tmp/hyperwheel-release"
rmdir "$mnt/tmp/hyperwheel-release"
rm "$mnt/tmp/allowed_signers"

echo "== Hyperwheel's changes"
"$repo/image/customise.sh" "$mnt" "$version" "$admin"

echo "== Checks"
check() { if "$@"; then printf '  ok  %s\n' "$label"; else fail "$label"; fi; }
label="only admin has a password hash"
check [ "$(awk -F: '$2 ~ /^\$/ {print $1}' "$mnt/etc/shadow")" = admin ]
label="datamanager is uid 1000 and not in sudo, admin is in sudo"
check [ "$(in_image id -u datamanager)" = 1000 ]
check sh -c "! chroot '$mnt' id -nG datamanager | tr ' ' '\n' | grep -qx sudo"
check sh -c "chroot '$mnt' id -nG admin | tr ' ' '\n' | grep -qx sudo"
label="no trace of the first-boot wizard or the pi user"
check sh -c "! grep -qE '^(pi|rpi-first-boot-wizard):' '$mnt/etc/passwd'"
check sh -c "! grep -rqs rpi-first-boot-wizard '$mnt/etc/lightdm' '$mnt/etc/sudoers.d'"
check grep -qx "autologin-user=datamanager" "$mnt/etc/lightdm/lightdm.conf"
label="nothing in it differs from one Pi to the next"
for f in orthanc.json hotspot.json viewer site.toml routing.toml scanner.json setup-done first-boot-done; do
  check [ ! -e "$mnt/etc/hyperwheel/$f" ]
done
check [ -z "$(ls -A "$mnt/etc/hyperwheel/keys")" ]
check sh -c "! ls '$mnt'/etc/ssh/ssh_host_* >/dev/null 2>&1"
check [ "$(cat "$mnt/etc/machine-id")" = uninitialized ]
check [ ! -e "$mnt/boot/firmware/hyperwheel" ]
label="no private key in Hyperwheel's places"
check sh -c "! grep -rIlqs 'PRIVATE KEY' '$mnt/etc/hyperwheel' '$mnt/home' '$mnt/root' '$mnt/opt/hyperwheel' \
  '$mnt/var/lib/hyperwheel' '$mnt/etc/NetworkManager' '$mnt/boot/firmware'"
label="Hyperwheel's services are on, and what must not run is off"
for unit in hyperwheel-firstboot.service hyperwheel-firewall.service hyperwheel-connect.service \
            hyperwheel-dashboard.service hyperwheel-viewer.service hyperwheel-update.timer ssh.service; do
  check [ "$(in_image systemctl is-enabled "$unit")" = enabled ]
done
for unit in apt-daily.timer apt-daily-upgrade.timer packagekit.service rpi-eeprom-update.service \
            userconfig.service kernel-command-line.service; do
  check [ "$(in_image systemctl is-enabled "$unit" 2>/dev/null || true)" = masked ]
done
check [ -e "$mnt/etc/cloud/cloud-init.disabled" ]
label="the sudo rule passes visudo, and lets datamanager run setup until the Pi is set up"
check in_image visudo -c -q
check grep -q "NOPASSWD: /usr/local/bin/hyperwheel setup" "$mnt/etc/sudoers.d/hyperwheel"
label="the SSH server accepts Hyperwheel's settings"
in_image ssh-keygen -q -t ed25519 -N '' -f /tmp/test-host-key
mkdir -p "$mnt/run/sshd"
check in_image /usr/sbin/sshd -t -o HostKey=/tmp/test-host-key
rm -f "$mnt/tmp/test-host-key" "$mnt/tmp/test-host-key.pub"
label="the firewall's rules load"
in_image /opt/hyperwheel/current/venv/bin/hyperwheel firewall --print > "$mnt/tmp/rules.nft"
check offline /usr/sbin/nft -c -f /tmp/rules.nft
rm "$mnt/tmp/rules.nft"
label="hyperwheel help runs as datamanager"
check sh -c "chroot '$mnt' runuser -u datamanager -- /usr/local/bin/hyperwheel help | grep -q 'hyperwheel setup'"
label="the Wi-Fi country is GB"
check grep -q "cfg80211.ieee80211_regdom=GB" "$mnt/boot/firmware/cmdline.txt"

echo "== The package list"
in_image dpkg-query -W -f '${Package}\t${Version}\n' > "$out/$name-packages.txt"
wc -l < "$out/$name-packages.txt" | sed 's/^/  packages: /'

echo "== Closing it"
rm -f "$mnt/usr/sbin/policy-rc.d"
rm -rf "$mnt"/var/cache/apt/*.bin "$mnt"/tmp/* "$mnt"/var/tmp/*
sync
umount -R "$mnt"
e2fsck -fy "${loop}p2" >/dev/null || [ $? -le 1 ]
losetup -d "$loop"
loop=""

echo "== Packing"
(cd "$work" && zip -q -X "$out/$name.img.zip" "$name.img")
(cd "$out" && sha256sum "$name.img.zip" > "$name.img.zip.sha256")
cat "$out/$name.img.zip.sha256"
ls -l "$out"
