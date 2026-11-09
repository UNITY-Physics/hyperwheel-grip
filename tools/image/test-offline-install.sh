#!/usr/bin/env bash
# Install a release on the pinned official image, with no network, the way a Pi does it.
# Runs as root on an arm64 Linux machine (CI), since the image's programs run directly.
#
#     sudo tools/image/test-offline-install.sh <release-folder> <signers-file> as-shipped|lists-refreshed|image
#
# as-shipped       the image's own apt state.
# lists-refreshed  `apt-get update` first, with the network. Then apt's repositories offer the
#                  same package versions as the release, as on a Pi that has been online.
# image            as the image build does it (--image), then first boot with a hotspot password
#                  on the card, as written on a Mac.
#
# The install itself runs in an empty network namespace, so anything that tried to
# download would fail.

set -euo pipefail
export LC_ALL=C

release=$(realpath "${1:?release folder}")
signers=$(realpath "${2:?signers file}")
mode=${3:?as-shipped, lists-refreshed or image}
case "$mode" in as-shipped|lists-refreshed|image) ;; *) echo "Unknown mode: $mode" >&2; exit 2 ;; esac
repo=$(cd "$(dirname "$0")/../.." && pwd)

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

echo "== The base image"
curl -fsSL --retry 3 -o "$work/base.img.xz" "$url"
echo "$sha256  $work/base.img.xz" | sha256sum -c -
xz -d "$work/base.img.xz"
image=$work/base.img

# The release needs more room than the image leaves (a Pi grows its card at first boot).
truncate -s +2G "$image"
echo ", +" | sfdisk -q -N 2 "$image"
loop=$(losetup -P --show -f "$image")
e2fsck -fy "${loop}p2" >/dev/null || [ $? -le 1 ]
resize2fs -f "${loop}p2" >/dev/null

mkdir "$mnt"
mount "${loop}p2" "$mnt"
mount "${loop}p1" "$mnt/boot/firmware"
for fs in proc sys dev dev/pts; do mount --bind "/$fs" "$mnt/$fs"; done
# Nothing starts in the chroot. The packages' own scripts ask this first.
printf '#!/bin/sh\nexit 101\n' > "$mnt/usr/sbin/policy-rc.d"
chmod 755 "$mnt/usr/sbin/policy-rc.d"
mkdir "$mnt/test"
mount --bind "$release" "$mnt/test" -o ro
cp "$signers" "$mnt/root/allowed_signers"

in_image() { chroot "$mnt" "$@"; }
offline() { unshare --net chroot "$mnt" "$@"; }

if [ "$mode" = lists-refreshed ]; then
  echo "== Refreshing apt's lists, with the network"
  resolv=$(mktemp "$work/resolv.XXXXXX")
  cp -a "$mnt/etc/resolv.conf" "$resolv" 2>/dev/null || true
  rm -f "$mnt/etc/resolv.conf"
  cp /etc/resolv.conf "$mnt/etc/resolv.conf"
  in_image apt-get update -q
  rm -f "$mnt/etc/resolv.conf"
  [ ! -s "$resolv" ] || cp -a "$resolv" "$mnt/etc/resolv.conf"
  # Without this the test proves nothing: apt must really be offered the same version.
  version=$(grep '^orthanc=' "$repo/release/debs.list" | cut -d= -f2)
  in_image apt-cache madison orthanc | grep -F "$version" \
    || { echo "apt's repositories don't offer orthanc $version, so this test can't show anything." >&2; exit 1; }
fi

echo "== Checking there's no network for the install"
if offline /bin/sh -c 'getent hosts deb.debian.org || curl -fsS --max-time 5 -o /dev/null http://93.184.216.34/'; then
  echo "The install isn't offline." >&2
  exit 1
fi

echo "== Installing the release, offline ($mode)"
if [ "$mode" = image ]; then
  offline /bin/sh /test/install.sh --signers /root/allowed_signers --image
else
  offline /bin/sh /test/install.sh --signers /root/allowed_signers --no-start
