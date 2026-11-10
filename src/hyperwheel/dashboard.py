"""The Upload Monitor: a small read-only web page on the Pi's hotspot, and on the Pi itself.

Operators use it (often on an iPad) to see when everything from the scanner is
on the Pi; data managers use it to see what reached GRIP and what needs
attention. It reads the state store, never the logs, and changes nothing.

It runs as its own unprivileged service and listens on the hotspot address,
and on 127.0.0.1 for the Pi's own browser (which works even when the hotspot
isn't up). Studies from logins with no routing entry are never shown.

On a newly flashed Pi (no site settings yet) it serves one other page instead,
where the Pi joins Raspberry Pi Connect: the sign-in link to send to the admin,
and, as a small extra below it, a box for a one-use key the admin sent. Pasting a
key is the only thing this service ever changes, and only until there are site
settings.
"""

from __future__ import annotations

import json
import shutil
import logging
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import connect, hostkeys, reach
from .config import DashboardSettings, Routing, SiteConfig
from .ids import login_from_operators_name
from .state import PENDING_FILE_STATES, Store

log = logging.getLogger("hyperwheel.dashboard")

HEARTBEAT = "service_heartbeat"
_HIDDEN = ("unrouted", "purged", "archived")
_STATE_TEXT = {
    "receiving": "Receiving images",
    "stable": "Checking",
    "checking": "Checking",
    "held": "Held: needs a data manager",
    "ready": "Waiting to upload",
    "uploading": "Uploading",
    "uploaded": "On GRIP",
}
KEY_TEXT = {
    "working": "Working",
    "rejected": "Rejected by GRIP (wrong, disabled or expired)",
    "missing": "No key stored",
    "unreadable": "Key file can't be read",
}


def _on_grip(files: list) -> int:
    return sum(f.state in ("confirmed", "deleted") for f in files)


def _count_texts(state: str, dicom: list, rrdf: list, raw_pending: bool) -> tuple[str, str]:
    """What to say about a study's files. A count of 0/0 only means they aren't listed yet, so it isn't shown."""
    arriving = state in ("receiving", "stable", "checking")
    dicom_text = "DICOM receiving" if arriving and not dicom else f"DICOM {_on_grip(dicom)}/{len(dicom)} on GRIP"
    if not rrdf:
        raw_text = "Raw data still to come" if arriving or raw_pending else "No raw data"
    else:
        raw_text = f"Raw data {_on_grip(rrdf)}/{len(rrdf)} on GRIP" + (" (still fetching)" if raw_pending else "")
    return dicom_text, raw_text


# Key states that stop a project's uploads (see uploader.check_keys).
_KEY_BLOCKED = ("missing", "unreadable", "rejected")


def _waiting_for_key(store: Store, pending_files: list) -> list[str]:
    """The projects whose files wait for a working GRIP key, if every file still to upload is one of them
    (otherwise something is uploading, and the key alert names the project that isn't)."""
    project_of = {s.id: s.project for s in store.studies()}
    projects = {project_of.get(f.study_id) for f in pending_files}
    if None in projects:
        return []
    for name in projects:
        status = store.key_status(name)
        if not (status and status[0] in _KEY_BLOCKED):
            return []
    return sorted(projects)


