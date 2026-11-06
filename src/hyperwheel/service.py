"""The Hyperwheel service: two loops in one process.

- The local loop follows Orthanc, fetches raw data from the scanner and does
  housekeeping. It only talks to this Pi and the scanner, so it keeps going
  while a slow upload is running.
- The upload loop sends staged files to GRIP.

Each loop has its own connection to the state store. Anything unexpected is
logged and the loop carries on after a pause; nothing is deleted on an error.
"""

from __future__ import annotations

import fcntl
import logging
import signal
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from . import reach
from .config import Routing, SiteConfig
from .dashboard import HEARTBEAT, clock_synced
from .keys import KeyStore, read_secret_json
from .orthanc import Orthanc, OrthancClient, OrthancError
from .organiser import Organiser
from .rawpull import RawPuller, ScannerFiles, SshScanner
from .state import StateError, Store
from .uploader import Uploader

log = logging.getLogger("hyperwheel.service")

ETC = Path("/etc/hyperwheel")


class Service:
    def __init__(self, site: SiteConfig, routing: Routing, *, orthanc: Callable[[], Orthanc] | None = None,
                 scanner: Callable[[], ScannerFiles] | None = None, keys: KeyStore | None = None,
                 clock: Callable[[], float] = time.time, synced: Callable[[], bool] = clock_synced,
                 scanner_probe: Callable[[SiteConfig], int | None] = reach.scanner_state,
                 internet_probe: Callable[[Routing], bool | None] = reach.internet_state,
                 local_every_s: float = 15.0, upload_every_s: float = 30.0, housekeeping_every_s: float = 600.0,
                 **uploader_options):
        self.site, self.routing, self.clock, self.synced = site, routing, clock, synced
        self.scanner_probe, self.internet_probe = scanner_probe, internet_probe
        self.orthanc_factory = orthanc or self._orthanc
        self.scanner_factory = scanner or self._scanner
        self.keys = keys or KeyStore.for_process(site.keys_dir)
        self.local_every_s, self.upload_every_s = local_every_s, upload_every_s
        self.housekeeping_every_s = housekeeping_every_s
        self.uploader_options = uploader_options
        self.stop = threading.Event()
        self._last_housekeeping = 0.0
        # Every key is checked when the service starts (so after every update) and then
        # every 12 hours, without waiting for something to upload.
        self.key_check_every_s = 12 * 3600
        self._last_key_check = float("-inf")

    # ---- real connections -----------------------------------------------------------

    def _orthanc(self) -> Orthanc:
        login = read_secret_json("orthanc", ETC / "orthanc.json")
        return OrthancClient(self.site.orthanc.url, auth=(login["username"], login["password"]),
                             timeout_s=self.site.orthanc.timeout_s)

    def _scanner(self) -> ScannerFiles:
        s = self.site.scanner
        return SshScanner(s.host, s.port, s.username, lambda: read_secret_json("scanner", ETC / "scanner.json")["password"],
                          Path(s.known_hosts), s.rrdf_dir, s.timeout_s)

    # ---- one round of each loop (also used by tests) -----------------------------------

    def local_round(self, store: Store, organiser: Organiser, puller: RawPuller) -> None:
        store.set_cursor(HEARTBEAT, int(self.clock()))
        reach.record_scanner(store, self.scanner_probe(self.site), self.clock())
        organiser.follow()
        puller.pull_once()
        if self.clock() - self._last_housekeeping >= self.housekeeping_every_s:
            organiser.housekeeping()
            self._last_housekeeping = self.clock()

    def upload_round(self, uploader: Uploader) -> None:
        reach.record_internet(uploader.store, self.internet_probe(self.routing), self.clock())
        if not self.synced():
            log.info("clock not set yet; uploads wait")
            return
        if self.clock() - self._last_key_check >= self.key_check_every_s:
            uploader.check_keys()
            self._last_key_check = self.clock()
        uploader.run_once()

    # ---- the loops --------------------------------------------------------------------

    def _local_loop(self) -> None:
        store = Store(self.site.state_db, clock=self.clock)
        orthanc = self.orthanc_factory()
        organiser = Organiser(store, self.site, self.routing, orthanc, clock=self.clock)
        puller = RawPuller(store, self.site, self.scanner_factory, clock=self.clock)
        self._loop("local", self.local_every_s, lambda: self.local_round(store, organiser, puller))
        store.close()

    def _upload_loop(self) -> None:
        store = Store(self.site.state_db, clock=self.clock)
        uploader = Uploader(store, self.site, self.routing, self.keys, self.orthanc_factory(), clock=self.clock,
                            **self.uploader_options)
        self._loop("upload", self.upload_every_s, lambda: self.upload_round(uploader))
        store.close()

    def _loop(self, name: str, every_s: float, round_: Callable[[], None]) -> None:
        while not self.stop.is_set():
            try:
                round_()
            except (OrthancError, StateError) as e:
                log.warning("%s loop: %s; trying again shortly", name, e)
            except Exception:
                log.exception("%s loop: unexpected error; trying again shortly", name)
            self.stop.wait(every_s)

    def run(self) -> None:
        with single_instance(self.site.state_db):
            self._run()

    def _run(self) -> None:
        self.site.staging_dir.mkdir(parents=True, exist_ok=True)
        Store(self.site.state_db).close()  # create or migrate before the loops start
        threads = [threading.Thread(target=self._local_loop, name="local", daemon=True),
                   threading.Thread(target=self._upload_loop, name="upload", daemon=True)]
        for t in threads:
            t.start()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop.set())
        log.info("Hyperwheel started for site %s", self.site.site)
        while not self.stop.is_set():
            self.stop.wait(1)
        for t in threads:
            t.join(timeout=120)
        log.info("Hyperwheel stopped")


class AlreadyRunning(RuntimeError):
    """Another Hyperwheel service already works on this state store."""


@contextmanager
def single_instance(state_db: Path):
    """Hold a lock beside the state store for as long as the service runs.

    Two copies working on the same files would upload and delete them twice over
    (seen when a test runner was left behind). The lock is released when the
    process ends, however it ends.
    """
    state_db.parent.mkdir(parents=True, exist_ok=True)
    fh = open(state_db.with_name(state_db.name + ".lock"), "a+")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning(f"another Hyperwheel service is already running on {state_db}") from None
        yield
    finally:
        fh.close()
