"""GRIP's upload endpoints, as in GRIP's mock API (no sessions).

- DICOM: STOW-RS ``POST /dicomweb/studies/{StudyInstanceUID}``, one file per request.
- Raw files: ``PUT /v1/studies/{StudyInstanceUID}/raw/{name}`` with ``Content-Range``;
  progress by ``HEAD`` on the same URL (``Upload-Offset``, ``Upload-Length``,
  ``Upload-Complete``).
- What GRIP has of a study: ``GET /v1/studies/{StudyInstanceUID}`` (404 = nothing yet),
  with the SHA-256 and size of every stored DICOM instance and raw file.

Region: the two store calls (STOW-RS, and the raw file's URL: PUT and its
progress) go under ``/<region>`` when the site has one. The token, study status
and QIDO calls never take a region.

Checksums: GRIP gives the SHA-256 (and MD5) of each stored file, computed over
the whole file: in the study status, and in the raw PUT reply once the file is
complete. The uploader compares them with its own (``Stored``).

Paths are relative to the project's API base URL. Every value put in a path is
URL-encoded. Request and response bodies are never logged.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any

import requests

from ..ids import quote_path
from .errors import Refused, Transient
from .http import Http

# DICOM JSON tags in a STOW-RS response.
_REFERENCED_SOP_SEQUENCE = "00081199"
_FAILED_SOP_SEQUENCE = "00081198"
_REFERENCED_SOP_INSTANCE_UID = "00081155"
_FAILURE_REASON = "00081197"


@dataclass(frozen=True)
class StowResult:
    status: int
    stored: frozenset[str] = field(default_factory=frozenset)
    failed: dict[str, int | None] = field(default_factory=dict)


@dataclass(frozen=True)
class RawProgress:
    offset: int
    length: int | None
    complete: bool


@dataclass(frozen=True)
class Stored:
    """GRIP's own account of a stored file: its SHA-256 (lower-case hex) and size, each None if GRIP gave none."""
    sha256: str | None
    size: int | None

    def matches(self, sha256: str, size: int) -> bool:
        return self.sha256 is not None and self.sha256 == sha256.lower() and self.size == size


@dataclass(frozen=True)
class ChunkResult:
    status: int
    offset: int | None
    complete: bool
    stored: Stored | None = None  # once complete: GRIP's checksum and size of the whole file, if it gave them


def stored_instance(status: dict[str, Any] | None, sop_uid: str) -> Stored | None:
    """GRIP's checksum and size of one DICOM instance, from a study status. None if GRIP doesn't list it."""
    return _listed(status, "instances", "sopInstanceUid", sop_uid)


def stored_raw(status: dict[str, Any] | None, name: str) -> Stored | None:
    """GRIP's checksum and size of one complete raw file, from a study status. None if GRIP doesn't list it
    as complete."""
    return _listed(status, "rawFiles", "name", name, complete=True)


def _listed(status, key: str, id_key: str, value: str, *, complete: bool = False) -> Stored | None:
    items = status.get(key) if isinstance(status, dict) else None
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get(id_key) == value:
            if complete and item.get("complete") is not True:
                return None
            return _stored(item)
    return None


def _stored(item: dict[str, Any]) -> Stored:
    sha = item.get("sha256")
    size = item.get("size")
    ok_sha = isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdefABCDEF" for c in sha)
    return Stored(sha.lower() if ok_sha else None,
                  size if isinstance(size, int) and not isinstance(size, bool) else None)