def _and_list(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _routed_project(routing: Routing, operators_name: str | None) -> str | None:
    """The project a study's login routes to, for a study not routed yet (its images are still arriving)."""
    project = routing.project_for_login(login_from_operators_name(operators_name))
    return project.name if project else None


def snapshot(store: Store, site: SiteConfig, routing: Routing, *, now: float,
             free_bytes: Callable[[Path], int] | None = None, clock_synced: bool | None = None,
             update_hold: Callable[[], str | None] | None = None,
             connect_state: Callable[[], dict | None] = connect.read_state) -> dict:
    """Everything the page shows, as plain data."""
    if update_hold is None:
        from .updater import Paths, hold_text
        update_hold = lambda: hold_text(Paths())  # noqa: E731
    free_bytes = free_bytes or (lambda p: shutil.disk_usage(p).free)
    studies = [s for s in store.studies() if s.state not in _HIDDEN]
    cards, receiving, fetching = [], False, False
    for s in sorted(studies, key=lambda s: (s.study_date or "", s.study_time or ""), reverse=True):
        project = s.project or _routed_project(routing, s.operators_name)
        if project is None and s.state in ("receiving", "stable"):
            # Images still arriving from a login with no routing entry: never shown. They still count as
            # arriving, so the page never says it's safe to switch off while the scanner is sending (the first
            # image's login may not be every image's).
            receiving = True
            continue
        files = store.files(s.id)
        dicom = [f for f in files if f.kind == "dicom"]
        rrdf = [f for f in files if f.kind == "rrdf" and f.state != "seen"]
        folder = f"rrdf_{s.study_date}_{(s.study_time or '')[:6]}"
        raw_waiting = len([f for f in store.files(kind="rrdf", states=("seen",)) if f.scanner_folder == folder])
        raw_pending = site.rrdf.enabled and (raw_waiting > 0 or (s.raw_checked_at is None and bool(s.project)))
        if s.state in ("receiving", "stable", "checking"):
            receiving = True
        elif raw_pending and s.state != "held":
            fetching = True
        flags = store.open_flags(study_id=s.id)
        dicom_text, raw_text = _count_texts(s.state, dicom, rrdf, raw_pending)
        cards.append({
            "session": _label(s.study_date, s.study_time),
            "project": project or "",
            "patient_id": s.patient_id if s.patient_id is not None else "",
            "state": _STATE_TEXT.get(s.state, s.state),
            "dicom": {"total": len(dicom), "on_grip": _on_grip(dicom),
                      "text": dicom_text},
            "raw": {"total": len(rrdf), "on_grip": _on_grip(rrdf),
                    "still_fetching": raw_pending, "text": raw_text},
            "flags": [{"level": f.level, "text": f.detail} for f in flags],
        })

    pending_files = store.files(states=PENDING_FILE_STATES)
    pending = len(pending_files)
    general = [{"level": f.level, "text": f.detail} for f in store.open_flags() if f.study_id is None]
    if held := update_hold():
        general.append({"level": "flag", "text": held})
    heartbeat = store.cursor(HEARTBEAT)
    running = bool(heartbeat) and now - heartbeat <= 300
    links = reach.read(store)
    scanner_down = running and links.scanner_down(now)
    if not running:
        general.insert(0, {"level": "alert", "text": "The Hyperwheel service is not running. Nothing is being "
                                                     "fetched or uploaded. Please contact your Hyperwheel admin."})
    else:
        general[:0] = reach.messages(links, now)
    if clock_synced is False:
        general.append({"level": "flag", "text": "The Pi's clock isn't set yet. Uploads wait until it is."})
    general += [{"level": "notice", "text": text} for text in hostkeys.new_key_notices(store, now)]
    try:
        free_gb = free_bytes(site.staging_dir if site.staging_dir.exists() else Path("/")) / 1024 ** 3
    except OSError:
        free_gb = None
    disk_low = free_gb is not None and free_gb < site.disk.warn_free_gb
    if disk_low:
        general.append({"level": "alert", "text": f"The Pi's disk is nearly full ({free_gb:.1f} GB free)."})
    long_wait = any(now - f.created_at >= site.dashboard.leave_on_after_hours * 3600 for f in pending_files)

    attention = any(f["level"] in ("hold", "alert") for f in general) or any(
        f["level"] in ("hold", "alert") for c in cards for f in c["flags"])
    files = f"{pending} file{'s' if pending != 1 else ''}"
    leave_on = False
    if not running:
        headline, sub = "Hyperwheel is not running", "Contact your Hyperwheel admin."
    elif scanner_down and (receiving or fetching):
        headline = "Can't reach the scanner"
        sub = ("Images or raw data are still to come from the scanner. Please check that the scanner is on and "
               "the cable is connected.")
    elif receiving:
        headline, sub = "Receiving images from the scanner", ""  # the badge says to keep the scanner on
    elif fetching:
        headline, sub = "Fetching raw data from the scanner", ""
    elif pending and (no_key := _waiting_for_key(store, pending_files)):
        headline = "Waiting for a GRIP key"
        sub = (f"Everything from the scanner is on the Pi. {files} still to upload. Uploads for "
               f"{_and_list(no_key)} start once a working GRIP key is set. The Pi keeps the files, so you can "
               "switch the scanner off.")
    elif pending:
        offline = scanner_down or links.internet_down(now)
        headline = "Waiting to upload to GRIP" if offline else "Uploading to GRIP"
        leave_on = long_wait or disk_low
        if leave_on:
            why = (f"some have waited more than {site.dashboard.leave_on_after_hours:g} hours" if long_wait
                   else "the Pi's disk is getting full")
            sub = (f"Everything from the scanner is on the Pi. {files} still to upload, and {why}. The Pi's "
                   "internet goes through the scanner.")
        else:
            sub = (f"Everything from the scanner is on the Pi. {files} still to upload. You can switch the scanner "
                   "off: the Pi keeps the files and uploads them next time the scanner is on.")
    else:
        headline, sub = "Everything is on GRIP", "Nothing is waiting on the Pi."
    on_pi_complete = running and not receiving and not fetching
    if not on_pi_complete:
        badge = "Please keep the scanner on" if not scanner_down else "Waiting for the scanner"
    elif leave_on:
        badge = "Please leave the scanner on to finish uploading"
    else:
        badge = "Everything from the scanner is on the Pi: safe to switch off"

    keys = []
    for name in routing.projects:
        status = store.key_status(name)
        keys.append({"project": name, "status": KEY_TEXT.get(status[0], status[0]) if status else "Not checked yet",
                     "ok": bool(status and status[0] == "working")})

    connect_result, connect_text = connect.describe(connect_state(), now, set_up=True)
    return {
        "site": site.site,
        "headline": headline,
        "connect": {"ok": connect_result == connect.OK, "problem": connect_result == connect.PROBLEM,
                    "text": connect_text},
        "subline": sub,
        "on_pi_complete": on_pi_complete,
        "safe_to_switch_off": on_pi_complete and not leave_on,
        "badge": badge,
        "attention": attention,
        "general": general,
        "studies": cards,
        "keys": keys,
        "free_gb": round(free_gb, 1) if free_gb is not None else None,
        "updated": time.strftime("%H:%M:%S", time.localtime(now)),
    }


def _label(date: str | None, t: str | None) -> str:
    d, t = date or "", (t or "")[:6]
    if len(d) == 8 and len(t) == 6:
        return f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}"
    return f"{d} {t}".strip()


