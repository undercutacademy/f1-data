#!/usr/bin/env python3
"""
Backfill ALL missing sessions for a given year.
Processes every available session that doesn't already have drivers.json.

Usage:
    python scripts/backfill_year.py <year>
    python scripts/backfill_year.py <year> --sessions Q,R   # only Q and Race
"""
import json
import sys
import subprocess
import fastf1
from pathlib import Path


def session_cached(root, year, event_slug, session_type):
    return (root / str(year) / event_slug / session_type / "drivers.json").exists()


def run(year, event_name, session_type):
    print(f"\n{'='*60}")
    print(f">>> Processing {year} {event_name} {session_type}")
    print(f"{'='*60}")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "process_session.py"),
         str(year), event_name, session_type],
        timeout=600,  # 10 min max per session
    )
    return result.returncode == 0


def main():
    if len(sys.argv) < 2:
        print("Usage: python backfill_year.py <year> [--sessions Q,R]")
        sys.exit(1)

    year = int(sys.argv[1])

    # Optional session filter
    session_filter = None
    if "--sessions" in sys.argv:
        idx = sys.argv.index("--sessions")
        if idx + 1 < len(sys.argv):
            session_filter = set(sys.argv[idx + 1].split(","))
            print(f"Filtering to sessions: {session_filter}")

    root = Path(__file__).parent.parent

    # Enable FastF1 cache
    cache_dir = Path("/tmp/fastf1_cache")
    cache_dir.mkdir(exist_ok=True)
    fastf1.Cache.enable_cache(str(cache_dir))

    events_file = root / str(year) / "events.json"
    if not events_file.exists():
        print(f"No events.json for {year}")
        sys.exit(1)

    with open(events_file) as f:
        events = json.load(f)

    processed, failed, skipped = [], [], []

    for event in events:
        slug = event["slug"]
        name = event["name"]
        sessions_file = root / str(year) / slug / "sessions.json"
        if not sessions_file.exists():
            print(f"  No sessions.json for {name}, skipping")
            continue

        with open(sessions_file) as f:
            sessions = json.load(f)

        for s in sessions:
            if not s.get("available", False):
                continue

            stype = s["type"]

            if session_filter and stype not in session_filter:
                continue

            if session_cached(root, year, slug, stype):
                skipped.append(f"{name} {stype}")
                continue

            try:
                ok = run(year, name, stype)
                if ok:
                    processed.append(f"{name} {stype}")
                else:
                    failed.append(f"{name} {stype}")
            except subprocess.TimeoutExpired:
                print(f"  TIMEOUT: {name} {stype}")
                failed.append(f"{name} {stype} (timeout)")

    print(f"\n{'='*60}")
    print(f"BACKFILL SUMMARY — {year}")
    print(f"{'='*60}")
    print(f"Processed: {len(processed)}")
    for p in processed:
        print(f"  ✓ {p}")
    print(f"Skipped (already cached): {len(skipped)}")
    print(f"Failed: {len(failed)}")
    for f_ in failed:
        print(f"  ✗ {f_}")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
