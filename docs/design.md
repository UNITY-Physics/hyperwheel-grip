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
  the GRIP project, its API address and its key (one key per project). A
  login with no routing entry is never uploaded: its DICOM is deleted on the
  Pi and its raw files are never pulled from the scanner.
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
/etc/hyperwheel/routing.toml       login -> project          (kept across updates)
/etc/hyperwheel/keys/<project>.json  one key per project, root only, mode 600
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
  (host key pinned at install) and applies the rule below. The scanner
  doesn't offer SFTP, so it lists folders with `ls` and downloads with the
  SCP protocol (`scp -f`), as the old pipeline did. It only reads.
- **Uploader** (`uploader.py`) sends staged files to GRIP.
- **Upload Monitor** (`dashboard.py`) shows the state store on the hotspot.
- **Reachability** (`reach.py`): each round the service checks whether the
  scanner answers (and, if not, whether the cable has a link) and whether
  GRIP answers. The Pi's internet goes through the scanner, so "the scanner
  isn't answering" is told apart from "no internet". `hyperwheel doctor`
  runs the same checks live, plus Orthanc, the clock, disk and keys.

The Upload Monitor tells operators it is safe to switch the scanner off once
everything from the scanner is on the Pi: the Pi keeps the files and uploads
them next time the scanner is on. It asks them to leave the scanner on only
when files have waited more than 48 hours or the disk is getting full.

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
(timeouts, 5xx, 429), `refused` (other 4xx and STOW failure reasons: kept and
flagged) and `unassigned` (a raw file not matched to exactly one study: kept
and flagged).

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
(scanner local time, to the second). A raw file is assigned to a study only if
both hold:

1. its folder name equals the StudyDate and StudyTime of exactly one routed
   study on this Pi;
2. the timestamp in its own name (`_YYYYMMDD_HHMMSS.h5`, the acquisition start,
   also scanner local time) lies between that study's start and its last
   acquisition plus 60 minutes, and before the next study's start.

A folder that matches no routed study is never pulled. A file that fails the
second check, or a folder that matches two studies, is pulled into
`stagingArea/_unassigned/`, flagged, and neither uploaded nor deleted. A file
is pulled only once its size is the same on two listings. Folders are checked
again for late files for 7 days.

File modification times on the scanner are not used: they are shown in a
different time zone from the names.

After each pull, a series that should have raw data (ORIGINAL images, not a
localizer) but has no raw file with its exact acquisition time is flagged,
and so is a study whose scanner folder can't be found 10 minutes after it
became stable. A site that saves no raw data sets `rrdf.enabled = false`.

## Checks

Only unsafe problems hold anything back, and only what is unsafe is held:

- mixed logins in one study: the study is held (not staged, not uploaded,
  not deleted);
- an invalid UID: that image stays in Orthanc and is not uploaded;
- an unassigned raw file: that file is kept on the Pi and not uploaded.

Everything else is flagged only, and the data goes to GRIP unchanged: a
blank, padded or unexpected Patient ID (`patient_id_pattern` per project in
the routing file), a Patient ID that differs from an ID in the patient name,
a phantom not under the site's phantom ID or a participant under it, the
same ID twice in one day, a different sex or birth date for the same ID,
and series without raw data. The phantom check uses the site's
`phantom_patient_id`; a site without one skips it.

Patient data in the state store (Patient ID, name, sex, birth date) is
cleared when a study is archived, 7 days after it was last stable, and
straight away for a login with no routing entry.

## Upload Monitor

The page shows one headline for the operator ("Receiving images", "Fetching
raw data", "Uploading to GRIP", "Everything is on GRIP", or "Hyperwheel is
not running") and whether everything from the scanner is on the Pi. Below
that, for each session: the project, the Patient ID as sent, its state, how
many DICOM and raw files are on GRIP, and its flags in plain language; then
each project's key status. Studies from logins with no routing entry are
never shown. The page reads the state store read-only and inserts no data
as HTML.

## Keys

One key (client ID + secret) per project, stored root-only and read by the
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
The updater checks the signature, unpacks the release, copies the state
database, migrates it, switches `current`, restarts and runs a health check;
if the check fails it switches back and restores the database copy. Updates
apply automatically when no study is arriving.

## Testing

- Unit tests run against `tests/fakegrip.py`, a local fake of GRIP's mock API
  with fault injection (dropped connections before and after storing, expired
  tokens, unlisted STOW results, `X-Mock-Fault`).
- `tests/integration/` runs the uploader against GRIP's hosted mock with
  synthetic data only (see the module docstring for the settings).
