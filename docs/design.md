# Hyperwheel (GRIP edition): design

Hyperwheel runs on a Raspberry Pi 5 cabled to a Hyperfine Swoop scanner. It
receives the scanner's DICOM, pulls the raw research data (RRDF) over SSH,
checks each study for operator mistakes and uploads it to the right GRIP
project. A local file is deleted only once GRIP has confirmed it.

## Rules

- **Never lose data.** A local file is deleted only after a successful
  response for that file's own upload: STOW-RS 200 listing the instance as
  stored (`00081199`), or the last raw-file chunk accepted with
  `complete: true`. Anything else keeps the file.
- **Routing on the Pi.** The part of `OperatorsName` after the last `/` picks
  the GRIP dataset (a "project" in the code) and its key (one key per
  dataset, named by its client ID). GRIP's address is the site's: the
  release's default unless the site file gives one. A login with no routing
  entry is never uploaded: its DICOM is deleted on the Pi and its raw files
  are never pulled from the scanner. Data managers change the datasets with
  `hyperwheel datasets`.
- **One GRIP session per StudyInstanceUID**, never closed. GRIP's API has
  no session calls: GRIP files everything by StudyInstanceUID, and files that
  arrive later join the same study. A resend overwrites (DICOM by SOP
  Instance UID, raw files by name).
- **No changes to the data.** DICOM tags are sent as received; RRDF files keep
  their names and are not matched to series.
- **No raw tag values in paths or URLs.** Folder names are the routed project
  name and a checked StudyInstanceUID; every value in a request path is
  URL-encoded.

## Local layout

```
/etc/hyperwheel/site.toml          site settings            (kept across updates)
/etc/hyperwheel/routing.toml       login -> dataset          (kept across updates)
/etc/hyperwheel/keys/<project>.json  one key per dataset, root only, mode 600
/var/lib/hyperwheel/state.db       state of every study and file
/var/lib/hyperwheel/stagingArea/<project>/<StudyInstanceUID>/DICOM/<SOPInstanceUID>.dcm
/var/lib/hyperwheel/stagingArea/<project>/<StudyInstanceUID>/RRDF/<original name>
/opt/hyperwheel/releases/<version>/  and  /opt/hyperwheel/current -> releases/<version>
```

## Components

One service (`hyperwheel run`) with two loops, plus the Upload Monitor as a
separate read-only service (`hyperwheel dashboard`):

- **Organiser** (`organiser.py`) follows Orthanc's change log (`/changes`),
  not Lua callbacks. A new instance records the study (or reopens it). When
  Orthanc reports the study stable, it is routed by its login: no routing
  entry means it is deleted from Orthanc and never uploaded; a routed study's
  DICOM is copied into the staging area unchanged and hashed, the checks run,
  and the study is ready to upload.
- **Raw-data pull** (`rawpull.py`) reads the scanner's RRDF folders over SSH
  and applies the rule below. The scanner doesn't offer SFTP, so it lists
  folders with `ls` and downloads with the SCP protocol (`scp -f`), as the old
  pipeline did. It only reads.
- **Scanner keys** (`hostkeys.py`): a scanner SSH key the Pi hasn't seen is
  accepted and remembered, never refused. The key changes when the scanner's
  software is upgraded, and a site with two scanners and one Pi moves the Pi to
  the other scanner when one breaks down; both must just work. The Pi keeps
  every key it has seen, matched by fingerprint rather than address (both
  scanners answer on the same address), so moving back and forth is quiet after
  the first time. `hyperwheel status` and the Upload Monitor show a notice for
  24 hours after a new key appears; the Pi's first scanner raises none. What
  this gives up: a device plugged into the scanner cable could pose as the
  scanner.
- **Uploader** (`uploader.py`) sends staged files to GRIP.
- **Upload Monitor** (`dashboard.py`) shows the state store on the hotspot, and on 127.0.0.1 for the Pi's own browser.
- **Reachability** (`reach.py`): each round the service checks whether the
  scanner answers (and, if not, whether the cable has a link) and whether
  GRIP answers. The Pi's internet goes through the scanner, so "the scanner
  isn't answering" is told apart from "no internet". `hyperwheel doctor`
  runs the same checks live, plus Orthanc, the clock, disk and keys.