class GripApi:
    def __init__(self, http: Http, region: str | None = None):
        self.http = http
        # The prefix of the two store calls only (STOW-RS and the raw file's URL). Never on token, status, QIDO.
        self._store = f"/{quote_path(region)}" if region else ""

    def check_key(self, fresh: bool = False) -> None:
        """Get a token (or reuse a valid one). Raises KeyProblem, KeyRejected or Transient.

        ``fresh`` drops any cached token first, so GRIP itself checks the key now.
        """
        if fresh:
            self.http.tokens.invalidate()
        self.http.tokens.get()

    # ---- studies ---------------------------------------------------------------

    def study_status(self, study_uid: str) -> dict[str, Any] | None:
        """What GRIP has of a study (instances, raw files with offsets), or None if nothing yet."""
        r = self.http.call("GET", f"/v1/studies/{quote_path(study_uid)}")
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise _refused("study status", r)
        try:
            body = r.json()
        except ValueError:
            raise Transient("study status: answer not understood") from None
        if not isinstance(body, dict):
            raise Transient("study status: answer not understood")
        return body

    # ---- DICOM -----------------------------------------------------------------

    def stow(self, study_uid: str, dicom: bytes) -> StowResult:
        """STOW-RS one DICOM file to its study."""
        boundary = f"hyperwheel-{uuid.uuid4().hex}"
        body = b"".join([
            f"--{boundary}\r\nContent-Type: application/dicom\r\n\r\n".encode(),
            dicom,
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        r = self.http.call(
            "POST", f"{self._store}/dicomweb/studies/{quote_path(study_uid)}", data=body,
            headers={
                "Content-Type": f'multipart/related; type="application/dicom"; boundary={boundary}',
                "Accept": "application/dicom+json",
            },
        )
        if r.status_code in (200, 202, 409):
            stored, failed = _parse_stow(r)
            return StowResult(r.status_code, stored, failed)
        raise _refused("STOW-RS", r)

    # ---- raw files -------------------------------------------------------------

    def _raw_path(self, study_uid: str, name: str) -> str:
        return f"{self._store}/v1/studies/{quote_path(study_uid)}/raw/{quote_path(name)}"

    def raw_progress(self, study_uid: str, name: str) -> RawProgress | None:
        """What GRIP already has of a raw file, or None if GRIP doesn't know the file (or the study)."""
        r = self.http.call("HEAD", self._raw_path(study_uid, name))
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise _refused("RRDF file progress", r)
        try:
            offset = int(r.headers.get("Upload-Offset", "0"))
            length = int(r.headers["Upload-Length"]) if "Upload-Length" in r.headers else None
        except ValueError:
            raise Transient("RRDF file progress: answer not understood") from None
        return RawProgress(offset, length, r.headers.get("Upload-Complete", "").lower() == "true")

    def raw_put(self, study_uid: str, name: str, chunk: bytes, start: int, total: int, *,
                attempts: int | None = None) -> ChunkResult:
        """Send one chunk. Returns 200, 409 (offset differs) and 422 (checksum) results; raises on others.

        ``complete`` comes from the JSON answer (GRIP's mock sends no Upload-Complete header on a PUT).
        """
        end = start + len(chunk) - 1
        r = self.http.call(
            "PUT", self._raw_path(study_uid, name), data=chunk, attempts=attempts,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": f"bytes {start}-{end}/{total}",
                "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest(),
            },
        )
        if r.status_code not in (200, 409, 422):
            raise _refused("RRDF file chunk", r)
        offset: int | None
        try:
            offset = int(r.headers["Upload-Offset"]) if "Upload-Offset" in r.headers else None
        except ValueError:
            offset = None
        complete = r.headers.get("Upload-Complete", "").lower() == "true"
        try:
            body = r.json()
        except ValueError:
            body = None
        stored = None
        if isinstance(body, dict):
            complete = complete or body.get("complete") is True
            if offset is None and isinstance(body.get("offset"), int):
                offset = body["offset"]
            if complete:
                stored = _stored(body)
        return ChunkResult(r.status_code, offset, complete, stored)


def _parse_stow(r: requests.Response) -> tuple[frozenset[str], dict[str, int | None]]:
    try:
        body = r.json()
    except ValueError:
        return frozenset(), {}
    if isinstance(body, list):
        body = body[0] if body and isinstance(body[0], dict) else {}
    if not isinstance(body, dict):
        return frozenset(), {}

    def items(tag: str) -> list[dict]:
        seq = body.get(tag)
        values = seq.get("Value") if isinstance(seq, dict) else None
        return [v for v in values if isinstance(v, dict)] if isinstance(values, list) else []

    def first(item: dict, tag: str) -> Any:
        element = item.get(tag)
        values = element.get("Value") if isinstance(element, dict) else None
        return values[0] if isinstance(values, list) and values else None

    stored = frozenset(s for s in (first(i, _REFERENCED_SOP_INSTANCE_UID) for i in items(_REFERENCED_SOP_SEQUENCE))
                       if isinstance(s, str))
    failed: dict[str, int | None] = {}
    for item in items(_FAILED_SOP_SEQUENCE):
        sop = first(item, _REFERENCED_SOP_INSTANCE_UID)
        if not isinstance(sop, str):
            continue
        reason = first(item, _FAILURE_REASON)
        if isinstance(reason, str):
            try:
                reason = int(reason, 0)
            except ValueError:
                reason = None
        failed[sop] = reason if isinstance(reason, int) else None
    return stored, failed


def _refused(what: str, r: requests.Response) -> Refused:
    """GRIP's error answer is JSON with "error" (a code) and "message" (text); both are kept, short."""
    try:
        body = r.json()
    except ValueError:
        body = None
    parts = [body.get(k) for k in ("error", "message")] if isinstance(body, dict) else []
    error = ": ".join(p for p in parts if isinstance(p, str))[:200] or None
    return Refused(f"{what}: HTTP {r.status_code}", r.status_code, error)
