#!/usr/bin/env python3
"""
Auto-detect completed F1 sessions not yet cached and process them.
Only checks sessions from the last 14 days to stay fast and free.
"""
import json
import sys
import subprocess
import fastf1
from pathlib import Path
from datetime import datetime, timedelta, timezone


def session_cached(root, year, event_slug, session_type):
    # A session is only "cached" if it has BOTH drivers.json AND telemetry files.
    # FastF1 sometimes returns results-only data when telemetry hasn't backfilled
    # yet (typical for sessions that finished in the last few hours). Without the
    # telemetry check, the first partial run would lock out all future retries.
    session_dir = root / str(year) / event_slug / session_type
    drivers_file = session_dir / "drivers.json"
    tel_dir = session_dir / "telemetry"
    if not drivers_file.exists():
        return False
    try:
        with open(drivers_file) as f:
            if len(json.load(f)) == 0:
                return False
    except (ValueError, OSError):
        return False
    return tel_dir.exists() and any(tel_dir.glob("*.json"))


def run(year, event_name, session_type):
    print(f"\n>>> Processing {year} {event_name} {session_type}")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "process_session.py"),
         str(year), event_name, session_type]
    )
    return result.returncode == 0


def main():
    root = Path(__file__).parent.parent

    # Enable FastF1 cache
    cache_dir = Path("/tmp/fastf1_cache")
    cache_dir.mkdir(exist_ok=True)
    fastf1.Cache.enable_cache(str(cache_dir))

    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    current_year = datetime.now().year
    processed, failed = [], []

    for year in [current_year, current_year - 1]:
        events_file = root / str(year) / "events.json"
        if not events_file.exists():
            print(f"No events.json for {year}, skipping.")
            continue

        with open(events_file) as f:
            events = json.load(f)

        for event in events:
            slug = event["slug"]
            name = event["name"]
            sessions_file = root / str(year) / slug / "sessions.json"
            if not sessions_file.exists():
                continue

            with open(sessions_file) as f:
                sessions = json.load(f)

            now = datetime.now(timezone.utc)
            for s in sessions:
                # Decide what to process from the session date directly,
                # not from `available` (which now means "data on disk").
                date_str = s.get("date", "")
                if not date_str:
                    continue
                try:
                    dt = datetime.fromisoformat(date_str.replace(" ", "T"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if dt > now:
                    continue  # session hasn't happened yet
                if dt < cutoff:
                    continue  # older than the 14-day window

                stype = s["type"]
                if session_cached(root, year, slug, stype):
                    print(f"  Already cached: {year} {name} {stype}")
                    continue

                ok = run(year, name, stype)
                (processed if ok else failed).append(f"{year} {name} {stype}")

    print(f"\n--- Summary ---")
    print(f"Processed: {processed or 'none'}")
    print(f"Failed:    {failed or 'none'}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
