#!/usr/bin/env python3
"""
Add analysis.json + weather.json to already-ingested sessions.

For every session directory that has drivers.json, loads the session cheaply
(laps + weather, no telemetry, no race-control messages) and writes ONLY
analysis.json / weather.json when missing. Never touches existing files.

Usage:
    python scripts/backfill_analysis.py <year>
    python scripts/backfill_analysis.py <year> --sessions R,Q
    python scripts/backfill_analysis.py <year> --event "Abu Dhabi Grand Prix"

Environment:
    F1_DATA_OUT       data root (default: repo root)
    FASTF1_CACHE_DIR  FastF1 cache dir (default: /tmp/fastf1_cache)
"""
import argparse
import json
import sys
import time
from pathlib import Path

import fastf1

# scripts/__pycache__ is tracked in git and local_ingest commits with `git add -A`;
# don't let this import rewrite process_session's .pyc.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from process_session import data_root, enable_cache, write_analysis, write_weather  # noqa: E402


def ingested_sessions(event_dir: Path):
    """Session-type subdirectories that have a non-empty drivers.json."""
    if not event_dir.is_dir():
        return []
    out = []
    for d in sorted(p for p in event_dir.iterdir() if p.is_dir()):
        try:
            with open(d / "drivers.json") as f:
                if json.load(f):
                    out.append(d)
        except (OSError, ValueError):
            continue
    return out


def backfill_session(year: int, event_name: str, session_dir: Path) -> str:
    need_analysis = not (session_dir / "analysis.json").exists()
    need_weather = not (session_dir / "weather.json").exists()
    if not (need_analysis or need_weather):
        return "skipped"

    stype = session_dir.name
    print(f"\n>>> {year} {event_name} {stype}")
    t0 = time.monotonic()
    session = fastf1.get_session(year, event_name, stype)
    session.load(laps=True, telemetry=False, weather=True, messages=True)

    if need_analysis:
        tel_dir = session_dir / "telemetry"
        tel_files = {p.name for p in tel_dir.glob("*.json")} if tel_dir.is_dir() else set()
        write_analysis(session, session_dir,
                       lambda abbr, lap: f"{abbr}_{lap}.json" in tel_files)
    if need_weather:
        write_weather(session, session_dir)
    print(f"  ({time.monotonic() - t0:.1f}s)")
    return "done"


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    ap.add_argument("year", type=int)
    ap.add_argument("--sessions", help="comma-separated session types, e.g. R,Q")
    ap.add_argument("--event", help="event name or slug as in <year>/events.json")
    args = ap.parse_args()

    root = data_root()
    enable_cache()

    events_file = root / str(args.year) / "events.json"
    if not events_file.exists():
        print(f"No events.json for {args.year} under {root}")
        sys.exit(1)
    with open(events_file, encoding="utf-8") as f:
        events = json.load(f)

    if args.event:
        want = args.event.strip().lower()
        events = [e for e in events if want in (e["name"].lower(), e["slug"])]
        if not events:
            print(f"No event matching {args.event!r} in {events_file}")
            sys.exit(1)

    session_filter = set(args.sessions.split(",")) if args.sessions else None

    done, skipped, failed = [], 0, []
    for ev in events:
        for sdir in ingested_sessions(root / str(args.year) / ev["slug"]):
            if session_filter and sdir.name not in session_filter:
                continue
            label = f"{ev['name']} {sdir.name}"
            try:
                if backfill_session(args.year, ev["name"], sdir) == "done":
                    done.append(label)
                else:
                    skipped += 1
            except Exception as e:
                print(f"  FAILED {label}: {e}")
                failed.append(label)

    print(f"\nBACKFILL ANALYSIS - {args.year}: {len(done)} written, "
          f"{skipped} already complete, {len(failed)} failed")
    for f_ in failed:
        print(f"  x {f_}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
