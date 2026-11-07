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
- a default **Wi-Fi country**, so the hotspot can start at first boot, on a
  2.4 GHz channel allowed everywhere (1, 6 or 11). Setup then switches to the
  site's country;
- the **firewall** (below), SSH, and the user accounts without passwords;
- the first-boot service: it starts the hotspot `Hyperwheel` with the shared
  password from the card (see "Setting up a Pi"), and runs
  `rpi-connect signin`, showing the code on the Upload Monitor;
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
    sharing works) and Raspberry Pi Connect. It isn't in the `sudo` group. A
    sudo rule (`/etc/sudoers.d/hyperwheel`, written by every release and
    checked with `visudo` first) lets it run exactly these as root, with no
    other arguments: `hyperwheel status`, `hyperwheel doctor`,
    `hyperwheel keys list` and `hyperwheel update status` without a password,
    and `hyperwheel keys set` and `hyperwheel scanner password` with its own
    password. The screen locks after 15
    minutes idle and unlocks with that Pi's `datamanager` password.
  - Data managers never type `sudo`. `hyperwheel help` lists what they can
    run, one command per line. A command that needs root runs itself again
    through sudo with the same words, so the sudo rule above still decides
    what's allowed; anything it doesn't allow gets one plain line saying so.
    `sudo hyperwheel ...` works too. Typed by `admin`, `hyperwheel help` also
    lists the admin commands.
  - `admin` never has a desktop session. It is reached over SSH, or with
    `su - admin` from Connect's remote shell, and uses `sudo` with its own
    password. Screen sharing alone never gives admin rights.
  - `hyperwheel` is a system account (no login, password or shell) that runs
    the service and owns `/var/lib/hyperwheel`; Orthanc runs as Debian's
    `orthanc` user.
- Logins are by username and password. Passwords are generated, never
  chosen, in a readable form: three made-up five-letter chunks such as
  `borak-kefim-tumos` (about 49 bits). The `datamanager` password is
  **different on each Pi**, made by setup. The `admin` password is the same on
  every Pi and reaches the Pis only as a hash through the signed update
  channel. Neither is ever in the image, the repo or the logs.
- SSH is allowed only from the hotspot and the scanner cable.
- On the Pi itself, hotspot devices reach only the Upload Monitor and SSH
  (plus the DHCP and DNS the hotspot needs), never Orthanc's DICOM port. The
  hotspot routes through to the scanner and the internet beyond it, so
  operators can use the scanner interface from their laptops, with a
  per-site setting to turn routing off. The Pi's own uploads get priority
  over hotspot traffic.
- On the scanner cable: SSH, DICOM (port 4242) and ping.
- The release-signing key is only for signing releases, never for logins.

## Setting up a Pi (`hyperwheel setup`)

