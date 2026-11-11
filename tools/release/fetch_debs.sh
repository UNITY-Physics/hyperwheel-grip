#!/usr/bin/env bash
# Fetch the pinned .deb files in release/debs.list for Raspberry Pi OS Trixie (arm64),
# from the same archives a Pi uses (Debian trixie + Raspberry Pi's), in an arm64 Docker
# container on the build machine, and check them against release/debs.lock.
#
#   tools/release/fetch_debs.sh <out-folder>                check against debs.lock
#   tools/release/fetch_debs.sh --update-lock <out-folder>  (re)write debs.lock
set -euo pipefail
here="$(cd "$(dirname "$0")/../.." && pwd)"
update=0
if [[ "${1:-}" == "--update-lock" ]]; then update=1; shift; fi
out="${1:?usage: fetch_debs.sh [--update-lock] <out-folder>}"
mkdir -p "$out"
out="$(cd "$out" && pwd)"
IMAGE="debian:trixie@sha256:9c6e381421d0f48d1b8133e573f33c185ee16cbf505eda9c4a8a4791b25ea8a6"
pkgs=$(grep -v '^#' "$here/release/debs.list" | grep -v '^$' | tr '\n' ' ')

docker run --rm --platform linux/arm64 -v "$here/release/apt:/keys:ro" -v "$out:/out" "$IMAGE" bash -euo pipefail -c "
  cat > /etc/apt/sources.list.d/raspi.sources <<SRC
Types: deb
URIs: http://archive.raspberrypi.com/debian/
Suites: trixie
Components: main
Signed-By: /keys/raspberrypi-archive-keyring.pgp
SRC
  # The image already lists Debian trixie, trixie-updates and trixie-security.
  apt-get -qq update
  cd /out && rm -f ./*.deb
  for want in $pkgs; do
    name=\${want%%=*}; version=\${want#*=}
    if apt-get -qq download \"\$want\" 2>/dev/null; then continue; fi
    if [ $update -eq 1 ]; then
      # A new lock is written only from files apt checked against the archive's signed index.
      echo \"\$want: not in the archives' index, so it can't go in a new debs.lock. Pin a version the index has.\" >&2
      exit 1
    fi
    # A newer version replaced it in the archive's index, but the archives keep old files in their pool. Fetch
    # that exact file from there: debs.lock's checksum (checked below) says it's the one pinned.
    source=\$(apt-cache show \"\$name\" | sed -n 's/^Source: \\([^ ]*\\).*/\\1/p' | head -n1)
    source=\${source:-\$name}
    case \$source in lib*) letter=\${source:0:4} ;; *) letter=\${source:0:1} ;; esac
    for arch in arm64 all; do
      file=\"\${name}_\${version#*:}_\${arch}.deb\"
      for base in http://archive.raspberrypi.com/debian http://deb.debian.org/debian http://deb.debian.org/debian-security; do
        if /usr/lib/apt/apt-helper -qq download-file \"\$base/pool/main/\$letter/\$source/\$file\" \"/out/\$file\" 2>/dev/null; then
          echo \"\$want: no longer in the archive's index, fetched from its pool\"; continue 3
        fi
      done
    done
    echo \"\$want: not in the archives' index or pool\" >&2; exit 1
  done
  chown $(id -u):$(id -g) /out/*.deb
"
cd "$out"
shasum -a 256 ./*.deb | sed 's#  \./#  #' | sort -k2 > debs.sha256
if [[ $update -eq 1 ]]; then
  { echo "# SHA-256 of each pinned .deb in debs.list (written by fetch_debs.sh --update-lock)"; cat debs.sha256; } \
    > "$here/release/debs.lock"
  echo "debs.lock written: $(wc -l < debs.sha256 | tr -d ' ') files"
else
  if diff <(grep -v '^#' "$here/release/debs.lock") debs.sha256 >/dev/null; then
    echo "all $(wc -l < debs.sha256 | tr -d ' ') .deb files match debs.lock"
  else
    echo "MISMATCH with release/debs.lock:" >&2; diff <(grep -v '^#' "$here/release/debs.lock") debs.sha256 >&2; exit 1
  fi
fi
rm debs.sha256
