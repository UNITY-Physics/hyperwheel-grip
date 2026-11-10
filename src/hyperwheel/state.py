"""The durable local record of every study and every file.

One SQLite database. Every state change is committed before the action that
depends on it (a file is marked ``confirmed`` before it is deleted), so after
a reboot or power cut the pipeline picks up where it stopped.

Study states
    receiving   instances are arriving
    stable      Orthanc reports the study stable; routing is decided next
    unrouted    no routing entry for the login: delete on the Pi, never upload
    purged      unrouted study deleted (a small record is kept for late instances)
    checking    operator-error checks are running
    held        an unsafe problem needs a data manager (see flags)
    ready       checks done, waiting to upload
    uploading   files are going up (all DICOM first, then raw files)
    uploaded    every known file confirmed by GRIP and deleted locally;
                still watched for late DICOM and raw files
    archived    no longer watched

A new instance sends a study back to ``receiving``; a late raw file sends it
back to ``uploading``. Sessions on GRIP never close.

File states (DICOM and RRDF)
    staged      DICOM copied from Orthanc into the staging area, hashed
    seen        RRDF listed on the scanner, not pulled yet
    pulled      RRDF pulled into the staging area unchanged, hashed
    unassigned  RRDF pulled but not matched to one study: kept and flagged
    sending     an upload is in progress
    retry_wait  timeout, 5xx or 429: tried again after next_attempt_at
    refused     GRIP refused it (other 4xx, a STOW failure reason, two
                checksum resets): kept, flagged, retried only on request
    confirmed   GRIP confirmed this file's own upload; local delete pending
    deleted     local copy (and Orthanc's) deleted; the row is kept so the
                file is never pulled or uploaded again by mistake (a DICOM
                instance the scanner sends again goes back to staged)

Changes to the database only add (see ``_SCHEMA``), so that the previous
release can still open a database a newer release has migrated, and a
rollback never has to restore an older copy (which would lose what happened
since the update). The previous release ignores columns and tables it doesn't
know, and leaves alone rows in states it doesn't know.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from pathlib import Path

from .ids import is_uid

log = logging.getLogger("hyperwheel.state")

STUDY_STATES = (
    "receiving", "stable", "unrouted", "purged", "checking", "held", "ready",
    "uploading", "uploaded", "archived",
)
FILE_STATES = (
    "staged", "seen", "pulled", "unassigned", "sending", "retry_wait", "refused",
    "confirmed", "deleted",
)

_STUDY_MOVES = {
    "receiving": {"stable"},
    "stable": {"unrouted", "checking"},
    "unrouted": {"purged", "stable"},
    "purged": {"unrouted", "stable"},  # stable: sent again once its login has a dataset
    "checking": {"held", "ready"},
    "held": {"ready", "checking"},
    "ready": {"uploading"},
    "uploading": {"uploaded", "ready"},
    "uploaded": {"uploading", "archived"},
    "archived": {"uploading"},
}
# Any routed study goes back to receiving when a new instance arrives.
for _s in ("stable", "checking", "held", "ready", "uploading", "uploaded", "archived"):
    _STUDY_MOVES[_s] = _STUDY_MOVES[_s] | {"receiving"}

_FILE_MOVES = {
    "seen": {"pulled", "unassigned"},
    "unassigned": {"pulled"},
    "staged": {"sending"},
    "pulled": {"sending"},
    "sending": {"sending", "retry_wait", "refused", "confirmed"},
    "retry_wait": {"sending"},
    "refused": {"staged", "pulled"},
    "confirmed": {"deleted"},
    # The scanner sent an instance again after it was uploaded and deleted.
    "deleted": {"staged"},
}

# Files the uploader still has to send.
PENDING_FILE_STATES = ("staged", "pulled", "sending", "retry_wait")
# Files GRIP has (whether or not the local delete has happened yet).
DONE_FILE_STATES = ("confirmed", "deleted")

# Each migration may only add: new tables, new non-unique indexes, and new
# columns that are nullable or have a default, so an older release can keep
# inserting rows. Never drop, rename or tighten anything (tests enforce this).
_SCHEMA = [
    # version 1
    """
    CREATE TABLE studies (
        id INTEGER PRIMARY KEY,
        study_uid TEXT NOT NULL UNIQUE,
        orthanc_id TEXT,
        project TEXT,
        state TEXT NOT NULL,
        study_date TEXT,
        study_time TEXT,
        patient_id TEXT,
        patient_name TEXT,
        operators_name TEXT,
        last_acquisition TEXT,
        grip_session_id TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE files (
        id INTEGER PRIMARY KEY,
        study_id INTEGER REFERENCES studies(id),
        kind TEXT NOT NULL CHECK (kind IN ('dicom', 'rrdf')),
        name TEXT NOT NULL,
        path TEXT,
        sop_uid TEXT,
        series_uid TEXT,
        orthanc_instance_id TEXT,
        scanner_folder TEXT,
        size INTEGER,
        sha256 TEXT,
        state TEXT NOT NULL,
        upload_offset INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        checksum_resets INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL,
        last_error TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE UNIQUE INDEX files_dicom ON files(study_id, sop_uid) WHERE kind = 'dicom';
    CREATE UNIQUE INDEX files_rrdf ON files(scanner_folder, name) WHERE kind = 'rrdf';
    CREATE INDEX files_by_study ON files(study_id, kind, state);
    CREATE TABLE flags (
        id INTEGER PRIMARY KEY,
        code TEXT NOT NULL,
        detail TEXT NOT NULL,
        study_id INTEGER REFERENCES studies(id),
        file_id INTEGER REFERENCES files(id),
        project TEXT,
        raised_at REAL NOT NULL,
        cleared_at REAL
    );
    CREATE INDEX flags_open ON flags(cleared_at, code);
    CREATE TABLE key_status (
        project TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        detail TEXT,
        checked_at REAL NOT NULL
    );
    """,
    # version 2: what the organiser, the raw pull and the checks need
    """
    ALTER TABLE studies ADD COLUMN stable_at REAL;
    ALTER TABLE studies ADD COLUMN raw_checked_at REAL;
    ALTER TABLE studies ADD COLUMN patient_sex TEXT;
    ALTER TABLE studies ADD COLUMN patient_birth_date TEXT;
    ALTER TABLE files ADD COLUMN series_description TEXT;
    ALTER TABLE files ADD COLUMN acquisition_time TEXT;
    ALTER TABLE files ADD COLUMN image_type TEXT;
    ALTER TABLE flags ADD COLUMN level TEXT NOT NULL DEFAULT 'flag';
    ALTER TABLE flags ADD COLUMN ref TEXT;
    CREATE TABLE cursors (name TEXT PRIMARY KEY, value INTEGER NOT NULL)
    """,
    # version 3: every scanner SSH key this Pi has seen (see hostkeys.py)
    """
    CREATE TABLE scanner_host_keys (
        fingerprint TEXT PRIMARY KEY,
        key_type TEXT NOT NULL,
        host TEXT NOT NULL,
        first_seen REAL NOT NULL,
        last_seen REAL NOT NULL,
        quiet INTEGER NOT NULL DEFAULT 0
    )
    """,
]


class StateError(Exception):
    pass


def _make(cls, row):
    """A dataclass from a row, ignoring columns a newer release added."""
    names = _FIELDS.get(cls)
    if names is None:
        names = _FIELDS[cls] = {f.name for f in dataclass_fields(cls)}
    return cls(**{k: row[k] for k in row.keys() if k in names})


_FIELDS: dict[type, set[str]] = {}


@dataclass(frozen=True)
class Study:
    id: int
    study_uid: str
    orthanc_id: str | None
    project: str | None
    state: str
    study_date: str | None
    study_time: str | None
    patient_id: str | None
    patient_name: str | None
    operators_name: str | None
    last_acquisition: str | None
    grip_session_id: str | None
    created_at: float
    updated_at: float
    stable_at: float | None = None
    raw_checked_at: float | None = None
    patient_sex: str | None = None
    patient_birth_date: str | None = None

    def __repr__(self) -> str:  # keep patient data out of logs and tracebacks
        return f"Study(id={self.id}, uid={self.study_uid}, project={self.project}, state={self.state})"


@dataclass(frozen=True)
class File:
    id: int
    study_id: int | None
    kind: str
    name: str
    path: str | None
    sop_uid: str | None
    series_uid: str | None
    orthanc_instance_id: str | None
    scanner_folder: str | None
    size: int | None
    sha256: str | None
    state: str
    upload_offset: int
    attempts: int
    checksum_resets: int
    next_attempt_at: float | None
    last_error: str | None
    created_at: float
    updated_at: float
    series_description: str | None = None
    acquisition_time: str | None = None
    image_type: str | None = None


@dataclass(frozen=True)
class Flag:
    id: int
    code: str
    detail: str
    study_id: int | None
    file_id: int | None
    project: str | None
    raised_at: float
    cleared_at: float | None
    level: str = "flag"  # "hold" (stops something until a person acts), "alert" or "flag"
    ref: str | None = None


@dataclass(frozen=True)
class HostKey:
    fingerprint: str  # "SHA256:..." as OpenSSH shows it
    key_type: str
    host: str  # where it was last seen
    first_seen: float
    last_seen: float
    quiet: int = 0  # 1: taken as known without a notice (the first scanner, or the setup's known_hosts)


FLAG_LEVELS = ("hold", "alert", "flag")

_FILE_UPDATABLE = {
    "path", "size", "sha256", "upload_offset", "attempts", "checksum_resets", "next_attempt_at",
    "last_error", "study_id",
}
_STUDY_UPDATABLE = {
    "orthanc_id", "project", "study_date", "study_time", "patient_id", "patient_name",
    "operators_name", "last_acquisition", "grip_session_id", "stable_at", "raw_checked_at", "patient_sex",
    "patient_birth_date",
}
_FILE_DESCRIPTIVE = {
    "sop_uid", "series_uid", "orthanc_instance_id", "scanner_folder", "series_description", "acquisition_time",
    "image_type",
}


class Store:
    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time, readonly: bool = False):
        """Open the store. ``readonly`` is for the dashboard: it can't change anything and never migrates."""
        self.clock = clock
        if readonly:
            self._db = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, isolation_level=None,
                                       timeout=30)
            self._db.row_factory = sqlite3.Row
            if self.schema_version < len(_SCHEMA):
                raise StateError(f"state database is schema version {self.schema_version}. This release expects "
                                 f"at least {len(_SCHEMA)}")
            return
        self._db = sqlite3.connect(str(path), isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = FULL")
        self._db.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self._db.in_transaction:
            yield self._db
            return
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def _migrate(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > len(_SCHEMA):
            # Written by a newer release (e.g. after a rollback). Its changes only
            # add, so this release can work with it as it is; never downgrade it.
            log.warning("state database is schema version %d, newer than this release's %d. Using it as it is",
                        version, len(_SCHEMA))
            return
        for number, script in enumerate(_SCHEMA[version:], start=version + 1):
            with self.transaction() as db:
                for statement in script.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute(f"PRAGMA user_version = {number}")

    @property
    def schema_version(self) -> int:
        return self._db.execute("PRAGMA user_version").fetchone()[0]

    # ---- studies ------------------------------------------------------------

    def add_study(self, study_uid: str, *, state: str = "receiving", **fields) -> Study:
        if not is_uid(study_uid):
            raise StateError("StudyInstanceUID is not a valid UID (digits and dots, at most 64 characters)")
        if state not in STUDY_STATES:
            raise StateError(f"unknown study state {state!r}")
        bad = set(fields) - _STUDY_UPDATABLE
        if bad:
            raise StateError(f"unknown study field(s) {sorted(bad)}")
        now = self.clock()
        cols = ["study_uid", "state", "created_at", "updated_at", *fields]
        vals = [study_uid, state, now, now, *fields.values()]
        with self.transaction() as db:
            cur = db.execute(
                f"INSERT INTO studies ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals
            )
            return self._study(db, cur.lastrowid)

    def study(self, study_id: int) -> Study:
        return self._study(self._db, study_id)

    def study_by_uid(self, study_uid: str) -> Study | None:
        row = self._db.execute("SELECT * FROM studies WHERE study_uid = ?", (study_uid,)).fetchone()
        return _make(Study, row) if row else None

    def studies(self, states: Iterable[str] | None = None) -> list[Study]:
        if states is None:
            rows = self._db.execute("SELECT * FROM studies ORDER BY created_at, id").fetchall()
        else:
            states = tuple(states)
            rows = self._db.execute(
                f"SELECT * FROM studies WHERE state IN ({', '.join('?' * len(states))}) ORDER BY created_at, id",
                states,
            ).fetchall()
        return [_make(Study, r) for r in rows]

    def move_study(self, study_id: int, new_state: str, **fields) -> Study:
        bad = set(fields) - _STUDY_UPDATABLE
        if bad:
            raise StateError(f"unknown study field(s) {sorted(bad)}")
        with self.transaction() as db:
            current = self._study(db, study_id)
            if new_state != current.state and new_state not in _STUDY_MOVES.get(current.state, ()):
                raise StateError(f"study {study_id}: can't go from {current.state} to {new_state}")
            sets = ["state = ?", "updated_at = ?", *(f"{k} = ?" for k in fields)]
            db.execute(
                f"UPDATE studies SET {', '.join(sets)} WHERE id = ?",
                [new_state, self.clock(), *fields.values(), study_id],
            )
            return self._study(db, study_id)

    def update_study(self, study_id: int, **fields) -> Study:
        current = self.study(study_id)
        return self.move_study(study_id, current.state, **fields)

    @staticmethod
    def _study(db: sqlite3.Connection, study_id: int) -> Study:
        row = db.execute("SELECT * FROM studies WHERE id = ?", (study_id,)).fetchone()
        if row is None:
            raise StateError(f"no study with id {study_id}")
        return _make(Study, row)

    # ---- files --------------------------------------------------------------

    def add_file(self, *, kind: str, name: str, state: str, study_id: int | None, **fields) -> File:
        if kind not in ("dicom", "rrdf"):
            raise StateError(f"unknown file kind {kind!r}")
        if state not in FILE_STATES:
            raise StateError(f"unknown file state {state!r}")
        allowed = _FILE_UPDATABLE | _FILE_DESCRIPTIVE
        bad = set(fields) - allowed
        if bad:
            raise StateError(f"unknown file field(s) {sorted(bad)}")
        if kind == "dicom" and not is_uid(fields.get("sop_uid")):
            raise StateError("a DICOM file needs a valid SOP Instance UID")
        now = self.clock()
        cols = ["kind", "name", "state", "study_id", "created_at", "updated_at", *fields]
        vals = [kind, name, state, study_id, now, now, *fields.values()]
        with self.transaction() as db:
            cur = db.execute(
                f"INSERT INTO files ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals
            )
            return self._file(db, cur.lastrowid)

    def file(self, file_id: int) -> File:
        return self._file(self._db, file_id)

    def files(self, study_id: int | None = None, *, kind: str | None = None,
              states: Iterable[str] | None = None) -> list[File]:
        where, args = [], []
        if study_id is not None:
            where.append("study_id = ?")
            args.append(study_id)
        if kind is not None:
            where.append("kind = ?")
            args.append(kind)
        if states is not None:
            states = tuple(states)
            where.append(f"state IN ({', '.join('?' * len(states))})")
            args.extend(states)
        sql = "SELECT * FROM files" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id"
        return [_make(File, r) for r in self._db.execute(sql, args).fetchall()]

    def rrdf_file(self, scanner_folder: str, name: str) -> File | None:
        row = self._db.execute("SELECT * FROM files WHERE kind = 'rrdf' AND scanner_folder = ? AND name = ?",
                               (scanner_folder, name)).fetchone()
        return _make(File, row) if row else None

    def move_file(self, file_id: int, new_state: str, **fields) -> File:
        bad = set(fields) - _FILE_UPDATABLE
        if bad:
            raise StateError(f"unknown file field(s) {sorted(bad)}")
        with self.transaction() as db:
            current = self._file(db, file_id)
            if new_state not in _FILE_MOVES.get(current.state, ()) or (current.state == "deleted" and current.kind != "dicom"):
                raise StateError(f"file {file_id}: can't go from {current.state} to {new_state}")
            sets = ["state = ?", "updated_at = ?", *(f"{k} = ?" for k in fields)]
            db.execute(
                f"UPDATE files SET {', '.join(sets)} WHERE id = ?",
                [new_state, self.clock(), *fields.values(), file_id],
            )
            return self._file(db, file_id)

    def update_file(self, file_id: int, **fields) -> File:
        """Change fields without changing state (e.g. the saved upload offset)."""
        bad = set(fields) - _FILE_UPDATABLE
        if bad:
            raise StateError(f"unknown file field(s) {sorted(bad)}")
        with self.transaction() as db:
            sets = ["updated_at = ?", *(f"{k} = ?" for k in fields)]
            db.execute(f"UPDATE files SET {', '.join(sets)} WHERE id = ?", [self.clock(), *fields.values(), file_id])
            return self._file(db, file_id)

    def update_descriptive(self, file_id: int, **fields) -> File:
        """Change what is known about a file (series, Orthanc ID...), never its state."""
        bad = set(fields) - _FILE_DESCRIPTIVE
        if bad:
            raise StateError(f"unknown file field(s) {sorted(bad)}")
        with self.transaction() as db:
            sets = ["updated_at = ?", *(f"{k} = ?" for k in fields)]
            db.execute(f"UPDATE files SET {', '.join(sets)} WHERE id = ?", [self.clock(), *fields.values(), file_id])
            return self._file(db, file_id)

    @staticmethod
    def _file(db: sqlite3.Connection, file_id: int) -> File:
        row = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        if row is None:
            raise StateError(f"no file with id {file_id}")
        return _make(File, row)

    # ---- flags --------------------------------------------------------------

    def raise_flag(self, code: str, detail: str, *, study_id: int | None = None, file_id: int | None = None,
                   project: str | None = None, level: str = "flag", ref: str | None = None) -> Flag:
        """Raise a flag, or update the open flag with the same code and target."""
        if level not in FLAG_LEVELS:
            raise StateError(f"unknown flag level {level!r}")
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM flags WHERE cleared_at IS NULL AND code = ? AND study_id IS ? AND file_id IS ? "
                "AND project IS ? AND ref IS ?",
                (code, study_id, file_id, project, ref),
            ).fetchone()
            if row:
                if row["detail"] != detail or row["level"] != level:
                    db.execute("UPDATE flags SET detail = ?, level = ? WHERE id = ?", (detail, level, row["id"]))
                return _make(Flag, {**dict(row), "detail": detail, "level": level})
            cur = db.execute(
                "INSERT INTO flags (code, detail, study_id, file_id, project, raised_at, level, ref) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (code, detail, study_id, file_id, project, self.clock(), level, ref),
            )
            return _make(Flag, db.execute("SELECT * FROM flags WHERE id = ?", (cur.lastrowid,)).fetchone())

    def clear_flags(self, code: str, *, study_id: int | None = None, file_id: int | None = None,
                    project: str | None = None, ref: str | None = None) -> int:
        with self.transaction() as db:
            cur = db.execute(
                "UPDATE flags SET cleared_at = ? WHERE cleared_at IS NULL AND code = ? AND study_id IS ? "
                "AND file_id IS ? AND project IS ? AND ref IS ?",
                (self.clock(), code, study_id, file_id, project, ref),
            )
            return cur.rowcount

    def clear_study_flags(self, study_id: int, codes: Iterable[str]) -> None:
        """Clear these flags on the study and on its files."""
        codes = tuple(codes)
        if not codes:
            return
        with self.transaction() as db:
            db.execute(
                f"UPDATE flags SET cleared_at = ? WHERE cleared_at IS NULL AND study_id = ? "
                f"AND code IN ({', '.join('?' * len(codes))})",
                (self.clock(), study_id, *codes),
            )

    def open_flags(self, *, study_id: int | None = None, project: str | None = None) -> list[Flag]:
        where, args = ["cleared_at IS NULL"], []
        if study_id is not None:
            where.append("study_id = ?")
            args.append(study_id)
        if project is not None:
            where.append("project = ?")
            args.append(project)
        rows = self._db.execute(f"SELECT * FROM flags WHERE {' AND '.join(where)} ORDER BY id", args).fetchall()
        return [_make(Flag, r) for r in rows]

    # ---- cursors (e.g. the last Orthanc change handled) -----------------------

    def cursor(self, name: str) -> int:
        row = self._db.execute("SELECT value FROM cursors WHERE name = ?", (name,)).fetchone()
        return row["value"] if row else 0

    def set_cursor(self, name: str, value: int) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO cursors (name, value) VALUES (?, ?) "
                       "ON CONFLICT(name) DO UPDATE SET value = excluded.value", (name, value))

    def last_arrival(self) -> float | None:
        """When the newest study or file was first recorded (the updater waits until this is long ago)."""
        row = self._db.execute("SELECT max(t) AS t FROM (SELECT max(created_at) AS t FROM files "
                               "UNION ALL SELECT max(created_at) FROM studies)").fetchone()
        return row["t"]

    # ---- scanner host keys ---------------------------------------------------

    # last_seen is written at most this often, not on every connection.
    HOST_KEY_TOUCH_S = 3600

    def remember_host_key(self, fingerprint: str, key_type: str, host: str, *, quiet: bool = False) -> bool:
        """Record a scanner key. True if this Pi had never seen it before."""
        now = self.clock()
        with self.transaction() as db:
            cur = db.execute("INSERT INTO scanner_host_keys (fingerprint, key_type, host, first_seen, last_seen, "
                             "quiet) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(fingerprint) DO NOTHING",
                             (fingerprint, key_type, host, now, now, int(quiet)))
            if cur.rowcount:
                return True
            db.execute("UPDATE scanner_host_keys SET last_seen = ?, host = ? WHERE fingerprint = ? "
                       "AND (last_seen < ? OR host != ?)",
                       (now, host, fingerprint, now - self.HOST_KEY_TOUCH_S, host))
            return False

    def host_keys(self) -> list[HostKey]:
        rows = self._db.execute("SELECT * FROM scanner_host_keys ORDER BY first_seen, fingerprint").fetchall()
        return [_make(HostKey, r) for r in rows]

    # ---- key status ---------------------------------------------------------

    def set_key_status(self, project: str, status: str, detail: str | None = None) -> None:
        if status not in ("working", "rejected", "missing", "unreadable", "unknown"):
            raise StateError(f"unknown key status {status!r}")
        with self.transaction() as db:
            db.execute(
                "INSERT INTO key_status (project, status, detail, checked_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project) DO UPDATE SET status = excluded.status, detail = excluded.detail, "
                "checked_at = excluded.checked_at",
                (project, status, detail, self.clock()),
            )

    def forget_project(self, project: str) -> None:
        """A dataset taken off this Pi: its key status and its open key flags go."""
        with self.transaction() as db:
            db.execute("DELETE FROM key_status WHERE project = ?", (project,))
            db.execute("UPDATE flags SET cleared_at = ? WHERE cleared_at IS NULL AND project = ?",
                       (self.clock(), project))

    def drop_study(self, study_id: int) -> list[str]:
        """Drop a study that was waiting to upload to a dataset just removed from this Pi, after the person
        removing it confirmed its files may go. Its file records and flags are deleted, and it becomes unrouted
        with no dataset, so the organiser deletes it from Orthanc as it does a study from a login with no
        dataset. Returns the paths of its local copies, for the caller to delete (after this, so a crash leaves
        at worst a stray file, never a record of a file that's gone)."""
        with self.transaction() as db:
            study = self._study(db, study_id)
            paths = [r["path"] for r in db.execute("SELECT path FROM files WHERE study_id = ?", (study_id,))
                     if r["path"]]
            db.execute("DELETE FROM flags WHERE study_id = ? OR file_id IN (SELECT id FROM files WHERE study_id = ?)",
                       (study_id, study_id))
            db.execute("DELETE FROM files WHERE study_id = ?", (study_id,))
            now = self.clock()
            db.execute("UPDATE studies SET state = 'unrouted', project = NULL, updated_at = ?, "
                       "stable_at = COALESCE(stable_at, ?) WHERE id = ?", (now, now, study.id))
        return paths

    def key_status(self, project: str) -> tuple[str, str | None, float] | None:
        row = self._db.execute("SELECT status, detail, checked_at FROM key_status WHERE project = ?",
                               (project,)).fetchone()
        return (row["status"], row["detail"], row["checked_at"]) if row else None
