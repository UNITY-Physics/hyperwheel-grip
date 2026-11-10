# Image, releases and updates

Status: built so far: the release format with signing and checking, OE2 and its proxy, the
installer, the updater, setup, the admin password, first boot, Connect's sign-in and the firewall.
The image workflow is built, and the first image is still to be made and tested.

Every Pi runs the same software: one pinned **image** that carries the OS,
and one tested **release** that carries Orthanc, Orthanc Explorer 2 (OE2) and
Hyperwheel at fixed versions. Updates are new releases that the Pis pull.
Nothing is installed by hand in the field.

## The image

The official Raspberry Pi OS **Trixie desktop** image of 2026-10-06, with
Hyperwheel's changes added in CI. Its SHA-256 is pinned in `image/base.lock`, so
the base is byte for byte what was tested. The result is a dated file with its
own SHA-256 and a list of every package in it. Only the hash and the list are
published on GitHub. The file itself is the pin.

It contains (`image/customise.sh`):

- **release 1, already installed**, by the same offline installer an update
  uses (`--image`, which starts nothing and leaves what differs on each Pi to
  first boot). A freshly flashed Pi works without downloading anything. Until
  it's set up, the Upload Monitor shows the page where it joins Connect;
- the release-signing public keys, and **admin's password** set from the signed
  admin password file as it was at build time, so admin works before the Pi has
  ever been online;
- **two accounts and no others:** `datamanager` (uid 1000, logged in to the
  desktop automatically, able to read the Pi's logs, its password locked until setup gives it one, lingering
  on so Connect runs with nobody logged in) and `admin` (in `sudo`). The official
  image's `pi` placeholder and its first-boot wizard are removed;
- **no other way in:** cloud-init and the user-renaming service are switched
  off, and a first-run script that Raspberry Pi Imager puts on the card is
  replaced by one that only removes Imager's boot options and restarts. So a
  site that answers Yes to Imager's customisation by mistake gets no extra user,
  Wi-Fi or password;
