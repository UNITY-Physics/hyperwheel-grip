#!/usr/bin/env bash
# Build Orthanc Explorer 2 for the Pi (arm64) in Docker, from a pinned commit.
#
#   tools/oe2/build.sh <out-folder>
#
# The folder gets libOrthancExplorer2.so, VERSION, SOURCE, LICENSE and oe2-source.tar.gz
# (OE2 is AGPL-3.0, so the source we built from ships with it). Pass the folder to
# tools/release/build_release.py build --oe2 <out-folder>.
set -euo pipefail
OE2_VERSION=1.14.1
OE2_COMMIT=7b1b6801c91c8ba470bda35054e4a09aad28b7dc
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:?usage: build.sh <out-folder>}"
mkdir -p "$out"
tag="hyperwheel-oe2:${OE2_VERSION}"
docker build --platform linux/arm64 --build-arg OE2_VERSION="$OE2_VERSION" --build-arg OE2_COMMIT="$OE2_COMMIT" \
  -t "$tag" "$here"
cid=$(docker create --platform linux/arm64 "$tag")
trap 'docker rm -f "$cid" >/dev/null' EXIT
rm -rf "$out"/*
docker cp -q "$cid:/out/." "$out/"
file "$out/libOrthancExplorer2.so"
(cd "$out" && shasum -a 256 libOrthancExplorer2.so oe2-source.tar.gz)
