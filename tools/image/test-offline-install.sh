#!/usr/bin/env bash
# Install a release on the pinned official image, with no network, the way a Pi does it.
# Runs as root on an arm64 Linux machine (CI), since the image's programs run directly.
#
#     sudo tools/image/test-offline-install.sh <release-folder> <signers-file> as-shipped|lists-refreshed
#
# as-shipped       the image's own apt state.
# lists-refreshed  `apt-get update` first, with the network. Then apt's repositories offer the
#                  same package versions as the release, as on a Pi that has been online.
#
# The install itself runs in an empty network namespace, so anything that tried to
# download would fail.

set -euo pipefail
export LC_ALL=C

release=$(realpath "${1:?release folder}")
signers=$(realpath "${2:?signers file}")
mode=${3:?as-shipped or lists-refreshed}
case "$mode" in as-shipped|lists-refreshed) ;; *) echo "Unknown mode: $mode" >&2; exit 2 ;; esac
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
offline /bin/sh /test/install.sh --signers /root/allowed_signers --no-start

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
echo "Offline install on the official image passed ($mode)."
