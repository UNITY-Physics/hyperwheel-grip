"""Command line: ``hyperwheel run``, ``dashboard``, ``status``, ``doctor`` and ``update``."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from .config import ConfigError, load_routing, load_site
from .dashboard import clock_synced, serve, snapshot
from .state import Store


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["update"]:  # runs as root, also on a Pi that isn't set up yet
        from . import updater
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
        return updater.main(argv[1:])
    ap = argparse.ArgumentParser(prog="hyperwheel", description="Hyperwheel: scanner -> GRIP uploads")
    ap.add_argument("--site", type=Path, default=Path("/etc/hyperwheel/site.toml"))
    ap.add_argument("--routing", type=Path, default=Path("/etc/hyperwheel/routing.toml"))
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the pipeline (the hyperwheel service)")
    sub.add_parser("dashboard", help="run the Upload Monitor web page")
    sub.add_parser("status", help="show what the Upload Monitor shows, as text")
    sub.add_parser("doctor", help="check the scanner, GRIP, Orthanc, clock, disk and keys")
    sub.add_parser("update", help="updates: check, now, hold, release-hold, status (as root)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        site = load_site(args.site)
        routing = load_routing(args.routing, test_mode=site.test_mode)
    except ConfigError as e:
        print(f"Configuration problem: {e}", file=sys.stderr)
        return 2

    if args.command == "run":
        from .service import AlreadyRunning, Service
        service = Service(site, routing)
        try:
            service.run()
        except AlreadyRunning as e:
            print(f"Not started: {e}.", file=sys.stderr)
            return 3
        return 1 if service.failed else 0
    if args.command == "dashboard":
        serve(site, routing)
        return 0
    if args.command == "doctor":
        from . import doctor
        results, code = doctor.run(site, routing)
        doctor.print_report(site, results)
        return code

    store = Store(site.state_db, readonly=True)
    try:
        s = snapshot(store, site, routing, now=time.time(), clock_synced=clock_synced())
    finally:
        store.close()
    print(s["headline"])
    print(f"  {s['subline']}")
    print(f"  {s['badge']}")
    for f in s["general"]:
        print(f"  [{f['level']}] {f['text']}")
    for st in s["studies"]:
        print(f"\n{st['session']}  {st['project']}  {st['state']}")
        print(f"  DICOM {st['dicom']['on_grip']}/{st['dicom']['total']} on GRIP, "
              f"raw data {st['raw']['on_grip']}/{st['raw']['total']} on GRIP"
              + (" (still fetching)" if st["raw"]["still_fetching"] else ""))
        for f in st["flags"]:
            print(f"  [{f['level']}] {f['text']}")
    print("\nGRIP keys:")
    for k in s["keys"]:
        print(f"  {k['project']}: {k['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