The admin runs `hyperwheel setup` once on a newly flashed Pi, over the
Connect shell. Until the Pi is set up, the `datamanager` account may run it
without a password (there's no admin password to ask for yet). Once it is set
up, setup refuses to run, and the sudo rule no longer lists it.

1. **The site file.** The admin writes one per site, from
   `config/examples/site-file.toml`: the site name, time zone, Wi-Fi country,
   update channel, phantom Patient ID, and which scanner logins go to which
   GRIP project. No keys or passwords are in it. Setup asks for it to be
   pasted (it ends with the line `# end of site file`), checks it, shows it
   back and writes `site.toml` and `routing.toml` once it is confirmed. Site
   settings already on the Pi are never overwritten.
2. **Time zone and Wi-Fi country** from the site file.
3. **The scanner cable:** every scanner is 10.42.0.1 and the Pi is
   10.42.0.88. Setup adds that connection (from the next restart) and says
   whether the scanner answers.
4. **The hotspot** `Hyperwheel`. Its password comes from the private copy of
   the image, where the admin's Mac put it
   (`/boot/firmware/hyperwheel/hotspot-password`). Setup moves it into
   `/etc/hyperwheel/hotspot.json` and the NetworkManager connection (both
   root-only) and deletes the copy. Only if there's none does it ask, hidden
   and twice.
5. **The scanner's raw-data password**, tried on the scanner first, as
   `hyperwheel scanner password` does. It can be skipped and set later.
6. **The datamanager password**, generated, shown once, and set only after it
   has been typed back. Then the screen is cleared.
7. **The admin account**, in the `sudo` group, with its password from the
   update channel.
8. **Password-free sudo removed** (Raspberry Pi OS's `010_pi-nopasswd`) and
   `datamanager` taken out of the `sudo` group, but only once another account
   can still become root with a password. Until then setup says so and the
   Pi isn't marked set up.

Then Hyperwheel starts, and setup offers to restart the Pi, which the scanner
cable address and the Wi-Fi country need. If setup stops half way, running it
again carries on: what's done stays done, and nothing is redone without
asking.

## GRIP keys (SOP outline)

One API key per project (a GRIP dataset), agreed with GRIP. Keys don't
expire; they are rotated or revoked by hand. Each site's data managers own
their keys and set them through the **data-manager account**, which can run
only the key and status commands. The SOP has a page for each step, with
screenshots.

`hyperwheel keys list` shows each project on the Pi, whether it has a key
(with only the last four characters of the client ID) and whether it last
worked. It never shows a secret.

**Setting a key** (at install, or for a new project):

1. Get the project's client ID and secret from GRIP.
2. On the Pi, open the "GRIP keys" launcher on the desktop (or log in over
   SSH as the data-manager user) and type `hyperwheel keys set`. It asks
   for the data-manager password. (`hyperwheel help` lists everything the
   data-manager account can run.)
3. Pick the project from the list by its number, then paste the client ID
   and the secret. The secret is typed hidden and never shown, logged or put
   on a command line. Pressing Enter at any question stops, and nothing is
   saved.
4. The command asks GRIP for a token with the new key **before** saving it.
   If GRIP refuses it, or can't be reached to check it, nothing is saved and
   the old key stays.
5. The key is saved root-only (mode 600), and the upload service is
   restarted so it picks the key up. Uploads continue where they stopped.
6. The service checks every key when it starts, so a minute later
   `hyperwheel keys list` and the Upload Monitor show the project's key
   as working.

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

## Scanner raw-data password (SOP outline)

The Pi fetches raw data from the scanner with a raw-data login. Its password
changes with the scanner's software (from version 12, every release has a
new one), so it is a setting on each Pi, never in the repo or a release. It
is in `/etc/hyperwheel/scanner.json`, root-only (mode 600).

After a scanner upgrade, or when the scanner is missing the RRDF entitlement
that Hyperfine adds, the scanner refuses the login. The Upload Monitor,
`hyperwheel status` and `hyperwheel doctor` then show an alert, raw data stays
on the scanner, and DICOM keeps uploading. The Pi tries the login again every
15 minutes, no more often, so the scanner account isn't locked.

**Setting the new password:**

1. Get the new raw-data password from your Hyperwheel admin.
2. On the Pi, as the data manager or admin, type
   `hyperwheel scanner password`. It asks for the account's password.
3. Paste the raw-data password. It is typed hidden and never shown, logged
   or put on a command line. Pressing Enter stops, and nothing is saved.
4. The command logs in to the scanner with it and lists the raw-data folder
   (read-only) **before** saving it. If the scanner refuses it, or can't be
   reached, nothing is saved and the old password stays. If you're sure the
   password is right and the scanner still refuses it, the scanner may be
   missing the RRDF entitlement: tell your Hyperwheel admin.
5. The password is saved root-only, and the Hyperwheel service is restarted
   to use it. The alert clears at the next fetch, within a minute.

No new release is needed.

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
signature, by a key in the Pi's `/etc/hyperwheel/release_allowed_signers`, for
the `hyperwheel-release` namespace, then every file's size and SHA-256, with nothing extra in the
folder. After changing `requirements/runtime.lock`, run
`tools/release/lock_wheels.py`; after changing `release/debs.list`, run
`fetch_debs.sh --update-lock`.

**Signing.** OpenSSH signatures (`ssh-keygen -Y sign`) with a
passphrase-protected key on the release signer's Mac, backed up in their password manager.
The Pi verifies with `ssh-keygen -Y verify` against its own allowed-signers
file, `/etc/hyperwheel/release_allowed_signers`. That file comes from the image
(or from `--signers` on the first install) and is the only list a Pi trusts. A
release doesn't carry one, and an update never replaces it, so a release can't
change who may sign the next one. `release/allowed_signers` in the repo is the
copy the image and the build and publish tools use. Changing the signer means
changing that copy and the file on every Pi, by hand or with a new image.
There is **one release signer**, so a new release can only be published by
them. The allowed-signers file takes one line per signer, so another can be
added later with no change to the format: a release signed by any listed
signer is accepted.

## On the Pi

```
/opt/hyperwheel/releases/<version>/   each release, read-only, root-owned
/opt/hyperwheel/current -> releases/<version>
/opt/hyperwheel/previous -> releases/<version>
/etc/hyperwheel/                      site.toml, routing.toml, keys/, Orthanc and scanner logins,
                                      hotspot password, update settings: never touched by an update
/etc/hyperwheel/release_allowed_signers   who may sign releases (from the image, or the first
                                      install's --signers); never replaced by an update
/etc/hyperwheel/update-hold           present while the Pi is held
/var/lib/hyperwheel/                  state.db and stagingArea: never replaced by an update
/var/lib/hyperwheel-update/           the updater's status, failed versions and downloads (root)
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
- **Holding a Pi:** `hyperwheel update hold --reason "..."` (admin, over
  Connect or SSH; it asks for the admin password) writes
  `/etc/hyperwheel/update-hold`. A held Pi keeps its version, and downloads
  nothing, until `hyperwheel update release-hold`. The Upload Monitor and `hyperwheel doctor` show the hold.
- `hyperwheel update status` shows the release and the last check's result.
- A Pi never moves backwards because a channel file names an older version,
  except through a rollback (below).

## What starts an update

- A **timer** (`hyperwheel-update.timer`) runs `hyperwheel update check` 20
  minutes after boot, then 6 hours after each check ends, with up to 30
  minutes of random delay. It downloads and verifies a new release in the
  background; a Pi offline just tries again next time.
- It **applies** the release only when the Pi is idle: the Upload Monitor
  would say everything from the scanner is on the Pi, nothing new has been
  recorded for 30 minutes, and Orthanc has received no image for 30 minutes
  (the last also covers a Pi whose service is down). It waits up to 5 hours;
  if the Pi is never idle that long, the next check tries again, with the
  download kept.
- The maintainer can start it at once over Connect: `hyperwheel update now`.
  It runs the same check in `hyperwheel-update.service` (so a dropped
  connection can't cut an install short), with a one-minute quiet spell
  instead of 30, and it also applies releases marked `manual_only` or
  `kernel_or_firmware`, which the timer only downloads. It ends with the
  check's result in one line, e.g. `updated: 0.1.3 -> 0.1.4` or
  `rolled back to 0.1.4: 0.1.5 failed (Hyperwheel's service didn't start
  within 3 minutes) and won't be tried again`.
- **Never applied by the updater:** releases that change Orthanc's database
  (`orthanc_db_change`). They back up Orthanc's data first, and go to the dev
  Pi, then a pilot site, by hand.
- **Only forwards:** the updater takes only a version newer than the current
  one, and never one that already failed on this Pi.

## Applying an update

1. Download the tar; check its size and SHA-256 against the channel file;
   unpack it; check its signature and every file, and that its manifest is
   the one the channel file names.
2. Record which project keys work now (last token request succeeded).
3. Wait until the Pi is idle. Run the new release's own `install.sh`, the
   same steps as an install by hand: Orthanc's `.deb` files only if Orthanc's
   version changes, the release into `releases/<new>` with its venv built
   offline, switch `current`, restart. If it stops before the switch,
   nothing has changed: the Pi stays on its release and that version isn't
   tried again.
4. **Health check** (up to 3 minutes): Orthanc runs and answers, Hyperwheel
   runs and has written its heartbeat since the switch, and the Upload
   Monitor answers. If not, roll back.
5. **Key check, per project, once the Pi is online:** each project's
   uploads wait until its key gets a GRIP token.
   - A key that works: that project's uploads go on.
   - A key that fails: that project's uploads are paused and flagged, the
     others go on. If it worked before the update, the Pi goes back to the
     previous release, which tries the same key. If the previous release
     gets a token, the new release is at fault and the Pi stays back. If it
     doesn't, the key has expired or been revoked. That isn't the release's
     fault: the Pi installs the new release again and the project stays
     paused and flagged until the key is replaced.
   - The updater waits up to 15 minutes for each key's check. GRIP not
     answering in that time is not a failure.
6. **Rollback:** the previous release's own `install.sh` puts it back, with
   its own Orthanc `.deb` files, settings and units. The state database is
   not restored (see below). `previous` points at the release it pointed at
   before the update, and the failed release is removed. The failed version is
   not tried again; a newer one is. If the release put back doesn't pass the
   health check either, the result is an ALERT.
7. Afterwards only `current` and `previous` are kept in `releases/`, and the
   downloads are removed.

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
