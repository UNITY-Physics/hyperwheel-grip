# Image, releases and updates

Status: agreed plan. Built so far: the record-format rule, and the release format with
signing and checking (below). Not built yet: OE2, the installer, the updater, the image.

Every Pi runs the same software: one pinned **image** that carries the OS,
and one tested **release** that carries Orthanc, Orthanc Explorer 2 (OE2) and
Hyperwheel at fixed versions. Updates are new releases that the Pis pull.
Nothing is installed by hand in the field.

## The image

Our own build with `pi-gen` (Raspberry Pi's image builder) from Raspberry Pi
OS **Trixie desktop**, built once, published as a dated file with its SHA-256
and a list of every package in it. The file itself is the pin.

It contains:

- the desktop, with Chromium bookmarks for the scanner interface
  (`https://<scanner>:8080/`), OE2 and the Upload Monitor, and no keyring
  prompt;
- **Raspberry Pi Connect** (`rpi-connect`), for screen sharing and a remote
  shell;
- the **EEE fix**: a NetworkManager dispatcher script that turns
  Energy-Efficient Ethernet off on `eth0` each time it comes up (EEE made the
  scanner's PACS checks fail);
- the **hotspot connection set up but switched off and without a password**.
  It is never an open hotspot: the install adds the shared password from the
  site config and only then switches it on;
- the **firewall** (below), SSH, and the user accounts without passwords;
- the first-boot service: it detects the scanner network (10.42.0.x or
  10.0.0.x), sets the Pi's address, and runs `rpi-connect signin`, showing
  the code on the Upload Monitor;
- the release-signing public keys;
- **release 1, already installed**, by the same offline installer an update
  uses. A freshly flashed Pi works without downloading anything. Until the
  site's settings and keys are added, the service waits as "not set up yet"
  and the Upload Monitor says so.

Nothing site-specific or secret is in the image.

Sites download the image (about 1.5 GB, an estimate) **the day before**, in a
browser, which can resume a broken download. They check its SHA-256 and flash
it with Raspberry Pi Imager ("Use custom"). The SOP gives the exact steps:

- Windows (PowerShell): `Get-FileHash .\hyperwheel-<date>.img.xz` and compare
  with the published value;
- Mac (Terminal): `shasum -a 256 ~/Downloads/hyperwheel-<date>.img.xz`.

## Orthanc in the release

Orthanc comes from Debian's `.deb` files, but the release brings its own
settings and loads **only the plugins Hyperwheel needs**, by listing each
plugin file in `"Plugins"` (not Debian's whole plugin folder):

| Plugin | Loaded | Why |
|---|---|---|
| Orthanc Explorer 2 | yes | The web interface the maintainer uses over Connect. |
| Legacy Orthanc Explorer (built in) | no (`"OrthancExplorerEnabled": false`) | OE2 replaces it; one interface to keep safe. |
| Delayed deletion | no | Deletes files in the background. Hyperwheel deletes one instance at a time after GRIP confirms it, which is quick on its own; this adds a queue that could outlive a crash. |
| Housekeeper | no | Rewrites the database and files after settings change. It loads the SD card for nothing on a Pi whose settings are fixed by the release. |
| Modality worklists | no | Serves worklists to scanners. The Swoop doesn't use one, and it would answer DICOM queries on the scanner network. |
| Multitenant DICOM | no | Several AE titles with labels. The Pi has one AE title, HYPERWHEEL. |
| Serve folders | no | Serves local folders over HTTP; nothing to serve, and less exposed without it. |
| DICOMweb (Trixie's `orthanc-dicomweb`) | no | Hyperwheel uses Orthanc's REST API; OE2 works without it. Add it only with a viewer, if one is wanted later. |

The other settings the release fixes: AE title HYPERWHEEL on port 4242,
the REST API on the Pi only (`"RemoteAccessAllowed": false`) with a login whose
password stays in `/etc/hyperwheel`, and `"StableAge": 60`.

## Raspberry Pi Connect

Each Pi is signed in to Connect at first boot (the code shows on the Upload
Monitor and is approved in Connect). Which Connect account or organisation
approves it is a site setting, so it can move from the maintainer's own account to a UNITY
organisation without a new release. Moving a Pi later means signing it in
again.

## Logins, SSH and the firewall

- Three accounts:
  - `datamanager` runs the desktop (logged in automatically, so Connect screen
    sharing works) and Raspberry Pi Connect. It can run only the key, status
    and doctor commands. The screen locks after 15 minutes idle and unlocks
    with that Pi's `datamanager` password.
  - `admin` never has a desktop session. It is reached over SSH, or with
    `su - admin` from Connect's remote shell, and uses `sudo` with its own
    password. Screen sharing alone never gives admin rights.
  - `hyperwheel` is a system account (no login, password or shell) that runs
    the service and owns `/var/lib/hyperwheel`; Orthanc runs as Debian's
    `orthanc` user. Logins are by username
  and password, with a **different password on each Pi**, set at setup and
  never in the image, the repo or the logs.
- SSH is allowed only from the hotspot and the scanner cable.
- On the hotspot, devices reach only the Upload Monitor and SSH to the Pi
  itself (plus the DHCP and DNS the hotspot needs), nothing beyond: no
  routing to the scanner network or the internet. The dev Pi keeps sharing
  its connection.
- On the scanner cable: SSH, DICOM (port 4242) and ping.
- The release-signing key is only for signing releases, never for logins.

## GRIP keys (SOP outline)

One API key per project (a GRIP dataset), agreed with GRIP. Keys don't
expire; they are rotated or revoked by hand. Each site's data managers own
their keys and set them through the **data-manager account**, which can run
only the key and status commands. The SOP has a page for each step, with
screenshots.

**Setting a key** (at install, or for a new project):

1. Get the project's client ID and secret from GRIP.
2. On the Pi, open the "GRIP keys" launcher on the desktop (or log in over
   SSH as the data-manager user) and run `hyperwheel keys set`.
3. Pick the project from the list, then paste the client ID and the secret.
   The secret is typed hidden and never shown, logged or put on a command
   line.
4. The command asks GRIP for a token with the new key **before** saving it.
   If GRIP refuses it, nothing is saved and the old key stays.
5. The key is saved root-only (mode 600), and the upload service is
   restarted so it picks the key up. Uploads continue where they stopped.
6. The Upload Monitor shows the project's key as "Working" after its next
   upload.

**Rotating a key** (on a schedule, e.g. once a year; at once if a key may
have leaked or someone who had it leaves):

1. Ask GRIP for a new key for the project, keeping the old one active.
2. Set the new key on the Pi as above. The command checks it with GRIP before
   saving it.
3. Check the Upload Monitor shows the key as "Working".
4. Only then ask GRIP to revoke the old key.
5. Note the date in the site's records (not on the Pi).

If a key stops working (revoked, or entered wrong), the Upload Monitor shows
an alert for that project. Only that project's uploads pause, and its files
are kept until a working key is set.

## The release

One version, signed, with fixed contents:

| Part | What | Size |
|------|------|------|
| Orthanc | `.deb` files: `orthanc` 1.12.7+dfsg-4 and the 7 packages it needs that the image lacks, installed from local files with no download | 13 MB |
| OE2 | the plugin built for arm64 in CI at a pinned version, against Orthanc 1.12.7 (Trixie doesn't package it) | to be measured |
| Hyperwheel | our wheel and every Python package as pinned wheels with hashes, installed offline into a venv for this release | 9 MB |
| Orthanc settings | AE title HYPERWHEEL, port 4242, plugins; the Orthanc password stays in `/etc/hyperwheel` | small |
| OS packages | pinned Trixie security updates as `.deb` files, when needed (below) | varies |
| systemd units, installer, manifest | the manifest lists every file's SHA-256, the release's channel, the minimum image version and its flags | small |

Each part is downloaded on its own and found by its hash, so an update
fetches only what changed (a code-only update is a few MB). The release
build checks that the `.deb` set installs on the pinned image.

**Building and signing a release.** GitHub Actions builds it; the release
signer signs it on their own Mac. No secret is ever in a workflow.

1. On GitHub: Actions → "Build release" → Run workflow, on `main`. It runs
   the tests, fetches the pinned `.deb` files, builds OE2 and the release
   folder on an arm64 machine, and keeps it as the artifact
   `hyperwheel-release-unsigned` (30 days), with a `SHA256SUMS` list.
2. On the release signer's Mac: `gh run download <run id> -n hyperwheel-release-unsigned`,
   then sign and pack it as in steps 3 and 4 below.

The same steps also run by hand on the build Mac, for a test build:

1. `tools/release/fetch_debs.sh <folder>` fetches the pinned `.deb` files in
   `release/debs.list` in an arm64 Docker container that uses the same archives
   as a Pi (Debian trixie and Raspberry Pi's, with the key in `release/apt/`),
   and checks them against `release/debs.lock`.
2. `tools/release/build_release.py build --debs <folder> [--oe2 <folder>]`
   builds `dist/releases/hyperwheel-<version>/`: the wheels (Hyperwheel's own,
   and every package in `requirements/runtime-arm64.lock`, fetched with
   `pip --require-hashes`), the `.deb` files, OE2 if given, and `manifest.json`.
   The version is the package version.
3. The release signer signs the manifest:
   `ssh-keygen -Y sign -f ~/.ssh/hyperwheel-release -n hyperwheel-release <folder>/manifest.json`.
4. `tools/release/build_release.py pack <folder>` checks the signature and every
   file, as a Pi will, and writes `hyperwheel-<version>-arm64.tar` (the same
   bytes every time for the same release).

A Pi checks a release with `python -m hyperwheel.release verify <folder>`: the
signature, by a key in `release/allowed_signers`, for the `hyperwheel-release`
namespace, then every file's size and SHA-256, with nothing extra in the
folder. After changing `requirements/runtime.lock`, run
`tools/release/lock_wheels.py`; after changing `release/debs.list`, run
`fetch_debs.sh --update-lock`.

**Signing.** OpenSSH signatures (`ssh-keygen -Y sign`) with a
passphrase-protected key on the release signer's Mac, backed up in their password manager.
The Pi verifies with `ssh-keygen -Y verify` against an allowed-signers file
in the image and in each release. There is **one release signer**, so a new
release can only be published by them. The allowed-signers file takes one line
per signer, so another can be added later with no change to the format: a
release signed by any listed signer is accepted.

## On the Pi

```
/opt/hyperwheel/releases/<version>/   each release, read-only, root-owned
/opt/hyperwheel/current -> releases/<version>
/opt/hyperwheel/previous -> releases/<version>
/etc/hyperwheel/                      site.toml, routing.toml, keys/, Orthanc and scanner logins,
                                      hotspot password, update settings: never touched by an update
/var/lib/hyperwheel/                  state.db and stagingArea: never replaced by an update
/var/lib/orthanc/                     Orthanc's data
```

## Which release a Pi takes, and holding a Pi

- The release signer publishes a signed **channel file** (one per channel: `dev`, `pilot`,
  `stable`) naming the version for that channel, its tar's name, size and
  SHA-256, and its manifest's hash. Promoting a release means signing a new
  channel file. A Pi only ever moves to the version its channel file names.
- **Where they are:** each release is a GitHub release `v<version>` (its tar,
  manifest and signature). Each channel file is `channel.json` and
  `channel.json.sig` on the GitHub release `channel-<name>`, so a Pi always
  downloads from the same address. A channel file is signed with the same key
  and namespace as a manifest, names its own channel (a Pi refuses another
  channel's file), and can't be mistaken for a manifest.
- **Publishing** (on the release signer's Mac, with `gh` logged in; no secret
  is involved):
  ```
  .venv/bin/python tools/release/publish.py release dist/releases/hyperwheel-<v>-arm64.tar
  .venv/bin/python tools/release/publish.py channel-file --channel dev dist/releases/hyperwheel-<v>-arm64.tar
  ssh-keygen -Y sign -f ~/.ssh/hyperwheel-release -n hyperwheel-release dist/channels/dev/channel.json
  .venv/bin/python tools/release/publish.py channel dist/channels/dev
  ```
  Each step checks the release or channel file the way a Pi will before
  anything is published, and `channel` also checks that the release it names
  is published with the same tar size and checksum. Every step takes
  `--dry-run`, except `channel-file`, which only writes a local file.
- Each Pi's channel is a site setting (`[updates] channel` in `site.toml`):
  the dev Pi on `dev`, the pilot site on `pilot`, every other Pi on
  `stable`.
- **Holding a Pi:** `hyperwheel update hold` (admin, over Connect or SSH)
  writes a hold to `/etc/hyperwheel`. A held Pi keeps its version until
  `hyperwheel update release-hold`; the Upload Monitor and `hyperwheel
  doctor` show the hold.
- A Pi never moves backwards because a channel file names an older version,
  except through a rollback (below).

## What starts an update

- A **timer** checks the channel file every 6 hours (with a random delay),
  when the Pi is online, and downloads and verifies a new release in the
  background.
- It **applies** the release only when the Pi is idle: nothing being
  received or fetched, and no new DICOM for 30 minutes.
- The maintainer can start it at once over Connect: `sudo hyperwheel update now`.
- **Never applied automatically:** releases that change Orthanc's database.
  They are marked in the manifest, back up Orthanc's data first, and go to
  the dev Pi, then a pilot site, by hand.

## Applying an update

1. Verify the signature and every hash; unpack into `releases/<new>`;
   build the venv offline.
2. Record which project keys work now (last token request succeeded).
3. Wait until the Pi is idle; stop Hyperwheel; install Orthanc's `.deb`
   files only if Orthanc's version changes; switch `current`; start.
4. **Health check:** the service runs, Orthanc answers, and the Upload
   Monitor is up. If not, roll back.
5. **Key check, per project, once the Pi is online:** each project's
   uploads wait until its key gets a GRIP token.
   - A key that works: that project's uploads go on.
   - A key that fails: that project's uploads are paused and flagged, the
     others go on. If it worked before the update, the previous release
     tries the same key. If the previous release gets a token, the new
     release is at fault and the Pi rolls back. If it doesn't, the key has
     expired or been revoked. That isn't the release's fault: the Pi keeps
     the new release and the project stays paused and flagged until the key
     is replaced.
6. **Rollback:** switch `current` back to `previous` and reinstall the
   previous Orthanc `.deb` files if they changed. The state database is not
   restored (see below). The failed version is not tried again until a newer
   one is published.

## Settings and keys

An update never overwrites, resets, prints or logs the site's settings or
keys. A new setting comes with a default in the code, so no file has to
change. A setting each site must fill in stops the update before step 3:
the old release keeps running and the Upload Monitor flags it. The updater
logs only file names and versions, never values.

## The record format only adds

Changes to the state database only add: new tables, new non-unique indexes,
and new columns that are nullable or have a default. An older release opens
a database a newer one has migrated, ignores columns and tables it doesn't
know, and leaves alone rows in states it doesn't know. So a rollback never
restores an older copy of the database, which would lose what happened since
the update. `tests/test_state.py` enforces this for every migration.

## OS, kernel and firmware updates

- OS security updates ship as pinned `.deb` files in a release, within
  Trixie, with no reflash. Never a blanket `apt upgrade`.
- Kernel or firmware updates only when needed. They are tested on the dev
  Pi first and marked in the manifest. They are installed only while the Pi
  is idle, followed by a reboot and the health check.