def clock_synced() -> bool:
    """systemd-timesyncd creates this file once the clock has been set from the network."""
    return Path("/run/systemd/timesync/synchronized").exists()


LOOPBACK = "127.0.0.1"


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        reach.allow_unassigned_address(self.socket)  # the hotspot's address may not be up yet
        super().server_bind()


# Binds that already include the Pi itself: a second listener on 127.0.0.1 would clash with them.
_COVERS_LOOPBACK = {LOOPBACK, "localhost", "0.0.0.0", ""}


def addresses(bind: str) -> list[str]:
    """Where the Upload Monitor listens: the hotspot address, and the Pi itself."""
    return [bind] if bind in _COVERS_LOOPBACK or bind.startswith("127.") else [bind, LOOPBACK]


def _serve_all(servers: list[ThreadingHTTPServer]) -> None:
    for server in servers[1:]:
        threading.Thread(target=_serve_logged, args=(server,), daemon=True).start()
    servers[0].serve_forever()


def _serve_logged(server: ThreadingHTTPServer) -> None:
    try:
        server.serve_forever()
    except Exception:  # noqa: BLE001 - the other address keeps working, so say so and carry on
        log.exception("Upload Monitor: stopped answering on %s", server.server_address[0])


def serve(site: SiteConfig, routing: Routing, *, bind: str | None = None, port: int | None = None) -> None:
    _serve_all([make_server(site, routing, bind=address, port=port or site.dashboard.port)
                for address in addresses(bind or site.dashboard.bind)])


# ---- a newly flashed Pi ----------------------------------------------------------------------------------


def not_set_up_status(state: dict | None, now: float) -> dict:
    """What the page on a newly flashed Pi shows. The key itself is never in it."""
    signed_in = connect.describe(state, now, set_up=False)[0] == connect.OK
    key = (state or {}).get("key", "none")
    return {"signed_in": signed_in, "link": None if signed_in else (state or {}).get("link"),
            "key_refused": key == "refused" and not signed_in, "key_trying": key == "trying" and not signed_in}