The Upload Monitor tells operators it is safe to switch the scanner off only
once everything is on GRIP. Until then it says "Please keep the scanner on":
the Pi reaches the internet through the scanner, and it isn't meant to keep
data. When everything that can upload is on GRIP and only refused files
(tried again once a day) or sessions held for a check are left, it says "Safe
to switch off. Some files are not on GRIP. Ask your Hyperwheel admin if they
stay.", not in green. (An old
`[dashboard] leave_on_after_hours` setting is ignored.)

The local loop (Orthanc, scanner, housekeeping) and the upload loop run
side by side, so a slow upload never delays fetching raw data from the
scanner. Uploads wait until the Pi's clock has been set from the network.

## State

Every study and file has an explicit state in SQLite (`src/hyperwheel/state.py`
lists them all). Each change is committed before the action that depends on
it, so after a reboot or power cut the pipeline resumes:

- a file marked `confirmed` but still on disk is deleted on the next run;
- a raw file in `sending` resumes from GRIP's `Upload-Offset`;
- rows stay after a file is deleted, so a raw file still on the scanner is
  never pulled or uploaded again.

Study: `receiving → stable → checking → ready → uploading → uploaded → archived`,
with `held` for unsafe problems and `unrouted → purged` for logins with no
routing entry. A new instance sends a study back to `receiving`; a late raw
file sends it back to `uploading`.

File: `staged`/`pulled → sending → confirmed → deleted`, with `retry_wait`
(timeouts, 5xx, 429) and `refused` (other 4xx and STOW failure reasons: kept
and flagged). A file GRIP refused is tried again once a day, since most
refusals are something GRIP fixes on its side: if GRIP takes it, it goes up
and its flag clears, and if not, it stays refused and flagged. A problem with
the Pi's own copy (missing, changed or empty) isn't tried again. A raw file
listed on the scanner but not fetched yet is `seen`.

## Upload order and retries

All DICOM first (every study), then raw files (agreed with GRIP). A study's
raw files go once GRIP has the study, i.e. at least one of its DICOM files is
confirmed and none is still unsent. A refused DICOM file stays flagged on its
own and doesn't hold the raw files back. One file per request.

- DICOM: STOW-RS `POST /dicomweb/studies/{StudyInstanceUID}`.
- Raw files: `PUT /v1/studies/{StudyInstanceUID}/raw/{name}` in 8 MiB chunks
  with `Content-Range` (and `X-Chunk-SHA256`), the name URL-encoded, no series.
- Resume: the Pi asks GRIP (`HEAD` on the raw URL). If GRIP has part of the
  file at the same size, it continues from GRIP's offset; otherwise (nothing
  there, another size, or GRIP dropped or won't continue the partial upload)
  it sends the whole file again, which overwrites. Neither raises a flag. If
  GRIP has the whole file already, only the last chunk is sent again, to get
  the file's own `complete: true`.
- What GRIP has of a study: `GET /v1/studies/{StudyInstanceUID}` (404 =
  nothing yet).
- The key is checked (a token) before any file of a study is touched, so a
  bad key leaves the files exactly as they were.
- Every project's key is also checked when the service starts (so after every
  update) and every 12 hours, with a fresh token from GRIP and no upload. The
  Upload Monitor then shows each key as working or not, even when nothing is
  waiting, and a key that fails pauses only its own project.
- Slow links (tested with `tc netem` on the dev Pi): a request's body has to
  be sent within the connect timeout, so each request gets the time its size
  needs at `slowest_link_bytes_per_s` (16 kB/s). A raw chunk that gets no
  answer is tried again at half the size, down to `min_chunk_bytes`; chunks
  also halve when one takes longer than `target_chunk_s` (30 s), and grow
  back after 8 quick ones. Small chunks also keep the link's queue
  short, so token requests and reachability checks aren't stuck behind them.
- An upload GRIP answered in the last 90 s counts as proof that the scanner
  and the internet work, so a quick check that times out behind busy upload
  traffic doesn't raise a false alarm on the Upload Monitor or in
  `hyperwheel doctor`.