- the desktop, with Chromium bookmarks on the bookmarks bar for the scanner
  (`https://10.42.0.1:8080/`), the Upload Monitor (`http://127.0.0.1:5000/`, the
  same page as on the hotspot, and there even when the hotspot isn't up) and OE2
  (`http://127.0.0.1:8442/ui/app/`), both only reachable on the Pi itself. The
  Upload Monitor is Chromium's start page and home page, Raspberry Pi OS's own
  bookmarks and welcome page are left out (its first-start preferences are
  diverted with `dpkg-divert`, so a package update can't bring them back. Ours
  are a copy of the package's file made when the image is built, so a release
  that ships a newer `rpi-chromium-mods` should check whether it changed),
  Chromium never asks to sign in, and there's no keyring prompt
  (`--password-store=basic`);
- **Raspberry Pi Connect** (`rpi-connect`), signed out;
- the **scanner cable** connection (10.42.0.88), so a new Pi reaches the
  internet through the scanner before it's set up;
- the **EEE fix**: a NetworkManager dispatcher script that turns
  Energy-Efficient Ethernet off on `eth0` each time it comes up (EEE made the
  scanner's PACS checks fail);
- **Wi-Fi country GB**, so the hotspot starts at first boot, on channel 6, which
  every country allows. Setup then switches to the site's country;
- the **firewall** (below) and SSH;
- the first-boot service, which runs once, before the network and Orthanc
  start. It makes this Pi's own Orthanc login and certificate, moves the shared
  hotspot password from the card into the site settings and the hotspot's
  connection (root-only), and deletes it from the card with the folder and any
  hidden files macOS left. NetworkManager then starts the hotspot `Hyperwheel`
  by itself. A card with no password waits for setup, which asks;
- **nothing that changes by itself:** apt's daily timers, PackageKit (and so the
  desktop's update notifier) and `rpi-eeprom-update` are masked.

Nothing site-specific or secret is in the image, and nothing in it differs from
one Pi to the next: SSH host keys, the machine ID, Orthanc's login and the
certificate are all made at first boot.

**Building it.** `image-build.yml`, run by hand with a published release (such
as `0.1.13`), downloads the official image and stops unless its SHA-256 matches
`image/base.lock`. It installs the release offline in a chroot, as a Pi would
(signature and every file checked), applies `image/customise.sh`, then checks the
result: only admin has a password hash, no per-Pi file is there, the right
services are on and off, `visudo`, `sshd -t` and `nft -c` accept what's there,
and `hyperwheel help` runs. It's kept as a workflow artifact for 30 days:
`hyperwheel-<date>.img.zip`, its SHA-256, and the list of every package in it.
The admin downloads it, checks the SHA-256, and publishes only the SHA-256 and the
package list, on a GitHub release `image-<date>`. On a pull request that changes
the image, the same build runs with a release from that commit, signed with a
throwaway key, and nothing is kept.

CI also installs a release on the pinned official image with no network, on
every change to the installer (`tools/image/test-offline-install.sh`).

**The private copy.** The sites flash a private copy of the image that also
holds the shared hotspot password, so nobody types it at setup. The admin makes
it on their Mac:

```
tools/image/add-hotspot-password.sh ~/Downloads/hyperwheel-<date>.img.zip
```

It asks for the password twice, hidden, checks it as setup does (8 to 63
characters a keyboard can type, no space at the start or end), and writes
`hyperwheel-<date>-private.img.zip` beside the original (mode 600), with the
password in `hyperwheel/hotspot-password` on the boot partition. The original
stays as it was. The password is handled only by the shell's own commands, so it
never appears on a command line, and it's never printed. The script removes the
hidden files macOS leaves on the partition, and prints the private copy's
SHA-256 for the sites. Share the private copy only with the sites, privately:
anyone who has it can read the password from it.

Sites download the private copy (about 2.1 GB) **the day before**,
in a browser, which can resume a broken download. They check its SHA-256 and
flash it with Raspberry Pi Imager ("Use custom"). The SOP gives the exact steps:

- Windows (PowerShell): `Get-FileHash .\hyperwheel-<date>-private.img.zip` and
  compare with the value from the admin;
- Mac (Terminal): `shasum -a 256 ~/Downloads/hyperwheel-<date>-private.img.zip`.

## Orthanc in the release

Orthanc comes from Debian's `.deb` files, but the release brings its own
settings and loads **only the plugins Hyperwheel needs**, by listing each
plugin file in `"Plugins"` (not Debian's whole plugin folder):

| Plugin | Loaded | Why |
|---|---|---|
| Orthanc Explorer 2 | yes | The web interface the admin uses over Connect. |
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

New Pis join the UNITY organisation on Connect. The image holds no key, and
sites answer No to Imager's customisation, so a Pi can't end up in someone's own
account. Before setup, the Upload Monitor shows the sign-in link, which the
site sends to the admin. Whoever opens the link first gets the Pi, so it goes
only to the admin, who opens it, picks the UNITY organisation and names the Pi. The link is the only way in: there's no key to
paste. Connect starts at every boot with nobody logged in (lingering is on) and
keeps retrying until it's signed in. Its state shows on the Upload Monitor, in
`hyperwheel status` and in `hyperwheel doctor`.

`hyperwheel-connect.service` does this. Before setup, it keeps a sign-in link
ready. Once the Pi is set up, the service only reads the state, and nothing signs
the Pi in or out.

The Upload Monitor changes nothing. Releases before 0.1.15 let a site paste a
Connect key on it. The installer deletes any such key left on a Pi.

## Logins, SSH and the firewall

- Three accounts:
  - `datamanager` runs the desktop (logged in automatically, so Connect screen
    sharing works) and Raspberry Pi Connect. It isn't in the `sudo` group. A
    sudo rule (`/etc/sudoers.d/hyperwheel`, written by every release and
    checked with `visudo` first) lets it run exactly these as root, with no
    other arguments: `hyperwheel status`, `hyperwheel doctor`,
    `hyperwheel datasets list` and `hyperwheel update status` without a
    password, and `hyperwheel datasets add`, `datasets change`,
    `datasets remove` and `hyperwheel scanner password` with its own
    password. From the end of setup, the screen locks
    after 15 minutes idle and unlocks with that Pi's `datamanager` password.
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
- **The firewall** is one nftables table, `inet hyperwheel`, loaded by
  `hyperwheel-firewall.service` before the network comes up, and replaced in one
  step when it's reloaded. NetworkManager keeps its own tables for the hotspot,
  and a packet must get through both.
  - From the hotspot, the Pi itself answers only SSH, the Upload Monitor (5000)
    and OE2 (8443), plus the DHCP and DNS the hotspot needs, and ping.
  - From the scanner cable, it answers SSH, and DICOM (4242) only from the
    scanner (10.42.0.1).
  - Everything else is dropped, including Orthanc's DICOM port and REST API from
    the hotspot.
  - Hotspot devices reach the scanner and the internet beyond it through the
    Pi. A site can turn that off with `hotspot_routing = false` in its site
    file (`[hotspot] routing = false` in `site.toml`, then
    `sudo systemctl reload hyperwheel-firewall`). Nothing on the scanner side
    can start a connection towards the hotspot.
  - There's no speed limit on hotspot traffic.
  - `tools/firewall/test-firewall.sh` loads the rules into a stand-in Pi and
    tries each side, in CI on every change.
- **SSH** slows down wrong passwords, never locking anyone out. sshd allows 3
  tries per connection, and an address whose connections keep failing waits a
  little longer each time, up to a minute (`PerSourcePenalties`). The firewall
  also holds back an address that opens more than about 20 new SSH connections a
  minute.
- OE2 is at `https://192.168.99.1:8443` (and on the Pi itself at
  `http://127.0.0.1:8442`, which never leaves the Pi, so its Chromium shows no
  certificate warning), behind a small proxy, `hyperwheel-viewer.service`:
  - People sign in with `datamanager` or `admin`, checked against the system's
    own password hashes, so no password is stored a second time. A sign-in lasts
    8 hours, or until the service restarts.
  - Wrong passwords from an address wait 1, 2, 4 ... up to 30 seconds before the
    next try, and one password is checked at a time. Never a lockout. Failed
    sign-ins are logged with the account and address, never what was typed.
  - Nobody can delete, modify or upload through OE2. Those buttons are hidden,
    and the proxy refuses the requests too: only reads go through, plus OE2's
    three searches. Downloads (zip and DICOM files) stay on for both accounts,
    and each one is logged.
  - Orthanc's own login stays internal and root-only. The proxy adds it.
  - The service runs as a user systemd makes for it, in the `shadow` group so
    it can check passwords. It can't read the staged files or the GRIP keys.
  - The certificate is made once on each Pi (first boot, or the installer on a
    Pi that has none) and never replaced. No public authority signs one for
    192.168.99.1, so a laptop's browser warns on its first visit to each Pi.
    `hyperwheel status` shows the certificate's fingerprint, to compare with the
    browser's.
- The release-signing key is only for signing releases, never for logins.

## Setting up a Pi (`hyperwheel setup`)

The admin runs `hyperwheel setup` once on a newly flashed Pi, over the
Connect shell. Until the Pi is set up, the `datamanager` account may run it
without a password (there's no admin password to ask for yet). Once it is set
up, setup refuses to run, and the sudo rule no longer lists it.

1. **The site file.** The admin writes one per site, from
   `config/examples/site-file.toml`: the site name, time zone, Wi-Fi country,
   update channel and phantom Patient ID. Which scanner logins go to which
   GRIP dataset is optional: datasets are usually added after setup, with
   `hyperwheel datasets add`. GRIP's address comes with the release, so the
   site file needs none (its `[grip]` section can override it for one site).
   No keys or passwords are in it. Setup asks for it to be
   pasted (it ends with the line `# end of site file`), checks it, shows it
   back and writes `site.toml` and `routing.toml` once it is confirmed. Site
   settings already on the Pi are never overwritten.
2. **The Pi's name, time zone and Wi-Fi country** from the site file. The name is
   `hyperwheel-` and the site's name, such as `hyperwheel-site-a`.
3. **The scanner cable:** every scanner is 10.42.0.1 and the Pi is
   10.42.0.88. Setup adds that connection (from the next restart) and says
   whether the scanner answers.
4. **The hotspot** `Hyperwheel`. On a flashed image, first boot has already set
   it up from the password the admin's Mac put on the private copy
   (`/boot/firmware/hyperwheel/hotspot-password`). Otherwise setup does the same
   here: it moves the password into `/etc/hyperwheel/hotspot.json` and the
   NetworkManager connection (both root-only), and deletes the copy, its folder
   and any hidden files macOS left. Only if there's none does it ask, hidden and
   twice.
5. **The scanner's raw-data password**, tried on the scanner first, as
   `hyperwheel scanner password` does. It can be skipped and set later.
6. **The datamanager password**, generated, shown once, and set only after it
   has been typed back. Then the screen is cleared.
7. **The admin account**, in the `sudo` group. Setup fetches the admin
   password file (below) straight away and sets its hash.
8. **Password-free sudo removed** (Raspberry Pi OS's `010_pi-nopasswd`, which
   the Hyperwheel image already leaves out) and
   `datamanager` taken out of the `sudo` group, but only once another account
   can still become root with a password. If the admin password couldn't be
   fetched, setup says so, and the updater finishes this step by itself once
   it arrives, at an update check.

Then Hyperwheel starts, and setup offers to restart the Pi, which the scanner
cable address and the Wi-Fi country need. If setup stops half way, running it
again carries on: what's done stays done, and nothing is redone without
asking.

## The admin password

The `admin` password is the same on every Pi. Only its hash leaves the release
signer's Mac, in one signed file that every channel reads: `admin.json` and its
signature on the GitHub release `admin-password`. Publishing a release never
touches it, and changing the password is one new file and one signature.

```
.venv/bin/python tools/release/publish.py admin-file
ssh-keygen -Y sign -f ~/.ssh/hyperwheel-release -n hyperwheel-release dist/admin/admin.json
.venv/bin/python tools/release/publish.py admin dist/admin
```

- `admin-file` shows generated passwords (three five-letter chunks) until one
  is taken, asks for it to be typed back, clears the screen, and writes only
  its hash. The password is never written anywhere: keep it in the password
  manager.
- The hash is SHA-512 crypt (`$6$`) with a random salt and 1,000,000 rounds:
  about 0.6 s per admin login on a Pi, and very slow to guess from the
  published hash.
- The file is signed like a release (same key and namespace). Its own type
  field keeps a signed manifest or channel file from passing for it.
- Its version only goes up. A Pi never takes a file older than the one it
  has, so an old password can't come back.
- Each Pi checks for it at every update check (every 6 hours, on hold or
  not, and with `hyperwheel update now`) and sets it with `chpasswd -e`.
  `hyperwheel update status`, typed by admin, shows which version it has.

## Switch-over checklist (per Pi)

Each Pi moves from the old Hyperwheel (uploading to Flywheel) to this one on a
freshly flashed card. The SOP will give each step with pictures. So far:

1. Before the card is wiped: the old Flywheel backlog has finished (or is
   copied off), and the site's scanner logins are noted for its site file.
2. Flash the card, run `hyperwheel setup`, and add each GRIP dataset with
   `hyperwheel datasets add`.
3. **Check each scanner's DICOM destination** in the scanner's settings: IP
   address 10.42.0.88, port 4242, AE title HYPERWHEEL. The Pi is always
   10.42.0.88 on the scanner cable, so a scanner sending anywhere else is
   changed to it.
4. Send a test exam and check it on GRIP and on the Upload Monitor.

## GRIP datasets (SOP outline)

One API key per GRIP dataset, agreed with GRIP. Keys don't expire. They are
rotated or revoked by hand. Each dataset is named by its **client ID**, the
one GRIP gives with its key (for example `site-a-study-one`): that's the name
`hyperwheel datasets list` and the Upload Monitor show. On the Pi, a client ID
that can't be a folder name gets a safe one made from it, which people never
need to see.

GRIP's address is the same for every dataset and every Pi: it comes with the
release (for now GRIP's test gateway, then production's once GRIP sends it,
reaching every Pi as a normal update). A site file's `[grip]` section can give
another for one site, just in case. `hyperwheel doctor` shows the address a Pi
uses. Nobody types it. On a Pi set up before 0.1.16, each dataset still has
GRIP's address in its own entry. The first `hyperwheel datasets` change drops
that entry's address if it's the site's, and from then on the dataset follows
the site's. An entry whose address differs keeps it (doctor lists it), so
before the release that changes the default, check those Pis.

Each site's data managers own their datasets and keys, and manage them through
the **data-manager account**, which can run only the dataset, scanner-password
and status commands. Datasets are usually added after setup (the site file's
datasets are optional). The SOP has a page for each step, with screenshots.

- `hyperwheel datasets list` shows each dataset, its scanner logins, and
  whether its key last worked. It never shows a secret.
- `hyperwheel datasets add` asks for the client ID, the scanner login (the
  part of the operator name after the last `/`, lower case, then any more
  logins) and the secret. A login can belong to only one dataset: one that's
  already used stops it, naming the dataset that has it. It asks GRIP for a
  token with the key **before** saving anything. If GRIP refuses it, or can't
  be reached to check it, nothing is saved.
- `hyperwheel datasets change` changes a dataset's logins (only for scans that
  finish after the change, and logins left out are then deleted on the Pi. It
  waits while images are still arriving from a login being left out. To move a
  login to another dataset, take it off one, then add it to the other), or
  replaces its key (the client ID stays the dataset's: another client ID is
  another dataset). A new key is checked with GRIP the same way.
- `hyperwheel datasets remove` deletes a dataset and its key from the Pi,
  after saying that scans from its logins will then be deleted on the Pi, not
  uploaded, and asking for the dataset's name to confirm. While images are
  still arriving for it, it waits (try again once the Upload Monitor no longer
  says it's receiving). If any of its sessions would be deleted on the Pi
  (not all on GRIP: files waiting or refused, or a session held for a check),
  it says how many and stops unless the person also types `delete them`. Raw
  files of its sessions held as unassigned stay on the Pi, flagged, and it
  says so. Raw data of its sessions still on the scanner (not fetched
  yet) is never fetched once it's removed: it says so and asks for `go on`. Then, with the upload service stopped for a moment, it counts
  again (stopping if more arrived), deletes those sessions on the Pi and in
  Orthanc like a study from a login with no dataset, and only then takes the
  dataset and its key off the Pi. Stopped part-way, the dataset is still
  there, and running `remove` again finishes it. What's on GRIP stays there.
- While an update is being installed, `add`, `change` and `remove` change
  nothing and say to try again in a few minutes.
- A session deleted on the Pi because its login had no dataset (or its
  dataset was removed) goes through as a new one if the scanner sends it again
  once its login has a dataset.

Every command asks for the data-manager password (except `list`). The secret
is typed hidden and never shown, logged or put on a command line. Pressing
Enter at any question stops, and nothing is changed. Keys are saved root-only
(mode 600). The dataset settings (`/etc/hyperwheel/routing.toml`) are checked,
then replaced in one step. After a change, Hyperwheel and the Upload Monitor
restart to use it, and uploads continue where they stopped. The service checks
every key when it starts, so a minute later `hyperwheel datasets list` and the
Upload Monitor show the key as working.

A release before 0.1.16 can't read dataset settings written by these commands
(a dataset with no address of its own, or a client ID). So while a release is
being installed and checked, which is when it may roll back, the commands
change nothing and say to try again. That includes the update that installs
0.1.16 itself, run by the earlier release's updater: for half an hour after a
release switch, a running update check counts as installing. Don't install an
older release by hand on a Pi whose datasets were changed.

The old commands, `hyperwheel keys list` and `keys set`, now say which
`datasets` command replaced them.

**Rotating a key** (on a schedule, e.g. once a year, or at once if a key may
have leaked or someone who had it leaves):

1. Ask GRIP for a new secret for the dataset, keeping the old one active.
2. On the Pi, type `hyperwheel datasets change`, pick the dataset and
   replace its key. The command checks it with GRIP before saving it.
3. Check the Upload Monitor shows the key as "Working".
4. Only then ask GRIP to revoke the old one.
5. Note the date in the site's records (not on the Pi).

If a key stops working (revoked, or entered wrong), the Upload Monitor shows
an alert for that dataset. Only that dataset's uploads pause, and its files
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
  `stable`. To move a Pi to another channel (for example the pilot site to
  `stable` after the pilot): `hyperwheel update channel stable` (admin; it
  asks for the admin password). Only that line of `site.toml` changes, and the
  file is checked before it replaces the old one. The Pi takes the new
  channel's release at its next check, or straight away with
  `hyperwheel update now`. It never moves backwards: on a channel that names
  an older release, it keeps its version until the channel names a newer one.
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