def make_not_set_up_server(*, bind: str, port: int, folder: Path = Path(connect.FOLDER),
                           site_toml: Path = Path("/etc/hyperwheel/site.toml"),
                           now: Callable[[], float] = time.time) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/":
                return _send(self, 200, NOT_SET_UP_PAGE.encode(), "text/html; charset=utf-8")
            if self.path == "/setup.js":
                return _send(self, 200, NOT_SET_UP_SCRIPT.encode(), "text/javascript; charset=utf-8")
            if self.path == "/api/status":
                body = not_set_up_status(connect.read_state(folder), now())
                return _send(self, 200, json.dumps(body).encode(), "application/json")
            return _send(self, 404, b"Not found", "text/plain")

        def do_POST(self):
            # JSON only: a page on another site can't send that to this Pi without the Pi agreeing first.
            if self.path != "/api/connect-key" or self.headers.get("Content-Type") != "application/json":
                return _send(self, 404, b"Not found", "text/plain")
            if site_toml.exists():
                return _answer(self, 403, "This Pi is set up already.")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return _answer(self, 400, "That didn't arrive. Please try again.")
            if not 0 < length <= 1024:
                return _answer(self, 400, connect.key_problem(""))
            try:
                key = json.loads(self.rfile.read(length))["key"]
            except (ValueError, KeyError, TypeError):
                return _answer(self, 400, "That didn't arrive. Please try again.")
            if not isinstance(key, str):
                return _answer(self, 400, connect.key_problem(""))
            try:
                problem = connect.accept_key(key, folder)
            except OSError:
                return _answer(self, 500, "The Pi couldn't keep the key. Tell your Hyperwheel admin.")
            if problem:
                return _answer(self, 400, problem)
            return _answer(self, 200, "The Pi has the key and is signing in.")

    server = Server((bind, port), Handler)
    return server


def serve_not_set_up(*, bind: str | None = None, port: int | None = None) -> None:
    defaults = DashboardSettings()
    _serve_all([make_not_set_up_server(bind=address, port=port or defaults.port)
                for address in addresses(bind or defaults.bind)])


def _answer(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    _send(handler, status, json.dumps({"message": message}).encode(), "application/json")


def _send(handler: BaseHTTPRequestHandler, status: int, body: bytes, ctype: str) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)


