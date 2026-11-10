"""Dev Pi only: upload synthetic studies to GRIP's mock while netem.sh shapes eth0.

Like the service: an upload loop, and a separate loop that records whether the scanner and GRIP
answer and prints what the Upload Monitor and `hyperwheel doctor` would say whenever it changes.
Synthetic data only, with its own state store and staging (the --conf folder's site.toml).
A study is staged at the start and at each new phase netem.sh logs (--per-phase of them), so
uploads are running in every phase. Stops when netem.sh is done and nothing is left to upload,
or after <minutes>.

    python netem_upload.py 60 [--conf e2e-netem] [--per-phase 1]
"""
import argparse
import hashlib, io, logging, os, sys, threading, time
from pathlib import Path

import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ImplicitVRLittleEndian, generate_uid

from hyperwheel import doctor, reach
from hyperwheel.config import load_routing, load_site
from hyperwheel.dashboard import HEARTBEAT, snapshot
from hyperwheel.keys import KeyStore
from hyperwheel.state import Store
from hyperwheel.uploader import Uploader

here = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("minutes", type=float)
ap.add_argument("--conf", type=Path, default=here / "e2e-netem", help="folder with site.toml and routing.toml")
ap.add_argument("--per-phase", type=int, default=1, help="synthetic studies staged at each new phase")
args = ap.parse_args()
conf = args.conf
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    datefmt="%H:%M:%S", stream=sys.stdout)
for noisy in ("urllib3", "requests"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("netem-test")
site = load_site(conf / "site.toml")
routing = load_routing(conf / "routing.toml", test_mode=site.test_mode, grip=site.grip)
PROJECT = next(iter(routing.projects))
end = time.time() + args.minutes * 60
stop = threading.Event()


class NoOrthanc:
    """Synthetic studies were never in Orthanc."""

    def delete_instance(self, iid):
        pass

    def delete_study(self, sid):
        pass


def dicom(study_uid, series_uid, n):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.4"
    meta.MediaStorageSOPInstanceUID = sop = generate_uid()
    meta.TransferSyntaxUID = ImplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID, ds.SOPInstanceUID = meta.MediaStorageSOPClassUID, sop
    ds.StudyInstanceUID, ds.SeriesInstanceUID = study_uid, series_uid
    ds.PatientID, ds.PatientName = f"SYNTH-NETEM-{n}", "SYNTHETIC^NETEM"
    ds.StudyDate, ds.StudyTime = "20261105", f"10{n:02d}00"
    ds.Modality, ds.SeriesDescription, ds.OperatorsName = "MR", "synthetic", "site_a/sandbox"
    ds.Rows = ds.Columns = 64
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit, ds.PixelRepresentation = 15, 0
    ds.PixelData = os.urandom(64 * 64 * 2)
    buf = io.BytesIO()
    pydicom.dcmwrite(buf, ds, enforce_file_format=True)
    return sop, buf.getvalue()


def stage(store, n):
    uid, series = generate_uid(), generate_uid()
    study = store.add_study(uid, state="ready", project=PROJECT, study_date="20261105", study_time=f"10{n:02d}00",
                            patient_id=f"SYNTH-NETEM-{n}", patient_name="SYNTHETIC^NETEM",
                            operators_name="site_a/sandbox")
    base = site.staging_dir / PROJECT / uid
    for _ in range(3):
        sop, data = dicom(uid, series, n)
        path = base / "DICOM" / f"{sop}.dcm"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        store.add_file(kind="dicom", name=path.name, state="staged", study_id=study.id, path=str(path), sop_uid=sop,
                       series_uid=series, size=len(data), sha256=hashlib.sha256(data).hexdigest())
    for i, size in enumerate((12 * 1024 * 1024 + 123, 9 * 1024 * 1024 + 7)):
        name = f"fse_fast_t2_rrdf_20261105_10{n:02d}{i:02d}.h5"
        data = os.urandom(size)
        path = base / "RRDF" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        store.add_file(kind="rrdf", name=name, state="pulled", study_id=study.id, path=str(path),
                       scanner_folder=f"rrdf_20261105_10{n:02d}00", size=size, sha256=hashlib.sha256(data).hexdigest())
    log.info("staged synthetic study %d (3 DICOM, 2 raw files, %.0f MB)", n, (21 * 1024 * 1024) / 1e6)


NETEM_LOG = here / "netem.log"


def netem_lines():
    try:
        return NETEM_LOG.read_text().splitlines()
    except OSError:
        return []


def upload_loop():
    store = Store(site.state_db)
    up = Uploader(store, site, routing, KeyStore(site.keys_dir), NoOrthanc())
    n, phases_seen = 1, 0
    stage(store, n)
    while not stop.is_set() and time.time() < end:
        lines = netem_lines()
        phases = [l for l in lines if " PHASE " in l]
        for line in phases[phases_seen:]:
            log.info("netem: %s", line)
            for _ in range(args.per_phase):
                n += 1
                stage(store, n)
        phases_seen = len(phases)
        if any(l.endswith(" done") for l in lines) and not store.files(states=("staged", "pulled", "sending",
                                                                                "retry_wait")):
            log.info("netem.sh is done and everything is uploaded")
            stop.set()
            break
        reach.record_internet(store, reach.internet_state(routing), time.time())
        r = up.run_once()
        if r.confirmed or r.parked or r.refused:
            log.info("upload round: %d confirmed, %d parked, %d refused", r.confirmed, r.parked, r.refused)
        stop.wait(10)
    store.close()


def status_loop():
    store = Store(site.state_db)
    last, last_doctor = None, 0.0
    while not stop.is_set() and time.time() < end:
        now = time.time()
        store.set_cursor(HEARTBEAT, int(now))
        reach.record_scanner(store, reach.scanner_state(site), now)
        s = snapshot(store, site, routing, now=now, clock_synced=True)
        files = store.files()
        counts = {st: sum(f.state == st for f in files) for st in ("staged", "pulled", "sending", "retry_wait",
                                                                   "refused", "deleted")}
        view = (s["headline"], s["badge"], tuple(f"[{g['level']}] {g['text']}" for g in s["general"]),
                tuple(sorted({f['text'] for c in s["studies"] for f in c["flags"]})))
        if view != last:
            print(f"=== {time.strftime('%H:%M:%S')} MONITOR: {s['headline']} | {s['badge']} | files {counts}",
                  *[f"    {g}" for g in view[2]], *[f"    study flag: {t}" for t in view[3]], sep="\n", flush=True)
            last = view
        if now - last_doctor >= 120:
            results, code = doctor.run(site, routing)
            keep = [r for r in results if r[1] in ("Scanner", "GRIP over the internet")]
            print(f"=== {time.strftime('%H:%M:%S')} DOCTOR (exit {code}):",
                  *[f"    {r} {w}: {d}" for r, w, d in keep], sep="\n", flush=True)
            last_doctor = now
        stop.wait(10)
    store.close()


try:
    NETEM_LOG.unlink()  # a log from an earlier run
except OSError:
    pass
Store(site.state_db).close()  # create the database before the two loops open it
threads = [threading.Thread(target=upload_loop), threading.Thread(target=status_loop)]
for t in threads:
    t.start()
for t in threads:
    t.join()
store = Store(site.state_db, readonly=True)
files = store.files()
print("=== FINAL", {st: sum(f.state == st for f in files) for st in ("staged", "pulled", "sending", "retry_wait",
                                                                     "refused", "confirmed", "deleted")}, flush=True)
for f in files:
    if f.state not in ("deleted",):
        print(f"    {f.kind} {f.name[:40]} {f.state} offset={f.upload_offset} attempts={f.attempts} {f.last_error}")
print("    flags:", sorted({fl.code for fl in store.open_flags()}))
left = [p for p in site.staging_dir.rglob("*") if p.is_file()]
print(f"    staged files left on disk: {len(left)}", flush=True)