GRIP's retry rule: 429 and 503 after `Retry-After`; other 5xx and timeouts
with back-off and jitter; a new token on 401; other 4xx are never retried and
are reported. When a call still fails, the file waits in `retry_wait` and is
tried again later with a growing delay.

## Raw data to session

The scanner keeps each exam's raw files in `RRDF/rrdf_<StudyDate>_<StudyTime>/`
(scanner local time, to the second). A folder whose name is the StudyDate and
StudyTime of a routed study on this Pi is that study's, and every `.h5` file in
it belongs to that study. Nothing else is checked: not the time in a file's
name, and not the next study's start.

A folder that matches no routed study is never pulled. Every `.h5` file in a
matched folder is fetched, unless GRIP would refuse its name (empty or only
spaces, a `/`, `\` or `..` in it, or over 255 bytes, or a
space at either end): that one stays on
the scanner and is flagged. Names stay unchanged. A file is pulled only once
its size is the same on two listings. Folders are checked again for late files
for 7 days. Releases before 0.1.16 held some raw files as unassigned: on
update, they move into the study their folder names.

File modification times on the scanner are not used: they are shown in a
different time zone from the names.

Raw files aren't matched to series, and the Pi never flags missing raw data.
Timestamps between series and raw files don't always line up, and DICOM and
raw data go to GRIP separately. Whatever raw data the study's folder holds is
uploaded, and a data manager sees on GRIP if something is missing. A site that
saves no raw data sets `rrdf.enabled = false`.

## Checks

Only unsafe problems hold anything back, and only what is unsafe is held:

- mixed logins in one study: the study is held (not staged, not uploaded,
  not deleted);
- an invalid UID: that image stays in Orthanc and is not uploaded.

Everything else is flagged only, and the data goes to GRIP unchanged: a
blank, padded or unexpected Patient ID (`patient_id_pattern` per project in
the routing file), a Patient ID that differs from an ID in the patient name,
a phantom not under the site's phantom ID or a participant under it, the
same ID twice in one day, and a different sex or birth date for the same
ID. Missing raw data is never flagged. The phantom check uses the site's
`phantom_patient_id`; a site without one skips it.

Patient data in the state store (Patient ID, name, sex, birth date) is
cleared when a study is archived, 7 days after it was last stable, and
straight away for a login with no routing entry.

## Upload Monitor

The page shows one headline for the operator ("Receiving images", "Fetching
raw data", "Uploading to GRIP", "Waiting for a GRIP key" when every file
still to upload belongs to a project with no working key, "Everything is on
GRIP", or "Hyperwheel is not running") and whether everything from the
scanner is on the Pi. Below that, for each session: the project, the Patient
ID as sent, its state, how many DICOM and raw files are on GRIP, and its flags
in plain language; then each project's key status. Studies from logins with no routing entry are
never shown. The page reads the state store read-only and inserts no data
as HTML.

## Keys

One key (client ID + secret) per dataset, stored root-only and read by the
service through systemd's `LoadCredential=`. A key is tested against GRIP
before it replaces the old one. The state store keeps each project's key
status (`working`, `rejected`, `missing`, `unreadable`); a key that stops
working pauses that project's uploads, keeps every file and raises an alert.
Keys never go in the image, the repository, logs or process arguments.

## Updates

Signed, versioned releases published on GitHub, pulled by each Pi without
credentials. Each release pins every dependency (Python wheels with hashes,
the arm64 Orthanc Explorer 2 plugin, OS packages as `.deb` files when needed).
Channels (`dev`, `pilot`, `stable`) name the version each group of Pis runs.
The updater (`hyperwheel update`, run by a timer every 6 hours) checks the
channel file and the release's signature and files, waits until nothing is
arriving from the scanner, installs with the release's own `install.sh`,
then checks health and every project's key; if either fails it puts the
previous release back with that release's `install.sh`. The state database
is never restored: its migrations only add, so the older release can use it
(`docs/releases.md`).

## Testing

- Unit tests run against `tests/fakegrip.py`, a local fake of GRIP's mock API
  with fault injection (dropped connections before and after storing, expired
  tokens, unlisted STOW results, `X-Mock-Fault`).
- `tests/integration/` runs the uploader against GRIP's hosted mock with
  synthetic data only (see the module docstring for the settings).