def make_server(site: SiteConfig, routing: Routing, *, bind: str, port: int,
                now: Callable[[], float] = time.time, synced: Callable[[], bool] = clock_synced) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/":
                return _send(self, 200, PAGE.encode(), "text/html; charset=utf-8")
            if self.path == "/app.js":
                return _send(self, 200, SCRIPT.encode(), "text/javascript; charset=utf-8")
            if self.path == "/api/status":
                store = Store(site.state_db, readonly=True)
                try:
                    body = snapshot(store, site, routing, now=now(), clock_synced=synced())
                finally:
                    store.close()
                return _send(self, 200, json.dumps(body).encode(), "application/json")
            return _send(self, 404, b"Not found", "text/plain")

    server = Server((bind, port), Handler)
    return server


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Upload Monitor</title>
<style>
  :root { --bg:#f2f2f7; --card:#fff; --text:#111; --sub:#6e6e73; --line:#e5e5ea;
          --ok:#248a3d; --warn:#b25000; --bad:#d70015; --blue:#0066cc; }
  * { box-sizing: border-box; }
  body { margin:0; padding:16px; background:var(--bg); color:var(--text);
         font: 17px/1.4 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  main { max-width: 1100px; margin: 0 auto; }
  .card { background:var(--card); border-radius:12px; padding:20px; margin-bottom:16px;
          box-shadow:0 1px 3px rgba(0,0,0,.06); }
  h1 { font-size:28px; margin:0 0 6px; }
  .sub { color:var(--sub); margin:0; }
  .safe { display:inline-block; margin-top:12px; padding:6px 12px; border-radius:8px; font-weight:600; }
  .safe.yes { background:#e3f5e8; color:var(--ok); } .safe.no { background:#fff1e0; color:var(--warn); }
  h2 { font-size:15px; text-transform:uppercase; letter-spacing:.4px; color:var(--sub); margin:24px 4px 8px; }
  .flag { padding:10px 12px; border-radius:8px; margin-top:8px; background:#f5f5f7; }
  .flag.hold, .flag.alert { background:#ffeceb; color:#8a1010; }
  .flag.notice { background:#eef4ff; }
  .study-head { display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; align-items:baseline; }
  .study-title { font-weight:600; font-size:19px; }
  .meta { color:var(--sub); font-size:15px; }
  .counts { display:flex; gap:10px; flex-wrap:wrap; margin-top:10px; }
  .pill { padding:4px 10px; border-radius:6px; font-size:14px; font-weight:600; background:#eef4ff; color:var(--blue); }
  .pill.done { background:#e3f5e8; color:var(--ok); }
  .keys { display:grid; gap:8px; }
  .key { display:flex; justify-content:space-between; gap:12px; }
  .key .bad { color:var(--bad); font-weight:600; } .key .ok { color:var(--ok); }
  footer { color:var(--sub); font-size:13px; margin:16px 4px; }
</style>
</head>
<body>
<main>
  <section class="card">
    <h1 id="headline">Loading…</h1>
    <p class="sub" id="subline"></p>
    <div id="safe" class="safe" hidden></div>
    <div id="general"></div>
  </section>
  <h2>Sessions</h2>
  <div id="studies"></div>
  <h2>GRIP keys</h2>
  <section class="card keys" id="keys"></section>
  <h2>Raspberry Pi Connect</h2>
  <section class="card keys"><div class="key"><span>Remote access</span><span id="connect"></span></div></section>
  <footer id="footer"></footer>
</main>
<script src="/app.js"></script>
</body>
</html>
"""

SCRIPT = """
"use strict";
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function flags(parent, list) {
  for (const f of list) parent.appendChild(el("div", "flag " + f.level, f.text));
}
function render(s) {
  document.getElementById("headline").textContent = s.headline;
  document.getElementById("subline").textContent = s.subline;
  const safe = document.getElementById("safe");
  safe.hidden = false;
  safe.className = "safe " + (s.safe_to_switch_off ? "yes" : "no");
  safe.textContent = s.badge;
  const general = document.getElementById("general");
  general.replaceChildren();
  flags(general, s.general);

  const studies = document.getElementById("studies");
  studies.replaceChildren();
  if (!s.studies.length) studies.appendChild(el("section", "card meta", "No sessions on the Pi."));
  for (const st of s.studies) {
    const card = el("section", "card");
    const head = el("div", "study-head");
    head.appendChild(el("span", "study-title", st.project ? st.session + " · " + st.project : st.session));
    head.appendChild(el("span", "meta", st.state));
    card.appendChild(head);
    card.appendChild(el("div", "meta", "Patient ID: " + (st.patient_id.trim() ? st.patient_id : "(blank)")));
    const counts = el("div", "counts");
    const d = st.dicom, r = st.raw;
    counts.appendChild(el("span", "pill" + (d.total && d.on_grip === d.total ? " done" : ""), d.text));
    counts.appendChild(el("span", "pill" + (r.total && r.on_grip === r.total && !r.still_fetching ? " done" : ""),
                          r.text));
    card.appendChild(counts);
    flags(card, st.flags);
    studies.appendChild(card);
  }

  const keys = document.getElementById("keys");
  keys.replaceChildren();
  if (!s.keys.length) keys.appendChild(el("div", "meta", "No projects set up."));
  for (const k of s.keys) {
    const row = el("div", "key");
    row.appendChild(el("span", "", k.project));
    row.appendChild(el("span", k.ok ? "ok" : "bad", k.status));
    keys.appendChild(row);
  }
  const connect = document.getElementById("connect");
  connect.textContent = s.connect.text;
  connect.className = s.connect.ok ? "ok" : (s.connect.problem ? "bad" : "");
  document.getElementById("footer").textContent =
    s.site + " · updated " + s.updated + (s.free_gb !== null ? " · " + s.free_gb + " GB free" : "");
}
async function refresh() {
  try {
    const r = await fetch("/api/status", {cache: "no-store"});
    if (r.ok) render(await r.json());
  } catch (e) {
    document.getElementById("headline").textContent = "Can't reach the Pi";
  }
}
refresh();
setInterval(refresh, 5000);
"""

NOT_SET_UP_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Upload Monitor</title>
<style>
  :root { --bg:#f2f2f7; --card:#fff; --text:#111; --sub:#6e6e73; --ok:#248a3d; --bad:#d70015; --blue:#0066cc; }
  * { box-sizing: border-box; }
  body { margin:0; padding:16px; background:var(--bg); color:var(--text);
         font: 17px/1.4 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  main { max-width: 700px; margin: 0 auto; }
  .card { background:var(--card); border-radius:12px; padding:20px; margin-bottom:16px;
          box-shadow:0 1px 3px rgba(0,0,0,.06); }
  h1 { font-size:28px; margin:0 0 6px; }
  h2 { font-size:19px; margin:0 0 8px; }
  p { margin:0 0 10px; }
  .sub { color:var(--sub); }
  input { width:100%; font-size:17px; padding:10px; border:1px solid #c7c7cc; border-radius:8px; margin-bottom:10px; }
  button { font-size:17px; padding:10px 18px; border:0; border-radius:8px; background:var(--blue); color:#fff; }
  .link { overflow-wrap:anywhere; font-weight:600; }
  #key-message { margin:10px 0 0; }
  .small { font-size:15px; padding:14px 20px; }
  .small input, .small button { font-size:15px; }
  summary { cursor:pointer; color:var(--blue); }
  details[open] summary { margin-bottom:10px; }
  .ok { color:var(--ok); font-weight:600; } .bad { color:var(--bad); font-weight:600; }
</style>
</head>
<body>
<main>
  <section class="card">
    <h1>This Pi isn't set up yet</h1>
    <p class="sub">Your Hyperwheel admin sets it up over Raspberry Pi Connect, so the Pi has to sign in first.</p>
    <p id="state"></p>
  </section>
  <section class="card" id="link-card" hidden>
    <h2>Send this link to your Hyperwheel admin, and only to them</h2>
    <p>Don't open it yourself. Whoever opens it first gets remote access to this Pi.</p>
    <p class="link" id="link"></p>
  </section>
  <section class="card small" id="key-card" hidden>
    <details id="key-details">
      <summary>Your Hyperwheel admin sent you a key instead?</summary>
      <p>Paste it here. It works once.</p>
      <form id="key-form">
        <input id="key" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="Key">
        <button type="submit">Sign in</button>
      </form>
      <p id="key-message"></p>
    </details>
  </section>
</main>
<script src="/setup.js"></script>
</body>
</html>
"""

NOT_SET_UP_SCRIPT = """
"use strict";
const $ = id => document.getElementById(id);
let answer = "";
function render(s) {
  const state = $("state");
  if (s.signed_in) {
    state.className = "ok";
    state.textContent = "Raspberry Pi Connect: signed in. Your Hyperwheel admin can set up this Pi now.";
  } else {
    state.className = "";
    state.textContent = "Raspberry Pi Connect: not signed in yet. The Pi needs the scanner cable for the internet.";
  }
  $("key-card").hidden = s.signed_in;
  // While a pasted key is being tried, Connect gives no link, so the card would only say to wait for one.
  $("link-card").hidden = s.signed_in || s.key_trying;
  const message = $("key-message");
  if (s.key_refused || s.key_trying) $("key-details").open = true;
  if (s.key_refused) {
    message.className = "bad";
    message.textContent = "That key wasn't accepted. It may be used or out of date. " +
                          "Ask your Hyperwheel admin for a new one.";
  } else if (s.key_trying) {
    message.className = "";
    message.textContent = "The Pi is trying the key, and keeps trying until it has the internet.";
  } else {
    message.className = "";
    message.textContent = answer;
  }
  $("link").textContent = s.link || "The link shows here once the Pi has the internet.";
}
async function refresh() {
  try {
    const r = await fetch("/api/status", {cache: "no-store"});
    if (r.ok) render(await r.json());
  } catch (e) {
    $("state").textContent = "Can't reach the Pi";
  }
}
$("key-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const r = await fetch("/api/connect-key", {method: "POST", headers: {"Content-Type": "application/json"},
                                               body: JSON.stringify({key: $("key").value})});
    answer = (await r.json()).message;
    if (r.ok) $("key").value = "";
  } catch (e) {
    answer = "Can't reach the Pi";
  }
  $("key-message").textContent = answer;
});
refresh();
setInterval(refresh, 5000);
"""
