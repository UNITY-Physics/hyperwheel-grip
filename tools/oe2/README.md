# Orthanc Explorer 2 for the Pi

Orthanc publishes no arm64 build of Orthanc Explorer 2 (OE2), and Trixie doesn't package
it, so we build it: `tools/oe2/build.sh <out-folder>` (needs Docker; on an Apple Silicon Mac
it builds natively for arm64).

- Pinned: OE2 1.14.1, commit `7b1b6801c91c8ba470bda35054e4a09aad28b7dc` (checked after the
  clone), in a pinned `debian:trixie` image.
- Static build against OE2's bundled plugin SDK (1.11.2), so it runs on Orthanc 1.12.7 and
  needs only the C/C++ runtime on the Pi.
- The web application is built from source with `npm ci` and OE2's own lock file, not
  downloaded prebuilt (OE2's download option has no checksum).
- OE2 is AGPL-3.0: the folder also gets `LICENSE`, `SOURCE` (where it came from) and
  `oe2-source.tar.gz` (the exact source), and all of it ships in the release.

Output: `libOrthancExplorer2.so`, `VERSION`, `SOURCE`, `LICENSE`, `oe2-source.tar.gz`. Pass the
folder to `tools/release/build_release.py build --oe2 <out-folder>`.

To change the version: set `OE2_VERSION` and `OE2_COMMIT` in `build.sh` (the commit from
`gh api repos/orthanc-server/orthanc-explorer-2/commits/<version> -q .sha`), rebuild, and test
it on the dev Pi with Orthanc 1.12.7 before a release.
