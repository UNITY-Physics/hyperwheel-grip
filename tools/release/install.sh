#!/bin/sh
# Install this release on a Pi, offline: sudo sh install.sh [options]
# (see python3 -m hyperwheel.installer --help). The installer runs from this
# release's own Hyperwheel wheel; later updates are checked by the release
# already installed before any of the new release's code runs.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
whl=$(ls "$here"/wheels/hyperwheel-*-py3-none-any.whl)
exec env PYTHONPATH="$whl" /usr/bin/python3 -m hyperwheel.installer "$here" "$@"