fi

echo "== Checking the result"
grep -vE '^(#|$)' "$repo/release/debs.list" | while IFS== read -r package version; do
  have=$(in_image dpkg-query -W -f '${db:Status-Abbrev} ${Version}' "$package")
  [ "$have" = "ii  $version" ] || { echo "$package: expected $version, found: $have" >&2; exit 1; }
  echo "  $package $version"
done
problems=$(in_image dpkg --audit)
[ -z "$problems" ] || { echo "$problems" >&2; exit 1; }
in_image apt-get check -q
in_image /usr/sbin/Orthanc --version | head -1
offline /usr/local/bin/hyperwheel help >/dev/null
in_image test -L /opt/hyperwheel/current

if [ "$mode" = image ]; then
  echo "== Checking the image holds nothing that differs on each Pi"
  for f in orthanc.json orthanc.d/users.json hotspot.json first-boot-done setup-done; do
    [ ! -e "$mnt/etc/hyperwheel/$f" ] || { echo "$f shouldn't be in the image." >&2; exit 1; }
  done
  for unit in hyperwheel-firstboot.service hyperwheel-connect.service hyperwheel-dashboard.service \
              hyperwheel-viewer.service hyperwheel-firewall.service; do
    [ "$(in_image systemctl is-enabled "$unit")" = enabled ] || { echo "$unit isn't enabled." >&2; exit 1; }
  done
  grep -q "hyperwheel setup" "$mnt/etc/sudoers.d/hyperwheel"

  echo "== The image's SSH server accepts Hyperwheel's SSH settings"
  grep -q PerSourcePenalties "$mnt/etc/ssh/sshd_config.d/05-hyperwheel.conf"
  in_image ssh-keygen -q -t ed25519 -N '' -f /tmp/test-host-key
  mkdir -p "$mnt/run/sshd"
  in_image /usr/sbin/sshd -t -o HostKey=/tmp/test-host-key
  in_image /usr/sbin/sshd -T -o HostKey=/tmp/test-host-key | grep -qx "maxauthtries 3"
  rm -f "$mnt/tmp/test-host-key" "$mnt/tmp/test-host-key.pub"

  echo "== The firewall's rules are accepted by the image's nft"
  in_image /opt/hyperwheel/current/venv/bin/hyperwheel firewall --print > "$mnt/tmp/rules.nft"
  offline /usr/sbin/nft -c -f /tmp/rules.nft
  rm -f "$mnt/tmp/rules.nft"

  echo "== First boot, with a password on the card as written on a Mac"
  card=$mnt/boot/firmware/hyperwheel
  mkdir "$card"
  printf 'test-hotspot-pw-1\n' > "$card/hotspot-password"
  printf 'x' > "$card/._hotspot-password"
  offline /usr/local/bin/hyperwheel first-boot
  [ ! -e "$card" ] || { echo "First boot left the password's folder on the card." >&2; ls -la "$card" >&2; exit 1; }
  # Asked inside the image: the orthanc group doesn't exist on the machine running this.
  has_mode() {
    have=$(in_image stat -c '%a %U %G' "$1")
    [ "$have" = "$2" ] || { echo "$1: expected $2, found $have." >&2; exit 1; }
  }
  has_mode /etc/hyperwheel/hotspot.json "600 root root"
  has_mode /etc/NetworkManager/system-connections/Hyperwheel.nmconnection "600 root root"
  has_mode /etc/hyperwheel/orthanc.json "600 root root"
  has_mode /etc/hyperwheel/orthanc.d/users.json "640 root orthanc"
  [ -e "$mnt/etc/hyperwheel/first-boot-done" ] || { echo "First boot left no marker." >&2; exit 1; }
  again=$(offline /usr/local/bin/hyperwheel first-boot)
  case "$again" in *"already done"*) ;; *) echo "First boot ran twice: $again" >&2; exit 1 ;; esac
fi

echo "Offline install on the official image passed ($mode)."
