"""The Upload Monitor: a small read-only web page on the Pi's hotspot.

Operators use it (often on an iPad) to see when everything from the scanner is
on the Pi; data managers use it to see what reached GRIP and what needs
attention. It reads the state store, never the logs, and changes nothing.

It runs as its own unprivileged service and listens on the hotspot address
only. Studies from logins with no routing entry are never shown.
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import hostkeys, reach
from .config import Routing, SiteConfig
from .state import PENDING_FILE_STATES, Store

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


def snapshot(store: Store, site: SiteConfig, routing: Routing, *, now: float,
             free_bytes: Callable[[Path], int] | None = None, clock_synced: bool | None = None,
             update_hold: Callable[[], str | None] | None = None) -> dict:
    """Everything the page shows, as plain data."""
    if update_hold is None:
        from .updater import Paths, hold_text
        update_hold = lambda: hold_text(Paths())  # noqa: E731
    free_bytes = free_bytes or (lambda p: shutil.disk_usage(p).free)
    studies = [s for s in store.studies() if s.state not in _HIDDEN]
    cards, receiving, fetching = [], False, False
    for s in sorted(studies, key=lambda s: (s.study_date or "", s.study_time or ""), reverse=True):
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
            "project": s.project or "",
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

    return {
        "site": site.site,
        "headline": headline,
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


def serve(site: SiteConfig, routing: Routing, *, bind: str | None = None, port: int | None = None) -> None:
    server = make_server(site, routing, bind=bind or site.dashboard.bind, port=port or site.dashboard.port)
    server.serve_forever()


def make_server(site: SiteConfig, routing: Routing, *, bind: str, port: int,
                now: Callable[[], float] = time.time, synced: Callable[[], bool] = clock_synced) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/":
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            if self.path == "/app.js":
                return self._send(200, SCRIPT.encode(), "text/javascript; charset=utf-8")
            if self.path == "/api/status":
                store = Store(site.state_db, readonly=True)
                try:
                    body = snapshot(store, site, routing, now=now(), clock_synced=synced())
                finally:
                    store.close()
                return self._send(200, json.dumps(body).encode(), "application/json")
            return self._send(404, b"Not found", "text/plain")

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
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
    head.appendChild(el("span", "study-title", st.session + " · " + st.project));
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
