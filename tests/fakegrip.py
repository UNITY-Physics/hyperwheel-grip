"""A small local fake of GRIP's upload API (no sessions), for unit tests.

It follows GRIP's mock (without sessions): a token, DICOM by STOW-RS (DICOM
JSON results and failure reasons), raw files by ``PUT /v1/studies/{uid}/raw/
{name}`` with ``Content-Range`` and ``Upload-Offset``, progress by GET or HEAD
on that URL, study status by ``GET /v1/studies/{uid}``, QIDO-RS and
X-Mock-Fault. A resend overwrites: DICOM by SOP Instance UID, raw files by
name. It adds faults the real mock can't produce on demand: dropped
connections before or after the server has stored the data, expired tokens,
STOW answers that leave a file out, refused file names, and a server that
forgets partial uploads (GRIP said a failed upload may have to be sent again
in full).

Region: the two store calls may carry a region prefix (``/api/<region>/dicomweb/
studies``, ``/api/<region>/v1/studies/{uid}/raw/{name}``), as on GRIP's gateway.
Every other call with a prefix is 404. Checksums: the study status lists each
instance's and complete raw file's ``sha256``, ``md5`` and ``size``, a raw PUT
reply has them once the file is complete, and HEAD gives ``Repr-Digest``.
``checksums = False`` leaves them out; ``corrupt`` makes GRIP's copy differ.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import pydicom

MAX_CHUNK = 64 * 1024 * 1024


@dataclass
class Fault:
    """Make matching requests fail.

    action: an HTTP status (int), "drop" (close the connection before handling),
    "drop_after" (handle the request, then close without answering),
    "expire_tokens" (expire every token, then handle normally, so the request gets 401),
    "stow_unlisted" (store, then answer 200 without listing the instance).
    """
    method: str
    path: str  # regular expression, matched against the path after the base path
    action: int | str
    count: int = 1
    retry_after: int | None = None


@dataclass
class RawUpload:
    size: int
    data: bytearray = field(default_factory=bytearray)

    @property
    def complete(self) -> bool:
        return len(self.data) == self.size


@dataclass
class FakeStudy:
    client_id: str
    study_uid: str
    # sop -> (sha256, series, tsuid, size, md5)
    stored: dict[str, tuple[str, str, str, int, str]] = field(default_factory=dict)
    raw: dict[str, RawUpload] = field(default_factory=dict)
    region: str = "no_region_specified"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class FakeGrip:
    def __init__(self, *, clients: dict[str, str] | None = None, token_ttl: int = 300):
        self.clients = dict(clients or {"test-client": "test-secret"})
        self.token_ttl = token_ttl
        self.tokens: dict[str, tuple[str, float]] = {}
        self.studies: dict[tuple[str, str], FakeStudy] = {}  # (client, StudyInstanceUID) -> study
        self.faults: list[Fault] = []
        self.requests: list[tuple[str, str, dict[str, str]]] = []  # (method, raw path, headers)
        self.token_requests = 0
        # Raw file names to refuse with 400 "invalid file name" (what triggers it on GRIP isn't known yet).
        self.invalid_names: set[str] = set()
        # When set, GRIP keeps no partial uploads: an unfinished raw file is gone on the next look.
        self.forget_partials = False
        # GRIP reports checksums (sha256, md5) of what it stored. False: a gateway that reports none.
        self.checksums = True
        # SOP Instance UIDs or raw file names (-> how many times) whose stored copy GRIP gets wrong.
        self.corrupt: dict[str, int] = {}
        # The region of each store call, in order (None: no region prefix).
        self.store_regions: list[str | None] = []
        self.lock = threading.RLock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                                        daemon=True)

    # ---- lifecycle ---------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/api"

    @property
    def token_url(self) -> str:
        return self.base_url + "/get/token"

    def start(self) -> "FakeGrip":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # ---- helpers for tests -------------------------------------------------------

    def add_fault(self, method: str, path: str, action: int | str, count: int = 1,
                  retry_after: int | None = None) -> None:
        with self.lock:
            self.faults.append(Fault(method, path, action, count, retry_after))

    def expire_tokens(self) -> None:
        with self.lock:
            self.tokens.clear()

    def study_for(self, study_uid: str) -> FakeStudy | None:
        with self.lock:
            return next((s for s in self.studies.values() if s.study_uid == study_uid), None)

    def requests_matching(self, method: str, pattern: str) -> list[tuple[str, str, dict[str, str]]]:
        return [r for r in self.requests if r[0] == method and re.search(pattern, r[1])]

    def _corrupted(self, key: str) -> bool:
        with self.lock:
            left = self.corrupt.get(key, 0)
            if left > 0:
                self.corrupt[key] = left - 1
                return True
        return False

    def _take_fault(self, method: str, path: str) -> Fault | None:
        with self.lock:
            for f in self.faults:
                if f.count > 0 and f.method == method and re.fullmatch(f.path, path):
                    f.count -= 1
                    return f
        return None


def _handler(fake: FakeGrip):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep test output quiet
            pass

        # ---- plumbing ---------------------------------------------------------------

        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("HEAD")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def _dispatch(self, method: str) -> None:
            raw_path = urlsplit(self.path).path
            fake.requests.append((method, raw_path, dict(self.headers.items())))
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            if not raw_path.startswith("/api"):
                return self._json(404, {"error": "not_found"})
            path = raw_path[len("/api"):]
            # Only the two store calls take a region prefix. Anything else with one is 404 below.
            self._region = None
            m = re.fullmatch(r"/([a-z0-9_-]+)(/dicomweb/studies(?:/[^/]+)?|/v1/studies/[^/]+/raw/[^/]+)", path)
            if m:
                self._region, path = m.group(1), m.group(2)

            forced = self.headers.get("X-Mock-Fault")
            if forced:
                status = int(forced)
                extra = {"Retry-After": "5"} if status in (429, 503) else {}
                return self._json(status, {"error": "mock_fault"}, extra)

            fault = fake._take_fault(method, path)
            if fault is not None:
                if fault.action == "drop":
                    self.close_connection = True
                    self.connection.close()
                    return
                if fault.action == "expire_tokens":
                    fake.expire_tokens()
                elif isinstance(fault.action, int):
                    extra = {"Retry-After": str(fault.retry_after)} if fault.retry_after is not None else {}
                    return self._json(fault.action, {"error": "injected"}, extra)

            self._answer = None
            self._route(method, path, body, fault)
            if fault is not None and fault.action == "drop_after":
                self.close_connection = True
                self.connection.close()
                return
            if self._answer is not None:
                self._send(*self._answer)

        def _json(self, status: int, obj, headers: dict[str, str] | None = None) -> None:
            data = json.dumps(obj).encode()
            self._send(status, data, {"Content-Type": "application/json", **(headers or {})})

        def _reply(self, status: int, obj, headers: dict[str, str] | None = None) -> None:
            data = json.dumps(obj).encode() if obj is not None else b""
            self._answer = (status, data, {"Content-Type": "application/json", **(headers or {})})

        def _send(self, status: int, data: bytes, headers: dict[str, str]) -> None:
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _client(self) -> str | None:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return None
            with fake.lock:
                entry = fake.tokens.get(auth[7:])
            if entry is None or entry[1] < time.time():
                return None
            return entry[0]

        # ---- routes -------------------------------------------------------------------

        def _route(self, method: str, path: str, body: bytes, fault: Fault | None) -> None:
            if method == "POST" and path == "/get/token":
                return self._token(body)
            client = self._client()
            if client is None:
                return self._reply(401, {"error": "invalid_token", "message": "Missing, invalid or expired bearer token"})
            if method == "GET" and path == "/v1/studies":
                with fake.lock:
                    found = [self._study_json(s) for s in fake.studies.values() if s.client_id == client]
                return self._reply(200, found)
            m = re.fullmatch(r"/v1/studies/([^/]+)", path)
            if m and method == "GET":
                s = self._own_study(client, unquote(m.group(1)))
                return self._reply(200, self._study_json(s)) if s else self._not_found("Unknown study")
            m = re.fullmatch(r"/v1/studies/([^/]+)/raw/([^/]+)", path)
            if m:
                uid, name = unquote(m.group(1)), unquote(m.group(2))
                if method in ("GET", "HEAD"):
                    return self._raw_status(client, uid, name)
                if method == "PUT":
                    return self._raw_put(client, uid, name, body)
            m = re.fullmatch(r"/dicomweb/studies(?:/([^/]+))?", path)
            if m and method == "POST":
                return self._stow(client, unquote(m.group(1)) if m.group(1) else None, body, fault)
            m = re.fullmatch(r"/dicomweb(?:/v1)?/studies/([^/]+)/instances", path)
            if m and method == "GET":
                return self._qido(client, unquote(m.group(1)))
            return self._not_found("Not found")

        def _not_found(self, message: str) -> None:
            return self._reply(404, {"status": 404, "error": "not_found", "message": message})

        def _token(self, body: bytes) -> None:
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            with fake.lock:
                fake.token_requests += 1
            if form.get("grant_type") != "client_credentials":
                return self._reply(400, {"error": "unsupported_grant_type"})
            client_id = form.get("client_id", "")
            if fake.clients.get(client_id) != form.get("client_secret"):
                return self._reply(401, {"error": "invalid_client"})
            token = f"{secrets.token_hex(8)}.{secrets.token_hex(8)}.{secrets.token_hex(8)}"
            with fake.lock:
                fake.tokens[token] = (client_id, time.time() + fake.token_ttl)
            return self._reply(200, {"access_token": token, "expires_in": fake.token_ttl, "token_type": "Bearer",
                                     "scope": form.get("scope", "")})

        def _own_study(self, client: str, uid: str) -> FakeStudy | None:
            with fake.lock:
                return fake.studies.get((client, uid))

        def _study(self, client: str, uid: str) -> FakeStudy:
            with fake.lock:
                return fake.studies.setdefault((client, uid), FakeStudy(client, uid))

        def _study_json(self, s: FakeStudy) -> dict:
            return {
                "studyInstanceUid": s.study_uid, "workspace": "example", "project": "demo", "quarantined": False,
                "createdAt": s.created_at, "instanceCount": len(s.stored), "rawFileCount": len(s.raw),
                "region": s.region,
                "instances": [{"sopInstanceUid": k, "size": v[3],
                               **({"sha256": v[0], "md5": v[4]} if fake.checksums else {})}
                              for k, v in s.stored.items()],
                "rawFiles": [{"name": n, "size": r.size, "complete": r.complete, "offset": len(r.data),
                              **self._digests(r)} for n, r in s.raw.items()],
            }

        def _digests(self, r: RawUpload) -> dict:
            if not (fake.checksums and r.complete):
                return {}
            return {"sha256": hashlib.sha256(r.data).hexdigest(), "md5": hashlib.md5(r.data).hexdigest()}

        def _raw_json(self, s: FakeStudy, name: str, r: RawUpload) -> dict:
            return {"studyInstanceUid": s.study_uid, "name": name, "size": r.size, "offset": len(r.data),
                    "complete": r.complete, **self._digests(r)}

        def _store_call(self, s: FakeStudy) -> None:
            fake.store_regions.append(self._region)
            if self._region:
                s.region = self._region

        def _raw_status(self, client: str, uid: str, name: str) -> None:
            s = self._own_study(client, uid)
            if s is None:
                return self._not_found(f"Unknown study {uid}")
            with fake.lock:
                r = s.raw.get(name)
                if r is not None and fake.forget_partials and not r.complete:
                    del s.raw[name]
                    r = None
            if r is None:
                return self._not_found(f"Unknown raw file {name}")
            headers = {"Upload-Offset": str(len(r.data)), "Upload-Length": str(r.size),
                       "Upload-Complete": "true" if r.complete else "false"}
            digests = self._digests(r)
            if digests:
                headers["Repr-Digest"] = (f"sha-256=:{base64.b64encode(bytes.fromhex(digests['sha256'])).decode()}:, "
                                          f"md5=:{base64.b64encode(bytes.fromhex(digests['md5'])).decode()}:")
            return self._reply(200, self._raw_json(s, name, r), headers)

        def _raw_put(self, client: str, uid: str, name: str, chunk: bytes) -> None:
            if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name in fake.invalid_names:
                return self._reply(400, {"status": 400, "error": "invalid_file_name", "message": "invalid file name"})
            if len(chunk) > MAX_CHUNK:
                return self._reply(413, {"error": "chunk too large"})
            content_range = self.headers.get("Content-Range")
            if content_range is None:
                start, end, total = 0, len(chunk) - 1, len(chunk)
            else:
                m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                if not m:
                    return self._reply(400, {"error": "bad Content-Range"})
                start, end, total = map(int, m.groups())
                if end - start + 1 != len(chunk) or end >= total:
                    return self._reply(400, {"error": "Content-Range does not match"})
            want = self.headers.get("X-Chunk-SHA256")
            s = self._study(client, uid)
            with fake.lock:
                self._store_call(s)
                r = s.raw.get(name)
                if r is not None and fake.forget_partials and not r.complete and start == 0:
                    r = None
                if r is None or r.size != total or (start == 0 and r.complete and end + 1 < total):
                    r = s.raw[name] = RawUpload(total)  # new file, or sent again: overwrites
                if want and hashlib.sha256(chunk).hexdigest() != want:
                    return self._reply(422, {"error": "chunk checksum mismatch"}, {"Upload-Offset": str(len(r.data))})
                have = len(r.data)
                if start > have:
                    return self._reply(409, {"status": 409, "error": "offset_mismatch",
                                             "message": f"Chunk starts at {start}, expected {have}"},
                                       {"Upload-Offset": str(have)})
                if end + 1 >= have:
                    r.data[start:] = chunk
                else:
                    r.data[start:end + 1] = chunk
                if r.complete and fake._corrupted(name):
                    r.data[0] ^= 0xFF  # GRIP put the file together wrongly
                return self._reply(200, self._raw_json(s, name, r), {"Upload-Offset": str(len(r.data))})

        def _stow(self, client: str, url_study: str | None, body: bytes, fault: Fault | None) -> None:
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            ctype = self.headers.get("Content-Type", "")
            if ctype.startswith("multipart/related"):
                m = re.search(r'boundary="?([^";]+)"?', ctype)
                if not m:
                    return self._reply(400, {"error": "no boundary"})
                parts = _multipart(body, m.group(1).encode())
            elif ctype.startswith("application/dicom"):
                parts = [body]
            else:
                return self._reply(415, {"error": "unsupported media type"})

            stored, failed = [], []
            for part in parts:
                try:
                    ds = pydicom.dcmread(io.BytesIO(part), stop_before_pixels=True)
                    sop, cls, study, series = (str(ds.SOPInstanceUID), str(ds.SOPClassUID),
                                               str(ds.StudyInstanceUID), str(ds.SeriesInstanceUID))
                    tsuid = str(ds.file_meta.TransferSyntaxUID)
                except Exception:
                    failed.append(("", "", 0xC000))
                    continue
                kept = part if not fake._corrupted(sop) else bytes([part[0] ^ 0xFF]) + part[1:]
                sha, md5 = hashlib.sha256(kept).hexdigest(), hashlib.md5(kept).hexdigest()
                if url_study and study != url_study:
                    failed.append((sop, cls, 0xA900))
                    continue
                with fake.lock:
                    target = self._study(client, study)
                    self._store_call(target)
                    target.stored[sop] = (sha, series, tsuid, len(part), md5)  # a resend overwrites
                stored.append((sop, cls))

            if fault is not None and fault.action == "stow_unlisted":
                stored = []
            result = {}
            if stored:
                result["00081199"] = {"vr": "SQ", "Value": [
                    {"00081150": {"vr": "UI", "Value": [c]}, "00081155": {"vr": "UI", "Value": [s]}}
                    for s, c in stored]}
            if failed:
                result["00081198"] = {"vr": "SQ", "Value": [
                    {"00081150": {"vr": "UI", "Value": [c]}, "00081155": {"vr": "UI", "Value": [s]},
                     "00081197": {"vr": "US", "Value": [reason]}} for s, c, reason in failed]}
            status = 200 if not failed else (202 if stored else 409)
            self._answer = (status, json.dumps(result).encode(), {"Content-Type": "application/dicom+json"})

        def _qido(self, client: str, study: str) -> None:
            s = self._own_study(client, study)
            with fake.lock:
                items = [] if s is None else [
                    {"00080018": {"vr": "UI", "Value": [sop]}, "0020000E": {"vr": "UI", "Value": [series]},
                     "00083002": {"vr": "UI", "Value": [tsuid]}} for sop, (_, series, tsuid, _, _) in s.stored.items()]
            self._answer = (200, json.dumps(items).encode(), {"Content-Type": "application/dicom+json"})

    return Handler


def _multipart(body: bytes, boundary: bytes) -> list[bytes]:
    parts = []
    for chunk in body.split(b"--" + boundary)[1:]:
        if chunk.startswith(b"--"):
            break
        head, _, data = chunk.partition(b"\r\n\r\n")
        if data.endswith(b"\r\n"):
            data = data[:-2]
        parts.append(data)
    return parts
